import json
import math
import select
import sys
import subprocess
import termios
import time
import tomllib
import tty
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.align import Align
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

app = typer.Typer(
    add_completion=False,
    help="Monitor GitHub Actions runs across one or more projects.",
)

REFRESH_SECONDS = 15
DEFAULT_LIMIT = 12
DEFAULT_SCAN_DEPTH = 2
DEFAULT_REPO_LIMIT = 100
SETTINGS_FILE_NAME = "settings.toml"

# Highlight the selected row with a background colour rather than `reverse`:
# reverse inverts each cell separately, turning per-cell foreground colours
# (green ticks, red failures, dim metadata) into clashing background blocks.
SELECTED_ROW_STYLE = "on grey30"

STATUS_STYLE = {
    "completed": ("✓", "green"),
    "in_progress": ("⟳", "yellow"),
    "queued": ("◷", "cyan"),
    "waiting": ("◷", "cyan"),
    "cancelled": ("✗", "dim"),
    "failed": ("✗", "red"),
    "action_required": ("!", "magenta"),
    "timed_out": ("✗", "red"),
    "skipped": ("–", "dim"),
    "stale": ("–", "dim"),
}

CONCLUSION_STYLE = {
    "success": ("✓", "green"),
    "failure": ("✗", "red"),
    "cancelled": ("✗", "dim"),
    "skipped": ("–", "dim"),
    "timed_out": ("✗", "red"),
    "action_required": ("!", "magenta"),
    "neutral": ("–", "dim"),
}


@dataclass(frozen=True)
class Project:
    name: str
    path: Path
    repo: str | None = None
    local: bool = False
    """True when ``path`` is this project's own checkout rather than a fallback."""


@dataclass(frozen=True)
class ProjectRuns:
    project: Project
    runs: list[dict]
    error: str | None = None
    prs: list[dict] | None = None
    pr_error: str | None = None
    worktrees: list[dict] | None = None
    default_branch: str | None = None


@dataclass(frozen=True)
class AgentBranch:
    name: str
    is_worktree: bool
    path: str | None
    pr: dict | None
    runs: list[dict]


@dataclass(frozen=True)
class SelectableRow:
    """A cursor stop in the dashboard: a standalone run, or a pull request."""

    kind: str  # "run" or "pr"
    project: Project
    run: dict | None = None
    pr: dict | None = None


class KeyWatcher:
    def __init__(self) -> None:
        self._fd: int | None = None
        self._settings: list[int | bytes] | None = None

    def __enter__(self) -> "KeyWatcher":
        if not sys.stdin.isatty():
            return self

        self._fd = sys.stdin.fileno()
        self._settings = termios.tcgetattr(self._fd)
        tty.setcbreak(self._fd)
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._fd is not None and self._settings is not None:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._settings)

    def suspend(self) -> None:
        """Restore normal terminal mode (e.g. before handing off to a pager)."""
        if self._fd is not None and self._settings is not None:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._settings)

    def resume(self) -> None:
        """Re-enter cbreak mode after returning from a pager."""
        if self._fd is not None:
            tty.setcbreak(self._fd)

    def read_key(self) -> str | None:
        """Return the next key action or None if no key is waiting."""
        if self._fd is None:
            return None

        readable, _, _ = select.select([sys.stdin], [], [], 0)
        if not readable:
            return None

        ch = sys.stdin.read(1)
        if ch in ("q", "Q"):
            return "quit"
        if ch in ("\r", "\n"):
            return "enter"
        if ch == " ":
            return "toggle"
        if ch == "\x1b":
            readable, _, _ = select.select([sys.stdin], [], [], 0.05)
            if readable:
                ch2 = sys.stdin.read(1)
                if ch2 == "[":
                    readable, _, _ = select.select([sys.stdin], [], [], 0.05)
                    if readable:
                        ch3 = sys.stdin.read(1)
                        if ch3 == "A":
                            return "up"
                        if ch3 == "B":
                            return "down"
        return None


def run_gh(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["gh", *args], cwd=cwd, capture_output=True, text=True)


def show_run_logs(console: Console, run: dict, project: Project) -> None:
    """Display logs for a workflow run; blocks until the pager exits."""
    run_id = run.get("databaseId")
    if not run_id:
        console.print("[red]No run ID available.[/]")
        input("Press Enter to return…")
        return

    args = ["run", "view", str(run_id), "--log"]
    if project.repo:
        args.extend(["--repo", project.repo])

    try:
        subprocess.run(["gh", *args], cwd=project.path)
    except FileNotFoundError:
        console.print("[red]gh CLI not found.[/]")

    console.print("\n[dim]Press Enter to return to monitor…[/]")
    sys.stdin.readline()


def default_settings_path() -> Path:
    return Path.cwd() / SETTINGS_FILE_NAME


def load_settings_repos(path: Path) -> list[str]:
    try:
        data = tomllib.loads(path.read_text())
    except (FileNotFoundError, tomllib.TOMLDecodeError, OSError):
        return []

    repos = data.get("repos", [])
    if not isinstance(repos, list):
        return []
    return [repo for repo in repos if isinstance(repo, str) and "/" in repo]


def save_settings_repos(path: Path, projects: list[Project]) -> None:
    repos = sorted({project.repo for project in projects if project.repo})
    if not repos:
        return

    repo_values = ", ".join(json.dumps(repo) for repo in repos)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"repos = [{repo_values}]\n")
    except OSError:
        return


def projects_from_repo_names(repos: list[str]) -> list[Project]:
    return [Project(repo.split("/")[-1], Path.cwd(), repo) for repo in repos]


def git_root(path: Path) -> Path | None:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip()).resolve()


def remote_repo(path: Path) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(path), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None

    remote = result.stdout.strip()
    if remote.startswith("git@github.com:"):
        remote = remote.removeprefix("git@github.com:")
    elif remote.startswith("https://github.com/"):
        remote = remote.removeprefix("https://github.com/")
    else:
        return None
    return remote.removesuffix(".git").strip("/") or None


def parse_worktree_list(output: str) -> list[dict]:
    """Parse `git worktree list --porcelain` into path/branch records."""
    worktrees: list[dict] = []
    current: dict = {}

    for line in output.splitlines():
        if not line.strip():
            if current.get("path"):
                worktrees.append({"path": current["path"], "branch": current.get("branch")})
            current = {}
            continue
        if line.startswith("worktree "):
            current = {"path": line.removeprefix("worktree ").strip()}
        elif line.startswith("branch "):
            ref = line.removeprefix("branch ").strip()
            current["branch"] = ref.removeprefix("refs/heads/")

    if current.get("path"):
        worktrees.append({"path": current["path"], "branch": current.get("branch")})
    return worktrees


def fetch_worktrees(path: Path) -> list[dict]:
    """List worktrees for a local repository; empty when the path is not one."""
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return []

    if result.returncode != 0:
        return []
    return parse_worktree_list(result.stdout)


def project_worktrees(project: Project) -> list[dict]:
    """Worktrees for a project checked out locally.

    Repo-only projects (``--mine`` or ``OWNER/REPO`` arguments) carry the
    current directory as their path, so they would otherwise report the
    monitor's own worktrees as if they belonged to that repository.
    """
    if not project.local:
        return []
    return fetch_worktrees(project.path)


def repos_for_authenticated_user(repo_limit: int) -> list[Project]:
    if repo_limit < 1:
        return []

    pages = max(1, math.ceil(repo_limit / 100))
    projects: list[Project] = []
    for page_number in range(1, pages + 1):
        args = [
            "api",
            "/user/repos",
            "-f",
            "affiliation=owner,collaborator,organization_member",
            "-f",
            "sort=pushed",
            "-f",
            "per_page=100",
            "-f",
            f"page={page_number}",
        ]

        try:
            result = run_gh(args)
        except FileNotFoundError:
            return []

        if result.returncode != 0:
            return []

        try:
            repos = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            return []

        if not repos:
            return projects

        for repo in repos:
            full_name = repo.get("full_name")
            if full_name:
                projects.append(Project(full_name.split("/")[-1], Path.cwd(), full_name))
            if len(projects) >= repo_limit:
                return projects
    return projects


def parse_selection(selection: str, total: int) -> list[int]:
    normalized = selection.strip().lower()
    if normalized in {"all", "*"}:
        return list(range(total))
    if not normalized:
        return []

    selected: set[int] = set()
    for part in normalized.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            start, end = part.split("-", 1)
            if not start.isdigit() or not end.isdigit():
                continue
            start_index = int(start) - 1
            end_index = int(end) - 1
            if start_index > end_index:
                start_index, end_index = end_index, start_index
            selected.update(range(max(0, start_index), min(total, end_index + 1)))
            continue
        if part.isdigit():
            index = int(part) - 1
            if 0 <= index < total:
                selected.add(index)
    return sorted(selected)


def choose_repositories(console: Console, projects: list[Project]) -> list[Project]:
    if not projects:
        return []

    table = Table(
        title="Select repositories to monitor",
        show_header=True,
        header_style="bold",
        border_style="bright_blue",
    )
    table.add_column("#", justify="right", style="cyan", width=4)
    table.add_column("Repository", style="bright_white")
    table.add_column("Project", style="dim")

    for index, project in enumerate(projects, start=1):
        table.add_row(str(index), project.repo or project.name, project.name)

    console.print(table)
    console.print(
        "[dim]Enter numbers, ranges like 1-5, comma lists like 1,4,8, or all.[/]"
    )

    while True:
        selected = parse_selection(Prompt.ask("Repositories", default="all"), len(projects))
        if selected:
            return [projects[index] for index in selected]
        console.print("[red]No valid repositories selected.[/]")


def discover_projects(
    paths: list[str],
    scan: str | None,
    mine: bool,
    select: bool,
    depth: int,
    repo_limit: int,
    console: Console,
) -> list[Project]:
    discovered: dict[Path | str, Project] = {}

    if mine:
        account_projects = repos_for_authenticated_user(repo_limit)
        if select:
            account_projects = choose_repositories(console, account_projects)
        for project in account_projects:
            key = project.repo or project.name
            discovered[key] = project

    if scan:
        base = Path(scan).expanduser().resolve()
        for git_dir in base.glob("**/.git"):
            project_path = git_dir.parent.resolve()
            if len(project_path.relative_to(base).parts) > depth:
                continue
            repo = remote_repo(project_path)
            discovered[project_path] = Project(project_path.name, project_path, repo, local=True)

    for value in paths or ([] if scan or mine else ["."]):
        path = Path(value).expanduser()
        if "/" in value and not path.exists():
            discovered[value] = Project(value.split("/")[-1], Path.cwd(), value)
            continue
        if not path.exists():
            discovered[value] = Project(value, Path.cwd(), value)
            continue

        root = git_root(path.resolve())
        is_local = root is not None
        root = root or path.resolve()
        repo = remote_repo(root)
        discovered[root] = Project(root.name, root, repo, local=is_local)

    return sorted(discovered.values(), key=lambda project: project.name.lower())


def fetch_runs(project: Project, limit: int) -> ProjectRuns:
    args = [
        "run",
        "list",
        "--limit",
        str(limit),
        "--json",
        ",".join(
            [
                "status",
                "conclusion",
                "name",
                "headBranch",
                "event",
                "createdAt",
                "updatedAt",
                "databaseId",
                "workflowName",
                "url",
            ]
        ),
    ]
    if project.repo:
        args.extend(["--repo", project.repo])

    try:
        result = run_gh(args, cwd=project.path)
    except FileNotFoundError as exc:
        missing = exc.filename or "gh"
        return ProjectRuns(
            project,
            [],
            f"Could not run {missing}. Is GitHub CLI installed?",
        )

    if result.returncode != 0:
        message = result.stderr.strip() or "GitHub CLI did not return runs."
        return ProjectRuns(project, [], message)

    try:
        runs = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        return ProjectRuns(project, [], f"Could not parse GitHub output: {exc}")

    return ProjectRuns(project, runs)


def fetch_prs(project: Project, limit: int) -> tuple[list[dict], str | None]:
    args = [
        "pr",
        "list",
        "--state",
        "open",
        "--limit",
        str(limit),
        "--json",
        ",".join(
            [
                "number",
                "title",
                "headRefName",
                "isDraft",
                "mergeStateStatus",
                "reviewDecision",
                "statusCheckRollup",
                "author",
                "updatedAt",
                "url",
            ]
        ),
    ]
    if project.repo:
        args.extend(["--repo", project.repo])

    try:
        result = run_gh(args, cwd=project.path)
    except FileNotFoundError as exc:
        missing = exc.filename or "gh"
        return [], f"Could not run {missing}. Is GitHub CLI installed?"

    if result.returncode != 0:
        message = result.stderr.strip() or "GitHub CLI did not return pull requests."
        return [], message

    try:
        prs = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        return [], f"Could not parse GitHub PR output: {exc}"

    return prs, None


def fetch_project_runs(project: Project, run_limit: int, pr_limit: int) -> ProjectRuns:
    group = fetch_runs(project, run_limit)
    prs, pr_error = fetch_prs(project, pr_limit)
    worktrees = project_worktrees(project)
    default_branch = default_branch_name(project.path) if project.local else None
    return ProjectRuns(
        group.project, group.runs, group.error, prs, pr_error, worktrees, default_branch
    )


def parse_github_time(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def time_ago(iso: str) -> str:
    delta = datetime.now(UTC) - parse_github_time(iso)
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def duration(created: str, updated: str) -> str:
    seconds = max(
        0,
        int((parse_github_time(updated) - parse_github_time(created)).total_seconds()),
    )
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


def run_style(run: dict) -> tuple[str, str]:
    status = run.get("status", "")
    conclusion = run.get("conclusion") or ""
    if status == "completed" and conclusion in CONCLUSION_STYLE:
        return CONCLUSION_STYLE[conclusion]
    return STATUS_STYLE.get(status, ("?", "dim"))


def pr_check_counts(pr: dict) -> Counter:
    counts: Counter = Counter()
    for check in pr.get("statusCheckRollup") or []:
        state = normalize_check_state(check)
        counts[str(state).lower()] += 1
    return counts


def normalize_check_state(check: dict) -> str:
    conclusion = check.get("conclusion") or check.get("workflowRun", {}).get("conclusion")
    state = check.get("state")
    status = check.get("status") or check.get("workflowRun", {}).get("status")

    if conclusion:
        return str(conclusion).lower()
    if state:
        return str(state).lower()
    if status:
        return str(status).lower()
    return "unknown"


def check_name(check: dict) -> str:
    workflow_run = check.get("workflowRun") or {}
    return (
        check.get("name")
        or check.get("context")
        or workflow_run.get("name")
        or workflow_run.get("workflowName")
        or "Unnamed check"
    )


def check_style(state: str) -> tuple[str, str]:
    if state in CONCLUSION_STYLE:
        return CONCLUSION_STYLE[state]
    return STATUS_STYLE.get(state, ("?", "dim"))


def normalize_run_state(run: dict) -> str:
    status = run.get("status") or "unknown"
    conclusion = run.get("conclusion")
    if status == "completed" and conclusion:
        return str(conclusion).lower()
    return str(status).lower()


def related_pr_runs(pr: dict, runs: list[dict]) -> list[dict]:
    branch = pr.get("headRefName")
    pull_ref = f"refs/pull/{pr.get('number')}/head" if pr.get("number") else None
    related: list[dict] = []
    for run in runs:
        head_branch = run.get("headBranch")
        if head_branch == branch or (pull_ref and head_branch == pull_ref):
            related.append(run)
    return related


def pr_related_run_ids(group: ProjectRuns) -> set[int]:
    related_ids: set[int] = set()
    for pr in group.prs or []:
        for run in related_pr_runs(pr, group.runs):
            run_id = run.get("databaseId")
            if isinstance(run_id, int):
                related_ids.add(run_id)
    return related_ids


def visible_run_check_counts(runs: list[dict]) -> Counter:
    counts: Counter = Counter()
    for run in runs:
        counts[normalize_run_state(run)] += 1
    return counts


def pr_progress_counts(pr: dict, runs: list[dict] | None = None) -> Counter:
    if runs is not None:
        visible_counts = visible_run_check_counts(runs)
        if visible_counts:
            return visible_counts
    return pr_check_counts(pr)


def check_progress(checks: Counter) -> tuple[int, int, int, int]:
    total = sum(checks.values())
    failing = (
        checks["failure"]
        + checks["failed"]
        + checks["error"]
        + checks["timed_out"]
        + checks["action_required"]
        + checks["cancelled"]
    )
    passing = checks["success"] + checks["neutral"] + checks["skipped"]
    completed = min(total, passing + failing)
    return completed, total, passing, failing


def pr_status(pr: dict, runs: list[dict] | None = None) -> tuple[str, str, str]:
    completed, total, passing, failing = check_progress(pr_progress_counts(pr, runs))
    check_detail = f"{completed}/{total} checks" if total else None

    if pr.get("isDraft"):
        detail = check_detail or "draft"
        return "D", "dim", detail
    if failing:
        if check_detail:
            suffix = "1 fail" if failing == 1 else f"{failing} fail"
            detail = f"{check_detail}, {suffix}"
        else:
            detail = "check failing" if failing == 1 else f"{failing} checks failing"
        return "✗", "red", detail
    if total and completed < total:
        return "⟳", "yellow", check_detail or "checks pending"

    review = pr.get("reviewDecision")
    if review == "CHANGES_REQUESTED":
        return "!", "magenta", "changes requested"
    if review == "REVIEW_REQUIRED":
        return "?", "cyan", "review required"

    merge_state = pr.get("mergeStateStatus")
    if merge_state in {"BLOCKED", "DIRTY", "UNKNOWN"}:
        return "!", "magenta", merge_state.lower().replace("_", " ")
    if total and passing == total:
        return "✓", "green", check_detail or "checks passing"
    return "•", "bright_blue", (merge_state or "open").lower().replace("_", " ")


def runs_for_branch(branch: str, runs: list[dict]) -> list[dict]:
    return [run for run in runs if run.get("headBranch") == branch]


def build_agent_rows(group: ProjectRuns) -> list[AgentBranch]:
    """Local worktrees first, then branches that only exist as open PRs."""
    prs_by_branch = {
        pr.get("headRefName"): pr for pr in group.prs or [] if pr.get("headRefName")
    }

    worktree_rows: list[AgentBranch] = []
    seen: set[str] = set()
    for worktree in group.worktrees or []:
        branch = worktree.get("branch")
        if not branch or branch in seen:
            continue
        seen.add(branch)
        pr = prs_by_branch.get(branch)
        runs = related_pr_runs(pr, group.runs) if pr else runs_for_branch(branch, group.runs)
        worktree_rows.append(
            AgentBranch(branch, True, worktree.get("path"), pr, runs)
        )

    remote_rows = [
        AgentBranch(branch, False, None, pr, related_pr_runs(pr, group.runs))
        for branch, pr in prs_by_branch.items()
        if branch not in seen
    ]

    worktree_rows.sort(key=lambda row: row.name.lower())
    remote_rows.sort(key=lambda row: row.name.lower())
    return worktree_rows + remote_rows


def agent_detail(row: AgentBranch, default_branch: str | None) -> str:
    """Detail text for a branch with no open pull request."""
    if row.name == default_branch or not row.runs:
        return ""
    return "local only"


def default_branch_name(path: Path) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(path), "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip().removeprefix("origin/") or None


def agent_test_status(row: AgentBranch) -> tuple[str, str]:
    """Summarize CI for a branch as a short label and style."""
    counts = visible_run_check_counts(row.runs) if row.runs else pr_check_counts(row.pr or {})
    completed, total, _, failing = check_progress(counts)

    if not total:
        return ("—", "dim") if row.pr else ("clean", "dim")
    if failing:
        return "tests ✗", "red"
    if completed < total:
        return "tests ⟳", "yellow"
    return "tests ✓", "green"


def is_active(run: dict) -> bool:
    return run.get("status") in {"in_progress", "queued", "waiting", "requested", "pending"}


def build_summary(project_runs: list[ProjectRuns]) -> Panel:
    runs = [run for group in project_runs for run in group.runs]
    prs = [pr for group in project_runs for pr in group.prs or []]
    conclusions = Counter(run.get("conclusion") or "none" for run in runs)
    active = sum(1 for run in runs if is_active(run))
    failures = conclusions["failure"] + conclusions["timed_out"] + conclusions["action_required"]
    draft_prs = sum(1 for pr in prs if pr.get("isDraft"))

    summary = Table.grid(expand=True)
    summary.add_column(justify="center")
    summary.add_column(justify="center")
    summary.add_column(justify="center")
    summary.add_column(justify="center")
    summary.add_column(justify="center")
    summary.add_row(
        metric("Projects", str(len(project_runs)), "cyan"),
        metric("Active", str(active), "yellow" if active else "green"),
        metric("Recent failures", str(failures), "red" if failures else "green"),
        metric("Open PRs", str(len(prs)), "yellow" if prs else "green"),
        metric("Draft PRs", str(draft_prs), "dim" if draft_prs else "green"),
    )
    return Panel(summary, border_style="bright_blue", padding=(1, 2))


def metric(label: str, value: str, style: str) -> Text:
    text = Text()
    text.append(f"{value}\n", style=f"bold {style}")
    text.append(label, style="dim")
    return text


def project_title(group: ProjectRuns) -> Text:
    active = sum(1 for run in group.runs if is_active(run))
    failures = sum(
        1 for run in group.runs if run.get("conclusion") in {"failure", "timed_out"}
    )

    title = Text(group.project.name, style="bold")
    if group.project.repo:
        title.append(f"  {group.project.repo}", style="dim")
    if active:
        title.append(f"  {active} active", style="yellow")
    if group.prs:
        title.append(f"  {len(group.prs)} PRs", style="bright_blue")
    if failures:
        title.append(f"  {failures} failing", style="red")
    return title


def standalone_runs(group: ProjectRuns) -> list[dict]:
    """Runs not already nested under a pull request."""
    related_ids = pr_related_run_ids(group)
    return [
        run
        for run in group.runs
        if not isinstance(run.get("databaseId"), int) or run["databaseId"] not in related_ids
    ]


def selectable_rows(
    groups: list[ProjectRuns], expanded_prs: set[int] | None = None
) -> list[SelectableRow]:
    """Cursor stops in dashboard order; a PR's runs are stops only while expanded."""
    expanded = expanded_prs or set()
    rows: list[SelectableRow] = []
    for group in groups:
        for run in standalone_runs(group):
            rows.append(SelectableRow("run", group.project, run=run))
        for pr in group.prs or []:
            rows.append(SelectableRow("pr", group.project, pr=pr))
            if pr.get("number") in expanded:
                for run in related_pr_runs(pr, group.runs):
                    rows.append(SelectableRow("run", group.project, run=run, pr=pr))
    return rows


def build_runs_table(group: ProjectRuns, selected_id: int | None = None) -> Table | Text:
    if group.error:
        return Text(group.error, style="red")

    runs = standalone_runs(group)

    if not runs:
        return Text("No standalone workflow runs found.", style="dim")

    table = Table.grid(expand=True)
    table.add_column(width=2)  # cursor indicator
    table.add_column(width=2)  # status icon
    table.add_column(ratio=3)
    table.add_column(ratio=2)
    table.add_column(width=12)
    table.add_column(width=11, justify="right")
    table.add_column(width=9, justify="right")

    for run in runs:
        icon, style = run_style(run)
        is_selected = selected_id is not None and run.get("databaseId") == selected_id
        cursor_cell = Text("▶ " if is_selected else "  ", style="bold bright_white")
        workflow = Text(run.get("workflowName") or run.get("name") or "Unnamed workflow")
        workflow.stylize("bold" if is_active(run) else style)
        branch = Text(run.get("headBranch") or "unknown", style="bright_white")
        event = Text(run.get("event") or "unknown", style="dim")
        elapsed = Text(duration(run["createdAt"], run["updatedAt"]), style="cyan")
        when = Text(time_ago(run["createdAt"]), style="dim")
        row_style = SELECTED_ROW_STYLE if is_selected else ""
        table.add_row(
            cursor_cell, Text(icon, style=style), workflow, branch, event, elapsed, when,
            style=row_style,
        )

    return table


def build_prs_table(
    group: ProjectRuns,
    expanded_prs: set[int] | None = None,
    selected_pr: int | None = None,
    selected_id: int | None = None,
) -> Table | Text | None:
    if group.pr_error:
        return Text(f"PRs: {group.pr_error}", style="red")

    prs = group.prs or []
    if not prs:
        return Text("No open pull requests.", style="dim")

    expanded = expanded_prs or set()

    table = Table.grid(expand=True)
    table.add_column(width=2)  # cursor indicator
    table.add_column(width=2)  # disclosure marker
    table.add_column(width=2)  # status icon
    table.add_column(width=8, style="cyan")
    table.add_column(ratio=4)
    table.add_column(ratio=2)
    table.add_column(ratio=2)
    table.add_column(width=12, justify="right")

    for pr in prs:
        runs = related_pr_runs(pr, group.runs)
        number = pr.get("number")
        is_expanded = number in expanded
        children = bool(runs or pr.get("statusCheckRollup"))
        icon, style, status = pr_status(pr, runs)
        author = pr.get("author") or {}
        title = Text(
            pr.get("title") or "Untitled pull request",
            overflow="ellipsis",
            no_wrap=True,
        )
        title.stylize("dim" if pr.get("isDraft") else "bold")
        updated = Text(time_ago(pr["updatedAt"]), style="dim")

        is_selected = selected_pr is not None and number == selected_pr
        cursor_cell = Text("▶ " if is_selected else "  ", style="bold bright_white")
        if children:
            marker = Text("▾ " if is_expanded else "▸ ", style="bright_black")
        else:
            marker = Text("  ")

        table.add_row(
            cursor_cell,
            marker,
            Text(icon, style=style),
            f"#{number if number is not None else '?'}",
            title,
            Text(status, style=style, overflow="ellipsis", no_wrap=True),
            Text(author.get("login") or "unknown", style="dim"),
            updated,
            style=SELECTED_ROW_STYLE if is_selected else "",
        )

        if not is_expanded:
            continue

        if runs:
            for run in runs:
                run_icon, run_style_name = run_style(run)
                workflow = Text(
                    f"  ↳ {run.get('workflowName') or run.get('name') or 'Unnamed workflow'}",
                    style="dim",
                )
                if is_active(run):
                    workflow.stylize("yellow")
                run_selected = (
                    selected_id is not None and run.get("databaseId") == selected_id
                )
                table.add_row(
                    Text("▶ " if run_selected else "  ", style="bold bright_white"),
                    Text("  "),
                    Text(run_icon, style=run_style_name),
                    "",
                    workflow,
                    Text(run.get("event") or "workflow", style="dim"),
                    Text(run.get("headBranch") or "unknown", style="bright_white"),
                    Text(time_ago(run["createdAt"]), style="dim"),
                    style=SELECTED_ROW_STYLE if run_selected else "",
                )
            continue

        for check in pr.get("statusCheckRollup") or []:
            state = normalize_check_state(check)
            check_icon, check_style_name = check_style(state)
            table.add_row(
                Text("  "),
                Text("  "),
                Text(check_icon, style=check_style_name),
                "",
                Text(f"  ↳ {check_name(check)}", style="dim"),
                Text(state.replace("_", " "), style=check_style_name),
                Text("check", style="dim"),
                "",
            )

    return table


def build_agents_table(group: ProjectRuns) -> Table | Text:
    rows = build_agent_rows(group)
    if not rows:
        return Text("No worktrees or PR branches.", style="dim")

    table = Table.grid(expand=True)
    table.add_column(width=2)
    table.add_column(ratio=3)
    table.add_column(width=9)
    table.add_column(width=8, style="cyan")
    table.add_column(ratio=2)
    table.add_column(width=10)

    for row in rows:
        test_label, test_style = agent_test_status(row)
        marker = Text("⎇ " if row.is_worktree else "  ", style="bright_black")
        name = Text(row.name, style="bright_white" if row.is_worktree else "dim")

        if row.pr:
            _, pr_style, pr_detail = pr_status(row.pr, row.runs)
            pr_cell = Text(f"#{row.pr.get('number', '?')}", style="cyan")
            detail = Text(pr_detail, style=pr_style)
        else:
            pr_cell = Text("")
            detail = Text(agent_detail(row, group.default_branch), style="dim")

        origin = Text("worktree" if row.is_worktree else "remote", style="dim")
        table.add_row(marker, name, Text(test_label, style=test_style), pr_cell, detail, origin)

    return table


def build_project_panel(
    group: ProjectRuns,
    selected_id: int | None = None,
    expanded_prs: set[int] | None = None,
    selected_pr: int | None = None,
) -> Panel:
    sections: list[Table | Text | Align] = []
    sections.append(Text("Runs", style="bold bright_white"))
    sections.append(build_runs_table(group, selected_id))
    sections.append(Text("Pull Requests", style="bold bright_white"))
    sections.append(
        build_prs_table(group, expanded_prs, selected_pr, selected_id)
        or Text("No open pull requests.", style="dim")
    )
    sections.append(Text("Agents", style="bold bright_white"))
    sections.append(build_agents_table(group))
    body = Group(*sections)

    border_style = "yellow" if any(is_active(run) for run in group.runs) else "bright_black"
    if any(run.get("conclusion") in {"failure", "timed_out"} for run in group.runs):
        border_style = "red"
    if group.error or group.pr_error:
        border_style = "red"

    return Panel(body, title=project_title(group), border_style=border_style, expand=True)


def build_dashboard(
    project_runs: list[ProjectRuns],
    last_updated: str,
    refresh_seconds: int,
    selected_id: int | None = None,
    expanded_prs: set[int] | None = None,
    selected_pr: int | None = None,
) -> Group:
    header = Text()
    header.append("GitHub Actions Monitor", style="bold bright_white")
    header.append(f"  updated {last_updated}", style="dim")
    header.append(f"  refresh {refresh_seconds}s", style="dim")
    header.append("  ↑/↓ navigate", style="dim")
    header.append("  Space expand", style="dim")
    header.append("  Enter logs", style="dim")
    header.append("  q quit", style="dim")

    panels = [
        build_summary(project_runs),
        *[
            build_project_panel(group, selected_id, expanded_prs, selected_pr)
            for group in project_runs
        ],
    ]
    return Group(Align.center(header), *panels)


@app.command()
def main(
    projects: Annotated[
        list[str] | None,
        typer.Argument(
            help="Repository paths or OWNER/REPO names. Defaults to the current project."
        ),
    ] = None,
    scan: Annotated[
        str | None,
        typer.Option(
            "--scan",
            metavar="DIR",
            help="Find git repositories under DIR and group runs by project.",
        ),
    ] = None,
    mine: Annotated[
        bool,
        typer.Option(
            "--mine",
            help="Monitor repositories visible to the authenticated GitHub user.",
        ),
    ] = False,
    select: Annotated[
        bool,
        typer.Option(
            "--select",
            help="Choose repositories from a startup list and save them.",
        ),
    ] = False,
    settings: Annotated[
        Path,
        typer.Option(
            "--settings",
            help="Settings file for saved GitHub repositories.",
        ),
    ] = default_settings_path(),
    repo_limit: Annotated[
        int,
        typer.Option(
            "--repo-limit",
            min=1,
            help=f"Maximum repositories to load with --mine. Default: {DEFAULT_REPO_LIMIT}.",
        ),
    ] = DEFAULT_REPO_LIMIT,
    depth: Annotated[
        int,
        typer.Option(
            "--depth",
            min=0,
            help=f"Maximum directory depth for --scan. Default: {DEFAULT_SCAN_DEPTH}.",
        ),
    ] = DEFAULT_SCAN_DEPTH,
    limit: Annotated[
        int,
        typer.Option(
            "--limit",
            min=1,
            help=f"Runs to fetch per project. Default: {DEFAULT_LIMIT}.",
        ),
    ] = DEFAULT_LIMIT,
    pr_limit: Annotated[
        int,
        typer.Option(
            "--pr-limit",
            min=1,
            help=f"Open pull requests to fetch per project. Default: {DEFAULT_LIMIT}.",
        ),
    ] = DEFAULT_LIMIT,
    refresh: Annotated[
        int,
        typer.Option(
            "--refresh",
            min=1,
            help=f"Refresh interval in seconds. Default: {REFRESH_SECONDS}.",
        ),
    ] = REFRESH_SECONDS,
) -> None:
    console = Console()
    selected_projects = projects or []
    explicit_source = bool(selected_projects or scan or mine or select)

    if not explicit_source:
        saved_repos = load_settings_repos(settings)
        if saved_repos:
            projects = projects_from_repo_names(saved_repos)
        else:
            account_projects = repos_for_authenticated_user(repo_limit)
            projects = choose_repositories(console, account_projects)
            save_settings_repos(settings, projects)

        if not projects:
            console.print("[red]No projects found.[/]")
            return

        run_monitor(console, projects, limit, pr_limit, refresh)
        return

    if select:
        mine = True

    projects = discover_projects(
        selected_projects,
        scan,
        mine,
        select,
        depth,
        repo_limit,
        console,
    )

    if select:
        save_settings_repos(settings, projects)

    if not projects:
        console.print("[red]No projects found.[/]")
        return

    run_monitor(console, projects, limit, pr_limit, refresh)


def run_monitor(
    console: Console, projects: list[Project], limit: int, pr_limit: int, refresh: int
) -> None:
    cursor = 0
    expanded_prs: set[int] = set()
    groups: list[ProjectRuns] = []

    with KeyWatcher() as keys:
        with Live(console=console, refresh_per_second=2, screen=True) as live:
            while True:
                groups = [fetch_project_runs(project, limit, pr_limit) for project in projects]
                now = datetime.now().strftime("%H:%M:%S")

                def render() -> None:
                    rows = selectable_rows(groups, expanded_prs)
                    selected = rows[cursor] if rows else None
                    live.update(
                        build_dashboard(
                            groups,
                            now,
                            refresh,
                            selected.run.get("databaseId")
                            if selected and selected.run
                            else None,
                            expanded_prs,
                            selected.pr.get("number")
                            if selected and selected.kind == "pr" and selected.pr
                            else None,
                        )
                    )

                rows = selectable_rows(groups, expanded_prs)
                if rows:
                    cursor = min(cursor, len(rows) - 1)
                render()

                deadline = time.monotonic() + refresh
                while time.monotonic() < deadline:
                    key = keys.read_key()
                    rows = selectable_rows(groups, expanded_prs)
                    if key == "quit":
                        return
                    if key == "up" and rows:
                        cursor = max(0, cursor - 1)
                        render()
                    elif key == "down" and rows:
                        cursor = min(len(rows) - 1, cursor + 1)
                        render()
                    elif key == "toggle" and rows:
                        selected = rows[cursor]
                        number = selected.pr.get("number") if selected.pr else None
                        if isinstance(number, int):
                            if selected.kind == "pr" and number in expanded_prs:
                                expanded_prs.discard(number)
                            else:
                                # Collapsing from a child run puts the cursor
                                # back on its pull request.
                                if selected.kind == "run":
                                    expanded_prs.discard(number)
                                    cursor = next(
                                        (
                                            i
                                            for i, row in enumerate(
                                                selectable_rows(groups, expanded_prs)
                                            )
                                            if row.kind == "pr"
                                            and row.pr
                                            and row.pr.get("number") == number
                                        ),
                                        cursor,
                                    )
                                else:
                                    expanded_prs.add(number)
                            new_rows = selectable_rows(groups, expanded_prs)
                            if new_rows:
                                cursor = min(cursor, len(new_rows) - 1)
                            render()
                    elif key == "enter" and rows:
                        selected = rows[cursor]
                        if selected.kind == "run" and selected.run:
                            live.stop()
                            keys.suspend()
                            show_run_logs(console, selected.run, selected.project)
                            keys.resume()
                            live.start()
                            render()
                        elif selected.pr:
                            number = selected.pr.get("number")
                            if isinstance(number, int):
                                expanded_prs.symmetric_difference_update({number})
                                render()
                    time.sleep(0.1)


if __name__ == "__main__":
    app()
