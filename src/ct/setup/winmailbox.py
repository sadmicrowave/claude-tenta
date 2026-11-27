"""The Direct transport's mailbox on a Windows worker, over SSH with PowerShell (scope: docs/design/WINDOWS-WORKER-LOOP.md, slice 4d-2).

`ensure` sets up, idempotently and reporting `changed`: a restricted local account (`<name>-mail`, a standard user that cannot log on interactively), the two bare repositories under `<root>\\state-mail`
with the access each side needs (the mail account writes the queue and reads the results; the worker account reads the queue and writes the results), a launcher file that is the only thing the account's key
may run (it puts the tools' git first on a PATH of its own and starts `ct.mailshell`), the account's key file, and a `Match User` block in sshd_config that points OpenSSH at that key file (a Windows account
that has never logged in has no profile to keep a key in). The sshd_config edit is bracketed by marker lines, kept in a backup, checked with `sshd -t` before it is used, and put back if the check fails.
`remove` undoes all of it. Same transport as wintools: the script on standard input, one `CT-RESULT {json}` or `CT-ERROR {json}` line."""
from .. import refusals
import re

from .winlayout import ROOT, USER, USER_RE, _PRE as _LAYOUT_PRE, _check_args
from .wintools import POWERSHELL, parse_result

KEY_RE = re.compile(r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256) [A-Za-z0-9+/=]{20,800}( [A-Za-z0-9@._-]{1,60})?$")      # one public key line, nothing the authorized_keys options could be hidden in
BEGIN, END = "# BEGIN armada mailbox", "# END armada mailbox"


def mail_user(name):
    """The account name for an install name: `<name>-mail`, which must fit a Windows account name (20 characters)."""
    user = f"{name}-mail"
    if not USER_RE.match(user):
        raise refusals.refuse("win.arg.mailbox_account", value=user)
    return user


def _check(root, name, worker, key=None):
    _check_args(root, worker)
    mail_user(name)
    if key is not None and not KEY_RE.match(key.strip()):
        raise refusals.refuse("win.arg.public_key")


def _script(root, name, worker, body, key=""):
    _check(root, name, worker, key or None)
    pre = _LAYOUT_PRE.format(root=root, user=mail_user(name))
    return pre + f"$w = '{worker}'\n$key = '{key.strip()}'\n" + "try {\n" + body + "\n} catch { Fail 'script' $_.Exception.Message }\n\n"


_PATHS = r"""  $mail = Join-Path $root 'mail'
  $box = Join-Path $root 'state-mail'
  $sshd = 'C:\ProgramData\ssh\sshd_config'
  $begin = '""" + BEGIN + r"""'; $end = '""" + END + r"""'
  $git = Join-Path $root 'tools\git\cmd\git.exe'
  $py = Join-Path $root 'tools\python\python.exe'"""


def ensure_script(root=ROOT, name="armada", worker=USER, key=""):
    return _script(root, name, worker, _PATHS + r"""
  if (-not (Test-Path $git)) { Fail 'git' "there is no git at ${git}: run the tools installer first (ct-setup add-worker)" }
  if (-not (Test-Path $py)) { Fail 'python' "there is no Python at ${py}: run the tools installer first (ct-setup add-worker)" }
  if (-not (Get-LocalUser -Name $w -ErrorAction SilentlyContinue)) { Fail 'account' "there is no account ${w}: run the layout step first" }
  if (-not (Test-Path (Join-Path $root 'release\src\ct\mailshell.py'))) { Fail 'release' 'the release is not there: fetch it first (ct-setup add-worker)' }
  $changed = $false
  # the account: a standard user, never an administrator, no interactive or remote-desktop logon (an ssh logon stays possible)
  $created = $false
  if (-not (Get-LocalUser -Name $u -ErrorAction SilentlyContinue)) {
    New-LocalUser -Name $u -Password (ConvertTo-SecureString (RandomPassword) -AsPlainText -Force) -PasswordNeverExpires -UserMayNotChangePassword -AccountNeverExpires -Description 'armada mailbox account' | Out-Null
    $created = $true; $changed = $true
  }
  if (Get-LocalGroupMember -SID 'S-1-5-32-544' -ErrorAction SilentlyContinue | Where-Object { $_.Name -like "*\$u" }) { Remove-LocalGroupMember -SID 'S-1-5-32-544' -Member $u; $changed = $true }
  if (Rights (Get-LocalUser -Name $u).SID.Value @() @('SeDenyInteractiveLogonRight', 'SeDenyRemoteInteractiveLogonRight')) { $changed = $true }
  # folders, the two bare repositories, and who may do what
  foreach ($d in @($mail, $box)) { if (-not (Test-Path $d)) { New-Item -ItemType Directory -Force -Path $d | Out-Null; $changed = $true } }
  foreach ($r in @('queue.git', 'results.git')) {
    $p = Join-Path $box $r
    if (-not (Test-Path (Join-Path $p 'HEAD'))) {
      $o = (& $git init -q --bare --initial-branch=main $p 2>&1) -join ' '
      if ($LASTEXITCODE -ne 0) { Fail 'repository' "git init failed for ${r}: $o" }
      $changed = $true
    }
  }
  function Acl($path, $rules, $pass) {
    $before = Sddl $path
    & icacls.exe $path /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' @rules | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail 'acl' "icacls failed on $path (exit $LASTEXITCODE)" }
    if ((Sddl $path) -ne $before) { $script:changed = $true }
  }
  Acl $box @("${u}:(RX)", "${w}:(RX)") $true
  Acl (Join-Path $box 'queue.git') @("${u}:(OI)(CI)M", "${w}:(OI)(CI)RX") $false
  Acl (Join-Path $box 'results.git') @("${w}:(OI)(CI)M", "${u}:(OI)(CI)RX") $false
  Acl $mail @("${u}:(OI)(CI)RX") $false
  foreach ($d in @('tools', 'release')) {                                                   # the account may read and run the tools and the release (git, Python, ct.mailshell), nothing else of the worker
    $p = Join-Path $root $d; $b = Sddl $p
    & icacls.exe $p /grant:r "${u}:(OI)(CI)RX" | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail 'acl' "icacls failed on $d (exit $LASTEXITCODE)" }
    if ((Sddl $p) -ne $b) { $changed = $true }
  }
  $b = Sddl $root; & icacls.exe $root /grant:r "${u}:(RX)" | Out-Null; if ((Sddl $root) -ne $b) { $changed = $true }
  # git refuses a repository that belongs to another account ('dubious ownership'): this git, and only these two repositories, are told they are meant
  foreach ($r in @('queue.git', 'results.git')) {
    $p = ((Join-Path $box $r) -replace '\\', '/')
    $have = @(& $git config --system --get-all safe.directory 2>$null)
    if ($have -notcontains $p) { & $git config --system --add safe.directory $p; $changed = $true }
  }
  # the launcher: the only program the key may run
  $launcher = Join-Path $mail 'mailshell.cmd'
  $text = "@echo off`r`nset `"PATH=$(Join-Path $root 'tools\git\cmd');%SystemRoot%\system32`"`r`nset `"PYTHONPATH=$(Join-Path $root 'release\src')`"`r`nset `"CT_MAILBOX=$($box -replace '\\', '/')`"`r`n`"$py`" -m ct.mailshell`r`n"
  if (-not (Test-Path $launcher) -or ((Get-Content $launcher -Raw) -ne $text)) { [IO.File]::WriteAllText($launcher, $text, [Text.Encoding]::ASCII); $changed = $true }
  # the key file, and the sshd_config block that points OpenSSH at it
  $akFile = Join-Path $mail 'authorized_keys'
  if ($key) {
    $line = 'restrict,command="' + $launcher + '" ' + $key
    if (-not (Test-Path $akFile) -or ((Get-Content $akFile -Raw).Trim() -ne $line)) {
      [IO.File]::WriteAllText($akFile, $line + "`n", [Text.Encoding]::ASCII)
      & icacls.exe $akFile /inheritance:r /grant:r '*S-1-5-18:F' '*S-1-5-32-544:F' "${u}:R" | Out-Null
      $changed = $true
    }
  }
  $block = "$begin`r`nMatch User $u`r`n    AuthorizedKeysFile $($akFile -replace '\\', '/')`r`n$end"
  $cur = [IO.File]::ReadAllText($sshd)
  $rx = [regex]::new('(?s)\r?\n?' + [regex]::Escape($begin) + '.*?' + [regex]::Escape($end))
  $new = $rx.Replace($cur, '').TrimEnd() + "`r`n`r`n" + $block + "`r`n"
  if ($new -ne $cur) {
    $bak = "$sshd.ct-backup"; if (-not (Test-Path $bak)) { Copy-Item $sshd $bak }
    [IO.File]::WriteAllText($sshd, $new, [Text.Encoding]::ASCII)
    $o = (& (Join-Path $env:SystemRoot 'System32\OpenSSH\sshd.exe') -t 2>&1) -join ' '
    if ($LASTEXITCODE -ne 0) { [IO.File]::WriteAllText($sshd, $cur, [Text.Encoding]::ASCII); Fail 'sshd' "sshd_config did not pass sshd -t, so it was put back: $o" }
    Restart-Service sshd
    $changed = $true
  }
  Write-Output ('CT-RESULT ' + (@{user = $u; mailbox = ($box -replace '\\', '/'); launcher = $launcher; created_user = $created; key = [bool]$key; changed = $changed} | ConvertTo-Json -Compress))""", key)


def remove_script(root=ROOT, name="armada", worker=USER):
    return _script(root, name, worker, _PATHS + r"""
  $was = $false
  $cur = [IO.File]::ReadAllText($sshd)
  $rx = [regex]::new('(?s)\r?\n?' + [regex]::Escape($begin) + '.*?' + [regex]::Escape($end))
  if ($rx.IsMatch($cur)) {
    [IO.File]::WriteAllText($sshd, $rx.Replace($cur, '').TrimEnd() + "`r`n", [Text.Encoding]::ASCII)
    $o = (& (Join-Path $env:SystemRoot 'System32\OpenSSH\sshd.exe') -t 2>&1) -join ' '
    if ($LASTEXITCODE -ne 0) { [IO.File]::WriteAllText($sshd, $cur, [Text.Encoding]::ASCII); Fail 'sshd' "sshd_config did not pass sshd -t, so it was put back: $o" }
    Restart-Service sshd; $was = $true
  }
  if (Get-LocalUser -Name $u -ErrorAction SilentlyContinue) { Remove-LocalUser -Name $u; $was = $true }
  foreach ($r in @('queue.git', 'results.git')) {
    if (-not (Test-Path -LiteralPath $git)) { break }                 # the tools folder is already gone (a removal that was run before): there is no git trust left to take out
    $p = ((Join-Path $box $r) -replace '\\', '/')
    if (@(& $git config --system --get-all safe.directory 2>$null) -contains $p) { & $git config --system --unset-all safe.directory ([regex]::Escape($p)); $was = $true }
  }
  Write-Output ('CT-RESULT ' + (@{user = $u; removed = $was; mailbox_kept = (Test-Path $box)} | ConvertTo-Json -Compress))""")


def _run(runner, script):
    rc, out = runner(list(POWERSHELL), script)
    return parse_result(out)


def ensure(runner, root=ROOT, name="armada", worker=USER, key=""):
    return _run(runner, ensure_script(root, name, worker, key))


def remove(runner, root=ROOT, name="armada", worker=USER):
    """Undo the account, the sshd_config block and the git trust. The mailbox folder (the jobs and results in it) is kept: deleting someone's history is not a side effect of un-setting an account."""
    return _run(runner, remove_script(root, name, worker))
