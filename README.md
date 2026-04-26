# GitHub Actions Monitor

A Rich terminal dashboard for watching GitHub Actions across one or more
projects. It groups workflow runs by project, highlights active runs, and keeps
recent failures visible without requiring a browser tab.

## Requirements

- Python 3.14 or newer
- `uv`
- GitHub CLI installed and authenticated with `gh auth login`

## Usage

Run the monitor:

```sh
uv run gh-monitor
```

On first launch, the app opens a repository picker using repositories visible to
your authenticated GitHub account. The selected GitHub repositories are saved to:

```text
~/.config/gh-actions-monitor/settings.toml
```

The file is intentionally small and editable:

```toml
repos = ["owner/repo", "owner/another-repo"]
```

Future no-argument launches reuse those saved repositories.

Watch all repositories visible to your authenticated GitHub account, grouped by
project:

```sh
uv run gh-monitor --mine
```

Choose repositories again and update the saved settings:

```sh
uv run gh-monitor --select
```

Watch specific repositories or local checkouts:

```sh
uv run gh-monitor owner/repo ../another-project
```

Scan a directory for Git repositories and group each one as a project:

```sh
uv run gh-monitor --scan ~/Project/private --depth 2
```

Useful options:

```sh
uv run gh-monitor --limit 20 --refresh 10 --repo-limit 200
uv run gh-monitor --settings ~/.config/gh-actions-monitor/work.toml --select
```

Explicit repository arguments and `--scan` do not change the settings file.
`--select` uses `gh api /user/repos` and accepts selections such as `all`, `1-5`,
or `1,4,8`. If `gh` cannot determine a repository, the dashboard shows the error
in that project's panel instead of exiting.
