"""Prove a service's sandbox still lets jobs work, BEFORE that unit file is kept.

Run as root by the update tool. It takes the sandbox settings out of the unit file, starts short-lived transient units that carry exactly
those settings and run as the worker user, and tries each thing a job needs: start the worker, build a no-network namespace, write its
state and home folders, reach GitHub over SSH, read the temperature sensors, log, and read the package database (and, when a privileged
thermal helper is installed, use sudo for it). Nothing here changes the machine.

Exit 0: every check passed or was skipped for a stated reason. 1: something a job needs is blocked by the sandbox. 2: it could not run.
"""
import os
import re
import shutil
import subprocess
import sys
import time

from . import config as _config
from . import ui as _ui
from .setup.report import sink as _sink

# Only these settings are copied from the unit: the sandbox and the worker's environment, never ExecStart, Type or the timeouts.
COPY = re.compile(r"^(User|Environment|EnvironmentFile|Protect\w+|Private\w+|Restrict\w+|ReadWritePaths|ReadOnlyPaths|InaccessiblePaths|"
                  r"LockPersonality|NoNewPrivileges|SystemCallArchitectures|SystemCallFilter|CapabilityBoundingSet|MemoryDenyWriteExecute|UMask)=(.*)$")
GIT_SSH = "ssh -o BatchMode=yes -o ConnectTimeout=15"


def unit_properties(text):
    """The [Service] settings of a unit file worth carrying into a test unit, as ['Key=value', ...]."""
    props, in_service = [], False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            in_service = line == "[Service]"
            continue
        m = COPY.match(line) if in_service else None
        if m:
            props.append(f"{m.group(1)}={m.group(2)}")
    return props


def only_loopback(proc_net_dev):
    """True if /proc/net/dev lists no interface but lo."""
    names = [l.split(":")[0].strip() for l in proc_net_dev.splitlines()[2:] if ":" in l]
    return names == ["lo"]


def checks(home, state, queue_repo, helper=None, have=os.path.exists, python="/usr/bin/python3", jail_argv=None, store="GitHub"):
    """[(name, argv, expect)]; expect(returncode, output) -> bool. An entry (name, None, reason) is skipped with that reason."""
    ok0 = lambda rc, out: rc == 0                                                       # noqa: E731
    out = [
        ("the worker starts", [python, "-m", "armada.cli", "status"], ok0),
        ("a no-network jail can be built", jail_argv or ["/usr/bin/unshare", "--user", "--net", "--map-current-user", "cat", "/proc/net/dev"],
         lambda rc, o: rc == 0 and only_loopback(o)),
        ("state and home folders are writable",
         ["/bin/sh", "-c", f"touch {state}/.sandbox-test && rm {state}/.sandbox-test && touch {home}/.sandbox-test && rm {home}/.sandbox-test"], ok0),
        ("a private /tmp works", ["/bin/sh", "-c", "touch /tmp/.sandbox-test && rm /tmp/.sandbox-test"], ok0),
        ("the sandbox is in force (the root file system is read-only)",
         ["/bin/sh", "-c", "awk '$5==\"/\" {print $6}' /proc/self/mountinfo | tr ',' '\\n' | grep -qx ro"], ok0),
        ("logging works", ["/usr/bin/logger", "-t", "arm-sandbox-test", "sandbox check"], ok0),
        ("the package database can be read", ["/usr/bin/apt-cache", "show", "--no-all-versions", "bash"], ok0),
    ]
    if have(os.path.join(str(queue_repo), ".git")):
        out.append((f"{store} can be reached" + (" over SSH" if store == "GitHub" else ""), ["/usr/bin/git", "-c", f"core.sshCommand={GIT_SSH}", "-C", str(queue_repo), "ls-remote", "--exit-code", "--heads", "origin"], ok0))
    else:
        out.append((f"{store} can be reached" + (" over SSH" if store == "GitHub" else ""), None, f"no queue clone at {queue_repo}"))
    if helper and have(helper):
        # the helper with no argument prints its usage and exits 2: that proves sudo let the worker in, without touching any hardware
        out.append(("sudo works for the thermal helper", ["/usr/bin/sudo", "-n", helper], lambda rc, o: rc == 2 and "usage" in o.lower()))
    else:
        out.append(("sudo works for the thermal helper", None, "no privileged thermal helper is installed"))
    if have("/sys/class/hwmon"):
        out.append(("temperature sensors can be read", ["/bin/sh", "-c", "cat /sys/class/hwmon/hwmon*/temp*_input >/dev/null 2>&1 || cat /sys/devices/platform/*/temp*_input >/dev/null"], ok0))
    else:
        out.append(("temperature sensors can be read", None, "no hwmon sensors on this machine"))
    return out


def run_in_sandbox(props, argv, timeout=60):
    cmd = ["systemd-run", "--quiet", "--wait", "--collect", "--pipe", "--service-type=exec", f"--unit=arm-sandbox-test-{os.getpid()}-{int(time.time() * 1000) % 100000}"]
    for p in props:
        cmd += ["-p", p]
    r = subprocess.run(cmd + ["--"] + argv, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr)


def run_all(props, runner=run_in_sandbox, plan=None):
    """[(name, 'ok'|'fail'|'skip', detail)]"""
    results = []
    for name, argv, expect in plan:
        if argv is None:
            results.append((name, "skip", expect))
            continue
        try:
            rc, out = runner(props, argv)
        except (OSError, subprocess.SubprocessError) as e:
            results.append((name, "fail", f"could not run: {e}"))
            continue
        good = expect(rc, out)
        results.append((name, "ok" if good else "fail", "" if good else (out.strip().splitlines() or [f"exit {rc}"])[-1][:200]))
    return results


def test_unit(unit_text, cfg, runner=run_in_sandbox, have=os.path.exists, helper=None, out=None):
    """Run the checks against a unit's sandbox settings. Returns 0 (all good), 1 (something is blocked), 2 (not testable here)."""
    out = out or _sink()
    props = unit_properties(unit_text)
    if not any(p.startswith(("Protect", "Private")) for p in props):
        out("that unit has no sandbox settings; nothing to test")
        return 0
    jail_argv = None
    if cfg.isolation.method == "bwrap":                 # prove the jail still works under the unit's own sandbox settings
        from . import jail
        jail_argv = jail.Bwrap(hide=[cfg.paths.config]).probe_argv(cfg.path("state"))
    plan = checks(cfg.paths.worker_home, cfg.path("state"), cfg.path("queue_clone"), helper=helper, have=have, jail_argv=jail_argv, store=getattr(cfg, "transport", None) and __import__("armada.transport", fromlist=["x"]).store_for(cfg) or "GitHub")
    results = run_all(props, runner=runner, plan=plan)
    width = max(len(n) for n, _, _ in results)
    mark = {"ok": "OK  ", "fail": "FAIL", "skip": "SKIP"}
    for name, state, detail in results:
        out(f"  {mark[state]}  {name:<{width}}  {detail}".rstrip())
    failed = [n for n, s, _ in results if s == "fail"]
    if failed:
        out(f"\nThe sandbox blocks something jobs need ({len(failed)} failed). The unit file should not be kept.")
        return 1
    out("\nThe sandbox lets jobs work.")
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cfg = _config.load()
    unit = argv[argv.index("--unit") + 1] if "--unit" in argv else f"/etc/systemd/system/{cfg.identity.name}-step.service"
    try:
        text = open(unit).read()
    except OSError as e:
        _ui.say("Cannot read the unit file", ("Unit", unit), ("Said", str(e)), tag="ERROR", color=_ui.RED)
        return 2
    if not shutil.which("systemd-run"):
        _ui.say("The sandbox cannot be tested", "systemd-run is not available on this machine.", tag="ERROR", color=_ui.RED)
        return 2
    return test_unit(text, cfg)


if __name__ == "__main__":
    sys.exit(main())
