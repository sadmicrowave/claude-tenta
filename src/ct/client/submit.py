"""Building and queueing a job: parameters, the text files it carries, splitting a large set into independent jobs, and its id.

The size limits are not decided here: the worker publishes the limits it actually enforces in registry.json, and these functions use
exactly those, so a job is never sent that the worker would refuse. (Until a worker publishes them, the conservative old defaults apply.)
"""
import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

from ..jobs import FILE_NAME_RE
from .repo import ClientError
from ..words import plural

SKIP_DIRS = {"node_modules", "__pycache__", "venv", "dist", "build", "target"}
ID_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"            # no 0/o or 1/l: easy to read out and to type
OLD_FILE_LIMITS = {"max_count": 50, "max_file_kb": 100, "max_total_kb": 300, "max_batches": 6}


def file_limits(info):
    """{'max_count', 'max_file_kb', 'max_total_kb', 'max_batches'} exactly as the worker's installed registry says."""
    given = ((info or {}).get("limits") or {}).get("files") or {}
    return {**OLD_FILE_LIMITS, **{k: v for k, v in given.items() if k in OLD_FILE_LIMITS}}


def glob_match(rel, pat):
    """'src/**/*.js' style globs: ** spans folders (or none), * stays inside one folder; a bare '*.js' matches at any depth."""
    if "/" not in pat:
        rel = rel.rsplit("/", 1)[-1]
    rx, i = "", 0
    while i < len(pat):
        if pat.startswith("**/", i):
            rx, i = rx + "(?:.*/)?", i + 3
        elif pat.startswith("**", i):
            rx, i = rx + ".*", i + 2
        elif pat[i] == "*":
            rx, i = rx + "[^/]*", i + 1
        elif pat[i] == "?":
            rx, i = rx + "[^/]", i + 1
        else:
            rx, i = rx + re.escape(pat[i]), i + 1
    return re.fullmatch(rx, rel) is not None


def collect_dir(d, limits, include=None, note=None):
    """Text files under `d`. `include` (glob patterns like 'src/**/*.js') keeps only what the command needs. Hidden files and folders,
    build output and dependency folders are never attached. `note(name, why)` is told about each file that was skipped."""
    base = Path(d).expanduser().resolve()
    files = {}
    for p in sorted(base.rglob("*")):
        rel = p.relative_to(base)
        if any(part.startswith(".") or part in SKIP_DIRS for part in rel.parts) or not p.is_file():
            continue
        if include and not any(glob_match(rel.as_posix(), pat) for pat in include):
            continue
        try:
            text = p.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        name = rel.as_posix()
        if not FILE_NAME_RE.fullmatch(name) or len(text) > limits["max_file_kb"] * 1000:
            if note:
                note(name, f"its name is not allowed, or it is over {limits['max_file_kb']} KB")
            continue
        files[name] = text
    return files


def plan_batches(files, batchable, limits):
    """Split `files` into jobs of at most max_count files each. Returns a list of dicts, or raises ClientError with the reason. The WHOLE set
    must fit the size limit first (nobody sends a huge tree as lots of jobs); splitting by count is allowed only when the class says its
    files are independent (`batchable`) and it needs at most max_batches jobs."""
    max_files, max_total = limits["max_count"], limits["max_total_kb"] * 1000
    total = sum(len(v) for v in files.values())
    if total > max_total:
        raise ClientError("These files do not fit in one job",
                          f"{plural(len(files), 'file')} / {total // 1000} KB is over the {max_total // 1000} KB limit for one job, and a bigger tree is never split. "
                          "Attach only the files the command needs (--include 'src/**/*.js').")
    if len(files) <= max_files:
        return [files]
    n = -(-len(files) // max_files)
    if not batchable:
        raise ClientError("These files do not fit in one job", f"{plural(len(files), 'file')} is over the {max_files}-file limit, and this class needs its files "
                          "together so it cannot be split. Attach only the files it needs (--include), or run it yourself.")
    if n > limits["max_batches"]:
        raise ClientError("These files do not fit in one job", f"{plural(len(files), 'file')} would need {plural(n, 'job')} (limit {limits['max_batches']}). Attach fewer files (--include).")
    names = sorted(files)
    size = -(-len(names) // n)
    return [{k: files[k] for k in names[i:i + size]} for i in range(0, len(names), size)]


def new_job_id(prefix, known=()):
    """`profile-k7m2xq`: the kind of job plus six random characters. Short enough to type; the time is in `status`."""
    known = set(known)
    while True:
        jid = f"{prefix[:40]}-{''.join(secrets.choice(ID_ALPHABET) for _ in range(6))}"
        if jid not in known and not any(k.startswith(jid + "-b") for k in known):
            return jid


def parse_params(pairs):
    """['n=3', 'dir=src'] -> {'n': 3, 'dir': 'src'}; whole numbers become numbers, everything else stays text."""
    params = {}
    for kv in pairs or []:
        if "=" not in kv:
            raise ClientError(f"Unreadable -p {kv!r}", "Write each job parameter as a name, an equals sign, then a value.", example="-p dir=src")
        k, v = kv.split("=", 1)
        params[k] = int(v) if re.fullmatch(r"-?\d+", v) else v
    return params


def build_job(cls, params=None, prompt=None, files=None):
    job = {"class": cls, "params": params or {}}
    if prompt:
        job["prompt"] = prompt
    if files:
        job["files"] = files
    return job


def job_ids(base, count):
    """One id for a single job; base-b1, base-b2, ... when a set of files was split into independent jobs."""
    return [base] if count == 1 else [f"{base[:60]}-b{i}" for i in range(1, count + 1)]


def stamp(jid, job, clock=lambda: datetime.now(timezone.utc)):
    """The job file as it is pushed: the id, the job, and a UTC creation time (the worker refuses jobs that are too old or from the future)."""
    return {"id": jid, **job, "created": clock().strftime("%Y-%m-%dT%H:%M:%SZ")}


def push_job(repos, jid, job, clock=lambda: datetime.now(timezone.utc)):
    """Jobs live on their own branch so main stays a clean log of code and registry changes. Refuses if the worker has locked itself."""
    locked = repos.pull_results()
    if locked:
        raise ClientError("The worker is locked, so this job was not submitted",
                          "It locked itself after a security event and runs nothing until you have reviewed it.",
                          ["First look at what happened, then unlock it:"], example="ct-admin quarantine\nct-admin unlock")
    repos.push_files(repos.jobs_branch, {f"jobs/{jid}.json": json.dumps(stamp(jid, job, clock), indent=2) + "\n"}, f"job: {jid}")
