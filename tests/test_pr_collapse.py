import io
from pathlib import Path

from rich.console import Console

from main import (
    Project,
    ProjectRuns,
    build_prs_table,
    selectable_rows,
)


def project() -> Project:
    return Project("demo", Path("/repos/demo"), "owner/demo")


def run(branch: str, workflow: str = "CI", conclusion: str | None = "success") -> dict:
    return {
        "databaseId": abs(hash((branch, workflow, conclusion))) % 100000,
        "headBranch": branch,
        "status": "completed",
        "conclusion": conclusion,
        "workflowName": workflow,
        "event": "pull_request",
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


def render(table: object, width: int = 100) -> str:
    output = io.StringIO()
    Console(file=output, width=width).print(table)
    return output.getvalue()


def group_with_two_prs() -> ProjectRuns:
    return ProjectRuns(
        project(),
        runs=[
            run("feat/one", "Unit Tests"),
            run("feat/one", "Lint", conclusion="failure"),
            run("feat/two", "E2E Tests"),
        ],
        prs=[pull_request(11, "feat/one"), pull_request(22, "feat/two")],
    )


class TestCollapsedByDefault:
    def test_pull_request_children_are_hidden_by_default(self) -> None:
        output = render(build_prs_table(group_with_two_prs()))

        assert "#11" in output
        assert "#22" in output
        assert "Unit Tests" not in output
        assert "E2E Tests" not in output

    def test_collapsed_pull_request_shows_a_disclosure_marker(self) -> None:
        output = render(build_prs_table(group_with_two_prs()))

        assert "▸" in output
        assert "▾" not in output

    def test_expanded_pull_request_shows_its_runs(self) -> None:
        group = group_with_two_prs()

        output = render(build_prs_table(group, expanded_prs={11}))

        assert "Unit Tests" in output
        assert "Lint" in output
        assert "E2E Tests" not in output

    def test_expanded_pull_request_shows_an_open_disclosure_marker(self) -> None:
        output = render(build_prs_table(group_with_two_prs(), expanded_prs={11}))

        assert "▾" in output
        assert "▸" in output  # the other PR is still collapsed

    def test_expanded_pull_request_without_runs_shows_its_checks(self) -> None:
        pr = pull_request(
            33,
            "feat/three",
            statusCheckRollup=[
                {"name": "Typecheck", "conclusion": "SUCCESS", "status": "COMPLETED"}
            ],
        )
        group = ProjectRuns(project(), runs=[], prs=[pr])

        assert "Typecheck" not in render(build_prs_table(group))
        assert "Typecheck" in render(build_prs_table(group, expanded_prs={33}))

    def test_selected_pull_request_gets_a_cursor(self) -> None:
        output = render(build_prs_table(group_with_two_prs(), selected_pr=22))

        assert "▶" in output


class TestSelectableRows:
    def test_runs_and_pull_requests_are_both_selectable(self) -> None:
        rows = selectable_rows([group_with_two_prs()])

        kinds = [row.kind for row in rows]
        assert "pr" in kinds
        assert kinds.count("pr") == 2

    def test_collapsed_pull_request_children_are_not_selectable(self) -> None:
        rows = selectable_rows([group_with_two_prs()])

        # Both PRs are collapsed, so their three runs must not be navigable.
        assert [row.kind for row in rows] == ["pr", "pr"]

    def test_expanding_a_pull_request_makes_its_runs_selectable(self) -> None:
        rows = selectable_rows([group_with_two_prs()], expanded_prs={11})

        kinds = [row.kind for row in rows]
        assert kinds == ["pr", "run", "run", "pr"]

    def test_standalone_runs_stay_selectable(self) -> None:
        group = ProjectRuns(
            project(),
            runs=[run("main", "Deploy")],
            prs=[pull_request(11, "feat/one")],
        )

        rows = selectable_rows([group])

        assert [row.kind for row in rows] == ["run", "pr"]

    def test_pull_request_row_carries_its_number(self) -> None:
        rows = selectable_rows([group_with_two_prs()])

        assert [row.pr["number"] for row in rows] == [11, 22]


class TestTitleTruncation:
    def test_long_pull_request_title_stays_on_one_line(self) -> None:
        long_title = (
            "docs: stop syncing a merely-behind branch, and correct "
            "the strict:false claim which is very long indeed"
        )
        group = ProjectRuns(
            project(), runs=[], prs=[pull_request(11, "feat/one", title=long_title)]
        )

        body = [line for line in render(build_prs_table(group), width=90).splitlines() if line.strip()]

        assert len(body) == 1
        assert "…" in body[0]
