"""Validation of a job file: everything a requester controls is checked here before anything runs.

Rules: ids are short and plain; commands are argv lists built from the registry's template (never a shell string); every parameter has a
declared type; input files have plain relative names (no dot-names, so a job cannot plant a settings file) and size limits; a job
expires after a maximum age (replays) and may not claim to be from the future; a profile run names exactly one command and says what it
does. Nothing here runs a command, and the lookups of the machine (which programs exist) are passed in so they can be tested.
"""
import os
import re
import shutil
from datetime import datetime

from .pkgspec import check_packages
from .registry import BASELINE_DENY, PLACEHOLDER
from .util import SAFE_PATH, now
from .words import plural

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{3,63}$")
FILE_NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*(/[A-Za-z0-9_][A-Za-z0-9_.-]*)*")
PROFILE_DENY = set(BASELINE_DENY)          # a profile run is an arbitrary command, so privilege tools are refused outright (policy can add more)
MAX_AGE_S = 86400
MAX_FUTURE_S = 300


class Reject(Exception):
    """A job that must not run. The message is shown to the requester, so it never includes anything from the machine."""


def check_id(jid, filename_id=None):
    if not isinstance(jid, str) or not ID_RE.match(jid):
        raise Reject("bad job id")
    if filename_id is not None and jid != filename_id:
        raise Reject("id missing or does not match filename")
    return jid


def expired(job, max_age_s=MAX_AGE_S, clock=now):
    """True when a job's own creation time is older than the limit. The ONE definition, used by the worker (it refuses such a job) and by your computer (it lists it as expired and will not approve it).
    A job with no readable time is not called expired here: the worker rejects that for its own reason."""
    try:
        created = datetime.fromisoformat(str(job["created"]).replace("Z", "+00:00"))
    except Exception:
        return False
    return created.tzinfo is not None and (clock() - created).total_seconds() > max_age_s


def check_age(job, max_age_s=MAX_AGE_S, clock=now):
    try:
        created = datetime.fromisoformat(str(job["created"]).replace("Z", "+00:00"))
    except Exception:
        raise Reject("missing or invalid 'created' timestamp")
    if created.tzinfo is None:
        raise Reject("'created' needs a timezone (use UTC, e.g. 2026-01-01T00:00:00Z)")
    age = (clock() - created).total_seconds()
    if age > max_age_s:
        raise Reject(f"job expired: it is more than {plural(max_age_s // 3600, 'hour')} old when the worker looked at it, so it is not run; send it again")
    if age < -MAX_FUTURE_S:
        raise Reject("job timestamp is in the future")


def check_param(name, spec, value):
    if "enum" in spec:
        if value not in spec["enum"]:
            raise ValueError(f"{name}: must be one of {spec['enum']}")
    elif "int" in spec:
        lo, hi = spec["int"]
        if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
            raise ValueError(f"{name}: must be an integer in [{lo}, {hi}]")
    elif "regex" in spec:
        if not isinstance(value, str) or not re.fullmatch(spec["regex"], value):
            raise ValueError(f"{name}: does not match the allowed pattern")
    else:
        raise ValueError(f"{name}: registry spec has no type")
    return str(value)


def check_policy_params(values, policy):
    """Refuse any parameter value (or profile argument) that matches a policy pattern. Raises ValueError with the policy's own reason."""
    for value in values:
        for rx, why in (policy or {}).get("deny_params", ()):
            if rx.search(str(value)):
                raise ValueError(f"not allowed: {why}")


def build_argv(cls, params, policy=None):
    """The command to run: the class's template with each {placeholder} replaced by a validated parameter. Always a list."""
    specs = cls.get("params", {}) or {}
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    extra = set(params) - set(specs)
    if extra:
        raise ValueError(f"unknown params: {sorted(extra)}")
    vals = {}
    for name, spec in specs.items():
        if name in params:
            vals[name] = check_param(name, spec, params[name])
            check_policy_params([vals[name]], policy)
        elif not spec.get("optional"):
            raise ValueError(f"missing param: {name}")
    argv = []
    for tok in cls["run"]:
        m = PLACEHOLDER.match(tok)
        if not m:
            argv.append(tok)
        elif m.group(1) in vals:
            argv.append(vals[m.group(1)])
        elif (specs.get(m.group(1)) or {}).get("optional"):
            continue
        else:
            raise ValueError(f"template uses undeclared param {m.group(1)}")
    return argv


def check_files(files, cls, limits):
    """Optional input files: small text files written into the job's empty work folder. Names are relative and may not start a path
    component with '.', so a job can't plant .claude/settings.json and the like. `limits` is the registry's limits (limits_of)."""
    if files is None:
        return {}
    if not cls.get("accepts_files"):
        raise ValueError("this job class does not accept files")
    fl = limits["files"]
    if not isinstance(files, dict) or len(files) > fl["max_count"]:
        raise ValueError(f"files must be an object with at most {fl['max_count']} entries")
    total = 0
    for name, content in files.items():
        if not isinstance(name, str) or len(name) > 200 or not FILE_NAME_RE.fullmatch(name):
            raise ValueError(f"bad file name: {name!r}")
        if not isinstance(content, str) or len(content) > fl["max_file_kb"] * 1000:
            raise ValueError(f"file {name!r}: content must be text under {fl['max_file_kb']} KB")
        total += len(content)
    if total > fl["max_total_kb"] * 1000:
        raise ValueError(f"files total over {fl['max_total_kb']} KB")
    return files


def job_timeout(cls, job):
    kind = cls.get("kind", "command")
    if kind == "profile":
        cap = int(cls.get("max_timeout_s", 1800))
        t = job.get("timeout_s", cap)
        return t if isinstance(t, int) and not isinstance(t, bool) and 10 <= t <= cap else cap
    return int(cls.get("timeout_s", 600 if kind == "claude" else 60))


def build_profile(job, cls, limits, find_program=None, is_executable=None, policy=None):
    """Validate a profile-run job: one exact command (argv list), optional small input files, optional timeout.
    Returns (argv, files, meta). `find_program` and `is_executable` default to looking at this machine."""
    find_program = find_program or (lambda name: shutil.which(name, path=SAFE_PATH))
    is_executable = is_executable or (lambda path: os.access(path, os.X_OK))
    argv = job.get("argv")
    if not isinstance(argv, list) or not 1 <= len(argv) <= 40 or \
            not all(isinstance(a, str) and 0 < len(a) <= 500 and "\0" not in a for a in argv):
        raise ValueError("argv must be a list of 1-40 non-empty strings")
    first = argv[0]
    pkgs = check_packages(job.get("packages"))
    denied = PROFILE_DENY | set((policy or {}).get("deny_commands", ()))
    if os.path.basename(first) in denied:
        raise ValueError(f"{os.path.basename(first)} is not allowed in a profile run")
    check_policy_params(argv[1:], policy)
    if "/" in first:
        if pkgs:
            raise ValueError("with packages, give the command by name (the program comes from the throwaway folder)")
        if not (first.startswith("/") and is_executable(first)):
            raise ValueError("a command with a path must be an absolute path to an executable")
    elif not pkgs and not find_program(first):
        raise ValueError(f"{first!r} is not installed on the worker (looked in {SAFE_PATH}); "
                         "if a package provides it, list the package (packages: ['apt:<name>'])")
    meta = {"packages": pkgs} if pkgs else {}
    for k, limit, required in (("what", 400, True), ("expect", 300, True), ("why", 300, False),
                               ("side_effects", 300, False), ("watch_for", 300, False)):
        v = job.get(k, "")
        if not isinstance(v, str) or len(v) > limit or (required and not v.strip()):
            raise ValueError(f"{k}: " + (f"required, text of at most {limit} characters" if required
                                          else f"text of at most {limit} characters"))
        if v:
            meta[k] = v
    est = {}
    for k, required in (("est_time_s", True), ("est_mem_mb", True), ("est_cpu_cores", False), ("est_disk_mb", False)):
        v = job.get(k)
        if v is None:
            if required:
                raise ValueError(f"{k}: required (your honest estimate, as a number)")
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1_000_000:
            raise ValueError(f"{k}: must be a number from 0 to 1000000")
        est[k[4:]] = v
    meta["estimates"] = est
    flags = job.get("flags", {})
    if not isinstance(flags, dict) or len(flags) > 20 or not all(
            isinstance(a, str) and isinstance(b, str) and 0 < len(a) <= 60 and len(b) <= 200 for a, b in flags.items()):
        raise ValueError("flags must map each flag to a short explanation (at most 20)")
    if flags:
        meta["flags"] = flags
    t = job.get("timeout_s")
    if t is not None:
        cap = int(cls.get("max_timeout_s", 1800))
        if isinstance(t, bool) or not isinstance(t, int) or not 10 <= t <= cap:
            raise ValueError(f"timeout_s must be an integer from 10 to {cap}")
    return argv, check_files(job.get("files"), cls, limits), meta
