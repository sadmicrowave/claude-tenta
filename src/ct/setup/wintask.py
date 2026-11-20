"""Start the worker on a Windows computer the way systemd does on Linux: a Scheduled Task, at startup, as the unprivileged worker account, restarted if it dies (scope: docs/design/WINDOWS-WORKER-TASK.md).

`ensure` registers it (idempotent: a hash of the definition is kept in the task's description, and a matching task is left alone, so the account's password is not touched again), `start`, `stop`, `status` and
`remove` do what they say. The password is set at registration and not kept; Task Scheduler holds it, so the account's password must not be changed afterwards (the layout's live probe refuses to run while
this task exists). Same transport as wintools: the script on standard input, one `CT-RESULT {json}` or `CT-ERROR {json}` line."""
import re

from .winlayout import ROOT, USER, _check_args
from .wintools import POWERSHELL, parse_result

TASK = "claude-tenta-worker"
DEFAULT_ARGS = "-m ct.cli run"
TASK_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]{2,40}$")
ARGS_RE = re.compile(r'^[A-Za-z0-9 _.:=,;()"-]{1,200}$')          # no single quote, no & | < > ^ % or backslash: nothing that cmd or PowerShell would read as more than text


def _args(root, user, task, args):
    _check_args(root, user)
    if not TASK_RE.match(task):
        raise ValueError(f"not a task name this will register: {task!r}")
    if not ARGS_RE.match(args):
        raise ValueError(f"not arguments this will run: {args!r}")


_PRE = """$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
function Fail($what, $detail) {{ Write-Output ('CT-ERROR ' + (@{{what = $what; detail = "$detail"}} | ConvertTo-Json -Compress)); exit 1 }}
$root = [Environment]::ExpandEnvironmentVariables('{root}')
$u = '{user}'
$tn = '{task}'
function RandomPassword {{ $b = New-Object byte[] 24; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); 'Aa1!' + [Convert]::ToBase64String($b) }}
function Procs {{
  # the processes of this worker (their command lines name this root), with the account each one runs as, read from the system
  Get-CimInstance Win32_Process | Where-Object {{ $_.CommandLine -and $_.CommandLine -like "*$root*" -and $_.ProcessId -ne $PID }} | ForEach-Object {{
    $o = Invoke-CimMethod -InputObject $_ -MethodName GetOwner
    @{{name = $_.Name; pid = [int]$_.ProcessId; owner = ("$($o.Domain)\\$($o.User)")}}
  }}
}}
"""


def _script(root, user, task, body, args=DEFAULT_ARGS):
    _args(root, user, task, args)
    return _PRE.format(root=root, user=user, task=task) + "try {\n" + body.replace("@ARGS@", args) + "\n} catch { Fail 'script' $_.Exception.Message }\n\n"


def ensure_script(root=ROOT, user=USER, task=TASK, args=DEFAULT_ARGS):
    return _script(root, user, task, r"""  $py = Join-Path $root 'tools\python\python.exe'
  if (-not (Test-Path $py)) { Fail 'python' "there is no Python at ${py}: run the tools installer first (ct-setup add-worker)" }
  if (-not (Get-LocalUser -Name $u -ErrorAction SilentlyContinue)) { Fail 'account' "there is no account ${u}: run the layout step first" }
  $log = Join-Path $root 'state\worker.log'
  $inner = 'set "PATH=' + (Join-Path $root 'tools\git\cmd') + ';%PATH%" && set "PYTHONPATH=' + (Join-Path $root 'release\src') + '" && set "CT_CONFIG=' + (Join-Path $root 'config\ct.toml') + '" && "' + $py + '" @ARGS@ >> "' + $log + '" 2>&1'
  $arg = '/d /c "' + $inner + '"'
  $hash = [BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($arg + '|' + $u + '|restart1m|nolimit|startup'))).Replace('-', '').ToLower().Substring(0, 16)
  $existing = Get-ScheduledTask -TaskName $tn -ErrorAction SilentlyContinue
  if ($existing -and $existing.Description -like "*definition $hash*") {
    Write-Output ('CT-RESULT ' + (@{task = $tn; user = $u; changed = $false; definition = $hash} | ConvertTo-Json -Compress)); return
  }
  $action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument $arg -WorkingDirectory (Join-Path $root 'release')
  $trigger = New-ScheduledTaskTrigger -AtStartup
  $settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Hidden
  $pw = RandomPassword                                                                           # set now, not kept: Task Scheduler holds it, so the account's password must not be changed afterwards
  Set-LocalUser -Name $u -Password (ConvertTo-SecureString $pw -AsPlainText -Force)
  Register-ScheduledTask -TaskName $tn -Action $action -Trigger $trigger -Settings $settings -User "$env:COMPUTERNAME\$u" -Password $pw -RunLevel Limited -Description "claude-tenta worker; definition $hash" -Force | Out-Null
  Write-Output ('CT-RESULT ' + (@{task = $tn; user = $u; changed = $true; definition = $hash} | ConvertTo-Json -Compress))""", args)


def start_script(root=ROOT, user=USER, task=TASK):
    return _script(root, user, task, r"""  if (-not (Get-ScheduledTask -TaskName $tn -ErrorAction SilentlyContinue)) { Fail 'task' "the task ${tn} is not registered: register it first" }
  Start-ScheduledTask -TaskName $tn
  Write-Output ('CT-RESULT ' + (@{task = $tn; started = $true} | ConvertTo-Json -Compress))""")


def stop_script(root=ROOT, user=USER, task=TASK):
    return _script(root, user, task, r"""  if (Get-ScheduledTask -TaskName $tn -ErrorAction SilentlyContinue) { Stop-ScheduledTask -TaskName $tn }
  Start-Sleep -Seconds 2
  Write-Output ('CT-RESULT ' + (@{task = $tn; processes = @(Procs)} | ConvertTo-Json -Compress -Depth 4))""")


def status_script(root=ROOT, user=USER, task=TASK):
    return _script(root, user, task, r"""  $t = Get-ScheduledTask -TaskName $tn -ErrorAction SilentlyContinue
  if (-not $t) { Write-Output ('CT-RESULT ' + (@{task = $tn; exists = $false; processes = @(Procs)} | ConvertTo-Json -Compress -Depth 4)); return }
  $i = Get-ScheduledTaskInfo -TaskName $tn
  $trig = @($t.Triggers | ForEach-Object { $_.CimClass.CimClassName })
  Write-Output ('CT-RESULT ' + (@{task = $tn; exists = $true; state = [string]$t.State; user = $t.Principal.UserId; logon = [string]$t.Principal.LogonType; triggers = $trig
    restart_count = [int]$t.Settings.RestartCount; restart_interval = [string]$t.Settings.RestartInterval; time_limit = [string]$t.Settings.ExecutionTimeLimit; instances = [string]$t.Settings.MultipleInstances
    last_result = [int]$i.LastTaskResult; processes = @(Procs)} | ConvertTo-Json -Compress -Depth 4))""")


def remove_script(root=ROOT, user=USER, task=TASK):
    return _script(root, user, task, r"""  $was = [bool](Get-ScheduledTask -TaskName $tn -ErrorAction SilentlyContinue)
  if ($was) { Stop-ScheduledTask -TaskName $tn -ErrorAction SilentlyContinue; Unregister-ScheduledTask -TaskName $tn -Confirm:$false }
  Start-Sleep -Seconds 1
  Write-Output ('CT-RESULT ' + (@{task = $tn; removed = $was; processes = @(Procs)} | ConvertTo-Json -Compress -Depth 4))""")


def _run(runner, script):
    rc, out = runner(list(POWERSHELL), script)
    return parse_result(out)


def ensure(runner, root=ROOT, user=USER, task=TASK, args=DEFAULT_ARGS):
    return _run(runner, ensure_script(root, user, task, args))


def start(runner, root=ROOT, user=USER, task=TASK):
    return _run(runner, start_script(root, user, task))


def stop(runner, root=ROOT, user=USER, task=TASK):
    return _run(runner, stop_script(root, user, task))


def status(runner, root=ROOT, user=USER, task=TASK):
    return _run(runner, status_script(root, user, task))


def remove(runner, root=ROOT, user=USER, task=TASK):
    return _run(runner, remove_script(root, user, task))


def problems(st, user=USER):
    """What is wrong with a task, from `status`, as plain sentences (empty when it is right)."""
    if not st.get("exists"):
        return ["the worker task is not registered"]
    bad = []
    if not str(st.get("user", "")).lower().endswith(user.lower()):
        bad.append(f"the task runs as {st.get('user')}, not {user}")
    if "StartupTrigger" not in " ".join(st.get("triggers") or []) and "BootTrigger" not in " ".join(st.get("triggers") or []):
        bad.append("the task has no start-at-boot trigger")
    if st.get("restart_count", 0) < 1:
        bad.append("the task does not restart the worker when it dies")
    if not str(st.get("time_limit", "")).startswith(("PT0S", "P0D")) and st.get("time_limit") not in (None, "PT0S"):
        bad.append(f"the task stops the worker after {st.get('time_limit')}")
    bad += [f"{p['name']} (process {p['pid']}) runs as {p['owner']}, not {user}" for p in st.get("processes") or [] if not str(p["owner"]).lower().endswith(user.lower())]
    return bad
