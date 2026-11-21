"""Windows process control for the worker's runtime: one Job Object per job, so the whole tree can be stopped at once, measured, and never outlives the worker (docs/design/WINDOWS-JOB-RUNNER.md).

Reached only through `ct.plat`. ctypes is imported inside the functions, never at module load, so this file imports on any computer; calling it anywhere but Windows raises OSError.
"""
import os
import sys

CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
CREATE_SUSPENDED = 0x00000004
_PROCESS_SUSPEND_RESUME, _PROCESS_QUERY_LIMITED, _PROCESS_VM_READ, _PROCESS_SET_QUOTA, _PROCESS_TERMINATE = 0x0800, 0x1000, 0x0010, 0x0100, 0x0001
_KILL_ON_JOB_CLOSE = 0x2000
_JOBS = {}                                                    # pid of the job's first process -> its Job Object handle


def _need_windows():
    if sys.platform != "win32":
        raise OSError("Windows process control was called on a computer that is not Windows")


def _api():
    """The kernel32 functions used, with their argument types set (a 64-bit handle must not be truncated to 32 bits)."""
    import ctypes
    from ctypes import wintypes as w
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    H = ctypes.c_void_p
    k.CreateJobObjectW.restype, k.CreateJobObjectW.argtypes = H, [H, w.LPCWSTR]
    k.SetInformationJobObject.restype, k.SetInformationJobObject.argtypes = w.BOOL, [H, ctypes.c_int, H, w.DWORD]
    k.QueryInformationJobObject.restype, k.QueryInformationJobObject.argtypes = w.BOOL, [H, ctypes.c_int, H, w.DWORD, H]
    k.AssignProcessToJobObject.restype, k.AssignProcessToJobObject.argtypes = w.BOOL, [H, H]
    k.TerminateJobObject.restype, k.TerminateJobObject.argtypes = w.BOOL, [H, w.UINT]
    k.OpenProcess.restype, k.OpenProcess.argtypes = H, [w.DWORD, w.BOOL, w.DWORD]
    k.CloseHandle.restype, k.CloseHandle.argtypes = w.BOOL, [H]
    k.K32GetProcessMemoryInfo.restype, k.K32GetProcessMemoryInfo.argtypes = w.BOOL, [H, H, w.DWORD]
    return ctypes, w, k


class _Structs:
    def __init__(self, ctypes, w):
        c = ctypes

        class IO(c.Structure):
            _fields_ = [(n, c.c_ulonglong) for n in ("r_ops", "w_ops", "o_ops", "r_bytes", "w_bytes", "o_bytes")]

        class Basic(c.Structure):
            _fields_ = [("PerProcessUserTimeLimit", c.c_int64), ("PerJobUserTimeLimit", c.c_int64), ("LimitFlags", w.DWORD), ("MinimumWorkingSetSize", c.c_size_t),
                        ("MaximumWorkingSetSize", c.c_size_t), ("ActiveProcessLimit", w.DWORD), ("Affinity", c.c_size_t), ("PriorityClass", w.DWORD), ("SchedulingClass", w.DWORD)]

        class Extended(c.Structure):
            _fields_ = [("Basic", Basic), ("Io", IO), ("ProcessMemoryLimit", c.c_size_t), ("JobMemoryLimit", c.c_size_t), ("PeakProcessMemoryUsed", c.c_size_t), ("PeakJobMemoryUsed", c.c_size_t)]

        class Accounting(c.Structure):
            _fields_ = [("TotalUserTime", c.c_int64), ("TotalKernelTime", c.c_int64), ("ThisPeriodTotalUserTime", c.c_int64), ("ThisPeriodTotalKernelTime", c.c_int64),
                        ("TotalPageFaultCount", w.DWORD), ("TotalProcesses", w.DWORD), ("ActiveProcesses", w.DWORD), ("TotalTerminatedProcesses", w.DWORD)]

        class MemCounters(c.Structure):
            _fields_ = [("cb", w.DWORD), ("PageFaultCount", w.DWORD), ("PeakWorkingSetSize", c.c_size_t), ("WorkingSetSize", c.c_size_t), ("QuotaPeakPagedPoolUsage", c.c_size_t),
                        ("QuotaPagedPoolUsage", c.c_size_t), ("QuotaPeakNonPagedPoolUsage", c.c_size_t), ("QuotaNonPagedPoolUsage", c.c_size_t), ("PagefileUsage", c.c_size_t),
                        ("PeakPagefileUsage", c.c_size_t)]

        class MemStatus(c.Structure):
            _fields_ = [("dwLength", w.DWORD), ("dwMemoryLoad", w.DWORD), ("ullTotalPhys", c.c_ulonglong), ("ullAvailPhys", c.c_ulonglong), ("ullTotalPageFile", c.c_ulonglong),
                        ("ullAvailPageFile", c.c_ulonglong), ("ullTotalVirtual", c.c_ulonglong), ("ullAvailVirtual", c.c_ulonglong), ("ullAvailExtendedVirtual", c.c_ulonglong)]
        self.Extended, self.Accounting, self.MemCounters, self.MemStatus = Extended, Accounting, MemCounters, MemStatus


def adopt(p):
    """Put the (suspended) process `p` and everything it starts into a new Job Object that is killed when the worker lets go of it, then let it run."""
    _need_windows()
    c, w, k = _api()
    s = _Structs(c, w)
    job = k.CreateJobObjectW(None, None)
    if not job:
        raise OSError(c.get_last_error(), "CreateJobObject failed")
    info = s.Extended()
    info.Basic.LimitFlags = _KILL_ON_JOB_CLOSE
    if not k.SetInformationJobObject(job, 9, c.byref(info), c.sizeof(info)):
        raise OSError(c.get_last_error(), "SetInformationJobObject failed")
    h = k.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE | _PROCESS_SUSPEND_RESUME, False, p.pid)
    if not h:
        raise OSError(c.get_last_error(), "OpenProcess failed")
    try:
        if not k.AssignProcessToJobObject(job, h):
            raise OSError(c.get_last_error(), "AssignProcessToJobObject failed")
        c.WinDLL("ntdll").NtResumeProcess(c.c_void_p(h))
    finally:
        k.CloseHandle(h)
    _JOBS[p.pid] = job


def terminate(pid):
    """End every process of the job that `pid` started. A job that is gone is not an error."""
    job = _JOBS.get(pid)
    if job:
        _, _, k = _api()
        k.TerminateJobObject(job, 1)


def pids(pid):
    """The process ids now in the job."""
    job = _JOBS.get(pid)
    if not job:
        return []
    c, w, k = _api()
    n = 256

    class List(c.Structure):
        _fields_ = [("Assigned", w.DWORD), ("InList", w.DWORD), ("Ids", c.c_size_t * n)]
    buf = List()
    if not k.QueryInformationJobObject(job, 3, c.byref(buf), c.sizeof(buf), None):
        return []
    return list(buf.Ids)[:buf.InList]


def rss_mb(pid):
    """Working-set memory (MB) summed over every process in the job."""
    c, w, k = _api()
    s = _Structs(c, w)
    total = 0
    for q in pids(pid):
        h = k.OpenProcess(_PROCESS_QUERY_LIMITED | _PROCESS_VM_READ, False, q)
        if not h:
            continue
        try:
            m = s.MemCounters()
            m.cb = c.sizeof(m)
            if k.K32GetProcessMemoryInfo(h, c.byref(m), m.cb):
                total += m.WorkingSetSize
        finally:
            k.CloseHandle(h)
    return total / 1048576


def cpu_s(pid):
    """CPU seconds (user + kernel) the job has used, including processes that already ended."""
    job = _JOBS.get(pid)
    if not job:
        return 0.0
    c, w, k = _api()
    a = _Structs(c, w).Accounting()
    if not k.QueryInformationJobObject(job, 1, c.byref(a), c.sizeof(a), None):
        return 0.0
    return (a.TotalUserTime + a.TotalKernelTime) / 1e7


def release(pid):
    """Let go of the job (this kills anything of it still running, by the kill-on-close flag) and return its CPU seconds."""
    used = cpu_s(pid)
    job = _JOBS.pop(pid, None)
    if job:
        _, _, k = _api()
        k.CloseHandle(job)
    return used


def cmdline(pid):
    """The command line of a process of this account, as text, or '' when it is not there or cannot be read (NtQueryInformationProcess, class 60: no process memory is read)."""
    _need_windows()
    c, w, k = _api()

    class UnicodeString(c.Structure):
        _fields_ = [("Length", c.c_ushort), ("MaximumLength", c.c_ushort), ("Buffer", c.c_void_p)]
    h = k.OpenProcess(_PROCESS_QUERY_LIMITED, False, pid)
    if not h:
        return ""
    try:
        nt = c.WinDLL("ntdll")
        nt.NtQueryInformationProcess.argtypes = [c.c_void_p, c.c_int, c.c_void_p, w.ULONG, c.POINTER(w.ULONG)]
        nt.NtQueryInformationProcess.restype = c.c_long
        size = 1024
        for _ in range(3):
            buf = c.create_string_buffer(size)
            need = w.ULONG(0)
            status = nt.NtQueryInformationProcess(h, 60, buf, size, c.byref(need)) & 0xFFFFFFFF
            if status == 0:
                us = UnicodeString.from_buffer(buf)
                return c.wstring_at(us.Buffer, us.Length // 2) if us.Buffer else ""
            if status in (0xC0000004, 0x80000005, 0xC0000023) and need.value > size:          # buffer too small: ask again with the size it named
                size = need.value
                continue
            return ""
        return ""
    finally:
        k.CloseHandle(h)


def memory():
    """(total_mb, available_mb) of this computer."""
    _need_windows()
    c, w, k = _api()
    m = _Structs(c, w).MemStatus()
    m.dwLength = c.sizeof(m)
    if not c.WinDLL("kernel32").GlobalMemoryStatusEx(c.byref(m)):
        return 0, None
    return int(m.ullTotalPhys // 1048576), int(m.ullAvailPhys // 1048576)
