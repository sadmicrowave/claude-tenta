"""ct.helper: the root service that runs approved root-level actions. Started by systemd for each connection to <name>-helper.sock (socket activation).

The worker asks over a unix socket that only root and the worker account can open. Nothing the worker says is believed:

  * the request carries the exact job file and the signed approval for it; the helper checks the signature against the root-owned approvers file (the same key
    that approves jobs from the administrator's computer), that it names this job id and this exact file, and that it has not expired;
  * the job's class and its action come from the root-owned installed registry, never from the request, and the class must be privileged;
  * the parameters are checked against the action's own specification, and the argument list is built from the template (no shell, no other way in);
  * the program on disk is checked again: a regular file owned by root, writable by nobody else, in folders that are the same, with the pinned SHA-256;
  * each job runs at most once (its id is recorded before it runs);
  * it runs in its own systemd unit with a read-only system, a private /tmp, no home folders, no network unless the action declared it, only the paths it
    declared writable, a memory cap and a time limit.

So a job that has been taken over still cannot reach root: it has no signature of the administrator's. Output is returned to the worker, which passes it
through the same output guard as any job.
"""
import base64
import hashlib
import json
import os
import pwd
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

from . import actions, approvals, jobs, registry

MAX_REQUEST = 262144
MAX_OUTPUT = 20000


def sandbox_enforced(run=subprocess.run):
    """Does this machine's systemd really enforce the sandbox an action is given? Some (a container's) accept the settings and silently ignore them, which would
    make every promise in the action's definition untrue. The probe tries to write to /usr from inside the same kind of unit."""
    if os.environ.get("CT_HELPER_ALLOW_UNENFORCED_SANDBOX") == "1":              # for a test machine only; never set on a real worker
        return True
    try:
        r = run(["systemd-run", "--quiet", "--wait", "--pipe", "--collect", "--property=ProtectSystem=strict", "--property=PrivateNetwork=yes", "/bin/sh", "-c",
                 "if touch /usr/.ct-sandbox-probe 2>/dev/null; then rm -f /usr/.ct-sandbox-probe; echo OPEN; else echo ENFORCED; fi"],
                capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False
    return "ENFORCED" in (r.stdout or "")


def reply(status, note, **kw):
    return {"status": status, "note": note, **kw}


def state_dir(cfg):
    return Path(f"{cfg.paths.state_root}-helper")


def handle(request_text, cfg, peer_uid, *, run=subprocess.run, now=time.time, check_exe=actions.check_executable, worker_uid=None, log=lambda m: None, sandbox_check=None):
    """Decide whether to run, run it, and return a dict {status, exit_code, output, note}. Never raises."""
    try:
        worker_uid = pwd.getpwnam(cfg.server.worker_user).pw_uid if worker_uid is None else worker_uid
        if peer_uid != worker_uid:
            return reply("refused", "only the worker account may ask")
        try:
            req = json.loads(request_text)
            job_bytes = base64.b64decode(req["job_b64"], validate=True)
            approval = req["approval"]
            job = json.loads(job_bytes)
        except (ValueError, KeyError, TypeError):
            return reply("refused", "the request is not well formed")
        jid = job.get("id") if isinstance(job, dict) else None
        if not isinstance(jid, str) or not jobs.ID_RE.match(jid):
            return reply("refused", "the job has no valid id")
        digest = hashlib.sha256(job_bytes).hexdigest()
        ok, why = approvals.check(approval, jid, digest, cfg.path("approvers"), now=now())
        if not ok:
            log(f"refused {jid}: no valid signed approval ({why})")
            return reply("refused", f"this job has no valid signed approval: {why}")
        try:
            reg = registry.parse(Path(cfg.path("registry")).read_text())
        except Exception as e:
            return reply("refused", f"the installed registry cannot be read ({e})")
        cls = (reg.get("classes") or {}).get(job.get("class"))
        if not isinstance(cls, dict) or cls.get("kind") != "privileged":
            return reply("refused", f"{job.get('class')!r} is not a privileged class")
        action = (reg.get("actions") or {}).get(cls.get("action"))
        if not isinstance(action, dict):
            return reply("refused", f"the class names an action ({cls.get('action')!r}) that is not installed")
        try:
            argv = actions.build_argv(action, job.get("params") or {})
        except ValueError as e:
            return reply("refused", f"the parameters are not allowed: {e}")
        done = state_dir(cfg) / "done"
        done.mkdir(parents=True, exist_ok=True, mode=0o700)
        marker = done / jid
        if marker.exists():
            return reply("refused", "this job has already been run by the helper; a job runs at most once")
        ok, why = check_exe(argv[0], action["sha256"])
        if not ok:
            log(f"refused {jid}: {why}")
            return reply("refused", why)
        if not (sandbox_check or (lambda: sandbox_enforced(run)))():                                   # before the job is used up: fixing the machine lets it run
            log(f"refused {jid}: this machine's systemd does not enforce the sandbox")
            return reply("refused", "this machine's systemd does not enforce the sandbox an action runs in (a container often does not), so root-level actions are refused here")
        marker.write_text(json.dumps({"at": int(now()), "action": cls["action"], "argv": argv}))          # before it runs: a crash must not allow a second run
        timeout = int(action.get("timeout_s", 30))
        cmd = ["systemd-run", "--quiet", "--wait", "--pipe", "--collect", "--service-type=exec", f"--unit=ct-action-{jid}",
               *[f"--property={p}" for p in actions.sandbox_properties(action, state_dir(cfg))], "--", *argv]
        log(f"running {jid}: {' '.join(argv)}")
        try:
            r = run(cmd, capture_output=True, text=True, timeout=timeout + 20, stdin=subprocess.DEVNULL, cwd="/")
        except FileNotFoundError:
            return reply("error", "systemd-run is not available, so the action cannot be sandboxed and was not run")
        except subprocess.TimeoutExpired:
            return reply("timeout", f"the action did not finish in {timeout} s", exit_code=None, output="")
        out = ((r.stdout or "") + (r.stderr or ""))[-MAX_OUTPUT:]
        log(f"finished {jid}: exit {r.returncode}")
        return reply("ok" if r.returncode == 0 else "error", "" if r.returncode == 0 else f"the program exited with {r.returncode}", exit_code=r.returncode, output=out)
    except Exception as e:                                                         # the helper must answer, whatever happens
        return reply("error", f"the helper failed: {type(e).__name__}")


def read_all(conn, limit=MAX_REQUEST):
    data = b""
    while len(data) <= limit:
        chunk = conn.recv(65536)
        if not chunk:
            break
        data += chunk
    return data


def main():
    from . import config
    cfg = config.load()
    conn = socket.socket(fileno=os.dup(0))
    pid, uid, gid = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
    data = read_all(conn)
    if len(data) > MAX_REQUEST:
        resp = reply("refused", "the request is too large")
    else:
        resp = handle(data.decode("utf-8", "replace"), cfg, uid, log=lambda m: print(f"ct-helper: {m}", file=sys.stderr))
    conn.sendall(json.dumps(resp).encode())
    conn.shutdown(socket.SHUT_WR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
