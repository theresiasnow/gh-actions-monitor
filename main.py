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


@dataclass(frozen=True)
class ProjectRuns:
    project: Project
    runs: list[dict]
    error: str | None = None
    prs: list[dict] | None = None
    pr_error: str | None = None


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
            discovered[project_path] = Project(project_path.name, project_path, repo)

    for value in paths or ([] if scan or mine else ["."]):
        path = Path(value).expanduser()
        if "/" in value and not path.exists():
            discovered[value] = Project(value.split("/")[-1], Path.cwd(), value)
            continue
        if not path.exists():
            discovered[value] = Project(value, Path.cwd(), value)
            continue

        root = git_root(path.resolve()) or path.resolve()
        repo = remote_repo(root)
        discovered[root] = Project(root.name, root, repo)

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
    return ProjectRuns(group.project, group.runs, group.error, prs, pr_error)


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


def build_runs_table(group: ProjectRuns, selected_id: int | None = None) -> Table | Text:
    if group.error:
        return Text(group.error, style="red")

    related_ids = pr_related_run_ids(group)
    runs = [
        run
        for run in group.runs
        if not isinstance(run.get("databaseId"), int) or run["databaseId"] not in related_ids
    ]

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
        row_style = "reverse" if is_selected else ""
        table.add_row(
            cursor_cell, Text(icon, style=style), workflow, branch, event, elapsed, when,
            style=row_style,
        )

    return table


def build_prs_table(group: ProjectRuns) -> Table | Text | None:
    if group.pr_error:
        return Text(f"PRs: {group.pr_error}", style="red")

    prs = group.prs or []
    if not prs:
        return Text("No open pull requests.", style="dim")

    table = Table.grid(expand=True)
    table.add_column(width=2)
    table.add_column(width=8, style="cyan")
    table.add_column(ratio=4)
    table.add_column(ratio=2)
    table.add_column(ratio=2)
    table.add_column(width=12, justify="right")

    for pr in prs:
        runs = related_pr_runs(pr, group.runs)
        icon, style, status = pr_status(pr, runs)
        author = pr.get("author") or {}
        title = Text(pr.get("title") or "Untitled pull request")
        title.stylize("dim" if pr.get("isDraft") else "bold")
        updated = Text(time_ago(pr["updatedAt"]), style="dim")
        table.add_row(
            Text(icon, style=style),
            f"#{pr.get('number', '?')}",
            title,
            Text(status, style=style),
            Text(author.get("login") or "unknown", style="dim"),
            updated,
        )
        if runs:
            for run in runs:
                run_icon, run_style_name = run_style(run)
                workflow = Text(
                    f"  ↳ {run.get('workflowName') or run.get('name') or 'Unnamed workflow'}",
                    style="dim",
                )
                if is_active(run):
                    workflow.stylize("yellow")
                table.add_row(
                    Text(run_icon, style=run_style_name),
                    "",
                    workflow,
                    Text(run.get("event") or "workflow", style="dim"),
                    Text(run.get("headBranch") or "unknown", style="bright_white"),
                    Text(time_ago(run["createdAt"]), style="dim"),
                )
            continue

        for check in pr.get("statusCheckRollup") or []:
            state = normalize_check_state(check)
            check_icon, check_style_name = check_style(state)
            table.add_row(
                Text(check_icon, style=check_style_name),
                "",
                Text(f"  ↳ {check_name(check)}", style="dim"),
                Text(state.replace("_", " "), style=check_style_name),
                Text("check", style="dim"),
                "",
            )

    return table


def build_project_panel(group: ProjectRuns, selected_id: int | None = None) -> Panel:
    sections: list[Table | Text | Align] = []
    sections.append(Text("Runs", style="bold bright_white"))
    sections.append(build_runs_table(group, selected_id))
    sections.append(Text("Pull Requests", style="bold bright_white"))
    sections.append(build_prs_table(group) or Text("No open pull requests.", style="dim"))
    body = Group(*sections)

    border_style = "yellow" if any(is_active(run) for run in group.runs) else "bright_black"
    if any(run.get("conclusion") in {"failure", "timed_out"} for run in group.runs):
        border_style = "red"
    if group.error or group.pr_error:
        border_style = "red"

    return Panel(body, title=project_title(group), border_style=border_style, expand=True)


def build_dashboard(
    project_runs: list[ProjectRuns], last_updated: str, refresh_seconds: int, selected_id: int | None = None
) -> Group:
    header = Text()
    header.append("GitHub Actions Monitor", style="bold bright_white")
    header.append(f"  updated {last_updated}", style="dim")
    header.append(f"  refresh {refresh_seconds}s", style="dim")
    header.append("  ↑/↓ navigate", style="dim")
    header.append("  Enter logs", style="dim")
    header.append("  q quit", style="dim")

    panels = [build_summary(project_runs), *[build_project_panel(group, selected_id) for group in project_runs]]
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
    groups: list[ProjectRuns] = []
    all_runs: list[tuple[Project, dict]] = []

    with KeyWatcher() as keys:
        with Live(console=console, refresh_per_second=2, screen=True) as live:
            while True:
                groups = [fetch_project_runs(project, limit, pr_limit) for project in projects]
                all_runs = [(g.project, run) for g in groups for run in g.runs]
                if all_runs:
                    cursor = min(cursor, len(all_runs) - 1)
                selected_id = all_runs[cursor][1].get("databaseId") if all_runs else None
                now = datetime.now().strftime("%H:%M:%S")
                live.update(build_dashboard(groups, now, refresh, selected_id))

                deadline = time.monotonic() + refresh
                while time.monotonic() < deadline:
                    key = keys.read_key()
                    if key == "quit":
                        return
                    if key == "up" and all_runs:
                        cursor = max(0, cursor - 1)
                        selected_id = all_runs[cursor][1].get("databaseId")
                        live.update(build_dashboard(groups, now, refresh, selected_id))
                    elif key == "down" and all_runs:
                        cursor = min(len(all_runs) - 1, cursor + 1)
                        selected_id = all_runs[cursor][1].get("databaseId")
                        live.update(build_dashboard(groups, now, refresh, selected_id))
                    elif key == "enter" and all_runs:
                        project, run = all_runs[cursor]
                        live.stop()
                        keys.suspend()
                        show_run_logs(console, run, project)
                        keys.resume()
                        live.start()
                        live.update(build_dashboard(groups, now, refresh, selected_id))
                    time.sleep(0.1)


if __name__ == "__main__":
    app()
