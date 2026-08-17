import io
import os
import subprocess
from pathlib import Path

from rich.console import Console

from main import (
    AgentBranch,
    Project,
    ProjectRuns,
    agent_detail,
    agent_test_status,
    build_agent_rows,
    build_agents_table,
    discover_projects,
    parse_worktree_list,
    project_worktrees,
)

PORCELAIN = """worktree /repos/demo
HEAD ed555e662db59d739e919d6f6c3bdf8358ae190c
branch refs/heads/main

worktree /repos/wt-codex
HEAD ed555e662db59d739e919d6f6c3bdf8358ae190c
branch refs/heads/agent/codex-task

worktree /repos/wt-detached
HEAD ed555e662db59d739e919d6f6c3bdf8358ae190c
detached
"""


def project() -> Project:
    return Project("demo", Path("/repos/demo"), "owner/demo")


def run(branch: str, status: str = "completed", conclusion: str | None = "success") -> dict:
    return {
        "databaseId": abs(hash((branch, status, conclusion))) % 100000,
        "headBranch": branch,
        "status": status,
        "conclusion": conclusion,
        "workflowName": "CI",
        "createdAt": "2026-08-17T10:00:00Z",
        "updatedAt": "2026-08-17T10:05:00Z",
    }


def pull_request(number: int, branch: str, **overrides: object) -> dict:
    pr = {
        "number": number,
        "title": f"PR {number}",
        "headRefName": branch,
        "isDraft": False,
        "mergeStateStatus": "CLEAN",
        "reviewDecision": "REVIEW_REQUIRED",
        "statusCheckRollup": [],
        "author": {"login": "tess"},
        "updatedAt": "2026-08-17T10:05:00Z",
    }
    pr.update(overrides)
    return pr


class TestParseWorktreeList:
    def test_parses_branch_worktrees(self) -> None:
        worktrees = parse_worktree_list(PORCELAIN)
        assert [w["branch"] for w in worktrees] == ["main", "agent/codex-task", None]

    def test_keeps_worktree_paths(self) -> None:
        worktrees = parse_worktree_list(PORCELAIN)
        assert worktrees[1]["path"] == "/repos/wt-codex"

    def test_detached_worktree_has_no_branch(self) -> None:
        worktrees = parse_worktree_list(PORCELAIN)
        assert worktrees[2]["branch"] is None

    def test_empty_output_yields_no_worktrees(self) -> None:
        assert parse_worktree_list("") == []


class TestBuildAgentRows:
    def test_worktree_rows_come_before_remote_rows(self) -> None:
        group = ProjectRuns(
            project(),
            runs=[],
            prs=[pull_request(415, "feat/remote-work")],
            worktrees=[{"path": "/repos/demo", "branch": "main"}],
        )

        rows = build_agent_rows(group)

        assert [(row.name, row.is_worktree) for row in rows] == [
            ("main", True),
            ("feat/remote-work", False),
        ]

    def test_worktree_is_matched_to_its_pull_request(self) -> None:
        pr = pull_request(412, "agent/codex-task")
        group = ProjectRuns(
            project(),
            runs=[],
            prs=[pr],
            worktrees=[{"path": "/repos/wt-codex", "branch": "agent/codex-task"}],
        )

        rows = build_agent_rows(group)

        assert len(rows) == 1
        assert rows[0].pr is pr
        assert rows[0].is_worktree is True

    def test_worktree_collects_runs_for_its_branch(self) -> None:
        matching = run("agent/qwen-task", conclusion="failure")
        group = ProjectRuns(
            project(),
            runs=[matching, run("main")],
            prs=[],
            worktrees=[{"path": "/repos/wt-qwen", "branch": "agent/qwen-task"}],
        )

        rows = build_agent_rows(group)

        assert [r["databaseId"] for r in rows[0].runs] == [matching["databaseId"]]

    def test_detached_worktrees_are_skipped(self) -> None:
        group = ProjectRuns(
            project(),
            runs=[],
            prs=[],
            worktrees=[
                {"path": "/repos/wt-detached", "branch": None},
                {"path": "/repos/demo", "branch": "main"},
            ],
        )

        assert [row.name for row in build_agent_rows(group)] == ["main"]

    def test_pull_request_on_a_worktree_branch_is_not_duplicated(self) -> None:
        group = ProjectRuns(
            project(),
            runs=[],
            prs=[pull_request(412, "agent/codex-task")],
            worktrees=[{"path": "/repos/wt-codex", "branch": "agent/codex-task"}],
        )

        assert [row.name for row in build_agent_rows(group)] == ["agent/codex-task"]

    def test_rows_sort_alphabetically_within_each_group(self) -> None:
        group = ProjectRuns(
            project(),
            runs=[],
            prs=[pull_request(2, "feat/zulu"), pull_request(1, "feat/alpha")],
            worktrees=[
                {"path": "/repos/wt-b", "branch": "agent/beta"},
                {"path": "/repos/wt-a", "branch": "agent/alpha"},
            ],
        )

        assert [row.name for row in build_agent_rows(group)] == [
            "agent/alpha",
            "agent/beta",
            "feat/alpha",
            "feat/zulu",
        ]

    def test_project_without_worktrees_still_lists_pull_request_branches(self) -> None:
        group = ProjectRuns(
            project(),
            runs=[],
            prs=[pull_request(415, "feat/remote-work")],
            worktrees=[],
        )

        rows = build_agent_rows(group)

        assert [(row.name, row.is_worktree) for row in rows] == [("feat/remote-work", False)]

    def test_no_worktrees_and_no_pull_requests_yields_no_rows(self) -> None:
        assert build_agent_rows(ProjectRuns(project(), runs=[], prs=[], worktrees=[])) == []


class TestLocalWorktreeDetection:
    def test_remote_only_project_does_not_borrow_cwd_worktrees(self) -> None:
        """A --mine project points at cwd, which is the monitor's own repo."""
        remote_only = Project("other-repo", Path.cwd(), "owner/other-repo")
        assert project_worktrees(remote_only) == []

    def test_local_checkout_lists_its_worktrees(self, tmp_path: Path) -> None:
        git_env = {
            "PATH": os.environ["PATH"],
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        }
        subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-q", "--allow-empty", "-m", "init"],
            check=True,
            env=git_env,
        )

        local = Project(tmp_path.name, tmp_path, "owner/demo", local=True)

        assert [w["branch"] for w in project_worktrees(local)] == ["main"]

    def test_discovered_path_project_is_marked_local(self, tmp_path: Path) -> None:
        subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
        console = Console(file=io.StringIO())

        projects = discover_projects([str(tmp_path)], None, False, False, 2, 10, console)

        assert [p.local for p in projects] == [True]

    def test_repo_name_argument_is_not_marked_local(self) -> None:
        console = Console(file=io.StringIO())

        projects = discover_projects(["owner/other-repo"], None, False, False, 2, 10, console)

        assert [(p.repo, p.local) for p in projects] == [("owner/other-repo", False)]


class TestRendering:
    def test_rendering_does_not_shell_out(self, monkeypatch) -> None:
        """The refresh loop re-renders on every keypress; git calls belong in fetch."""

        def fail(*args: object, **kwargs: object) -> None:
            raise AssertionError("build_agents_table must not run a subprocess")

        monkeypatch.setattr(subprocess, "run", fail)
        group = ProjectRuns(
            project(),
            runs=[],
            prs=[pull_request(412, "agent/codex-task")],
            worktrees=[{"path": "/repos/wt-codex", "branch": "agent/codex-task"}],
            default_branch="main",
        )

        Console(file=io.StringIO(), width=100).print(build_agents_table(group))

    def test_default_branch_row_omits_local_only(self) -> None:
        group = ProjectRuns(
            project(),
            runs=[run("main")],
            prs=[],
            worktrees=[{"path": "/repos/demo", "branch": "main"}],
            default_branch="main",
        )
        output = io.StringIO()

        Console(file=output, width=100).print(build_agents_table(group))

        assert "local only" not in output.getvalue()


class TestAgentDetail:
    def test_branch_with_runs_but_no_pull_request_is_local_only(self) -> None:
        row = AgentBranch(
            "agent/qwen-task", True, "/repos/wt-qwen", None, [run("agent/qwen-task")]
        )
        assert agent_detail(row, "main") == "local only"

    def test_default_branch_is_not_labelled_local_only(self) -> None:
        row = AgentBranch("main", True, "/repos/demo", None, [run("main")])
        assert agent_detail(row, "main") == ""

    def test_branch_without_runs_has_no_detail(self) -> None:
        row = AgentBranch("agent/idle", True, "/repos/wt-idle", None, [])
        assert agent_detail(row, "main") == ""


class TestAgentTestStatus:
    def test_passing_runs_report_tests_passing(self) -> None:
        row = AgentBranch("agent/codex-task", True, "/repos/wt-codex", None, [run("agent/codex-task")])
        label, _ = agent_test_status(row)
        assert label == "tests ✓"

    def test_failing_run_reports_tests_failing(self) -> None:
        row = AgentBranch(
            "agent/qwen-task",
            True,
            "/repos/wt-qwen",
            None,
            [run("agent/qwen-task", conclusion="failure")],
        )
        label, _ = agent_test_status(row)
        assert label == "tests ✗"

    def test_in_progress_run_reports_tests_running(self) -> None:
        row = AgentBranch(
            "agent/codex-task",
            True,
            "/repos/wt-codex",
            None,
            [run("agent/codex-task", status="in_progress", conclusion=None)],
        )
        label, _ = agent_test_status(row)
        assert label == "tests ⟳"

    def test_failing_run_wins_over_passing_run(self) -> None:
        row = AgentBranch(
            "agent/qwen-task",
            True,
            "/repos/wt-qwen",
            None,
            [run("agent/qwen-task"), run("agent/qwen-task", conclusion="failure")],
        )
        label, _ = agent_test_status(row)
        assert label == "tests ✗"

    def test_branch_without_runs_or_pull_request_is_clean(self) -> None:
        row = AgentBranch("main", True, "/repos/demo", None, [])
        label, _ = agent_test_status(row)
        assert label == "clean"

    def test_pull_request_checks_are_used_when_no_runs_match(self) -> None:
        pr = pull_request(
            412,
            "agent/codex-task",
            statusCheckRollup=[{"name": "build", "conclusion": "SUCCESS"}],
        )
        row = AgentBranch("agent/codex-task", True, "/repos/wt-codex", pr, [])
        label, _ = agent_test_status(row)
        assert label == "tests ✓"

    def test_pull_request_without_checks_is_not_reported_as_clean(self) -> None:
        pr = pull_request(412, "agent/codex-task", statusCheckRollup=[])
        row = AgentBranch("agent/codex-task", True, "/repos/wt-codex", pr, [])
        label, _ = agent_test_status(row)
        assert label == "—"
