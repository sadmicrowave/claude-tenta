"""cw-setup audit: look at a server and say what drifted from the configuration. Read-only: it changes nothing, and runs without root
(a check that needs root says so instead of guessing)."""
from .. import config
from . import hardening


def run(host, cfg, say=print):
    """Returns (rows, failures) where rows = [(what, ok, detail)]."""
    rows = []
    problem = config.secrets_file_problem(cfg.path("secrets"))
    rows.append(("secrets file is private", problem is None, problem or "ok"))
    cfg_file = cfg.path("config_file")
    try:
        open_mode = host.path(cfg_file).stat().st_mode & 0o007
    except OSError:
        open_mode = 0
    rows.append(("configuration file is private (it holds the alert address)", not open_mode, "ok" if not open_mode else f"{cfg_file} can be read by every account; run: chmod 640 {cfg_file}"))
    for mid, title, check in hardening.audit(host, cfg):
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
