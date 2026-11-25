"""Before any test runs: are the things the tests need actually here? One clear message instead of hundreds of errors that all mean the same thing.

Checks the declared Python dependencies (read from pyproject.toml, so a new dependency is covered the day it is added) and the system tools the tests call.
Run directly (`python tests/preflight.py`) or imported by tests/support.py, which every test module imports first."""
import importlib.metadata
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ("git", "ssh-keygen")
MACHINE_PATTERN = re.compile(r"^(ct-e2e|ct-mx|ct-u|ct-run|cw-e2e)")
NEED_GB = {"probe": 5, "e2e": 10}


def free_gb(path="/tmp"):
    return shutil.disk_usage(path).free / 1e9


def _run(cmd, timeout=20):
    """(exit status, output) of a command; a missing program or a hang is a failure with its own text, never an exception."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def _first_line(text):
    """The line that says what went wrong: `docker info` prints a client banner first and the failure last."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    bad = [l for l in lines if re.search(r"error|cannot|failed|refused|denied|not running|no such", l, re.I)]
    return (bad or lines or ["no output"])[0][:200]


def _disk(kind):
    gb = free_gb()
    return [f"only {gb:.0f} GB of free disk, the {kind} run needs {NEED_GB[kind]} GB: free some space (docker system prune, or scripts/e2e-clean --all)"] if gb < NEED_GB[kind] else []


def _probe_problems():
    if not shutil.which("docker"):
        return ["Docker was not found: install OrbStack (brew install orbstack) and open it"]
    rc, text = _run(["docker", "info"])
    out = [] if rc == 0 else [f"Docker is not running ({_first_line(text)}): open -a OrbStack"]
    return out + _disk("probe")


def _e2e_problems(cap):
    missing = [t for t in ("orb", "orbctl", "tar", "git") if not shutil.which(t)]
    if missing:
        return [f"the program {m} is not on the PATH" + (": install OrbStack (brew install orbstack)" if m.startswith("orb") else "") for m in missing]
    rc, text = _run(["orb", "list"])
    if rc != 0:
        return [f"OrbStack is not running ({_first_line(text)}): open -a OrbStack"] + _disk("e2e")
    names = [line.split()[0] for line in text.splitlines() if line.split() and MACHINE_PATTERN.match(line.split()[0])]
    out = [f"{len(names)} test machines already exist (limit {cap}): scripts/e2e-clean"] if len(names) >= cap else []
    return out + _disk("e2e")


def _real_problems(host):
    if not host:
        return ["no host given: scripts/probe --on user@host <probe>"]
    if not shutil.which("ssh"):
        return ["the program ssh is not on the PATH"]
    rc, text = _run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "AddressFamily=inet", host, "true"], timeout=15)
    return [] if rc == 0 else [f"{host} does not answer a key login ({_first_line(text)}): check the machine is on, then ssh {host} once by hand"]


def problems(kind="unit", arg=None, cap=4):
    if kind == "probe":
        return _probe_problems()
    if kind == "e2e":
        return _e2e_problems(cap)
    if kind == "real":
        return _real_problems(arg)
    out = []
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text()).get("project", {}).get("dependencies", [])
    for spec in declared:
        name = re.split(r"[<>=!~ ;\[]", spec, maxsplit=1)[0]
        try:
            importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            out.append(f"the Python package {name} is not installed for {sys.executable}")
    out += [f"the program {t} is not on the PATH" for t in TOOLS if not shutil.which(t)]
    return out


def require(kind="unit", arg=None, cap=4):
    found = problems(kind, arg, cap)
    if found and kind != "unit":
        sys.stderr.write(f"The {kind} run cannot start here:\n" + "".join(f"  - {p}\n" for p in found))
        raise SystemExit(1)
    if found:
        sys.stderr.write("The tests cannot run here:\n" + "".join(f"  - {p}\n" for p in found)
                         + "Use the project's own Python (it has the packages): ~/.ct-venv/bin/python, or: python3 -m venv ~/.ct-venv && ~/.ct-venv/bin/pip install -e .\n")
        raise SystemExit(1)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Check the environment before a run; exit 1 with the fixes when something is missing.")
    ap.add_argument("--for", dest="kind", default="unit", choices=("unit", "probe", "e2e", "real"))
    ap.add_argument("arg", nargs="?", help="the host for --for real (user@host)")
    ap.add_argument("--cap", type=int, default=4, help="most test machines allowed before an e2e run")
    a = ap.parse_args()
    require(a.kind, a.arg, a.cap)
