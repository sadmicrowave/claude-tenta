"""Running a job inside a jail: no network (unless its class needs it), no view of the worker's home folder or the install's configuration, no
capabilities, no new privileges, its own process tree, and only the folders it needs.

Three jails, chosen by `isolation.method` in the configuration:

  bwrap     bubblewrap (the default). A distribution-maintained tool that Flatpak also uses. On Ubuntu 24.04 and newer its AppArmor profile lets it
            create the namespace while the kernel keeps unprivileged user namespaces restricted for everyone else; the sandboxed command gets no
            capabilities and cannot create another namespace. Needs no root service and no extra systemd units.
  unshare   the original method (`unshare --user --net`). Network only: it cannot hide files, and on Ubuntu 24.04 and newer it works only if the
            system-wide user-namespace restriction is switched off. Kept for machines without bubblewrap.
  none      nothing is available. Every job is refused (fail closed) unless the configuration explicitly allows unsafe running.

Everything about the machine is behind an argument so the argument list a jail builds can be tested on any computer.
"""
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

from .util import SAFE_PATH

# Read-only system folders a command needs to run. Nothing else of the machine is visible inside: not /var, not /opt, not /home, not /root, not /run.
SYSTEM_DIRS = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32", "/etc")
# Files that identify this machine; empty inside the jail.
MASKED = ("/etc/hostname", "/etc/machine-id")
MIN_BWRAP = (0, 4, 1)          # earlier versions have known sandbox escapes (CVE-2017-5226, CVE-2020-5291)


class Refused(Exception):
    """A job that must not start because isolation is unavailable. The message says why in plain words."""


# ---- the original method: unshare ------------------------------------------------------------------------------------------------
_NETNS = []


def netns_flags(refresh=False):
    """Flags that run a command in its own, empty network namespace, or None if this machine cannot (probed once)."""
    if _NETNS and not refresh:
        return _NETNS[0]
    found = None
    for flags in (["--user", "--net", "--map-current-user"], ["--user", "--net", "--map-root-user"]):
        try:
            r = subprocess.run(["unshare", *flags, "cat", "/proc/net/dev"], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            break
        ifaces = [line.split(":")[0].strip() for line in r.stdout.splitlines()[2:] if ":" in line]
        if r.returncode == 0 and ifaces == ["lo"]:
            found = flags
            break
    _NETNS[:] = [found]
    return found


def isolate(argv, network, flags=None, require=True):
    """Wrap argv so it runs with no network, unless the class needs it. FAILS CLOSED: if no namespace can be built the job is refused
    (Refused), unless `require` is False, which runs it WITH the network and is for throwaway tests only."""
    if network:
        return argv
    flags = netns_flags() if flags is None else flags
    if flags:
        return ["unshare", *flags, "--", *argv]
    if require:
        raise Refused("this machine cannot isolate jobs from the network, so a no-network job will not run "
                      "(check that `unshare --user --net` works for the worker user)")
    return argv


# ---- the jails ---------------------------------------------------------------------------------------------------------------------
class Jail:
    name = "none"
    isolates = False                    # True only where a job really cannot reach the network or the rest of the machine (a namespace or bubblewrap jail), which is what lets a profile run start without approval

    def ready(self):
        return False

    def problem(self):
        return "no isolation method is available"

    def describe(self):
        return "no isolation"

    def wrap(self, argv, *, network, wd, home, env=None, require=True):
        if require:
            raise Refused(f"this machine cannot isolate jobs ({self.problem()}), so no job will run")
        return list(argv)


class NoJail(Jail):
    """Nothing is available (or, in tests, nothing is wanted)."""


class WindowsJobObject(Jail):
    """Windows has no namespaces or bind mounts: a job runs in a Job Object of its own (the runner puts it there), which stops its whole process tree and ends it with the worker, and nothing more. Files and the network
    are NOT isolated: a Windows worker is Special Use by design (docs/design/WINDOWS-SPECIAL-USE.md), and the registry already refuses a class that needs no network there."""
    name = "windows-job"
    isolates = False

    def ready(self):
        return True

    def problem(self):
        return None

    def describe(self):
        return "a Job Object per job (the whole process tree is stopped together); no file or network isolation on Windows"

    def wrap(self, argv, *, network, wd, home, env=None, require=True):
        return list(argv)


class Unshare(Jail):
    name = "unshare"
    isolates = True

    def __init__(self, flags=None):
        self._flags = flags                       # None: probe this machine; False or []: known to be unavailable; a list: use these

    def flags(self):
        return netns_flags() if self._flags is None else (self._flags or None)

    def ready(self):
        return bool(self.flags())

    def problem(self):
        return None if self.ready() else "`unshare --user --net` does not work for the worker account"

    def describe(self):
        return "no network for jobs (unshare); jobs can still see the worker's files"

    def wrap(self, argv, *, network, wd, home, env=None, require=True):
        return isolate(list(argv), network, flags=self.flags() or False, require=require)


def _under(path, folders):
    return any(path == d or path.startswith(d.rstrip("/") + "/") for d in folders)


def extra_ro_paths(env, home, wd):
    """Folders named by the job's own search paths (a throwaway tools folder for packages) that are not system folders: bound read-only."""
    out = []
    for key in ("PATH", "PYTHONPATH", "NODE_PATH"):
        for p in (env or {}).get(key, "").split(":"):
            if p.startswith("/") and not _under(p, SYSTEM_DIRS) and not _under(p, [str(home)]) and not _under(p, [str(wd)]) and p not in out and os.path.exists(p):
                out.append(p)
    return out


def parse_version(text):
    import re
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    return tuple(int(x or 0) for x in m.groups()) if m else None


class Bwrap(Jail):
    name = "bwrap"
    isolates = True

    def __init__(self, path=None, hide=(), run=subprocess.run, which=shutil.which, isdir=os.path.isdir):
        self._path, self.hide, self._run, self._which, self._isdir = path, tuple(hide), run, which, isdir
        self._checked = None

    @property
    def path(self):
        return self._path or self._which("bwrap", path="/usr/bin:/bin")

    def args(self, argv, *, network, wd, home, env=None):
        """The complete command line. Order matters: the root is empty, then each mount is added, later mounts over earlier ones."""
        wd, home = str(wd), str(home)
        a = [self.path or "bwrap", "--die-with-parent", "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--hostname", "localhost",
             "--cap-drop", "ALL"]
        if not network:
            a.append("--unshare-net")
        for d in SYSTEM_DIRS:
            a += ["--ro-bind-try", d, d]
        for h in self.hide:                       # the install's configuration sits under /etc: an empty folder takes its place
            if _under(h, SYSTEM_DIRS) and self._isdir(h):          # a folder that is not there has nothing to hide (and bwrap cannot make one inside a read-only bind)
                a += ["--tmpfs", h]
        for f in MASKED:
            a += ["--ro-bind-try", "/dev/null", f]
        a += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--tmpfs", home]
        if network:
            a += ["--ro-bind-try", "/run/systemd/resolve", "/run/systemd/resolve"]       # /etc/resolv.conf points here
        for p in extra_ro_paths(env, home, wd):
            a += ["--ro-bind", p, p]
        # No --new-session: the worker already starts every job in its own session with no terminal, so there is nothing to inject input into,
        # and a second session would hide the job's processes from the worker's own process-group accounting and kill.
        return a + ["--bind", wd, wd, "--chdir", wd, "--"] + list(argv)

    def _version_problem(self):
        try:
            r = self._run([self.path, "--version"], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as e:
            return f"bubblewrap could not be run: {e}"
        v = parse_version(r.stdout + r.stderr)
        if v and v < MIN_BWRAP:
            return f"bubblewrap {'.'.join(map(str, v))} is too old (it has known sandbox escapes); {'.'.join(map(str, MIN_BWRAP))} or newer is required"
        return None

    def probe_argv(self, wd):
        return self.args(["/usr/bin/cat", "/proc/net/dev"], network=False, wd=wd, home="/nonexistent-arm-home")

    def problem(self, refresh=False):
        """None if jobs can be jailed here, else the reason in plain words. Probed once: a real jailed command is run and must see only loopback."""
        if self._checked is not None and not refresh:
            return self._checked or None
        found = self._find_problem()
        self._checked = found or ""
        return found

    def _find_problem(self):
        p = self.path
        if not p:
            return "bubblewrap is not installed (sudo apt-get install bubblewrap)"
        try:
            if os.stat(p).st_mode & stat.S_ISUID:
                return "bubblewrap is installed setuid, which this tool refuses (it has had privilege-escalation bugs in that mode)"
        except OSError as e:
            return f"cannot look at {p}: {e.strerror}"
        bad = self._version_problem()
        if bad:
            return bad
        wd = tempfile.mkdtemp(prefix="arm-jail-probe-")
        try:
            r = self._run(self.probe_argv(wd), capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError) as e:
            return f"a test jail could not be started: {e}"
        finally:
            shutil.rmtree(wd, ignore_errors=True)
        names = [line.split(":")[0].strip() for line in (r.stdout or "").splitlines()[2:] if ":" in line]
        if r.returncode != 0:
            last = ((r.stderr or "").strip().splitlines() or ["no message"])[-1][:200]
            return f"a test jail could not be created ({last})"
        if names != ["lo"]:
            return "a test jail still had a network interface"
        return None

    def ready(self):
        return self.problem() is None

    def describe(self):
        return "jobs run in a bubblewrap jail: no network, no capabilities, and no view of the worker's home or configuration"

    def wrap(self, argv, *, network, wd, home, env=None, require=True):
        bad = self.problem()
        if bad:
            if require:
                raise Refused(f"this machine cannot isolate jobs ({bad}), so no job will run")
            return list(argv)
        return self.args(argv, network=network, wd=wd, home=home, env=env)


def from_config(cfg, **kw):
    """The jail the configuration asks for. The install's configuration folder is hidden inside it."""
    if os.name == "nt":
        return WindowsJobObject()
    if cfg.isolation.method == "unshare":
        return Unshare()
    return Bwrap(hide=[cfg.paths.config], **kw)
