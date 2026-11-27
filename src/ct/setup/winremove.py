"""Take a Windows worker off its computer: the last three steps of the removal (scope: docs/design/WINDOWS-UNINSTALL.md). The start task is `wintask.remove` and the mailbox account is `winmailbox.remove`; this is
what is left: the Python the installer put under the worker's own tools folder (through its own uninstaller, so no registration is left behind to make a later install fail with error 1601), the rights the
setup granted and denied, the worker account and its profile, and the folders. Windows itself, OpenSSH and the administrator account are never touched. `keep_data` leaves the state folders (the clones of the
two repositories and the job history), as the Linux removal does with --keep-data. Same transport as wintools: the script on standard input, one `CT-RESULT {json}` or `CT-ERROR {json}` line."""
from .winlayout import ROOT, USER, _check_args
from .wintools import POWERSHELL, PY_REGISTRATION, parse_result

KEEP = ("state", "state-mail")                              # what --keep-data leaves in place
RIGHTS = ("SeBatchLogonRight", "SeDenyInteractiveLogonRight", "SeDenyRemoteInteractiveLogonRight", "SeDenyNetworkLogonRight")      # the rights the setup changed, and nothing else


def remove_script(root=ROOT, user=USER, keep_data=False):
    _check_args(root, user)
    keep = "', '".join(KEEP)
    rights = "', '".join(RIGHTS)
    return f"""$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
function Fail($what, $detail) {{ Write-Output ('CT-ERROR ' + (@{{what = $what; detail = "$detail"}} | ConvertTo-Json -Compress)); exit 1 }}
$root = [Environment]::ExpandEnvironmentVariables('{root}')
$u = '{user}'
$keepData = ${'true' if keep_data else 'false'}
{PY_REGISTRATION}
$done = [ordered]@{{python = 'absent'; rights = 0; profile = $false; profile_left = ''; account = $false; folders = @(); kept = @()}}
try {{
  $task = Get-ScheduledTask -TaskName 'armada-worker' -ErrorAction SilentlyContinue
  if ($task) {{ Fail 'task' 'the worker task still exists; remove it first' }}
  # 1. The Python the setup installed under the worker's tools folder: its own uninstaller, then the folder goes with the rest.
  $py = Join-Path $root 'tools\\python'
  if (Test-Path -LiteralPath $py) {{ $done.python = 'folder only' }}
  foreach ($un in (OurPythonUninstallers $py)) {{                                          # also when the folder is already gone: a registration with no files makes a later install do nothing
    if (Test-Path -LiteralPath $un) {{ $pr = Start-Process -FilePath $un -ArgumentList '/uninstall', '/quiet' -Wait -PassThru; $done.python = "uninstalled (exit $($pr.ExitCode))" }}
  }}
  # 2. The rights the setup set for the worker and the mailbox account: take out the entries whose account no longer exists (this account too, once it is gone).
  $acct = Get-LocalUser -Name $u -ErrorAction SilentlyContinue
  $sid = if ($acct) {{ $acct.SID.Value }} else {{ '' }}
  # The profile can stay loaded for a few seconds after the start task is gone (Windows ends the logon session late), so removing it is retried; the account is removed either way, and a profile that is still
  # in use is reported, not an error: the same command run again removes it (it is found by its folder, not by the account).
  $deadline = (Get-Date).AddSeconds(40)
  while ($true) {{
    $prof = @(Get-CimInstance Win32_UserProfile | Where-Object {{ $_.LocalPath -like "*\\$u" -or ($sid -and $_.SID -eq $sid) }})
    if (-not $prof) {{ break }}
    try {{ $prof | Remove-CimInstance; $done.profile = $true; break }} catch {{ if ((Get-Date) -gt $deadline) {{ $done.profile_left = $prof[0].LocalPath; break }}; Start-Sleep -Seconds 3 }}
  }}
  if ($acct) {{ Remove-LocalUser -Name $u; $done.account = $true }}
  $cfg = Join-Path $env:TEMP 'ct-rights-out.inf'; $db = Join-Path $env:TEMP 'ct-rights-out.sdb'
  & secedit.exe /export /cfg $cfg /areas USER_RIGHTS | Out-Null
  $text = Get-Content -LiteralPath $cfg
  $out = @('[Unicode]', 'Unicode=yes', '[Version]', 'signature="$CHICAGO$"', 'Revision=1', '[Privilege Rights]')
  foreach ($r in @('{rights}')) {{
    $line = $text | Where-Object {{ $_ -match "^$r\\s*=" }} | Select-Object -First 1
    if (-not $line) {{ continue }}
    $have = (($line -split '=', 2)[1].Trim() -split ',') | ForEach-Object {{ $_.Trim() }} | Where-Object {{ $_ }}
    $keepers = @($have | Where-Object {{
      $e = $_.TrimStart('*')
      if ($e -eq $u) {{ $false }}
      elseif ($e -match '^S-1-5-21-') {{ try {{ ([Security.Principal.SecurityIdentifier]$e).Translate([Security.Principal.NTAccount]) | Out-Null; $true }} catch {{ $false }} }}
      else {{ $true }}
    }})
    if ($keepers.Count -ne @($have).Count) {{ $out += "$r = " + ($keepers -join ','); $done.rights++ }}
  }}
  Remove-Item -LiteralPath $cfg -Force -ErrorAction SilentlyContinue
  if ($done.rights -gt 0) {{
    $inf = Join-Path $env:TEMP 'ct-rights-out2.inf'
    Set-Content -LiteralPath $inf -Value $out -Encoding Unicode
    & secedit.exe /configure /db $db /cfg $inf /areas USER_RIGHTS | Out-Null
    $rc = $LASTEXITCODE
    Remove-Item -LiteralPath $inf, $db -Force -ErrorAction SilentlyContinue
    if ($rc -ne 0) {{ Fail 'rights' "secedit exited with $rc" }}
  }}
  # 3. The folders.
  if (Test-Path -LiteralPath $root) {{
    $left = @('{keep}')
    foreach ($item in Get-ChildItem -LiteralPath $root -Force) {{
      if ($keepData -and $left -contains $item.Name) {{ $done.kept += $item.Name; continue }}
      Remove-Item -LiteralPath $item.FullName -Recurse -Force
      $done.folders += $item.Name
    }}
    if (-not $done.kept.Count) {{ Remove-Item -LiteralPath $root -Force }}
  }}
  Write-Output ('CT-RESULT ' + ($done | ConvertTo-Json -Compress -Depth 4))
}} catch {{ Fail 'script' $_.Exception.Message }}

"""


def remove(runner, root=ROOT, user=USER, keep_data=False):
    rc, out = runner(list(POWERSHELL), remove_script(root, user, keep_data))
    return parse_result(out)
