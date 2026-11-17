# BugMind installer for Windows: user-level only, no administrator rights, no system changes.
#
#   irm https://raw.githubusercontent.com/Kiramido1/bugmind/main/install.ps1 | iex
#   .\install.ps1                 # from a cloned checkout: installs that checkout (editable)
#
# Options (from a clone, or via:  & ([scriptblock]::Create((irm <url>))) -Uninstall):
#   -Ref <branch|tag>   install a specific branch or tag (default: main)
#   -Uninstall          remove the bugmind command (your bugs and rules are kept)
#   -NoUvBootstrap      never download uv; fail instead if neither uv nor pipx is present
param([string]$Ref = "main", [switch]$Uninstall, [switch]$NoUvBootstrap)

$ErrorActionPreference = "Stop"
$Repo = "Kiramido1/bugmind"

function Say($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Ok($msg) { Write-Host "OK  $msg" -ForegroundColor Green }
# throw, not exit: `irm | iex` runs in the user's own session and exit would close their window
function Die($msg) { throw "BugMind installer: $msg" }
function Has($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }
function Run {  # run a native command; fail on a non-zero exit code (PowerShell 5 ignores it otherwise)
    $exe, $rest = $args  # plain $args: an advanced function would try to bind --force etc. as its own parameters
    & $exe @rest
    if ($LASTEXITCODE -ne 0) { Die "'$exe $rest' failed with exit code $LASTEXITCODE" }
}

# uv and pipx put tools in ~\.local\bin; make it usable in this session right away.
$Bin = Join-Path $HOME ".local\bin"
if (($env:Path -split ";") -notcontains $Bin) { $env:Path = "$Bin;$env:Path" }

# A checkout next to this script wins over the GitHub archive (contributors, offline installs).
$Here = if ($PSScriptRoot) { $PSScriptRoot } else { "" }
if ($Here -and (Select-String -Quiet -Pattern '^name = "bugmind"' -Path (Join-Path $Here "pyproject.toml") -ErrorAction SilentlyContinue)) {
    $Source = $Here
} else {
    $Source = "bugmind @ https://github.com/$Repo/archive/$Ref.zip"
}

if ($Uninstall) {
    if ((Has uv) -and ((uv tool list 2>$null) -match "^bugmind ")) { Run uv tool uninstall bugmind }
    elseif ((Has pipx) -and ((pipx list --short 2>$null) -match "^bugmind ")) { Run pipx uninstall bugmind }
    else { Die "bugmind is not installed with uv or pipx." }
    Ok "Removed the bugmind command."
    Write-Host "Your data was kept: $env:APPDATA\bugmind  $env:LOCALAPPDATA\bugmind" -ForegroundColor DarkGray
    return
}

Say "Installing BugMind from $($Source -replace '^bugmind @ ', '')"

if (-not (Has uv) -and -not (Has pipx)) {
    if ($NoUvBootstrap) { Die "Install uv (https://docs.astral.sh/uv/) or pipx first." }
    Say "Installing uv (Astral's Python tool manager) into $Bin"
    $env:UV_NO_MODIFY_PATH = "1"  # PATH is handled below with `uv tool update-shell`
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    Remove-Item Env:UV_NO_MODIFY_PATH
    if (-not (Has uv)) { Die "uv installation failed; see https://docs.astral.sh/uv/getting-started/installation/" }
}

if (Has uv) {
    # uv downloads a managed Python 3.12+ itself when none (or an older one) is installed.
    if ($Source -eq $Here) { Run uv tool install --force --editable $Source }
    else { Run uv tool install --force --reinstall $Source }
    uv tool update-shell 2>$null | Out-Null  # adds ~\.local\bin to the user PATH for new terminals
} else {
    if (-not (Has python) -or (python -c "import sys; print(sys.version_info >= (3, 12))") -ne "True") {
        Die "BugMind needs Python 3.12+. Install uv (it brings its own Python) and run this again."
    }
    Run pipx install --force $Source
    pipx ensurepath 2>$null | Out-Null
}

if (-not (Has bugmind)) { Die "Installed, but 'bugmind' is not on PATH. Add $Bin to PATH and open a new terminal." }
Ok "$(bugmind --version) installed at $((Get-Command bugmind).Source)"
Write-Host ""
Write-Host "Next:"
Write-Host "  bugmind init     # detects Claude Code / OpenCode"
Write-Host "  bugmind          # open the workspace"
Write-Host "Open a new terminal if 'bugmind' is not found there." -ForegroundColor DarkGray
