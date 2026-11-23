"""Building and queueing a job: parameters, the text files it carries, splitting a large set into independent jobs, and its id.

The size limits are not decided here: the worker publishes the limits it actually enforces in registry.json, and these functions use
exactly those, so a job is never sent that the worker would refuse. (Until a worker publishes them, the conservative old defaults apply.)
"""
import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

from .. import refusals
from ..jobs import file_name_problem
from . import refuse
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


class LeftOut:
    """What was NOT sent, and why, so nothing is dropped silently. `excluded`: left out by rule (hidden files and folders, build and dependency folders) as {path: files counted}; the person is told and can name
    one with --include. `unsent`: files that could not be sent (not text, too big, an unusable name, unreadable) as (name, catalog code, text): they make the set incomplete, so the job does not start unless the person
    chooses (--send-incomplete)."""

    def __init__(self):
        self.excluded, self.unsent = {}, []

    def exclude(self, path, count=0):
        self.excluded[path] = self.excluded.get(path, 0) + count

    def unsend(self, name, code, **facts):
        self.unsent.append((name, code, str(refusals.refuse(code, name=name, **facts))))


def by_rule(part):
    return part.startswith(".") or part in SKIP_DIRS


def named_by_include(rel, include):
    """True when an --include pattern NAMES the hidden or skipped part of `rel` literally ('.gitignore', '.github/**'): a wildcard never reaches into a skipped folder."""
    parts = rel.split("/")
    for i, part in enumerate(parts):
        if by_rule(part):
            return any(len(segs := pat.split("/")) >= i + 1 and all(s == p and not set("*?") & set(s) for s, p in zip(segs, parts[:i + 1])) for pat in include or ())
    return False


def collect_dir(d, limits, include=None, left=None):
    """Text files under `d`. `include` (glob patterns like 'src/**/*.js') keeps only what the command needs. Hidden files and folders, build output and dependency folders are left out unless an include names them,
    and every file that is left out or cannot be sent is recorded in `left` (a LeftOut), never dropped silently."""
    base = Path(d).expanduser().resolve()
    left = left if left is not None else LeftOut()
    files = {}
    for root, dirs, names in os.walk(base):
        rel_root = Path(root).relative_to(base).as_posix()
        prefix = "" if rel_root == "." else rel_root + "/"
        keep = []
        for dname in sorted(dirs):
            if by_rule(dname) and not named_by_include(prefix + dname, include):
                left.exclude(prefix + dname)
            else:
                keep.append(dname)
        dirs[:] = keep
        for fname in sorted(names):
            rel = prefix + fname
            if by_rule(fname) and not named_by_include(rel, include):
                left.exclude(rel, 1)
                continue
            if include and not any(glob_match(rel, pat) for pat in include):
                continue                                                          # the person's own filter: not a file that failed
            problem = file_name_problem(rel)
            if problem:
                left.unsend(rel, problem[0], reason=problem[1])
                continue
            try:
                text = (Path(root) / fname).read_text()
            except UnicodeDecodeError:
                left.unsend(rel, "files.not_text")
                continue
            except OSError as e:
                left.unsend(rel, "files.unreadable", reason=e.strerror or "it cannot be read")
                continue
            if len(text) > limits["max_file_kb"] * 1000:
                left.unsend(rel, "files.too_big", limit=limits["max_file_kb"])
                continue
            files[rel] = text
    return files


def check_complete(left, allow=False):
    """A set with files that could not be sent is an incomplete set: refuse, unless the person chose to send the rest (--send-incomplete). Left out by rule is not incomplete."""
    if left.unsent and not allow:
        entry = refusals.CATALOG["files.incomplete"]
        raise ClientError(entry.title, str(refusals.refuse("files.incomplete", count=plural(len(left.unsent), "file"))), list(entry.steps), facts=[("Code", entry.code)])


def plan_batches(files, batchable, limits):
    """Split `files` into jobs of at most max_count files each. Returns a list of dicts, or raises ClientError with the reason. The WHOLE set
    must fit the size limit first (nobody sends a huge tree as lots of jobs); splitting by count is allowed only when the class says its
    files are independent (`batchable`) and it needs at most max_batches jobs."""
    max_files, max_total = limits["max_count"], limits["max_total_kb"] * 1000
    total = sum(len(v) for v in files.values())
    if total > max_total:
        raise refuse.error("files.set.too_big", count=plural(len(files), "file"), kb=total // 1000, limit=max_total // 1000)
    if len(files) <= max_files:
        return [files]
    n = -(-len(files) // max_files)
    if not batchable:
        raise refuse.error("files.set.not_batchable", count=plural(len(files), "file"), limit=max_files)
    if n > limits["max_batches"]:
        raise refuse.error("files.set.batches", count=plural(len(files), "file"), jobs=plural(n, "job"), limit=limits["max_batches"])
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
            raise refuse.error("param.unreadable", ("Value", kv), example="-p dir=src")
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
        raise refuse.error("placement.locked.submit", example="ct-admin quarantine\nct-admin unlock")
    repos.push_files(repos.jobs_branch, {f"jobs/{jid}.json": json.dumps(stamp(jid, job, clock), indent=2) + "\n"}, f"job: {jid}")
