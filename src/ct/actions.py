"""Root-level actions: the one way a job may cause something to happen as root.

A job never gets `sudo`. An ACTION is a fixed program and typed parameters, written in the registry you sign (or in a pack), and a class of kind `privileged`
points at it. A small root service (ct.helper) runs it only for a job whose exact file carries a signed approval from the administrator's key, in its own
tight systemd sandbox, with the program checked by owner, mode and a pinned SHA-256 each time. This module is what both sides share: what a valid action
looks like, how its arguments are built, and how the program on disk is checked.

    actions:
      fan-profile:
        run: [/usr/local/sbin/fan-profile, "{profile}"]
        params: {profile: {enum: [quiet, balanced, performance]}}
        sha256: <the program's SHA-256>        # `ct-worker action-hash <path>` prints it
        writable: [/etc/mbpfan.conf]           # the only places it may write (everything else is read-only to it)
        network: false                         # no network unless it says so
        timeout_s: 30
"""
import hashlib
import os
import re
import stat

from . import jobs
from .registry import BASELINE_DENY, PLACEHOLDER, _param_ok

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
ALLOWED_DIRS = ("/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin")
# Programs that run whatever they are told to: an action must be a purpose-built program, not a way to run a command line chosen by a job.
GENERIC = frozenset({"sh", "bash", "dash", "zsh", "fish", "csh", "tcsh", "ksh", "busybox", "env", "xargs", "python", "python3", "perl", "ruby", "node", "php", "lua",
                     "awk", "gawk", "sed", "find", "nsenter", "chroot", "unshare", "setpriv", "systemd-run", "systemctl", "script", "strace", "gdb", "tee", "dd",
                     "mount", "umount", "nice", "nohup", "timeout", "time", "eval", "exec", "ssh", "scp", "curl", "wget", "nc", "ncat", "socat", "git", "make"})
KEYS = {"run", "params", "sha256", "writable", "network", "timeout_s", "description"}
WRITABLE_RE = re.compile(r"^/[A-Za-z0-9._/-]*$")
FORBIDDEN_WRITABLE = ("/", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/boot", "/etc", "/var", "/proc", "/sys", "/dev", "/root", "/home", "/opt", "/run")


def basename(path):
    return os.path.basename(path)


def validate(actions, deny_commands=()):
    """Problems (plain words) with an `actions:` mapping; [] when it is fine. `deny_commands` is the policy's list of refused programs."""
    if actions is None:
        return []
    if not isinstance(actions, dict):
        return ["actions: must be a mapping"]
    deny = set(BASELINE_DENY) | set(deny_commands or ())
    errs = []
    for name, a in actions.items():
        w = f"action {name!r}"
        if not isinstance(name, str) or not NAME_RE.match(name):
            errs.append(f"{w}: name must be lowercase letters, digits and dashes")
        if not isinstance(a, dict):
            errs.append(f"{w}: must be a mapping")
            continue
        for k in sorted(set(a) - KEYS):
            errs.append(f"{w}: unknown setting {k!r} (known: {', '.join(sorted(KEYS))})")
        run = a.get("run")
        if not isinstance(run, list) or not run or not all(isinstance(t, str) and t for t in run):
            errs.append(f"{w}: run must be a non-empty list of strings")
            continue
        prog = run[0]
        if not prog.startswith("/") or os.path.normpath(prog) != prog:
            errs.append(f"{w}: the program must be a plain absolute path (got {prog!r})")
        elif os.path.dirname(prog) not in ALLOWED_DIRS:
            errs.append(f"{w}: the program must be in one of {', '.join(ALLOWED_DIRS)} (got {os.path.dirname(prog)})")
        if basename(prog) in GENERIC:
            errs.append(f"{w}: {basename(prog)} runs whatever it is given, so it cannot be a root-level action; use a program made for this one job")
        if basename(prog) in deny:
            errs.append(f"{w}: {basename(prog)} is refused by policy")
        params = a.get("params") or {}
        if not isinstance(params, dict):
            errs.append(f"{w}: params must be a mapping")
            params = {}
        for pn, spec in params.items():
            if not _param_ok(spec):
                errs.append(f"{w}: param {pn!r} needs exactly one of enum / int [lo, hi] / regex")
        for tok in run[1:]:
            m = PLACEHOLDER.match(tok)
            if m and m.group(1) not in params:
                errs.append(f"{w}: run uses {{{m.group(1)}}} but no such param is declared")
            elif not m and "{" in tok:
                errs.append(f"{w}: a placeholder must be a whole argument like \"{{name}}\", not part of one")
        if not isinstance(a.get("sha256"), str) or not SHA_RE.match(a["sha256"]):
            errs.append(f"{w}: sha256 is required (the program's SHA-256 in lowercase hex): the program on disk must be exactly the one you approved")
        writable = a.get("writable", [])
        if not isinstance(writable, list) or not all(isinstance(p, str) and WRITABLE_RE.match(p) and ".." not in p for p in writable):
            errs.append(f"{w}: writable must be a list of absolute paths")
        else:
            for p in writable:
                if p.rstrip("/") in FORBIDDEN_WRITABLE or p == "/":
                    errs.append(f"{w}: writable may not be {p} (that is far more than one program needs; name the file or folder it writes)")
        if "network" in a and not isinstance(a["network"], bool):
            errs.append(f"{w}: network must be true or false")
        t = a.get("timeout_s", 30)
        if isinstance(t, bool) or not isinstance(t, int) or not 1 <= t <= 600:
            errs.append(f"{w}: timeout_s must be a whole number from 1 to 600")
    return errs


def build_argv(action, params):
    """The exact argument list for one run: the template with each placeholder replaced by a validated parameter. Raises ValueError."""
    return jobs.build_argv(action, params)


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def check_executable(path, expect_sha, lstat=os.lstat, sha=sha256_of):
    """(ok, why): is the program on disk the one that was approved, and could nobody but root have changed it? It must be a regular file (not a link) owned by root,
    not writable by group or others, in a chain of folders that are the same, and its SHA-256 must equal the pinned one. Checked again at every run."""
    try:
        st = lstat(path)
    except OSError as e:
        return False, f"{path} cannot be read ({e.strerror})"
    if not stat.S_ISREG(st.st_mode):
        return False, f"{path} is not a regular file (a link or a folder cannot be an action's program)"
    if st.st_uid != 0 or st.st_mode & 0o022:
        return False, f"{path} must be owned by root and writable by nobody else"
    if not st.st_mode & 0o111:
        return False, f"{path} is not executable"
    d = os.path.dirname(path)
    while d and d != "/":
        try:
            ds = lstat(d)
        except OSError as e:
            return False, f"{d} cannot be read ({e.strerror})"
        if stat.S_ISLNK(ds.st_mode) or ds.st_uid != 0 or ds.st_mode & 0o022:
            return False, f"{d} must be a real folder owned by root and writable by nobody else (somebody else could swap the program)"
        d = os.path.dirname(d)
    try:
        actual = sha(path)
    except OSError as e:
        return False, f"{path} cannot be read ({e.strerror})"
    if actual != expect_sha:
        return False, f"{path} is not the program that was approved (its SHA-256 is {actual[:12]}..., not {expect_sha[:12]}...): re-approve it if it was changed on purpose"
    return True, ""


def sandbox_properties(action, state_dir):
    """The systemd properties the action runs under: read-only everywhere except what it declared, private /tmp, no home folders, no network unless declared."""
    props = ["ProtectSystem=strict", "ProtectHome=yes", "PrivateTmp=yes", "NoNewPrivileges=yes", "LockPersonality=yes", "RestrictSUIDSGID=yes", "MemoryMax=256M",
             f"RuntimeMaxSec={int(action.get('timeout_s', 30))}", "RestrictAddressFamilies=AF_UNIX" + (" AF_INET AF_INET6" if action.get("network") else "")]
    if not action.get("network"):
        props.append("PrivateNetwork=yes")
    writable = list(action.get("writable") or [])
    if writable:
        props.append("ReadWritePaths=" + " ".join(writable))
    return props


def inspect(path):
    """Facts about a program for `action-hash` and `ct-admin action add`: where it is, its SHA-256 and whether nobody but root could have changed it.
    {'path', 'sha256', 'ok', 'problem'}; 'problem' says why a program cannot be an action (a place it must not live, unreadable, writable by others)."""
    if not os.path.isabs(path) or os.path.normpath(path) != path or os.path.dirname(path) not in ALLOWED_DIRS:
        return {"path": path, "sha256": None, "ok": False, "problem": f"it must be a plain absolute path in one of: {', '.join(ALLOWED_DIRS)}"}
    try:
        digest = sha256_of(path)
    except OSError as e:
        return {"path": path, "sha256": None, "ok": False, "problem": f"cannot be read ({e.strerror or e})"}
    ok, why = check_executable(path, digest)
    return {"path": path, "sha256": digest, "ok": ok, "problem": None if ok else why}
