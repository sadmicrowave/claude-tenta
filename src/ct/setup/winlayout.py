"""The folders and the unprivileged account of a Windows worker, over SSH with PowerShell (scope: docs/design/WINDOWS-WORKER-LAYOUT.md).

`ensure` creates them (idempotent, reports `changed`), `check` only reads, and `ensure(..., probe=True)` also proves the rules LIVE: it gives the account a throwaway password, runs a few commands AS that account and
reports what they could and could not do, then sets another throwaway password, so no password is kept anywhere. Same transport as wintools: the script on standard input, one `CT-RESULT {json}` or `CT-ERROR {json}` line."""
from .. import refusals
import re

from .wintools import POWERSHELL, TOOLS_RE, WinToolsError, parse_result

ROOT = r"%ProgramData%\claude-tenta"
USER = "ctworker"
USER_RE = re.compile(r"^[a-z][a-z0-9-]{2,19}$")                    # a Windows local account name is at most 20 characters
READ_ONLY = ("tools", "release", "config")                         # the worker account may read and run these
WRITABLE = ("state", r"state\jobs")                                # and may change these


def _check_args(root, user):
    if not TOOLS_RE.match(root) or ".." in root:
        raise refusals.refuse("win.arg.folder", value=root)
    if not USER_RE.match(user):
        raise refusals.refuse("win.arg.account", value=user)


_PRE = """$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
function Fail($what, $detail) {{ Write-Output ('CT-ERROR ' + (@{{what = $what; detail = "$detail"}} | ConvertTo-Json -Compress)); exit 1 }}
$root = [Environment]::ExpandEnvironmentVariables('{root}')
$u = '{user}'
function Sddl($p) {{ (Get-Acl -LiteralPath $p).Sddl }}
function Rights($sid, $allow, $deny) {{
  # secedit: read the rights, add this account's SID where it is missing, write back only the rights that change. Returns $true when something changed.
  $cfg = Join-Path $env:TEMP 'ct-rights.inf'; $db = Join-Path $env:TEMP 'ct-rights.sdb'
  & secedit.exe /export /cfg $cfg /areas USER_RIGHTS | Out-Null
  $text = Get-Content -LiteralPath $cfg
  $want = @(); $out = @('[Unicode]', 'Unicode=yes', '[Version]', 'signature="$CHICAGO$"', 'Revision=1', '[Privilege Rights]')
  foreach ($r in @($allow + $deny)) {{
    $line = $text | Where-Object {{ $_ -match "^$r\\s*=" }} | Select-Object -First 1
    $have = if ($line) {{ ($line -split '=', 2)[1].Trim() }} else {{ '' }}
    $names = ($have -split ',') | ForEach-Object {{ $_.Trim().TrimStart('*') -replace '^.*\\\\', '' }}       # secedit writes a local account by NAME and others by *SID
    if (($names -notcontains $sid) -and ($names -notcontains $u)) {{ $out += "$r = " + ($(if ($have) {{ $have + ',' }} else {{ '' }}) + "*$sid"); $want += $r }}
  }}
  Remove-Item -LiteralPath $cfg -Force -ErrorAction SilentlyContinue
  if (-not $want) {{ return $false }}
  Set-Content -LiteralPath $cfg -Value $out -Encoding Unicode
  & secedit.exe /configure /db $db /cfg $cfg /areas USER_RIGHTS | Out-Null
  $rc = $LASTEXITCODE
  Remove-Item -LiteralPath $cfg, $db -Force -ErrorAction SilentlyContinue
  if ($rc -ne 0) {{ Fail 'rights' "secedit exited with $rc while setting $($want -join ', ')" }}
  return $true
}}
function RandomPassword {{ $b = New-Object byte[] 24; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); 'Aa1!' + [Convert]::ToBase64String($b) }}
"""


def ensure_script(root=ROOT, user=USER, probe=False):
    _check_args(root, user)
    reads = "', '".join(READ_ONLY)
    writes = "', '".join(WRITABLE)
    return _PRE.format(root=root, user=user) + f"""$changed = $false
try {{
  $before = @()
  foreach ($d in @('{reads}', '{writes}')) {{ $p = Join-Path $root $d; if (Test-Path $p) {{ $before += (Sddl $p) }} else {{ $before += '' }} }}
  foreach ($d in @('{reads}', '{writes}')) {{
    $p = Join-Path $root $d
    if (-not (Test-Path $p)) {{ New-Item -ItemType Directory -Force -Path $p | Out-Null; $changed = $true }}
  }}
  $created = $false
  if (-not (Get-LocalUser -Name $u -ErrorAction SilentlyContinue)) {{
    New-LocalUser -Name $u -Password (ConvertTo-SecureString (RandomPassword) -AsPlainText -Force) -PasswordNeverExpires -UserMayNotChangePassword -AccountNeverExpires -Description 'claude-tenta worker (standard account)' | Out-Null
    $created = $true; $changed = $true
  }}
  $admin = Get-LocalGroupMember -SID 'S-1-5-32-544' -ErrorAction SilentlyContinue | Where-Object {{ $_.Name -like "*\\$u" }}
  if ($admin) {{ Remove-LocalGroupMember -SID 'S-1-5-32-544' -Member $u; $changed = $true }}
  $sid = (Get-LocalUser -Name $u).SID.Value
  if (Rights $sid @('SeBatchLogonRight') @('SeDenyInteractiveLogonRight', 'SeDenyNetworkLogonRight', 'SeDenyRemoteInteractiveLogonRight')) {{ $changed = $true }}       # it may run as a task, and nobody can log in as it
  $rootBefore = Sddl $root
  & icacls.exe $root /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' "${{u}}:(RX)" | Out-Null
  if ($LASTEXITCODE -ne 0) {{ Fail 'acl' "icacls failed on $root (exit $LASTEXITCODE)" }}
  foreach ($d in @('{reads}')) {{ & icacls.exe (Join-Path $root $d) /grant:r "${{u}}:(OI)(CI)RX" | Out-Null; if ($LASTEXITCODE -ne 0) {{ Fail 'acl' "icacls failed on $d (exit $LASTEXITCODE)" }} }}
  foreach ($d in @('{writes}')) {{ & icacls.exe (Join-Path $root $d) /grant:r "${{u}}:(OI)(CI)M" | Out-Null; if ($LASTEXITCODE -ne 0) {{ Fail 'acl' "icacls failed on $d (exit $LASTEXITCODE)" }} }}
  $after = @()
  foreach ($d in @('{reads}', '{writes}')) {{ $after += (Sddl (Join-Path $root $d)) }}
  if ((Sddl $root) -ne $rootBefore -or (($before -join '|') -ne ($after -join '|'))) {{ $changed = $true }}
{_probe_block(probe)}
  Write-Output ('CT-RESULT ' + (@{{root = $root; user = $u; created_user = $created; changed = $changed; probe = $probe}} | ConvertTo-Json -Compress -Depth 4))
}} catch {{ Fail 'script' $_.Exception.Message }}

"""


def _probe_block(probe):
    if not probe:
        return "  $probe = $null"
    return r"""  if (Get-ScheduledTask -TaskName 'claude-tenta-worker' -ErrorAction SilentlyContinue) { Fail 'probe' 'the worker task is registered with the account password and a probe would invalidate it: remove the task first' }
  $pw = RandomPassword
  Set-LocalUser -Name $u -Password (ConvertTo-SecureString $pw -AsPlainText -Force)
  $cred = New-Object System.Management.Automation.PSCredential("$env:COMPUTERNAME\$u", (ConvertTo-SecureString $pw -AsPlainText -Force))
  $out = Join-Path $root 'state\jobs\probe.txt'
  $probeScript = @"
`$r = [ordered]@{}
function T(`$k, `$sb) { try { & `$sb; `$r[`$k] = `$true } catch { `$r[`$k] = `$false } }
T 'reads_tools' { Get-ChildItem '$root\tools' | Out-Null }
T 'reads_release' { Get-ChildItem '$root\release' | Out-Null }
T 'writes_jobs' { Set-Content -Path '$root\state\jobs\w.txt' -Value x -ErrorAction Stop; Remove-Item '$root\state\jobs\w.txt' }
T 'writes_release' { Set-Content -Path '$root\release\w.txt' -Value x -ErrorAction Stop; Remove-Item '$root\release\w.txt' }
T 'writes_config' { Set-Content -Path '$root\config\w.txt' -Value x -ErrorAction Stop; Remove-Item '$root\config\w.txt' }
T 'writes_tools' { Set-Content -Path '$root\tools\w.txt' -Value x -ErrorAction Stop; Remove-Item '$root\tools\w.txt' }
`$r['is_admin'] = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
`$r | ConvertTo-Json -Compress | Set-Content -Path '$out'
"@
  $enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($probeScript))
  $tn = 'claude-tenta-probe'                                                                  # a one-time scheduled task as the account: the same way the worker will be started, and it needs no desktop
  try {
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -NonInteractive -EncodedCommand ' + $enc)
    Register-ScheduledTask -TaskName $tn -Action $action -User "$env:COMPUTERNAME\$u" -Password $pw -RunLevel Limited -Force | Out-Null
    Start-ScheduledTask -TaskName $tn
    $t0 = Get-Date
    do {
      Start-Sleep -Milliseconds 500
      $state = (Get-ScheduledTask -TaskName $tn).State
      $last = (Get-ScheduledTaskInfo -TaskName $tn).LastTaskResult
    } while (($state -ne 'Ready' -or $last -eq 267011 -or $last -eq 267009) -and ((Get-Date) - $t0).TotalSeconds -lt 90)       # 267011: has not run yet; 267009: running
    $last = (Get-ScheduledTaskInfo -TaskName $tn).LastTaskResult
  } catch { Set-LocalUser -Name $u -Password (ConvertTo-SecureString (RandomPassword) -AsPlainText -Force); Unregister-ScheduledTask -TaskName $tn -Confirm:$false -ErrorAction SilentlyContinue; Fail 'probe' ('could not run a task as ' + $u + ': ' + $_.Exception.Message) }
  Unregister-ScheduledTask -TaskName $tn -Confirm:$false -ErrorAction SilentlyContinue
  Set-LocalUser -Name $u -Password (ConvertTo-SecureString (RandomPassword) -AsPlainText -Force)          # no password is kept: this one is already gone
  if (-not (Test-Path $out)) { Fail 'probe' ('the task run as the worker account left no result (task result ' + $last + ')') }
  $probe = Get-Content $out -Raw | ConvertFrom-Json
  Remove-Item $out -Force"""


def check_script(root=ROOT, user=USER):
    _check_args(root, user)
    reads = "', '".join(READ_ONLY)
    writes = "', '".join(WRITABLE)
    return _PRE.format(root=root, user=user) + f"""try {{
  $acct = Get-LocalUser -Name $u -ErrorAction SilentlyContinue
  $admins = @(Get-LocalGroupMember -SID 'S-1-5-32-544' -ErrorAction SilentlyContinue | ForEach-Object {{ $_.Name }})
  $dirs = [ordered]@{{}}
  foreach ($d in @('{reads}', '{writes}')) {{ $dirs[$d] = (Test-Path (Join-Path $root $d)) }}
  Write-Output ('CT-RESULT ' + (@{{root = $root; user = $u; user_exists = [bool]$acct; enabled = [bool]($acct -and $acct.Enabled); is_admin = [bool]($admins -like "*\\$u"); dirs = $dirs}} | ConvertTo-Json -Compress -Depth 4))
}} catch {{ Fail 'script' $_.Exception.Message }}

"""


def problems(result, probe=None):
    """What is wrong with a layout, as plain sentences (empty when it is right). `result` is what check/ensure returned; `probe` the live probe's answers when there are any."""
    bad = []
    if "user_exists" in result:
        if not result["user_exists"]:
            bad.append(f"the account {result['user']} does not exist")
        elif not result["enabled"]:
            bad.append(f"the account {result['user']} is disabled")
        if result.get("is_admin"):
            bad.append(f"the account {result['user']} is an administrator; it must not be")
        bad += [f"the folder {d} is missing" for d, there in (result.get("dirs") or {}).items() if not there]
    if probe:
        want = {"reads_tools": True, "reads_release": True, "writes_jobs": True, "writes_release": False, "writes_config": False, "writes_tools": False, "is_admin": False}
        bad += [f"as the worker account, {k.replace('_', ' ')} is {probe.get(k)} but must be {v}" for k, v in want.items() if probe.get(k) is not v]
    return bad


def ensure(runner, root=ROOT, user=USER, probe=False):
    rc, out = runner(list(POWERSHELL), ensure_script(root, user, probe))
    return parse_result(out)


def check(runner, root=ROOT, user=USER):
    rc, out = runner(list(POWERSHELL), check_script(root, user))
    return parse_result(out)
