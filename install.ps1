# Install Vision on Windows, or update an install made with this script:
#
#   irm https://raw.githubusercontent.com/vision-sAi/vision/master/install.ps1 | iex
#
# It fetches Vision into ~\.local\share\vision\app, makes its own Python environment there (with uv, which it
# installs first if you don't have it), and puts a `vision` command in ~\.local\bin, adding that to your user
# PATH if it isn't there. Nothing is installed system-wide and nothing needs an administrator. The folders are
# the same ones Vision uses on Linux and macOS.
#
#   VISION_HOME     where to put it (default ~\.local\share\vision\app)
#   VISION_BRANCH   the branch to install (default master)
#   VISION_NO_MODIFY_PATH=1  don't touch your user PATH
$ErrorActionPreference = 'Stop'

$repo = if ($env:VISION_REPO) { $env:VISION_REPO } else { 'https://github.com/vision-sAi/vision' }
$branch = if ($env:VISION_BRANCH) { $env:VISION_BRANCH } else { 'master' }
$dest = if ($env:VISION_HOME) { $env:VISION_HOME } else { Join-Path $HOME '.local\share\vision\app' }
$bin = Join-Path $HOME '.local\bin'

function Step($text) { Write-Host "* $text" -ForegroundColor Cyan }
function Ok($text) { Write-Host "  ok  $text" -ForegroundColor Green }
# throw, not exit: under `irm | iex` an exit closes the user's PowerShell window, and the reason with it
function Die($text) { throw "Vision's install stopped: $text" }
function Have($name) { [bool](Get-Command $name -ErrorAction SilentlyContinue) }

Step 'Installing Vision'

# uv: Vision's Python and packages come through it, so no system Python is needed
$env:PATH = "$bin;$env:PATH"
if (-not (Have 'uv')) {
    Write-Host '  Getting uv (the Python package manager Vision uses)...'
    # In its own PowerShell, as uv's docs say: its installer refuses Windows' default script policy
    powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex" | Out-Null
    if ($LASTEXITCODE) { Die "Couldn't install uv. See https://docs.astral.sh/uv/ and run this again." }
    if (-not (Have 'uv')) { Die "uv installed, but it isn't in $bin. Open a new terminal and run this again." }
}
Ok (uv --version)

# the code: a git checkout when git is here (so an update is a pull), a tarball otherwise
New-Item -ItemType Directory -Force -Path $dest | Out-Null
$empty = -not (Get-ChildItem -Force $dest | Select-Object -First 1)
if ((Have 'git') -and (Test-Path (Join-Path $dest '.git'))) {
    git -C $dest pull --quiet --ff-only
    if ($LASTEXITCODE) { Die "Couldn't update $dest (local changes?). Fix it there, or remove it and run this again." }
} elseif ((Have 'git') -and $empty) {
    git clone --quiet --depth 1 --branch $branch $repo $dest
    if ($LASTEXITCODE) { Die "Couldn't download Vision from $repo." }
} else {
    $tmp = Join-Path ([IO.Path]::GetTempPath()) "vision-$PID.tar.gz"
    try { Invoke-WebRequest "$repo/archive/refs/heads/$branch.tar.gz" -OutFile $tmp -UseBasicParsing }
    catch { Die "Couldn't download Vision from $repo." }
    tar -xzf $tmp -C $dest --strip-components=1
    $code = $LASTEXITCODE
    Remove-Item -Force $tmp
    if ($code) { Die "Couldn't unpack Vision into $dest." }
}
Ok "Vision is in $dest"

# its environment: the serve extra too, as the desktop app talks to `vision serve`
Write-Host '  Setting up its Python environment...'
uv sync --project $dest --frozen --python 3.12 --extra serve --quiet
if ($LASTEXITCODE) { Die "Setup failed. The lines above say why; run this again once it's fixed." }

# the `vision` command; -P, as in bin/vision: never import a `vision` folder from the current directory
New-Item -ItemType Directory -Force -Path $bin | Out-Null
$python = Join-Path $dest '.venv\Scripts\python.exe'
Set-Content -Encoding Ascii -Path (Join-Path $bin 'vision.cmd') -Value "@`"$python`" -P -m vision %*"
Ok "vision command in $bin"

$note = ''
$userPath = [Environment]::GetEnvironmentVariable('PATH', 'User')
if (($userPath -split ';') -notcontains $bin) {
    if ($env:VISION_NO_MODIFY_PATH) {
        $note = "Add $bin to your PATH to run it."
    } else {
        [Environment]::SetEnvironmentVariable('PATH', "$bin;$userPath", 'User')
        $note = 'Open a new terminal first, so it finds the command.'
    }
}

if (-not (Have 'git')) { Write-Host '  Claude Code and Vision''s shell tool need Git for Windows: https://git-scm.com' -ForegroundColor Yellow }
Write-Host ''
Write-Host 'Done. Start it with:  vision'
if ($note) { Write-Host $note }
