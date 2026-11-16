# Installing BugMind on Debian / Linux

## 1. Prerequisites

```bash
python3 --version        # 3.12 or newer (or let uv provide it)
# uv or pipx is optional: the installer fetches uv (user-level) when neither is present
```

Install and log in to at least one agent CLI:

- **Claude Code** — `claude` on PATH, logged in (`claude` then `/login`).
- **OpenCode** — `opencode` on PATH, a provider configured (`opencode auth login`).
- **Custom agent** — any executable that follows [the custom contract](PROVIDERS.md#custom-agent).

`bugmind providers` checks what is installed without calling any model.

## 2. Install

```bash
curl -fsSL https://raw.githubusercontent.com/Kiramido1/bugmind/main/install.sh | sh
```

Or from a clone: `./install.sh` (editable install of that checkout). Options: `--ref <branch|tag>`,
`--uninstall`, `--no-uv-bootstrap`. Pass them after `sh -s --` when piping, e.g.
`curl -fsSL …/install.sh | sh -s -- --uninstall`.

The installer uses `uv`, or `pipx` if that is what you have. If neither is present it downloads
`uv` into `~/.local/bin`. It never uses `sudo`. Installing manually works too:

```bash
uv tool install "bugmind @ https://github.com/Kiramido1/bugmind/archive/main.tar.gz"
# or, from a clone:  uv tool install .   /   pipx install .
```

Both install into an isolated environment and put `bugmind` in `~/.local/bin`; nothing is
installed system-wide. For development use an editable install in a local venv:

```bash
uv venv .venv
uv pip install --python .venv/bin/python -e ".[dev]"
```

## 3. Initialise

```bash
bugmind init                    # interactive provider selection
bugmind init --provider claude  # non-interactive
```

This creates:

```
~/.config/bugmind/
├── config.yaml
├── agents/          # reserved for agent-specific files
└── templates/       # your templates; override the built-ins
~/.local/share/bugmind/<project>/
├── bugs/bugs.jsonl
├── reports/
└── memory/ (BACKEND_PREVENTION.md, BUG_PATTERNS.md, STATISTICS.md, .backups/)
```

`XDG_CONFIG_HOME` / `XDG_DATA_HOME` are honoured. `BUGMIND_CONFIG_DIR` and `BUGMIND_DATA_DIR`
override both (handy for separate workspaces).

## 4. Run

```bash
bugmind            # TUI
bugmind --help     # CLI
```

## Uninstall

```bash
uv tool uninstall bugmind      # or: pipx uninstall bugmind
# Your data is kept. Remove it explicitly only if you are sure:
#   ~/.config/bugmind  ~/.local/share/bugmind  ~/.cache/bugmind
```
