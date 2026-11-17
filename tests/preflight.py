"""Before any test runs: are the things the tests need actually here? One clear message instead of hundreds of errors that all mean the same thing.

Checks the declared Python dependencies (read from pyproject.toml, so a new dependency is covered the day it is added) and the system tools the tests call.
Run directly (`python tests/preflight.py`) or imported by tests/support.py, which every test module imports first."""
import importlib.metadata
import re
import shutil
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ("git", "ssh-keygen")


def problems():
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


def require():
    found = problems()
    if found:
        sys.stderr.write("The tests cannot run here:\n" + "".join(f"  - {p}\n" for p in found)
                         + "Use the project's own Python (it has the packages): ~/.ct-venv/bin/python, or: python3 -m venv ~/.ct-venv && ~/.ct-venv/bin/pip install -e .\n")
        raise SystemExit(1)


if __name__ == "__main__":
    require()
