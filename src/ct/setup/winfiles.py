"""The worker's own files on a Windows computer, over SSH with PowerShell (scope: docs/design/WINDOWS-WORKER-LOOP.md, slice 4d-3a): its configuration, its registry, its state folders and its two clones of the
mailbox repositories, with git told to trust exactly those clones. Idempotent, reports `changed`. Same transport as wintools: the script on standard input, one `CT-RESULT {json}` or `CT-ERROR {json}` line.

The text of a file travels inside the script as base64 (no quoting can break it) and is written as UTF-8 without a byte-order mark. A file that already holds the same bytes is left alone."""
import base64
import re
from pathlib import Path

from .. import config
from . import toml_out
from .winlayout import ROOT, USER, _PRE, _check_args
from .wintools import POWERSHELL, parse_result

PACK = Path(__file__).resolve().parents[3] / "packs" / "windows-core" / "classes" / "windows-core.yaml"
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{2,23}$")


def windows_config(name, host, admin_user, worker_user=USER, ssh_port=22, root=None, client_home=None):
    """The worker's ct.toml for a Windows computer whose files live under `root` (forward slashes, e.g. C:/ProgramData/armada)."""
    root = root or "C:/ProgramData/armada"
    answers = {"identity": {"name": name}, "server": {"host": host, "admin_user": admin_user, "worker_user": worker_user, "ssh_port": ssh_port},
               "paths": {"code": f"{root}/release", "state_root": f"{root}/state", "config": f"{root}/config", "worker_home": f"{root}/state", **({"client_home": client_home} if client_home else {})},
               "transport": {"kind": "direct"}}
    clean, problems = config.validate(answers)
    if problems:
        raise ValueError("; ".join(problems))
    return toml_out.dump(clean, header="Written by ct-setup for a Windows worker.")


def pack_text():
    """The registry a Windows worker starts with: the windows-core pack's classes."""
    return PACK.read_text(encoding="utf-8")


def _b64(text):
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def ensure_script(config_text, registry_text, root=ROOT, worker=USER):
    _check_args(root, worker)
    return _PRE.format(root=root, user=worker) + f"""$cfgB64 = '{_b64(config_text)}'
$regB64 = '{_b64(registry_text)}'
try {{
  $changed = $false
  $git = Join-Path $root 'tools\\git\\cmd\\git.exe'
  if (-not (Test-Path $git)) {{ Fail 'git' "there is no git at ${{git}}: run the tools installer first (ct-setup add-worker)" }}
  $conf = Join-Path $root 'config'
  $state = Join-Path $root 'state'
  $box = Join-Path $root 'state-mail'
  foreach ($d in @($conf, $state, (Join-Path $state 'jobs'))) {{ if (-not (Test-Path $d)) {{ Fail 'layout' "the folder $d is missing: run the layout step first" }} }}
  foreach ($d in @((Join-Path $state 'run'), (Join-Path $state 'state'))) {{ if (-not (Test-Path $d)) {{ New-Item -ItemType Directory -Force -Path $d | Out-Null; $changed = $true }} }}      # the worker's own folders: they inherit the worker account's access from state
  foreach ($r in @('queue.git', 'results.git')) {{ if (-not (Test-Path (Join-Path $box "$r\\HEAD"))) {{ Fail 'mailbox' "the mailbox repository $r is missing: run the mailbox step first" }} }}
  function Put($path, $b64) {{
    $bytes = [Convert]::FromBase64String($b64)
    if ((Test-Path $path) -and ([BitConverter]::ToString([IO.File]::ReadAllBytes($path)) -eq [BitConverter]::ToString($bytes))) {{ return $false }}
    [IO.File]::WriteAllBytes($path, $bytes)
    return $true
  }}
  if (Put (Join-Path $conf 'ct.toml') $cfgB64) {{ $changed = $true }}
  if (Put (Join-Path $conf 'registry.yaml') $regB64) {{ $changed = $true }}
  foreach ($c in @(@('queue', 'queue.git'), @('results', 'results.git'))) {{
    $dest = Join-Path $state $c[0]
    if (-not (Test-Path (Join-Path $dest '.git'))) {{
      $ErrorActionPreference = 'Continue'                                                    # git writes warnings (an empty repository) to stderr; PowerShell 5.1 would stop on them
      $o = (& $git clone -q (Join-Path $box $c[1]) $dest 2>&1) -join ' '
      $rc = $LASTEXITCODE
      $ErrorActionPreference = 'Stop'
      if ($rc -ne 0) {{ Fail 'clone' "git clone of $($c[1]) failed: $o" }}
      $changed = $true
    }}
    $p = ($dest -replace '\\\\', '/')
    if (@(& $git config --system --get-all safe.directory 2>$null) -notcontains $p) {{ & $git config --system --add safe.directory $p; $changed = $true }}
  }}
  Write-Output ('CT-RESULT ' + (@{{config = (Join-Path $conf 'ct.toml'); registry = (Join-Path $conf 'registry.yaml'); queue_clone = (Join-Path $state 'queue'); results_clone = (Join-Path $state 'results'); changed = $changed}} | ConvertTo-Json -Compress))
}} catch {{ Fail 'script' $_.Exception.Message }}

"""


def ensure(runner, config_text, registry_text, root=ROOT, worker=USER):
    rc, out = runner(list(POWERSHELL), ensure_script(config_text, registry_text, root, worker))
    return parse_result(out)
