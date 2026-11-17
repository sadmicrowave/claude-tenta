"""claude-update: keeps the `claude` tool the worker runs current. Run weekly by a systemd timer, as the worker account, outside any job's jail.

Jobs only ever run the tool inside a jail where its folder is read-only, so the tool can never update itself there; this timer is what keeps it patched.
Two kinds of install exist (see setup/server.py): the native program (updated with `claude update`) and, on processors too old for it, the last
plain-JavaScript release, which is pinned: newer releases need a newer processor, so there is nothing newer it can be updated to. The result is written
to <state>/claude-update.json and shown by `status`.
"""
import json
import os
import re
import subprocess
import time
from pathlib import Path

SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
RESULT_FILE = "claude-update.json"
TOKEN_FILE = "claude-token.json"           # <config>/claude-token.json: when the subscription token was stored (it lasts about a year)
TOKEN_LIFE_DAYS = 365
TOKEN_WARN_DAYS = 335


def tool_path(home):
    return Path(home) / ".local" / "bin" / "claude"


def kind_of(home):
    p = tool_path(home)
    if not p.exists():
        return "missing"
    real = os.path.realpath(p)
    if "/.local/share/claude/versions/" in real:
        return "native"
    if "/node_modules/" in real:
        return "legacy"
    return "unknown"


def _run(home):
    def run(argv):
        return subprocess.run(argv, capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL,
                              env={"HOME": str(home), "PATH": f"{SAFE_PATH}:{home}/.local/bin", "LANG": "C.UTF-8"})
    return run


def version_of(home, run):
    try:
        r = run([str(tool_path(home)), "--version"])
    except (OSError, subprocess.SubprocessError):
        return ""
    found = re.search(r"(\d+\.\d+\.\d+)", r.stdout or "")
    return found.group(1) if found else ""


def token_age_days(config_dir, now=None):
    """Whole days since the subscription token was stored, or None when that is not known (an api-key install, or one from before this was recorded)."""
    try:
        set_at = int(json.loads((Path(config_dir) / TOKEN_FILE).read_text())["set_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return max(0, int(((time.time() if now is None else now) - set_at) // 86400))


def token_state(age_days):
    """'ok', 'expiring' (renew soon) or 'expired'; None when unknown."""
    if age_days is None:
        return None
    return "expired" if age_days >= TOKEN_LIFE_DAYS else "expiring" if age_days >= TOKEN_WARN_DAYS else "ok"


def tool_info(home, run=None):
    """{kind, version} of the claude tool installed for the worker (kind: native, legacy, missing, unknown); version '' when it will not run."""
    run = run or _run(home)
    kind = kind_of(home)
    return {"kind": kind, "version": version_of(home, run) if kind != "missing" else ""}


def update(home, run=None, clock=time.time):
    """Returns {ts, kind, before, after, ok, note}. Never raises: a failed update is a result to report, not a crash."""
    run = run or _run(home)
    kind = kind_of(home)
    before = version_of(home, run) if kind != "missing" else ""
    res = {"ts": int(clock()), "kind": kind, "before": before, "after": before, "ok": True, "note": ""}
    if kind == "native":
        try:
            r = run([str(tool_path(home)), "update"])
        except (OSError, subprocess.SubprocessError) as e:
            res.update(ok=False, note=f"could not run the update: {e}"[:200])
            return res
        res["after"] = version_of(home, run)
        if r.returncode != 0:
            res.update(ok=False, note=((r.stdout or "") + (r.stderr or "")).strip()[-200:] or f"claude update exited {r.returncode}")
    elif kind == "legacy":
        res.update(ok=False, note=f"the older release {before} cannot be updated here: newer releases need a newer processor, so use a newer machine for Claude jobs")
    elif kind == "missing":
        res.update(ok=False, note="the claude tool is not installed for the worker")
    else:
        res.update(ok=False, note="the claude tool was installed some other way, so it is not updated here")
    return res


def save(state, res):
    f = Path(state) / RESULT_FILE
    f.write_text(json.dumps(res))


def load(state):
    try:
        return json.loads((Path(state) / RESULT_FILE).read_text())
    except (OSError, ValueError):
        return None
