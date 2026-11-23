"""How fast this computer is, measured by the install itself.

The install runs a few steps that depend only on this computer (not on the network or a package cache): byte-compiling the code, importing it,
hashing a fixed amount of data, and creating and cloning a fixed git repository. Each step's seconds are kept in the worker's state. The release
ships a table of reference seconds per step, recorded on our own fleet (`scripts/record-calibration`). The ratio of this computer's seconds to the
reference is its speed factor: 2.0 means twice as slow as the reference. That ratio is only a starting estimate, published with its basis; the
worker's own finished profile runs replace it as they arrive (design: docs/design/PROFILE-TIME-LIMITS.md).

Pure standard library: it runs on the worker before anything else is set up."""
import hashlib
import json
import os
import py_compile
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PHASES = ("compile", "import", "hash", "git")
HASH_MB = 64                 # a fixed amount of data to hash: long enough to measure, short enough that an install does not notice it
ADDRESS = "".join(("c", chr(64), "example.invalid"))       # a throwaway commit identity, in a repository that is deleted at once
GIT_FILES = 200              # a fixed repository to create, commit and clone


def _timed(fn):
    start = time.monotonic()
    fn()
    return round(time.monotonic() - start, 3)


def _compile(code_dir):
    with tempfile.TemporaryDirectory() as tmp:
        for i, f in enumerate(sorted(Path(code_dir).rglob("*.py"))):
            py_compile.compile(str(f), cfile=str(Path(tmp) / f"{i}.pyc"), doraise=False, quiet=2)


def _import(code_dir):
    subprocess.run([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, sys.argv[1]); import ct.worker, ct.registry, ct.runner", str(Path(code_dir).parent)],
                   capture_output=True, timeout=120, env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"})


def _hash():
    block, h = bytes(range(256)) * 4096, hashlib.sha256()          # 1 MiB
    for _ in range(HASH_MB):
        h.update(block)


def _git():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "r"
        repo.mkdir()
        for i in range(GIT_FILES):
            (repo / f"f{i}.txt").write_text(f"file {i}\n" * 20)
        env = {"PATH": os.environ.get("PATH", ""), "HOME": tmp, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "c", "GIT_AUTHOR_EMAIL": ADDRESS,
               "GIT_COMMITTER_NAME": "c", "GIT_COMMITTER_EMAIL": ADDRESS}
        run = lambda *a, cwd=repo: subprocess.run(["git", *a], cwd=cwd, env=env, capture_output=True, timeout=120)      # noqa: E731
        run("init", "-q")
        run("add", "-A")
        run("commit", "-q", "-m", "x")
        run("clone", "-q", str(repo), str(Path(tmp) / "c"), cwd=tmp)


def measure(code_dir):
    """{phase: seconds} for the steps that can run here. A step that fails is left out, never guessed."""
    out = {}
    for name, fn in (("compile", lambda: _compile(code_dir)), ("import", lambda: _import(code_dir)), ("hash", _hash), ("git", _git)):
        try:
            out[name] = _timed(fn)
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
    return out


def speed_factor(measured, reference):
    """(factor, basis): the median of this computer's seconds over the reference seconds, step by step. 1.0 with the basis 'no reference' when nothing can be
    compared. The basis says how many steps it rests on, so a reader can tell a firm number from a thin one."""
    ratios = [measured[p] / reference[p] for p in PHASES if measured.get(p) and reference.get(p)]
    if not ratios:
        return 1.0, "no reference"
    return round(statistics.median(ratios), 2), f"install, {len(ratios)} of {len(PHASES)} steps"


def reference_from(recordings):
    """The shipped reference table: per step, the median seconds over the recorded machines."""
    return {p: round(statistics.median(r[p] for r in recordings if r.get(p)), 3) for p in PHASES if any(r.get(p) for r in recordings)}


EVIDENCE_KEEP = 20          # the most recent profile runs that count
STEP_LIMIT = 2.0            # one more run moves the factor by at most this much, up or down


def observe(path, ratio):
    """Keep one profile run's measured seconds over the person's estimate: how this computer compares with the one the estimate was made on."""
    doc = load(path)
    ratios = [r for r in doc.get("ratios", []) if isinstance(r, (int, float))] + [round(float(ratio), 3)]
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps({"ratios": ratios[-EVIDENCE_KEEP:]}) + "\n")


def combine(prior, ratios):
    """(factor, runs used): the install's prior, moved toward what profile runs showed. Replayed run by run, so the result needs no stored state:
    each run adds weight n/(n+2) to the median of what was seen (a geometric blend, since a ratio is a multiple), and no single run moves the factor
    by more than STEP_LIMIT. A run far from the others (over 10 times or under a tenth of their median, once there are three) is ignored."""
    ratios = [r for r in ratios if isinstance(r, (int, float)) and r > 0]
    if len(ratios) >= 3:
        mid = statistics.median(ratios)
        ratios = [r for r in ratios if mid / 10 <= r <= mid * 10]
    factor, seen = prior, []
    for r in ratios:
        seen.append(r)
        w = len(seen) / (len(seen) + 2)
        target = prior ** (1 - w) * statistics.median(seen) ** w
        factor = min(max(target, factor / STEP_LIMIT), factor * STEP_LIMIT)
    return round(factor, 2), len(ratios)


def load(path):
    try:
        doc = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def store(path, measured):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps({"at": int(time.time()), "phases": measured}, indent=1) + "\n")
