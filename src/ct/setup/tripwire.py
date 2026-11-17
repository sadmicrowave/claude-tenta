"""ct-setup tripwire-test: prove, on a real machine, that reading a decoy credential locks the worker down.

The tripwire is a chain: auditd logs every read of a decoy file, a root timer (canary-watch) finds the log entry, and the worker locks itself down and alerts.
Nothing short of a machine with a real kernel audit can test the first link (a container has none), so the end-to-end test skips it. This runs it for real:
it reads one decoy file as root, waits for the lockdown, checks it, and then unlocks. Run it as root on the worker; it asks first because it really does
lock the worker for a minute and really sends the security alert.
"""
import json
import shlex
import time


def decoys(host, cfg):
    try:
        data = json.loads(host.read(f"{cfg.path('state')}/canary.json") or "{}")
    except ValueError:
        return []
    return [f for f in data.get("files", []) if isinstance(f, str) and f.startswith("/")]


def worker_cli(cfg, *args):
    return ["runuser", "-u", cfg.server.worker_user, "--", "env", f"PYTHONPATH={cfg.paths.code}/lib", f"CT_CONFIG={cfg.paths.config}/ct.toml",
            "/usr/bin/python3", "-m", "ct.cli", *args]


def run(host, cfg, *, say=print, confirm=None, wait_s=150, step_s=5, sleep=time.sleep, clock=time.monotonic):
    """Returns (ok, reason). Always unlocks at the end if the test caused the lock."""
    state = f"{cfg.path('state')}/lockdown"
    status = f"{cfg.path('run_dir')}/canary-watch.json"
    if host.run(["systemctl", "is-active", "auditd"]).out.strip() != "active":
        return False, "auditd is not running, so nothing can log a read (is this a container? it needs a full machine with a kernel audit)"
    try:
        watching = json.loads(host.read(status) or "{}")
    except ValueError:
        watching = {}
    if not watching.get("monitoring"):
        return False, f"canary-watch is not monitoring ({watching.get('reason') or 'no status yet'})"
    files = decoys(host, cfg)
    if not files:
        return False, "no decoy files are planted yet (the worker plants them on its first pass)"
    if host.exists(state):
        return False, "the worker is already locked down; unlock it first (ct-admin unlock)"
    say(f"  This reads one decoy file ({files[0]}) as root. The worker will lock itself down within a minute or two and send its security alert, then this unlocks it.")
    if confirm is not None and not confirm("Run the tripwire test now?"):
        return False, "you answered no: nothing was done"
    caused = True
    try:
        host.run(["cat", files[0]])
        deadline = clock() + wait_s
        while clock() < deadline:
            if host.exists(state):
                break
            sleep(step_s)
        else:
            caused = False
            return False, f"the worker did not lock down within {wait_s} s of the read (auditd logged it? is the canary-watch timer running?)"
        say("  The worker locked itself down: the tripwire works. Unlocking it again.")
        return True, "reading a decoy file locked the worker down"
    finally:
        if caused and host.exists(state):
            r = host.run(worker_cli(cfg, "unlock"))
            if not r.ok or host.exists(state):
                say(f"  WARNING: it could not be unlocked here ({r.out.strip()[-120:]}). Unlock it yourself: ct-admin unlock")
