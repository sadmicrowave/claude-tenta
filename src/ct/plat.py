"""The seam between the worker and the operating system.

Everything the worker does that is specific to POSIX (Linux and macOS) lives in this one module: starting a process in a group of its own and stopping the whole group, reading the load and the free
disk, finding out whether a process id is a job, the file lock, the signals, the limits of a child, its CPU and memory use. The rest of the worker calls these and never the operating system's own
functions, so a port to another platform (see docs/design/WINDOWS-SPECIAL-USE.md) has one place to write, not a hunt through the code. A source-wide test (`tests/test_platform_seam.py`) fails if a
module of the worker's runtime reaches past this one.

Today there is one implementation, POSIX. Where Windows would differ, `_not_yet()` says so plainly rather than quietly doing something else.
"""
import os
import signal
import sys
from collections import namedtuple
from pathlib import Path

IS_WINDOWS = os.name == "nt"
TERM = signal.CTRL_BREAK_EVENT if IS_WINDOWS else signal.SIGTERM      # ask a process to stop (Windows: Ctrl-Break to its process group)
KILL = signal.SIGTERM if IS_WINDOWS else signal.SIGKILL               # make it stop (Windows: end the whole Job Object)
_CPU_DONE = [0.0]                                                      # Windows: CPU seconds of the jobs that finished (see finish)

def name():
    """The platform this worker runs on, as it publishes it: linux, macos or windows."""
    return "windows" if IS_WINDOWS else "macos" if sys.platform == "darwin" else "linux"


ChildUsage = namedtuple("ChildUsage", "cpu_s maxrss_kb")


def _not_yet(what):
    raise NotImplementedError(f"{what} is not written for Windows yet (docs/design/WINDOWS-SPECIAL-USE.md)")


# ---- processes -----------------------------------------------------------------------------------------------------------------------
def new_session(hold=False):
    """Keyword arguments for subprocess.Popen that put the child in a session and process group of its own, so the whole tree can be stopped at once.
    `hold=True` (Windows) creates it suspended: the caller MUST then call adopt(p), which puts it in its Job Object and lets it run. POSIX ignores it."""
    if IS_WINDOWS:
        from . import winproc
        return {"creationflags": winproc.CREATE_NEW_PROCESS_GROUP | winproc.CREATE_NO_WINDOW | (winproc.CREATE_SUSPENDED if hold else 0)}
    return {"start_new_session": True}


def adopt(p):
    """Windows: put the held process in a Job Object of its own and let it run. POSIX: nothing to do (the session already groups it)."""
    if IS_WINDOWS:
        from . import winproc
        winproc.adopt(p)


def finish(p):
    """The job's process has ended: let go of what was kept for it (Windows: close its Job Object, which ends anything of it still running, and keep its CPU time)."""
    if IS_WINDOWS:
        from . import winproc
        _CPU_DONE[0] += winproc.release(p.pid)


def group_rss_mb(pid):
    """Memory (MB) in use by the job's whole process tree: on Windows, its Job Object (on POSIX the runner's sensors read /proc)."""
    from . import winproc
    return winproc.rss_mb(pid)


def _stop_windows(pid, sig):
    """KILL ends the whole Job Object; anything else is Ctrl-Break to the process group (a program may catch it and clean up)."""
    from . import winproc
    if sig == KILL:
        winproc.terminate(pid)
        return
    try:
        os.kill(pid, signal.CTRL_BREAK_EVENT)
    except OSError:
        pass


def kill_group(pgid, sig=KILL):
    """Send `sig` to every process in the group; a group that is gone, or one that is not ours, is not an error."""
    if IS_WINDOWS:
        _stop_windows(pgid, sig)
        return
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def send_signal(pid, sig):
    if IS_WINDOWS:
        _stop_windows(pid, sig)
        return
    os.kill(pid, sig)


def on_terminate(handler):
    """Call `handler(signum, frame)` when the worker is told to stop."""
    if IS_WINDOWS:
        signal.signal(signal.SIGBREAK, handler)                # Ctrl-Break, which is what stopping the worker sends
        signal.signal(signal.SIGTERM, handler)
        return
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
        return ChildUsage(_CPU_DONE[0], 0)                      # the peak memory of a job is measured by the runner's polling, not here
    import resource
    ru = resource.getrusage(resource.RUSAGE_CHILDREN)
    return ChildUsage(ru.ru_utime + ru.ru_stime, ru.ru_maxrss)


def process_cmdline(pid):
    """The command line of a process as text, or '' when it is not there or cannot be read."""
    if IS_WINDOWS:
        from . import winproc
        try:
            return winproc.cmdline(pid)
        except OSError:
            return ""
    try:
        return Path(f"/proc/{pid}/cmdline").read_text()
    except Exception:
        return ""


# ---- the machine ---------------------------------------------------------------------------------------------------------------------
def loadavgs():
    """(1, 5, 15 minute) load averages."""
    if IS_WINDOWS:
        return (0.0, 0.0, 0.0)                                  # Windows has no load average: reported as 0 until the worker publishes something real
    return os.getloadavg()


def loadavg():
    return loadavgs()[0]


def disk_free_gb(path):
    """Free space under `path` in GB (one decimal), or None."""
    if IS_WINDOWS:
        import shutil
        try:
            return round(shutil.disk_usage(path).free / 1e9, 1)
        except OSError:
            return None
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
def flock_fd(fd, shared=False, wait=True):
    """Lock an open descriptor, shared or exclusive, waiting or not. True when taken. Closing the descriptor releases it. A Windows worker has no shared package environments (Special Use only), so there it always succeeds."""
    if IS_WINDOWS:
        return True
    import fcntl
    try:
        fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | (0 if wait else fcntl.LOCK_NB))
        return True
    except OSError:
        return False


def try_lock(f):
    """Take an exclusive lock on the open file `f` without waiting. True when taken, False when another process holds it. It is released when the file is closed."""
    if IS_WINDOWS:
        import msvcrt
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)           # the first byte; another process holding it makes this raise at once
            return True
        except OSError:
            return False
    import fcntl
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False
