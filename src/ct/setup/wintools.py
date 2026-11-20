"""Put `git` and `python` on a Windows computer, over SSH, with PowerShell and nothing else (scope: docs/design/WINDOWS-TOOLS-INSTALLER.md).

The PowerShell is built here as text and sent on the standard input of `powershell -Command -` (no `.ps1` file: the execution policy of a fresh Windows is Restricted, and no
quoting or command-line length limit of the default cmd shell applies). It prints one line, `CT-RESULT {json}` or `CT-ERROR {json}`, which `parse_result` reads.

  git     an existing `git` is used; else MinGit (the official minimal Git for Windows zip) is downloaded, its SHA-256 compared with the pinned one BEFORE anything is unpacked, and unpacked
          with `tar` into <tools>\\git. No installer, no administrator rights, nothing on the system PATH.
  python  an existing Python 3.11 or newer is used; else `winget` installs it for this user (winget checks the package's hash itself). python.org publishes only MD5 for the Windows
          embeddable zip, which cannot be a pin, so that route is not used; with no Python and no winget the error says what to do.
Standard library only.
"""
import json
import re

GIT = {"version": "2.56.1", "name": "MinGit-2.56.1-64-bit.zip", "size": 39849768,
       "url": "https://github.com/git-for-windows/git/releases/download/v2.56.1.windows.1/MinGit-2.56.1-64-bit.zip",
       "sha256": "4a92850b6e917da4fd010669b614b55dccf5b0ec333a534cc4be4700dfefdafd"}      # the digest GitHub states for that release asset
PYTHON_WINGET_ID = "Python.Python.3.13"
MIN_PYTHON = (3, 11)
DEFAULT_TOOLS = r"%ProgramData%\claude-tenta\tools"
POWERSHELL = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", "-"]       # the script itself arrives on standard input
TOOLS_RE = re.compile(r"^(%[A-Za-z]+%|[A-Za-z]:)[\\A-Za-z0-9 ._()-]*$")                                         # a folder: letters, digits, space . _ ( ) - and backslashes; no quote can get into the script


class WinToolsError(Exception):
    def __init__(self, what, detail):
        super().__init__(f"{what}: {detail}")
        self.what, self.detail = what, detail


def installer_script(tools_dir=DEFAULT_TOOLS):
    if not TOOLS_RE.match(tools_dir) or ".." in tools_dir:
        raise ValueError(f"not a folder this installer will write to: {tools_dir!r}")
    g = GIT
    return f"""$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$tools = [Environment]::ExpandEnvironmentVariables('{tools_dir}')
$changed = $false
function Fail($what, $detail) {{ Write-Output ('CT-ERROR ' + (@{{what = $what; detail = "$detail"}} | ConvertTo-Json -Compress)); exit 1 }}
try {{
  $py = $null; $pyv = ''
  foreach ($name in 'python', 'py') {{
    $c = Get-Command $name -ErrorAction SilentlyContinue
    if ($c -and $c.Source -notlike '*WindowsApps*') {{
      $v = (& $c.Source --version 2>&1) -join ''
      if ($v -match 'Python (\\d+)\\.(\\d+)' -and ([int]$Matches[1] -gt {MIN_PYTHON[0]} -or ([int]$Matches[1] -eq {MIN_PYTHON[0]} -and [int]$Matches[2] -ge {MIN_PYTHON[1]}))) {{ $py = $c.Source; $pyv = $v.Trim(); break }}
    }}
  }}
  if (-not $py) {{
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {{ Fail 'python' 'there is no Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer, and winget is not available to install one: install Python from python.org, then run this again' }}
    & winget install --id {PYTHON_WINGET_ID} --scope user --silent --accept-package-agreements --accept-source-agreements | Out-Null
    if ($LASTEXITCODE -ne 0) {{ Fail 'python' "winget exited with $LASTEXITCODE" }}
    $py = Join-Path $env:LOCALAPPDATA 'Programs\\Python\\Python313\\python.exe'
    if (-not (Test-Path $py)) {{ Fail 'python' 'winget finished, but python.exe is not where it was expected' }}
    $pyv = ((& $py --version 2>&1) -join '').Trim()
    $changed = $true
  }}
  $git = $null
  $mingit = Join-Path $tools 'git\\cmd\\git.exe'
  $existing = Get-Command git -ErrorAction SilentlyContinue
  if ($existing) {{ $git = $existing.Source }}
  elseif ((Test-Path $mingit) -and (((& $mingit --version 2>&1) -join '') -like '*{g["version"]}*')) {{ $git = $mingit }}
  if (-not $git) {{
    New-Item -ItemType Directory -Force -Path $tools | Out-Null
    $zip = Join-Path $tools '{g["name"]}'
    & curl.exe -L --fail --silent --show-error -o $zip '{g["url"]}'
    if ($LASTEXITCODE -ne 0) {{ Fail 'git' "the download failed (curl exit $LASTEXITCODE)" }}
    $got = (Get-FileHash -Algorithm SHA256 $zip).Hash.ToLower()
    if ($got -ne '{g["sha256"]}') {{ Remove-Item $zip -Force; Fail 'git' "the download's SHA-256 is $got but {g["sha256"]} was expected: refused, nothing was unpacked" }}
    $dest = Join-Path $tools 'git'
    if (Test-Path $dest) {{ Remove-Item -Recurse -Force $dest }}
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    & tar.exe -xf $zip -C $dest
    if ($LASTEXITCODE -ne 0) {{ Fail 'git' "tar exited with $LASTEXITCODE" }}
    Remove-Item $zip -Force
    if (-not (Test-Path $mingit)) {{ Fail 'git' 'unpacked, but git.exe is not at git\\cmd\\git.exe' }}
    $git = $mingit
    $changed = $true
  }}
  $gitv = ((& $git --version 2>&1) -join '').Trim()
  Write-Output ('CT-RESULT ' + (@{{git = $gitv; git_path = $git; python = $pyv; python_path = $py; changed = $changed}} | ConvertTo-Json -Compress))
}} catch {{ Fail 'script' $_.Exception.Message }}

"""   # the blank line at the end matters: PowerShell reading a script from standard input does not run the last block until it sees one (found on the real Windows 11 VM)


def parse_result(text):
    """The dict the script printed (git, git_path, python, python_path, changed), or WinToolsError carrying what failed and PowerShell's own words."""
    for line in str(text or "").splitlines():
        line = line.strip()
        if line.startswith("CT-RESULT "):
            return json.loads(line[len("CT-RESULT "):])
        if line.startswith("CT-ERROR "):
            e = json.loads(line[len("CT-ERROR "):])
            raise WinToolsError(e.get("what", "script"), e.get("detail", ""))
    raise WinToolsError("script", "no result line came back; PowerShell said: " + (str(text or "").strip()[-400:] or "nothing"))


def install(runner, tools_dir=DEFAULT_TOOLS):
    """`runner(argv, input_text) -> (exit code, output)` runs a command on the Windows computer. Returns what parse_result returns."""
    rc, out = runner(list(POWERSHELL), installer_script(tools_dir))
    return parse_result(out)
