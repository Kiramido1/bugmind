# Installing BugMind

On Linux, follow the steps below. On Windows, jump to [Windows](#windows).

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

## Windows

BugMind runs on Windows 10 and 11, in Windows Terminal (recommended) or the classic console.

### 1. Install an agent

Install and log in to at least one agent CLI, exactly as on Linux:

- **Claude Code**: `irm https://claude.ai/install.ps1 | iex`, then run `claude` and `/login`.
- **OpenCode**: `npm i -g opencode-ai`, then `opencode auth login`.

### 2. Install BugMind

In PowerShell (no administrator rights needed):

```powershell
irm https://raw.githubusercontent.com/Kiramido1/bugmind/main/install.ps1 | iex
```

The installer uses `uv`, or `pipx` if that is what you have. If neither is present it downloads `uv` into
`%USERPROFILE%\.local\bin`. uv brings its own Python 3.12+, so you do not need to install Python.
`bugmind.exe` lands in `%USERPROFILE%\.local\bin`, and that folder is added to your user PATH.
**Open a new terminal** afterwards.

From a clone (editable install of that checkout):

```powershell
git clone https://github.com/Kiramido1/bugmind
cd bugmind
.\install.ps1
```

If PowerShell refuses to run the script ("running scripts is disabled on this system"), allow it for this
window only and run it again:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
```

Options: `-Ref <branch|tag>`, `-Uninstall`, `-NoUvBootstrap`. With the one-liner, pass them like this:

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/Kiramido1/bugmind/main/install.ps1))) -Uninstall
```

Installing manually works too: `uv tool install "bugmind @ https://github.com/Kiramido1/bugmind/archive/main.zip"`.

### 3. Initialise and run

```powershell
bugmind init
bugmind
```

### Where files live

| | Windows | Linux |
|---|---|---|
| Config and templates | `%APPDATA%\bugmind\` | `~/.config/bugmind/` |
| Bugs, reports, knowledge | `%LOCALAPPDATA%\bugmind\<project>\` | `~/.local/share/bugmind/<project>/` |
| Pasted screenshots, agent workdir | `%TEMP%\bugmind\` | `~/.cache/bugmind/` |

`bugmind config path` prints the exact paths. `BUGMIND_CONFIG_DIR` and `BUGMIND_DATA_DIR` work the same way.

### Windows notes

- **Pasting screenshots:** Windows Terminal keeps `Ctrl+V` for its own text paste, so press **`Alt+V`** to attach
  the screenshot on the clipboard (from `Win+Shift+S`, for example). Copying an image file in Explorer and
  pressing `Alt+V` attaches it too. Dragging a file into the window or `/attach C:\path\shot.png` also work.
- **Copy report** uses the Windows clipboard, so Arabic text is kept intact.
- **Agents installed with npm** (`claude.cmd`, `opencode.cmd`) run through `cmd.exe`. BugMind refuses to pass
  them a file path containing `" % ! ^ & | < >`, because `cmd.exe` would interpret those characters. Rename the
  screenshot or use the agent's native `.exe` installer.
- **Uninstall** keeps your data. Remove `%APPDATA%\bugmind` and `%LOCALAPPDATA%\bugmind` yourself only if you are sure.
