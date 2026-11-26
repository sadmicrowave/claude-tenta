"""Environment markers: files at the top of a folder that say what its commands usually need (pyproject.toml, requirements*.txt, package.json, Cargo.toml, go.mod, Makefile).

They only PROPOSE. Detection is read-only and runs on the person's own computer: it reads a few small plain files from the folder it was given, never follows a link, never runs anything and sends nothing. Nothing is installed by a
proposal: the packages go on the profiling run only when the person asks for them (`--use-detected-packages`), and the worker's own approval screen and the adopt step still apply. Only pinned packages are proposed: npm pins come
from the lock file, pip pins from `==`; anything that cannot be pinned is listed as "needs a pin", never guessed."""
import configparser
import json
import re
import tomllib
from pathlib import Path

from .. import pkgspec
from . import refuse

MAX_MARKER_BYTES = 1_000_000                 # a marker file is small; a huge one is reported, not read
PIP_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([0-9A-Za-z.+!-]+)$")
TARGET = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_.-]*)\s*:(?!=)")
TOOLCHAINS = {"Cargo.toml": "the Rust toolchain", "go.mod": "the Go toolchain"}


class Detection:
    def __init__(self):
        self.found, self.packages, self.unpinned, self.notes, self.problems = [], [], [], [], []

    def add(self, *pkgs):
        self.packages = sorted({*self.packages, *pkgs})

    def lack_pin(self, entry):
        self.unpinned = sorted({*self.unpinned, entry})

    def usable(self):
        """The packages to put on a profiling run. More than the limit is refused with the count: a list is never cut short, because a cut list would run without software the person was told about."""
        if len(self.packages) > pkgspec.MAX_PACKAGES:
            raise refuse.error("packages.markers.too_many", count=len(self.packages), limit=pkgspec.MAX_PACKAGES, steps=["Name the packages you want with `--package`, up to the limit."])
        return list(self.packages)


def _read(path, det):
    """The text of a marker file, or None with the reason in `det.problems` (a link, a file that is too big, or the real error)."""
    try:
        if path.is_symlink() or not path.is_file():
            return None
        if path.stat().st_size > MAX_MARKER_BYTES:
            det.problems.append(f"{path.name} is larger than {MAX_MARKER_BYTES // 1000} KB, so it was not read.")
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        det.problems.append(f"{path.name} could not be read: {getattr(e, 'strerror', None) or e}")
        return None


def _pip(det, spec):
    spec = spec.split(";")[0].replace(" ", "")
    m = PIP_PIN.match(spec)
    if m:
        det.add(f"pip:{m.group(1)}=={m.group(2)}")
    elif spec:
        det.lack_pin(f"pip:{spec}")


def _python(base, det):
    names = sorted(p.name for p in base.glob("requirements*.txt") if _is_marker(p))
    any_python = bool(names)
    for name in names:
        text = _read(base / name, det)
        for line in (text or "").splitlines():
            line = line.split(" #")[0].strip()
            if line and not line.startswith(("#", "-")):
                _pip(det, line)
        det.found.append(name)
    if _is_marker(base / "pyproject.toml"):
        any_python = True
        det.found.append("pyproject.toml")
        text = _read(base / "pyproject.toml", det)
        if text is not None:
            try:
                data = tomllib.loads(text)
                for dep in (data.get("project") or {}).get("dependencies") or []:
                    _pip(det, str(dep))
            except (tomllib.TOMLDecodeError, AttributeError, TypeError) as e:
                det.problems.append(f"pyproject.toml could not be read: {e}")
    if _is_marker(base / "setup.cfg"):
        any_python = True
        det.found.append("setup.cfg")
        text = _read(base / "setup.cfg", det)
        if text is not None:
            cp = configparser.ConfigParser(interpolation=None)
            try:
                cp.read_string(text)
                for line in cp.get("options", "install_requires", fallback="").splitlines():
                    if line.strip():
                        _pip(det, line.strip())
            except configparser.Error as e:
                det.problems.append(f"setup.cfg could not be read: {e}")
    if any_python:
        det.add("apt:python3", "apt:python3-pip")


def _node(base, det):
    if not _is_marker(base / "package.json"):
        return
    det.found.append("package.json")
    text = _read(base / "package.json", det)
    lock_text = _read(base / "package-lock.json", det) if _is_marker(base / "package-lock.json") else None
    if lock_text is not None:
        det.found.append("package-lock.json")
    det.add("apt:nodejs", "apt:npm")
    if text is None:
        return
    try:
        pkg = json.loads(text)
        wanted = sorted({*(pkg.get("dependencies") or {}), *(pkg.get("devDependencies") or {})})
        versions = {}
        if lock_text is not None:
            lock = json.loads(lock_text)
            for key, v in (lock.get("packages") or {}).items():
                if key.startswith("node_modules/") and "node_modules/" not in key[len("node_modules/"):]:
                    versions[key[len("node_modules/"):]] = (v or {}).get("version")
            for key, v in (lock.get("dependencies") or {}).items():
                versions.setdefault(key, (v or {}).get("version"))
    except (ValueError, AttributeError, TypeError) as e:
        det.problems.append(f"package.json could not be read: {e}")
        return
    for name in wanted:
        entry = f"npm:{name}@{versions[name]}" if versions.get(name) else None
        if entry and pkgspec.is_pinned(entry) and pkgspec.PKG_RES["npm"].fullmatch(entry[4:]):
            det.add(entry)
        else:
            det.lack_pin(f"npm:{name}")


def _make(base, det):
    if not _is_marker(base / "Makefile"):
        return
    det.found.append("Makefile")
    det.add("apt:make")
    text = _read(base / "Makefile", det)
    first, calls = None, []
    for line in (text or "").splitlines():
        m = TARGET.match(line)
        if m and not line.startswith("."):
            if first:
                break
            first = m.group(1)
        elif first and line.startswith("\t") and line.strip():
            calls.append(line.strip())
    if first:
        det.notes.append(f"Makefile: the first target `{first}` runs: " + ("; ".join(calls[:3]) if calls else "(nothing)") + ". This is shown for you to check; nothing is installed for it.")


def _is_marker(path):
    return path.is_file() and not path.is_symlink()


def detect(folder):
    """What the folder's marker files propose (a Detection). Read-only; a missing or unreadable marker never stops the rest."""
    base, det = Path(folder).expanduser(), Detection()
    _python(base, det)
    _node(base, det)
    for name, tool in TOOLCHAINS.items():
        if _is_marker(base / name):
            det.found.append(name)
            det.problems.append(f"{name}: {tool} is needed, but this worker cannot install it yet (only apt, npm and pip packages are supported), so nothing is proposed for it.")
    _make(base, det)
    det.found = sorted(set(det.found), key=lambda n: ("requirements" not in n, n))
    return det


def proposal_lines(det):
    """What to show the person: what was found, what is suggested, what needs a pin, and the one flag that accepts the suggestion."""
    chips = lambda names: ", ".join(f"`{n}`" for n in names)      # noqa: E731  (a name or flag as typed is a code chip, like every command)
    lines = ["Found: " + chips(det.found) + "."] if det.found else []
    if det.packages:
        lines.append("Suggested packages (pinned): " + chips(det.packages))
        lines.append("To use them, run the same command again with `--use-detected-packages` (the command is below). Nothing is installed until you approve the profiling run.")
    if det.unpinned:
        lines.append("Not suggested, needs a pin: " + chips(det.unpinned) + ". Give an exact version with `--package` (for example `pip:name==1.2.3`).")
    lines += det.notes + det.problems
    return lines
