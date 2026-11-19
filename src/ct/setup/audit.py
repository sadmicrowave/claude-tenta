"""ct-setup audit: look at a server and say what drifted from the configuration. Read-only: it changes nothing, and runs without root
(a check that needs root says so instead of guessing)."""
import json
import re

from .. import config
from . import hardening
from .report import sink as _sink


PING = ("import socket,sys; s=socket.socket(socket.AF_UNIX); s.settimeout(15); s.connect(sys.argv[1]); s.sendall(b'{}'); s.shutdown(socket.SHUT_WR); "
        "print(s.recv(65536).decode())")           # a request the helper must refuse: any answer at all proves its service starts


def audited_modules(host, cfg):
    """The modules named in the configuration, and the offered ones the person accepted (recorded as applied or already satisfied when the server half ran): what the owner chose is checked, what was declined is not drift."""
    chosen = list(cfg.hardening.modules)
    try:
        done = json.loads(host.read(f"{cfg.paths.config}/hardening.json") or "{}")
    except ValueError:
        done = {}
    return chosen + [m for m in cfg.hardening.offered if m not in chosen and (done.get(m) or {}).get("status") in ("applied", "satisfied")]


def run(host, cfg, say=None):
    """Returns (rows, failures) where rows = [(what, ok, detail)]."""
    say = say or _sink()
    rows = []
    problem = config.secrets_file_problem(cfg.path("secrets"))
    rows.append(("secrets file is private", problem is None, problem or "ok"))
    text = host.read(cfg.path("config_file")) or ""
    leaked = re.search(r'(?m)^url\s*=\s*"https?://', text) is not None
    rows.append(("the private alert address is kept out of the configuration file", not leaked,
                 "ok" if not leaked else f"{cfg.path('config_file')} holds it and is readable by every account; run ct-setup install again, which moves it to the secrets file"))
    try:
        import yaml
        has_actions = bool((yaml.safe_load(host.read(cfg.path("registry")) or "") or {}).get("actions"))
    except Exception:
        has_actions = False
    if has_actions:                                      # root-level actions are only as safe as the sandbox they run in
        from .. import helper

        def probe(cmd, **kw):
            r = host.run(cmd)
            return type("R", (), {"stdout": r.out, "returncode": 0 if r.ok else 1})()
        enforced = helper.sandbox_enforced(probe)
        ping = host.run(["python3", "-c", PING, cfg.path("helper_socket")])
        answered = ping.ok and '"refused"' in (ping.out or "")
        rows.append(("the root-level helper answers", answered, "ok" if answered else "it did not start: " + ((ping.out or "").strip()[-160:] or "no answer") + f"  (see: journalctl -u {cfg.identity.name}-helper@*)"))
        rows.append(("the sandbox for root-level actions is enforced", enforced,
                     "ok" if enforced else "this machine's systemd ignores the sandbox settings (a container does), so the helper refuses every root-level action here"))
    for mid, title, check in hardening.audit(host, cfg, ids=audited_modules(host, cfg)):
        rows.append((f"{mid}: {title}", check.ok, check.detail))
    unknown = [m for m in cfg.hardening.modules if m not in hardening.MODULES]
    if unknown:
        rows.append(("hardening modules named in the configuration exist", False, "unknown: " + ", ".join(unknown)))
    rev = host.read(cfg.path("installed_rev"))
    rows.append(("a release is installed", bool(rev), (rev or "none").strip()[:9]))
    width = max(len(r[0]) for r in rows)
    for what, ok, detail in rows:
        say(f"  {'OK  ' if ok else 'FAIL'}  {what:<{width}}  {detail}")
    return rows, [r for r in rows if not r[1]]
