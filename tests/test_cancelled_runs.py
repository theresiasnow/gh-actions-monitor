from collections import Counter
from pathlib import Path

from main import (
    AgentBranch,
    Project,
    agent_test_status,
    check_progress,
    pr_status,
)


def project() -> Project:
    return Project("demo", Path("/repos/demo"), "owner/demo")


def run(workflow: str, status: str, conclusion: str | None) -> dict:
    return {
        "databaseId": abs(hash((workflow, status, conclusion))) % 100000,
        "headBranch": "fix/scope-mapping-groups-1605",
        "status": status,
        "conclusion": conclusion,
        "workflowName": workflow,
        "event": "pull_request",
        "createdAt": "2026-09-02T10:26:05Z",
        "updatedAt": "2026-09-02T10:26:05Z",
    }


def pull_request(**overrides: object) -> dict:
    pr = {
        "number": 1606,
        "title": "authentik: read .groups",
        "body": "",
        "headRefName": "fix/scope-mapping-groups-1605",
        "isDraft": False,
        "mergeStateStatus": "CLEAN",
        "reviewDecision": "REVIEW_REQUIRED",
        "statusCheckRollup": [],
        "author": {"login": "tess"},
        "updatedAt": "2026-09-02T10:26:05Z",
    }
    pr.update(overrides)
    return pr


class TestCheckProgress:
    def test_cancelled_is_not_counted_as_failing(self) -> None:
        _, _, _, failing = check_progress(Counter({"cancelled": 3, "success": 1}))

        assert failing == 0

    def test_cancelled_is_excluded_from_the_total(self) -> None:
        completed, total, passing, failing = check_progress(
            Counter({"cancelled": 3, "success": 1})
        )

        assert (completed, total, passing, failing) == (1, 1, 1, 0)

    def test_real_failures_are_still_counted(self) -> None:
        completed, total, passing, failing = check_progress(
            Counter({"failure": 2, "success": 1, "cancelled": 3})
        )

        assert (completed, total, passing, failing) == (3, 3, 1, 2)

    def test_other_failure_conclusions_are_unaffected(self) -> None:
        for state in ("failure", "failed", "error", "timed_out", "action_required"):
            _, _, _, failing = check_progress(Counter({state: 1}))
            assert failing == 1, state

    def test_only_cancelled_runs_reads_as_no_checks(self) -> None:
        completed, total, passing, failing = check_progress(Counter({"cancelled": 3}))

        assert (completed, total, passing, failing) == (0, 0, 0, 0)


class TestPrStatusWithSupersededRuns:
    def test_superseded_runs_do_not_show_as_failing(self) -> None:
        """PR 1606: three runs cancelled by a newer push, four queued after it."""
        runs = [
            run("Red-First Gate", "in_progress", None),
            run("Unit Tests", "queued", None),
            run("Lint", "queued", None),
            run("Integration Tests", "queued", None),
            run("Lint", "completed", "cancelled"),
            run("Unit Tests", "completed", "cancelled"),
            run("Red-First Gate", "completed", "cancelled"),
        ]

        icon, style, detail = pr_status(pull_request(), runs)

        assert style != "red"
        assert "fail" not in detail
        assert icon == "⟳"
        assert detail == "0/4 checks"

    def test_a_genuine_failure_still_shows_as_failing(self) -> None:
        runs = [
            run("Unit Tests", "completed", "failure"),
            run("Lint", "completed", "success"),
            run("Red-First Gate", "completed", "cancelled"),
        ]

        icon, style, detail = pr_status(pull_request(), runs)

        assert (icon, style) == ("✗", "red")
        assert detail == "2/2 checks, 1 fail"


class TestAgentRowWithSupersededRuns:
    def test_superseded_runs_do_not_mark_the_branch_red(self) -> None:
        runs = [
            run("Unit Tests", "queued", None),
            run("Lint", "completed", "cancelled"),
        ]
        row = AgentBranch(
            name="fix/scope-mapping-groups-1605",
            is_worktree=True,
            path="/repos/demo",
            pr=pull_request(),
            runs=runs,
        )

        label, style = agent_test_status(row)

        assert (label, style) == ("tests ⟳", "yellow")

    def test_a_branch_whose_runs_were_all_cancelled_is_not_red(self) -> None:
        runs = [run("Lint", "completed", "cancelled")]
        row = AgentBranch(
            name="fix/scope-mapping-groups-1605",
            is_worktree=True,
            path="/repos/demo",
            pr=pull_request(),
            runs=runs,
        )

        label, style = agent_test_status(row)

        assert style != "red"
