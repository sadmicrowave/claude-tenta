"""Running one command safely: no network unless the class needs it, a fixed program search path, hard limits, and measurements.

Everything about the machine (temperature sensors, /proc, the unshare probe) is behind an argument so this runs and is tested on any
computer; the defaults read a Linux machine. Nothing here knows an install's paths or names.

Isolation FAILS CLOSED: if the machine cannot build an empty network namespace a no-network job is refused, never run with the network.
"""
import json
import os
import resource
import signal
import subprocess
import time
from pathlib import Path

from .jail import Refused, Unshare, isolate, netns_flags  # noqa: F401  (re-exported)
from .util import SAFE_PATH

MAX_TIMEOUT = 3600
FSIZE_LIMIT = 200 * 1024 * 1024        # no single file a job writes (including its output) may pass this


class Killed(Exception):
    """Raised inside a running job's watcher when the worker is told to stop (SIGTERM)."""


def install_term_handler():
    def _on_term(signum, frame):
        raise Killed()
    signal.signal(signal.SIGTERM, _on_term)


# ---- process groups -------------------------------------------------------------------------------------------------------------
def kill_group(pgid, sig=signal.SIGKILL):
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def stop_group(p, term_wait=10):
    """Stop a Popen'd process group: SIGTERM, then SIGKILL. Returns (stdout, stderr) captured so far."""
    kill_group(p.pid, signal.SIGTERM)
    try:
        return p.communicate(timeout=term_wait)
    except subprocess.TimeoutExpired:
        kill_group(p.pid, signal.SIGKILL)
        try:
            return p.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            return "", ""


def run_group(argv, timeout, **kw):
    """subprocess.run with a hard timeout that kills the WHOLE process group (git -> ssh, and so on). Exit code 124 means it timed out."""
    p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True, **kw)
    try:
        out, err = p.communicate(timeout=timeout)
        return subprocess.CompletedProcess(argv, p.returncode, out, err)
    except subprocess.TimeoutExpired:
        kill_group(p.pid)
        try:
            out, err = p.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            out, err = "", ""
        return subprocess.CompletedProcess(argv, 124, out, (err or "") + f"\ntimed out after {timeout}s")


# ---- the machine ----------------------------------------------------------------------------------------------------------------
class LinuxSensors:
    """Temperature, memory and per-job memory from /sys and /proc. Missing sources give None or 0, never an error."""

    def __init__(self, hwmon="/sys/class/hwmon", proc="/proc", sensor_name="coretemp"):
        self.hwmon, self.proc, self.sensor_name = Path(hwmon), Path(proc), sensor_name
        try:
            self.page_mb = os.sysconf("SC_PAGE_SIZE") / 1048576
        except (ValueError, OSError):
            self.page_mb = 4096 / 1048576

    def logged_in(self):
        """The accounts that have a login session on this computer right now (`who`), or [] when it cannot be read."""
        try:
            r = run_group(["who"], 5)
        except Exception:
            return []
        return sorted({line.split()[0] for line in (r.stdout or "").splitlines() if line.split()}) if getattr(r, "returncode", 1) == 0 else []

    def temp(self):
        """Hottest CPU core in C, or None if the sensor is not readable."""
        temps = []
        for d in self.hwmon.glob("hwmon*") if self.hwmon.is_dir() else []:
            try:
                if (d / "name").read_text().strip() != self.sensor_name:
                    continue
            except OSError:
                continue
            for f in d.glob("temp*_input"):
                try:
                    temps.append(int(f.read_text()) / 1000)
                except (OSError, ValueError):
                    pass
        return max(temps) if temps else None

    def meminfo(self):
        """(total_mb, available_mb); (0, None) if unreadable."""
        d = {}
        try:
            for line in (self.proc / "meminfo").read_text().splitlines():
                k, v = line.split(":", 1)
                d[k] = int(v.split()[0]) // 1024
        except (OSError, ValueError):
            pass
        return d.get("MemTotal", 0), d.get("MemAvailable")

    def group_rss_mb(self, pgid):
        """Resident memory (MB) summed over every process in the job's process group or session."""
        total = 0.0
        for d in self.proc.iterdir() if self.proc.is_dir() else []:
            if not d.name.isdigit():
                continue
            try:
                rest = (d / "stat").read_text().rsplit(")", 1)[1].split()
                if int(rest[2]) != pgid and int(rest[3]) != pgid:
                    continue
                total += int((d / "statm").read_text().split()[1]) * self.page_mb
            except (OSError, ValueError, IndexError):
                pass
        return total


def tree_mb(path, max_entries=20000):
    """Disk used under a folder, in MB (a bounded walk; symlinks are not followed)."""
    total, n = 0, 0
    stack = [str(path)]
    while stack and n < max_entries:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    n += 1
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return total / 1048576


def read_text(path, limit=2_000_000):
    try:
        with open(path, "rb") as f:
            return f.read(limit).decode("utf-8", "replace")
    except OSError:
        return ""


# ---- network isolation ----------------------------------------------------------------------------------------------------------
# The jails live in jail.py; these names stay importable from here.


def class_network(cls):
    """True if jobs of this class may use the network. Default: no (only `claude` jobs need it unless declared)."""
    return bool(cls.get("network", cls.get("kind", "command") == "claude"))


# ---- the environment a job sees ---------------------------------------------------------------------------------------------------
def build_env(home, claude=False, extra_env=None, api_key=None, oauth_token=None):
    """System directories only on PATH, so nothing a job or a package step writes into the worker's home can shadow a system program.
    A `claude` job also gets the worker's ~/.local/bin (where claude is installed), LAST so it can never shadow anything, and the API key.
    A class's own variables (extra_env) are added; its PATH, if any, goes in front (a throwaway tools folder, set deliberately)."""
    env = {"PATH": SAFE_PATH + (f":{home}/.local/bin" if claude else ""), "HOME": str(home), "LANG": "C.UTF-8"}
    if extra_env:
        env.update({k: v for k, v in extra_env.items() if k != "PATH"})
        if extra_env.get("PATH"):
            env["PATH"] = extra_env["PATH"] + ":" + env["PATH"]
    if claude and api_key:
        env["ANTHROPIC_API_KEY"] = api_key             # claude jobs only; commands never see it
    if claude and oauth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token   # a claude.ai subscription token (from `claude setup-token`); claude jobs only
    return env


def _child_limits():
    resource.setrlimit(resource.RLIMIT_FSIZE, (FSIZE_LIMIT, FSIZE_LIMIT))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


# ---- running and watching one command ---------------------------------------------------------------------------------------------
def run_command(argv, timeout, wd, *, home, claude=False, marker=None, limits=None, network=True, extra_env=None, api_key=None, oauth_token=None,
                sensors=None, require_netns=True, jail=None):
    """Run one job command in its own process group and watch it.

    Returns (status, exit_code, output, metrics, note). status: ok | error | timeout | killed | limit.
    `limits` may hold max_rss_mb / max_temp_c / max_disk_mb; going over stops the job (status "limit").
    Metrics are measured for every job, so results show what each one really cost. `claude` marks a job whose stdout is a JSON document
    with a `result` field."""
    limits, sensors = limits or {}, sensors or LinuxSensors()
    env = build_env(home, claude, extra_env, api_key, oauth_token)
    timeout = min(int(timeout), MAX_TIMEOUT)
    try:
        launch = (jail or Unshare()).wrap(argv, network=network, wd=wd, home=home, claude=claude, env=env, require=require_netns)
    except Refused as e:
        return "error", None, "", {}, f"not started: {e}"
    outp, errp = Path(f"{wd}.out"), Path(f"{wd}.err")   # beside the work dir, so the job never sees them
    total_mb, avail_mb = sensors.meminfo()
    m = {"mem_total_mb": total_mb, "mem_available_start_mb": avail_mb, "start_temp_c": sensors.temp(),
         "load_start": round(os.getloadavg()[0], 2)}
    peak_rss = peak_temp = disk = 0.0
    ru0 = resource.getrusage(resource.RUSAGE_CHILDREN)
    t0 = time.time()
    p, reason, note, rc = None, None, "", None
    try:
        with open(outp, "wb") as fo, open(errp, "wb") as fe:
            # own session and process group so a timeout or `kill` takes down the whole tree, not just the top process
            p = subprocess.Popen(launch, stdout=fo, stderr=fe, stdin=subprocess.DEVNULL, cwd=str(wd), env=env,
                                 start_new_session=True, preexec_fn=_child_limits)
        if marker is not None:
            try:
                mk = json.loads(marker.read_text())
                mk["child_pgid"] = p.pid
                marker.write_text(json.dumps(mk))
            except Exception:
                pass
        tick = 0
        while True:
            elapsed = time.time() - t0
            try:
                rc = p.wait(timeout=0.25 if elapsed < 2 else 1.0)
                break
            except subprocess.TimeoutExpired:
                pass
            tick += 1
            elapsed = time.time() - t0
            rss = sensors.group_rss_mb(p.pid)
            peak_rss = max(peak_rss, rss)
            if tick % 3 == 0:
                t = sensors.temp()
                if t is not None:
                    peak_temp = max(peak_temp, t)
                    if limits.get("max_temp_c") and t > limits["max_temp_c"]:
                        reason, note = "limit", f"core temperature {t:.0f}C went over the {limits['max_temp_c']}C limit"
                if limits.get("max_disk_mb") or tick % 12 == 0:
                    disk = max(disk, tree_mb(wd))
                    if limits.get("max_disk_mb") and disk > limits["max_disk_mb"]:
                        reason, note = "limit", f"disk use {disk:.0f} MB went over the {limits['max_disk_mb']} MB limit"
            if limits.get("max_rss_mb") and rss > limits["max_rss_mb"]:
                reason, note = "limit", f"memory {rss:.0f} MB went over the {limits['max_rss_mb']} MB limit"
            if not reason and elapsed > timeout:
                reason = "timeout"
            if reason:
                stop_group(p)
                break
        status = reason or ("ok" if rc == 0 else "error")
        if p.returncode is not None:
            rc = p.returncode
    except Killed:
        if p:
            stop_group(p)
        status, rc = "killed", None
    except FileNotFoundError as e:
        return "error", None, f"not found: {e}", {}, ""
    wall = time.time() - t0
    ru1 = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = (ru1.ru_utime + ru1.ru_stime) - (ru0.ru_utime + ru0.ru_stime)
    t_end = sensors.temp()
    if t_end is not None:
        peak_temp = max(peak_temp, t_end)
    peak_rss = max(peak_rss, ru1.ru_maxrss / 1024 if ru0.ru_maxrss == 0 else 0)
    disk = max(disk, tree_mb(wd))
    m.update(wall_s=round(wall, 2), cpu_s=round(cpu, 2), cores_busy=round(cpu / wall, 2) if wall >= 0.5 else None,
             peak_rss_mb=round(peak_rss), peak_temp_c=round(peak_temp) if peak_temp else None,
             disk_mb=round(disk, 1), load_end=round(os.getloadavg()[0], 2))
    stdout, stderr = read_text(outp), read_text(errp)
    outp.unlink(missing_ok=True)
    errp.unlink(missing_ok=True)
    out = stdout + stderr
    if claude:
        from . import ledger
        used = ledger.usage_of(stdout)
        if used:
            m["claude"] = used                           # turns and notional cost the job reported, for the spend ledger and the result
    if claude and status == "ok":
        try:
            out = json.loads(stdout).get("result", stdout)
        except Exception:
            out = stdout
    elif claude:
        out = stdout
    return status, (rc if status in ("ok", "error") else None), out, m, note
