"""Validation of a job file: everything a requester controls is checked here before anything runs.

Rules: ids are short and plain; commands are argv lists built from the registry's template (never a shell string); every parameter has a
declared type; input files have names that stay inside the job folder (and no dot-names for a class whose tool reads settings from its folder) and size limits; a job
expires after a maximum age (replays) and may not claim to be from the future; a profile run names exactly one command and says what it
does. Nothing here runs a command, and the lookups of the machine (which programs exist) are passed in so they can be tested.
"""
import os
import re
import shutil
from datetime import datetime

from . import refusals
from .pkgspec import check_packages
from .registry import BASELINE_DENY, PLACEHOLDER
from .util import SAFE_PATH, now
from .words import plural

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{3,63}$")
PROFILE_DENY = set(BASELINE_DENY)          # a profile run is an arbitrary command, so privilege tools are refused outright (policy can add more)
MAX_AGE_S = 86400
MAX_FUTURE_S = 300


Reject = refusals.Reject                  # the one class (it lives with the catalog); kept under this name for the code that already imports it


def check_id(jid, filename_id=None):
    if not isinstance(jid, str) or not ID_RE.match(jid):
        raise refusals.refuse("job.id.invalid")
    if filename_id is not None and jid != filename_id:
        raise refusals.refuse("job.id.mismatch")
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
        raise refusals.refuse("job.created.invalid")
    if created.tzinfo is None:
        raise refusals.refuse("job.created.no_timezone")
    age = (clock() - created).total_seconds()
    if age > max_age_s:
        raise refusals.refuse("job.expired", hours=plural(max_age_s // 3600, "hour"))
    if age < -MAX_FUTURE_S:
        raise refusals.refuse("job.created.future")


def check_param(name, spec, value):
    if "enum" in spec:
        if value not in spec["enum"]:
            raise refusals.refuse("param.enum", name=name, options=spec["enum"])
    elif "int" in spec:
        lo, hi = spec["int"]
        if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
            raise refusals.refuse("param.int", name=name, low=lo, high=hi)
    elif "regex" in spec:
        if not isinstance(value, str) or not re.fullmatch(spec["regex"], value):
            raise refusals.refuse("param.regex", name=name)
    else:
        raise refusals.refuse("param.untyped", name=name)
    return str(value)


def check_policy_params(values, policy):
    """Refuse any parameter value (or profile argument) that matches a policy pattern. Raises ValueError with the policy's own reason."""
    for value in values:
        for rx, why in (policy or {}).get("deny_params", ()):
            if rx.search(str(value)):
                raise refusals.refuse("param.policy", reason=why)


def build_argv(cls, params, policy=None):
    """The command to run: the class's template with each {placeholder} replaced by a validated parameter. Always a list."""
    specs = cls.get("params", {}) or {}
    if not isinstance(params, dict):
        raise refusals.refuse("job.params.not_object")
    extra = set(params) - set(specs)
    if extra:
        raise refusals.refuse("param.unknown", names=sorted(extra))
    vals = {}
    for name, spec in specs.items():
        if name in params:
            vals[name] = check_param(name, spec, params[name])
            check_policy_params([vals[name]], policy)
        elif not spec.get("optional"):
            raise refusals.refuse("param.missing", name=name)
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
            raise refusals.refuse("template.undeclared", name=m.group(1))
    return argv


def file_name_problem(name, kind="command"):
    """None when an attached file's name is fine, else (catalog code, reason). A name is a path inside the job's work folder, so only what could leave it is refused: absolute, '..', empty parts,
    control characters, a backslash, a silly length. A leading dot is refused only for a class whose tool reads its settings from its working folder (an AI tool, by kind)."""
    if not isinstance(name, str) or not name:
        return "files.name.path", "it is empty"
    parts = name.split("/")
    if len(name) > 1024 or any(len(p) > 255 for p in parts):
        return "files.name.path", "it is too long"
    if "\\" in name or any(ord(c) < 32 or ord(c) == 127 for c in name):
        return "files.name.path", "it has a control character or a backslash"
    if name.startswith("/") or any(p in ("", ".", "..") for p in parts):
        return "files.name.path", "it must be a relative path inside the job's folder"
    if kind == "claude" and any(p.startswith(".") for p in parts):
        return "files.name.hidden", ""
    return None


def check_files(files, cls, limits):
    """Optional input files: small text files written into the job's empty work folder. A name is a path inside the work folder
    (see file_name_problem): it is refused only for what could leave the folder, and for a leading dot where the class's tool reads settings from the folder. `limits` is the registry's limits (limits_of)."""
    if files is None:
        return {}
    if not cls.get("accepts_files"):
        raise refusals.refuse("files.not_accepted")
    fl = limits["files"]
    if not isinstance(files, dict) or len(files) > fl["max_count"]:
        raise refusals.refuse("files.too_many", limit=fl["max_count"])
    total = 0
    for name, content in files.items():
        problem = file_name_problem(name, cls.get("kind", "command"))
        if problem:
            raise refusals.refuse(problem[0], name=name, reason=problem[1])
        if not isinstance(content, str) or len(content) > fl["max_file_kb"] * 1000:
            raise refusals.refuse("files.too_big", name=name, limit=fl["max_file_kb"])
        total += len(content)
    if total > fl["max_total_kb"] * 1000:
        raise refusals.refuse("files.total_too_big", limit=fl["max_total_kb"])
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
        raise refusals.refuse("profile.argv.shape")
    first = argv[0]
    pkgs = check_packages(job.get("packages"))
    denied = PROFILE_DENY | set((policy or {}).get("deny_commands", ()))
    if os.path.basename(first) in denied:
        raise refusals.refuse("profile.command.denied", program=os.path.basename(first))
    check_policy_params(argv[1:], policy)
    if "/" in first:
        if pkgs:
            raise refusals.refuse("profile.packages.path")
        if not (first.startswith("/") and is_executable(first)):
            raise refusals.refuse("profile.command.path_invalid")
    elif not pkgs and not find_program(first):
        raise refusals.refuse("profile.command.not_installed", program=first, looked=SAFE_PATH)
    meta = {"packages": pkgs} if pkgs else {}
    for k, limit, required in (("what", 400, True), ("expect", 300, True), ("why", 300, False),
                               ("side_effects", 300, False), ("watch_for", 300, False)):
        v = job.get(k, "")
        if not isinstance(v, str) or len(v) > limit or (required and not v.strip()):
            raise refusals.refuse("profile.field.invalid", field=k, rule=(f"required, text of at most {limit} characters" if required else f"text of at most {limit} characters"))
        if v:
            meta[k] = v
    est = {}
    for k, required in (("est_time_s", True), ("est_mem_mb", True), ("est_cpu_cores", False), ("est_disk_mb", False)):
        v = job.get(k)
        if v is None:
            if required:
                raise refusals.refuse("profile.estimate.required", field=k)
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1_000_000:
            raise refusals.refuse("profile.estimate.range", field=k)
        est[k[4:]] = v
    meta["estimates"] = est
    flags = job.get("flags", {})
    if not isinstance(flags, dict) or len(flags) > 20 or not all(
            isinstance(a, str) and isinstance(b, str) and 0 < len(a) <= 60 and len(b) <= 200 for a, b in flags.items()):
        raise refusals.refuse("profile.flags.invalid")
    if flags:
        meta["flags"] = flags
    t = job.get("timeout_s")
    if t is not None:
        cap = int(cls.get("max_timeout_s", 1800))
        if isinstance(t, bool) or not isinstance(t, int) or not 10 <= t <= cap:
            raise refusals.refuse("profile.timeout.range", cap=cap)
    return argv, check_files(job.get("files"), cls, limits), meta
