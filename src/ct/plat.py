"""The seam between the worker and the operating system.

Everything the worker does that is specific to POSIX (Linux and macOS) lives in this one module: starting a process in a group of its own and stopping the whole group, reading the load and the free
disk, finding out whether a process id is a job, the file lock, the signals, the limits of a child, its CPU and memory use. The rest of the worker calls these and never the operating system's own
functions, so a port to another platform (see docs/design/WINDOWS-SPECIAL-USE.md) has one place to write, not a hunt through the code. A source-wide test (`tests/test_platform_seam.py`) fails if a
module of the worker's runtime reaches past this one.

Today there is one implementation, POSIX. Where Windows would differ, `_not_yet()` says so plainly rather than quietly doing something else.
"""
import os
import signal
import subprocess
from collections import namedtuple
from pathlib import Path

IS_WINDOWS = os.name == "nt"
TERM = signal.SIGTERM                                     # ask a process to stop
KILL = getattr(signal, "SIGKILL", signal.SIGTERM)         # make it stop

ChildUsage = namedtuple("ChildUsage", "cpu_s maxrss_kb")


def _not_yet(what):
    raise NotImplementedError(f"{what} is not written for Windows yet (docs/design/WINDOWS-SPECIAL-USE.md)")


# ---- processes -----------------------------------------------------------------------------------------------------------------------
def new_session():
    """Keyword arguments for subprocess.Popen that put the child in a session and process group of its own, so the whole tree can be stopped at once."""
    if IS_WINDOWS:
        _not_yet("a process group for a job")
    return {"start_new_session": True}


def kill_group(pgid, sig=KILL):
    """Send `sig` to every process in the group; a group that is gone, or one that is not ours, is not an error."""
    if IS_WINDOWS:
        _not_yet("stopping a process tree")
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def send_signal(pid, sig):
    if IS_WINDOWS:
        _not_yet("stopping a process")
    os.kill(pid, sig)


def on_terminate(handler):
    """Call `handler(signum, frame)` when the worker is told to stop."""
    signal.signal(TERM, handler)


def quiet_broken_pipe():
    """A command whose output is cut off (`... | head`) ends quietly instead of printing an error."""
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)


def child_limits(fsize_bytes):
    """A function for Popen's preexec_fn that limits what a child may write and turns core dumps off, or None where that does not apply."""
    if IS_WINDOWS:
        return None
    import resource

    def limit():
        resource.setrlimit(resource.RLIMIT_FSIZE, (fsize_bytes, fsize_bytes))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    return limit


def child_usage():
    """CPU seconds and the largest memory (KiB) of the children that have finished, so far."""
    if IS_WINDOWS:
        _not_yet("measuring a child's CPU and memory")
    import resource
    ru = resource.getrusage(resource.RUSAGE_CHILDREN)
    return ChildUsage(ru.ru_utime + ru.ru_stime, ru.ru_maxrss)


def process_cmdline(pid):
    """The command line of a process as text, or '' when it is not there or cannot be read."""
    try:
        return Path(f"/proc/{pid}/cmdline").read_text()
    except Exception:
        return ""


# ---- the machine ---------------------------------------------------------------------------------------------------------------------
def loadavgs():
    """(1, 5, 15 minute) load averages."""
    if IS_WINDOWS:
        _not_yet("the load average")
    return os.getloadavg()


def loadavg():
    return loadavgs()[0]


def disk_free_gb(path):
    """Free space under `path` in GB (one decimal), or None."""
    try:
        st = os.statvfs(path)
        return round(st.f_bavail * st.f_frsize / 1e9, 1)
    except (OSError, AttributeError):
        return None


def uptime_s():
    """Seconds since the machine started, or None."""
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def cpuinfo_text():
    """What the processor says about itself (Linux: /proc/cpuinfo), or '' when it cannot be read."""
    try:
        return Path("/proc/cpuinfo").read_text()
    except OSError:
        return ""


# ---- files ---------------------------------------------------------------------------------------------------------------------------
def try_lock(f):
    """Take an exclusive lock on the open file `f` without waiting. True when taken, False when another process holds it. It is released when the file is closed."""
    if IS_WINDOWS:
        _not_yet("a file lock")
    import fcntl
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False
