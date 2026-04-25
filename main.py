import json
import subprocess
import time
from datetime import UTC, datetime

from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text

REFRESH_SECONDS = 15

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


def fetch_runs(limit: int = 20) -> list[dict]:
    result = subprocess.run(
        [
            "gh",
            "run",
            "list",
            "--limit",
            str(limit),
            "--json",
            "status,conclusion,name,headBranch,event,createdAt,updatedAt,databaseId,workflowName",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []
    return json.loads(result.stdout)


def time_ago(iso: str) -> str:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    delta = datetime.now(UTC) - dt
    s = int(delta.total_seconds())
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def duration(created: str, updated: str) -> str:
    a = datetime.fromisoformat(created.replace("Z", "+00:00"))
    b = datetime.fromisoformat(updated.replace("Z", "+00:00"))
    s = int((b - a).total_seconds())
    if s < 60:
        return f"{s}s"
    return f"{s // 60}m {s % 60}s"


def build_table(runs: list[dict], last_updated: str) -> Table:
    table = Table(
        title=f"GitHub Actions  [dim]updated {last_updated}[/]  [dim]refresh every {REFRESH_SECONDS}s[/]",
        show_header=True,
        header_style="bold",
        border_style="dim",
        expand=True,
    )
    table.add_column("", width=2, no_wrap=True)
    table.add_column("Workflow", min_width=20)
    table.add_column("Branch", min_width=15)
    table.add_column("Event", width=12)
    table.add_column("Duration", width=10, justify="right")
    table.add_column("When", width=10, justify="right")

    for run in runs:
        status = run["status"]
        conclusion = run.get("conclusion") or ""

        if status == "completed" and conclusion in CONCLUSION_STYLE:
            icon, style = CONCLUSION_STYLE[conclusion]
        elif status in STATUS_STYLE:
            icon, style = STATUS_STYLE[status]
        else:
            icon, style = "?", "dim"

        status_text = Text(icon, style=style)
        workflow = Text(run["workflowName"], style="bold" if status == "in_progress" else "")
        branch = Text(run["headBranch"], overflow="fold")
        event = Text(run["event"], style="dim")
        dur = duration(run["createdAt"], run["updatedAt"])
        when = time_ago(run["createdAt"])

        table.add_row(status_text, workflow, branch, event, dur, when)

    return table


def main():
    console = Console()
    with Live(console=console, refresh_per_second=1, screen=True) as live:
        while True:
            runs = fetch_runs()
            now = datetime.now().strftime("%H:%M:%S")
            live.update(build_table(runs, now))
            time.sleep(REFRESH_SECONDS)


if __name__ == "__main__":
    main()
