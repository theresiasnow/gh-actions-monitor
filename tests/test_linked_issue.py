import io
from pathlib import Path

from rich.console import Console

from main import (
    Project,
    ProjectRuns,
    build_prs_table,
    linked_issue_number,
)


def project() -> Project:
    return Project("demo", Path("/repos/demo"), "owner/demo")


def pull_request(number: int, branch: str, **overrides: object) -> dict:
    pr = {
        "number": number,
        "title": f"PR {number}",
        "body": "",
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


class TestLinkedIssueNumber:
    def test_closes_keyword_is_recognised(self) -> None:
        assert linked_issue_number({"body": "Closes #1577"}) == 1577

    def test_keywords_are_case_insensitive(self) -> None:
        assert linked_issue_number({"body": "fixes #42"}) == 42
        assert linked_issue_number({"body": "RESOLVES #43"}) == 43

    def test_all_closing_keywords_are_recognised(self) -> None:
        for keyword in (
            "close",
            "closes",
            "closed",
            "fix",
            "fixes",
            "fixed",
            "resolve",
            "resolves",
            "resolved",
        ):
            assert linked_issue_number({"body": f"{keyword} #7"}) == 7, keyword

    def test_keyword_may_appear_mid_body(self) -> None:
        body = "Some description.\n\n- Closes #1577\n- Notes on the change\n"
        assert linked_issue_number({"body": body}) == 1577

    def test_first_link_wins_when_several_are_present(self) -> None:
        assert linked_issue_number({"body": "Closes #10 and fixes #20"}) == 10

    def test_a_bare_issue_reference_is_not_a_link(self) -> None:
        assert linked_issue_number({"body": "Related to #1577"}) is None

    def test_a_keyword_inside_a_word_is_not_a_link(self) -> None:
        assert linked_issue_number({"body": "unfixed #1577"}) is None

    def test_a_full_url_link_is_recognised(self) -> None:
        body = "Closes https://github.com/owner/demo/issues/1577"
        assert linked_issue_number({"body": body}) == 1577

    def test_missing_or_empty_body_has_no_link(self) -> None:
        assert linked_issue_number({}) is None
        assert linked_issue_number({"body": None}) is None
        assert linked_issue_number({"body": ""}) is None


class TestIssuePrefixInTable:
    def test_linked_issue_is_shown_before_the_title(self) -> None:
        pr = pull_request(1603, "feat/guard", body="Closes #1577")
        group = ProjectRuns(project(), runs=[], prs=[pr])

        output = render(build_prs_table(group))

        assert "#1577 PR 1603" in output

    def test_pull_request_without_a_link_shows_only_the_title(self) -> None:
        pr = pull_request(1603, "feat/guard")
        group = ProjectRuns(project(), runs=[], prs=[pr])

        output = render(build_prs_table(group))

        assert "PR 1603" in output
        assert "#1577" not in output
