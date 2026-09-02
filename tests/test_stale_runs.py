from pathlib import Path

from main import (
    Project,
    ProjectRuns,
    pr_related_run_ids,
    pr_status,
    related_pr_runs,
)

BRANCH = "feat/auth-epic-surface-1607"
HEAD = "d18257d22c44d63b74b57629da93fa0d7bb2a21e"
OLDER = "cf562551f1b0f7b1f0c2b6a1d4e8f9a0b1c2d3e4"


def project() -> Project:
    return Project("demo", Path("/repos/demo"), "owner/demo")


def run(workflow: str, sha: str, status: str, conclusion: str | None) -> dict:
    return {
        "databaseId": abs(hash((workflow, sha, status, conclusion))) % 100000,
        "headBranch": BRANCH,
        "headSha": sha,
        "status": status,
        "conclusion": conclusion,
        "workflowName": workflow,
        "event": "pull_request",
        "createdAt": "2026-09-02T12:26:05Z",
        "updatedAt": "2026-09-02T12:26:05Z",
    }


def pull_request(**overrides: object) -> dict:
    pr = {
        "number": 1611,
        "title": "auth: guard every ops-portal route",
        "body": "Closes #1607",
        "headRefName": BRANCH,
        "headRefOid": HEAD,
        "isDraft": False,
        "mergeStateStatus": "CLEAN",
        "reviewDecision": "REVIEW_REQUIRED",
        "statusCheckRollup": [],
        "author": {"login": "tess"},
        "updatedAt": "2026-09-02T12:26:05Z",
    }
    pr.update(overrides)
    return pr


# A push supersedes the runs of the commit before it: the head commit is
# still running, while the previous commit left a real failure behind.
SUPERSEDED = [
    run("Red-First Gate", OLDER, "completed", "failure"),
    run("Build UI", OLDER, "completed", "success"),
    run("Unit Tests", OLDER, "completed", "success"),
]
CURRENT = [
    run("Red-First Gate", HEAD, "in_progress", None),
    run("Build UI", HEAD, "queued", None),
    run("Unit Tests", HEAD, "queued", None),
]


class TestRelatedPrRuns:
    def test_runs_from_an_older_commit_are_excluded(self) -> None:
        related = related_pr_runs(pull_request(), SUPERSEDED + CURRENT)

        assert [r["headSha"] for r in related] == [HEAD] * 3

    def test_runs_on_the_head_commit_are_kept(self) -> None:
        related = related_pr_runs(pull_request(), CURRENT)

        assert len(related) == 3

    def test_a_run_on_the_pull_ref_is_kept_when_its_commit_matches(self) -> None:
        pull_ref = run("Unit Tests", HEAD, "queued", None)
        pull_ref["headBranch"] = "refs/pull/1611/head"

        related = related_pr_runs(pull_request(), [pull_ref])

        assert related == [pull_ref]

    def test_another_branch_is_still_excluded(self) -> None:
        other = run("Unit Tests", HEAD, "completed", "failure")
        other["headBranch"] = "feat/unrelated"

        assert related_pr_runs(pull_request(), [other]) == []

    def test_runs_are_kept_when_the_pull_request_has_no_head_commit(self) -> None:
        pr = pull_request()
        del pr["headRefOid"]

        related = related_pr_runs(pr, SUPERSEDED + CURRENT)

        assert len(related) == 6

    def test_runs_without_a_head_sha_are_kept(self) -> None:
        stripped = [dict(r) for r in CURRENT]
        for r in stripped:
            del r["headSha"]

        assert len(related_pr_runs(pull_request(), stripped)) == 3


class TestPrStatus:
    def test_a_superseded_failure_does_not_mark_the_pull_request_failing(self) -> None:
        pr = pull_request()
        related = related_pr_runs(pr, SUPERSEDED + CURRENT)

        icon, style, detail = pr_status(pr, related)

        assert style != "red"
        assert "fail" not in detail

    def test_the_count_covers_only_the_head_commit(self) -> None:
        pr = pull_request()
        related = related_pr_runs(pr, SUPERSEDED + CURRENT)

        _, _, detail = pr_status(pr, related)

        assert detail == "0/3 checks"

    def test_a_failure_on_the_head_commit_is_still_reported(self) -> None:
        pr = pull_request()
        broken = run("Red-First Gate", HEAD, "completed", "failure")
        related = related_pr_runs(pr, SUPERSEDED + [broken] + CURRENT[1:])

        icon, style, detail = pr_status(pr, related)

        assert style == "red"
        assert "1 fail" in detail


class TestPrRelatedRunIds:
    def test_superseded_runs_are_not_claimed_by_the_pull_request(self) -> None:
        group = ProjectRuns(project(), runs=SUPERSEDED + CURRENT, prs=[pull_request()])

        ids = pr_related_run_ids(group)

        assert ids == {r["databaseId"] for r in CURRENT}
