"""Put `git` and `python` on a Windows computer, over SSH, with PowerShell and nothing else (scope: docs/design/WINDOWS-TOOLS-INSTALLER.md).

The PowerShell is built here as text and sent on the standard input of `powershell -Command -` (no `.ps1` file: the execution policy of a fresh Windows is Restricted, and no
quoting or command-line length limit of the default cmd shell applies). It prints one line, `CT-RESULT {json}` or `CT-ERROR {json}`, which `parse_result` reads.

  git     an existing `git` is used; else MinGit (the official minimal Git for Windows zip) is downloaded, its SHA-256 compared with the pinned one BEFORE anything is unpacked, and unpacked
          with `tar` into <tools>\\git. No installer, no administrator rights, nothing on the system PATH.
  python  an existing Python 3.11 or newer is used; else the official
          python.org installer is downloaded, its SHA-256 compared with the pinned one and its Authenticode signature checked (signer Python Software Foundation) BEFORE it is run, then run
          silently for this user into <tools>\\python (no administrator rights, nothing on PATH). python.org publishes only MD5 for the embeddable zip, which also has no venv, so it is not used.
Standard library only.
"""
from .. import refusals
import json
import re

GIT = {"version": "2.56.1", "name": "MinGit-2.56.1-64-bit.zip", "size": 39849768,
       "url": "https://github.com/git-for-windows/git/releases/download/v2.56.1.windows.1/MinGit-2.56.1-64-bit.zip",
       "sha256": "4a92850b6e917da4fd010669b614b55dccf5b0ec333a534cc4be4700dfefdafd"}      # the digest GitHub states for that release asset
PYTHON = {"version": "3.13.17", "name": "python-3.13.17-amd64.exe", "size": 29941280,
          "url": "https://www.python.org/ftp/python/3.13.17/python-3.13.17-amd64.exe",
          "sha256": "5da012101ba2e17113a9a1cd901e9ce1441086f2b7ecb5de06ec94163899a190",     # computed from the file on python.org (it publishes only MD5 and GPG); also checked: Authenticode signer
          "signer": "Python Software Foundation"}
PYYAML = {"version": "6.0.4",                         # the registry is YAML: the worker's own code needs this one package beyond the standard library
          "hashes": {"cp311": "0db75f402e99c25239003a499c214a61947b4ef5312af34711d4c63e5c179318", "cp312": "131d11bd9b60d705a6388366d77ed67850995a4d2be85fe149785a257c17e531",
                     "cp313": "0a3a351f04dc124f9357512bdb80ac37cd1750cf1371da33e7cbb96d80ac865f", "cp314": "1d2f08c122727c86f8303bb1dd45159ca1de1f2d68798e671ec46974e321008e",
                     "cp315": "67da066ca12eb60114ea87f2ecbcca622fff0c205c743cc900437127564fabf6"}}      # sha256 of each win_amd64 wheel on PyPI; pip refuses any other file
MIN_PYTHON = (3, 11)
DEFAULT_TOOLS = r"%ProgramData%\armada\tools"
POWERSHELL = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", "-"]       # the script itself arrives on standard input
TOOLS_RE = re.compile(r"^(%[A-Za-z]+%|[A-Za-z]:)[\\A-Za-z0-9 ._()-]*$")                                         # a folder: letters, digits, space . _ ( ) - and backslashes; no quote can get into the script


# PowerShell shared by the installer and the removal: the per-user Python bundle that registered itself for THIS target folder. Python's bundle entry has no install location, so it is found through the PythonCore key
# (whose InstallPath is the folder) and then the bundle's own uninstall entry. Returns the uninstaller paths, or nothing when no registration points at the folder.
PY_REGISTRATION = r"""function OurPythonUninstallers($pydir) {
  $ours = $false
  foreach ($k in (Get-ChildItem 'HKCU:\SOFTWARE\Python\PythonCore' -ErrorAction SilentlyContinue)) {
    $ip = (Get-ItemProperty -LiteralPath (Join-Path $k.PSPath 'InstallPath') -ErrorAction SilentlyContinue).'(default)'
    if ($ip -and $ip.TrimEnd('\') -ieq $pydir) { $ours = $true }
  }
  if (-not $ours) { return @() }
  @(Get-ChildItem 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall' -ErrorAction SilentlyContinue | ForEach-Object { Get-ItemProperty -LiteralPath $_.PSPath } |
    Where-Object { $_.DisplayName -match '^Python 3\.\d+\.\d+ \((32|64)-bit\)$' -and $_.UninstallString -match '^"([^"]+)"' } | ForEach-Object { $Matches[1] })
}
"""


class WinToolsError(Exception):
    def __init__(self, what, detail):
        super().__init__(f"{what}: {detail}")
        self.what, self.detail = what, detail


def installer_script(tools_dir=DEFAULT_TOOLS):
    if not TOOLS_RE.match(tools_dir) or ".." in tools_dir:
        raise refusals.refuse("win.arg.tools_folder", value=tools_dir)
    g = GIT
    p = PYTHON
    req = f"PyYAML=={PYYAML['version']} " + " ".join(f"--hash=sha256:{h}" for h in PYYAML["hashes"].values())
    return f"""$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$tools = [Environment]::ExpandEnvironmentVariables('{tools_dir}')
$changed = $false
{PY_REGISTRATION}function Fail($what, $detail) {{ Write-Output ('CT-ERROR ' + (@{{what = $what; detail = "$detail"}} | ConvertTo-Json -Compress)); exit 1 }}
try {{
  $py = $null; $pyv = ''
  foreach ($name in (Join-Path $tools 'python\\python.exe'), 'python', 'py') {{
    $c = Get-Command $name -ErrorAction SilentlyContinue
    if ($c -and $c.Source -notlike '*WindowsApps*' -and $c.Source -notlike '*\\Users\\*') {{       # not the Store stub, and not one under a person's profile (the worker's account could not read it)
      $v = ''
      try {{ $v = (& $c.Source --version 2>&1) -join '' }} catch {{ $v = '' }}                      # a stale entry (an uninstalled Python still on PATH) is skipped, not fatal
      if ($v -match 'Python (\\d+)\\.(\\d+)' -and ([int]$Matches[1] -gt {MIN_PYTHON[0]} -or ([int]$Matches[1] -eq {MIN_PYTHON[0]} -and [int]$Matches[2] -ge {MIN_PYTHON[1]}))) {{ $py = $c.Source; $pyv = $v.Trim(); break }}
    }}
  }}
  if (-not $py) {{                                                                          # no winget route: it installs under one person's profile, where the worker's own account could not read it
      New-Item -ItemType Directory -Force -Path $tools | Out-Null
      $exe = Join-Path $tools '{p["name"]}'
      & curl.exe -L --fail --silent --show-error -o $exe '{p["url"]}'
      if ($LASTEXITCODE -ne 0) {{ Fail 'python' "there is no Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer, the download from python.org failed (curl exit $LASTEXITCODE)" }}
      $got = (Get-FileHash -Algorithm SHA256 $exe).Hash.ToLower()
      if ($got -ne '{p["sha256"]}') {{ Remove-Item $exe -Force; Fail 'python' "the download's SHA-256 is $got but {p["sha256"]} was expected: refused, nothing was run" }}
      $sig = Get-AuthenticodeSignature $exe
      if (($sig.Status -ne 'Valid') -or ($sig.SignerCertificate.Subject -notlike '*{p["signer"]}*')) {{ Remove-Item $exe -Force; Fail 'python' "the installer's signature is $($sig.Status), not a valid one from {p["signer"]}: refused, nothing was run" }}
      $pydir = Join-Path $tools 'python'
      if (-not (Test-Path (Join-Path $pydir 'python.exe'))) {{                                  # a removal that left Python registered makes the installer do nothing: undo that registration first (it points at this folder only)
        foreach ($un in (OurPythonUninstallers $pydir)) {{ if (Test-Path -LiteralPath $un) {{ Start-Process -FilePath $un -ArgumentList '/uninstall', '/quiet' -Wait | Out-Null; $changed = $true }} }}
      }}
      $proc = Start-Process -FilePath $exe -ArgumentList '/quiet', 'InstallAllUsers=0', 'PrependPath=0', 'Include_launcher=0', 'Include_test=0', "TargetDir=$pydir" -Wait -PassThru
      Remove-Item $exe -Force
      if ($proc.ExitCode -ne 0) {{ Fail 'python' "the Python installer exited with $($proc.ExitCode)" }}
      $py = Join-Path $pydir 'python.exe'
      if (-not (Test-Path $py)) {{ Fail 'python' 'the Python installer finished, but python.exe is not where it was expected' }}
    $pyv = ((& $py --version 2>&1) -join '').Trim()
    $changed = $true
  }}
  $ErrorActionPreference = 'Continue'                                                         # pip and python write to stderr; PowerShell 5.1 would stop on it
  & $py -c "import yaml" 2>$null
  if ($LASTEXITCODE -ne 0) {{
    New-Item -ItemType Directory -Force -Path $tools | Out-Null
    $reqfile = Join-Path $tools 'requirements.txt'
    Set-Content -LiteralPath $reqfile -Value '{req}' -Encoding ASCII
    $o = (& $py -m pip install --disable-pip-version-check --no-deps --only-binary=:all: --require-hashes -r $reqfile 2>&1) -join ' '
    $rc = $LASTEXITCODE
    Remove-Item -LiteralPath $reqfile -Force -ErrorAction SilentlyContinue
    if ($rc -ne 0) {{ $ErrorActionPreference = 'Stop'; Fail 'pyyaml' "pip could not install PyYAML (exit $rc): $o" }}
    $changed = $true
  }}
  $ErrorActionPreference = 'Stop'
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


def ssh_runner(user, host, port=22, timeout=900):
    """The runner install() wants, over key-login SSH: `(exit code, output)`. OpenSSH's own client warnings (lines starting `** `, such as the post-quantum notice) are left out; every other word of stderr is kept."""
    import subprocess

    def run(argv, input_text):
        try:
            r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-p", str(port), f"{user}@{host}", " ".join(argv)], input=input_text, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as e:
            return 255, str(e)
        err = "\n".join(x for x in r.stderr.splitlines() if not x.startswith("** "))
        return r.returncode, (r.stdout + ("\n" + err if err.strip() else "")).replace("\r", "")
    return run


def install(runner, tools_dir=DEFAULT_TOOLS):
    """`runner(argv, input_text) -> (exit code, output)` runs a command on the Windows computer. Returns what parse_result returns."""
    rc, out = runner(list(POWERSHELL), installer_script(tools_dir))
    return parse_result(out)
