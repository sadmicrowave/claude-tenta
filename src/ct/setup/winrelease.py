"""Put the source of a Windows release on a Windows computer, over SSH, with PowerShell and the git the tools installer put there (scope: docs/design/WINDOWS-RELEASE-FETCH.md).

The release is the commit a `release-win-X.Y.Z` tag points to, found on THIS computer with `git ls-remote` (updatecheck.check_now(..., platform="windows")). The Windows computer fetches exactly that
commit from the release source into its own folder and the script compares the commit it ended up with to the one it was told: any difference is refused. Nothing in it is run here.
Same transport as wintools: the script on standard input, one `CT-RESULT {json}` or `CT-ERROR {json}` line. Standard library only.
"""
import re

from .wintools import DEFAULT_TOOLS, POWERSHELL, TOOLS_RE, WinToolsError, parse_result

DEFAULT_RELEASE = r"%ProgramData%\claude-tenta\release"
URL_RE = re.compile(r"^https://[A-Za-z0-9._~:/@%+=-]+$")                 # no quote, space or shell character can get into the script
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def fetch_script(url, commit, dest=DEFAULT_RELEASE, tools_dir=DEFAULT_TOOLS):
    if not URL_RE.match(url or ""):
        raise ValueError(f"not an https release source this will fetch from: {url!r}")
    if not SHA_RE.match(commit or ""):
        raise ValueError(f"not a full commit id: {commit!r}")
    for d in (dest, tools_dir):
        if not TOOLS_RE.match(d) or ".." in d:
            raise ValueError(f"not a folder this will write to: {d!r}")
    return f"""$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
function Fail($what, $detail) {{ Write-Output ('CT-ERROR ' + (@{{what = $what; detail = [string]$detail}} | ConvertTo-Json -Compress)); exit 1 }}
$dest = [Environment]::ExpandEnvironmentVariables('{dest}')
$tools = [Environment]::ExpandEnvironmentVariables('{tools_dir}')
try {{
  $git = $null
  $mingit = Join-Path $tools 'git\\cmd\\git.exe'
  $existing = Get-Command git -ErrorAction SilentlyContinue
  if ($existing) {{ $git = $existing.Source }} elseif (Test-Path $mingit) {{ $git = $mingit }}
  if (-not $git) {{ Fail 'git' 'there is no git on this computer: run the tools installer first (ct-setup add-worker)' }}
  $ErrorActionPreference = 'Continue'                                                     # git writes progress to stderr; PowerShell 5.1 would stop on it
  $changed = $false
  $have = ''
  if (Test-Path (Join-Path $dest '.git')) {{ $have = ((& $git -C $dest rev-parse HEAD 2>$null) -join '').Trim() }}
  if ($have -ne '{commit}') {{
    $new = $dest + '.new'                                                                 # built beside the old one: a failed fetch leaves the release that works
    if (Test-Path $new) {{ Remove-Item -Recurse -Force $new }}
    New-Item -ItemType Directory -Force -Path $new | Out-Null
    $out = (& $git -C $new init -q 2>&1) -join ' '
    if ($LASTEXITCODE -ne 0) {{ Fail 'release' "git init failed: $out" }}
    $out = (& $git -C $new fetch -q --depth 1 '{url}' '{commit}' 2>&1) -join ' '
    if ($LASTEXITCODE -ne 0) {{ Remove-Item -Recurse -Force $new; Fail 'release' "the fetch failed: $out" }}
    $out = (& $git -C $new checkout -q FETCH_HEAD 2>&1) -join ' '
    if ($LASTEXITCODE -ne 0) {{ Remove-Item -Recurse -Force $new; Fail 'release' "the checkout failed: $out" }}
    $have = ((& $git -C $new rev-parse HEAD 2>$null) -join '').Trim()
    if ($have -ne '{commit}') {{ Remove-Item -Recurse -Force $new; Fail 'release' "the commit fetched is $have but {commit} was expected: refused, nothing is kept" }}
    if (Test-Path $dest) {{ Remove-Item -Recurse -Force $dest }}
    Move-Item $new $dest
    $changed = $true
  }}
  Write-Output ('CT-RESULT ' + (@{{commit = $have; path = $dest; changed = $changed}} | ConvertTo-Json -Compress))
}} catch {{ Fail 'script' $_.Exception.Message }}

"""


def fetch(runner, url, commit, dest=DEFAULT_RELEASE, tools_dir=DEFAULT_TOOLS):
    """`runner(argv, input_text) -> (exit code, output)`. Returns {commit, path, changed} or raises WinToolsError with what failed and git's own words."""
    rc, out = runner(list(POWERSHELL), fetch_script(url, commit, dest, tools_dir))
    return parse_result(out)
