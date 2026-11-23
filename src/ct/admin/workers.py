"""The workers this computer administers, and a side-by-side comparison of them.

Each worker is a complete install with its own configuration file (its own queue and results repositories, its own server). The list is a small file next to
the main configuration: `workers.toml`, one `[workers.<name>]` table per extra worker holding the path of its configuration. The worker in the main
configuration is always the first row, so a computer with one worker sees a one-row comparison and nothing changes for it.

The comparison reads each worker's published `worker-state.json` (see ct.workerstate), so it needs no login to any worker.
"""
import json
import re
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from .. import config, refusals, theme
from ..words import plural

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
STALE_S = 7200


def load_list(path):
    """{name: config path} from workers.toml (an absent file is an empty list)."""
    try:
        raw = tomllib.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return {n: t["config"] for n, t in (raw.get("workers") or {}).items() if isinstance(t, dict) and isinstance(t.get("config"), str)}


def save_list(path, workers):
    lines = ["# Workers this computer administers besides the one in the main configuration. Managed by `ct-admin workers add` and `remove`.\n"]
    for name in sorted(workers):
        lines.append(f'\n[workers.{name}]\nconfig = "{workers[name]}"\n')
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("".join(lines))


def add(path, name, config_path):
    if not NAME_RE.match(name or ""):
        raise refusals.refuse("worker.name.invalid")
    cfg_file = Path(config_path).expanduser()
    if not cfg_file.is_file():
        raise refusals.refuse("worker.config.missing", path=cfg_file)
    workers = load_list(path)
    workers[name] = str(cfg_file)
    save_list(path, workers)


def remove(path, name):
    workers = load_list(path)
    if name not in workers:
        return False
    del workers[name]
    save_list(path, workers)
    return True


def read_state(results_dir):
    """The worker-state.json in a results clone, or None."""
    try:
        return json.loads((Path(results_dir) / "worker-state.json").read_text())
    except (OSError, ValueError):
        return None


def age_s(doc, now=None):
    try:
        t = datetime.strptime(doc["updated"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except (KeyError, ValueError, TypeError):
        return None
    return (time.time() if now is None else now) - t


def claude_summary(doc):
    """(has Claude access: bool, short text) from a state document."""
    c = (doc or {}).get("claude") or {}
    auth = c.get("auth", "none")
    if auth == "none":
        return False, "none"
    tool = c.get("tool") or {}
    kind, version = tool.get("kind", "?"), tool.get("version", "")
    if kind == "missing" or not version:
        return False, f"{auth}: tool NOT INSTALLED"
    older = " (older release, pinned)" if kind == "legacy" else ""
    days = c.get("token_days")
    if days is not None and days >= 365:
        return False, f"{auth}: {version}{older}, TOKEN EXPIRED ({plural(days, 'day')} old)"
    token = "" if days is None else f", token {plural(days, 'day')} old" + (" (renew soon)" if days >= 335 else "")
    return True, f"{auth}: {version}{older}{token}"


def human_age(seconds):
    if seconds is None:
        return "?"
    m = int(seconds // 60)
    return "just now" if m < 1 else f"{m} min ago" if m < 120 else f"{m // 60} h ago"


def profile_time_text(pt):
    """What a profile run's time limit is built from, in one line ('' when the worker does not say)."""
    if not isinstance(pt, dict) or "factor" not in pt:
        return ""
    ceiling = f"ceiling {pt['ceiling_s']:g} s (shared computer)" if pt.get("ceiling_s") else "no ceiling"
    return f"estimate x {pt['factor']:g} x {pt.get('margin', 3):g}; {ceiling}; from {pt.get('basis', 'none yet')}"


def row_for(name, doc, now=None):
    """The cells of one worker's row (plain text; the caller adds colour)."""
    if doc is None:
        return {"name": name, "os": "-", "cpu": "-", "memory": "-", "claude": "-", "state": "no state published (an older release, or not set up yet)",
                "release": "-", "load": "-", "age": "-", "has_claude": False, "locked": False, "stale": True}
    hw, obs = doc.get("hardware") or {}, doc.get("observed") or {}
    age = age_s(doc, now)
    o = doc.get("os") or {}
    has, text = claude_summary(doc)
    mem_total, mem_free = hw.get("mem_total_mb"), obs.get("mem_available_mb")
    load, temp = obs.get("load_1m"), obs.get("temp_c")
    today = obs.get("claude_today") or {}
    spend = ("PAUSED: " if today.get("paused") else "") + f"{today.get('jobs', 0)}" + (f"/{today['limit_jobs']}" if today.get("limit_jobs") else "") + f" jobs, {today.get('turns', 0)}" + (f"/{today['limit_turns']}" if today.get("limit_turns") else "") + " turns" if today else ""
    return {"name": name, "os": f"{o.get('id', '?')} {o.get('version', '')}".strip(), "spend": spend, "paused": bool(today.get("paused")),
            "cpu": f"{plural(hw.get('cores', '?'), 'core')}" + ("" if hw.get("native_claude", True) else ", old CPU"),
            "memory": f"{mem_free / 1024:.1f} of {mem_total / 1024:.1f} GB free" if mem_total and mem_free is not None else "?",
            "claude": text, "state": "LOCKED" if doc.get("locked") else (("jail ON, relaxed" if (doc.get("isolation") or {}).get("mode") == "relaxed" else "jail ON") if (doc.get("isolation") or {}).get("ready") else "jail OFF"),
            "ceiling": (f"{obs['job_ceiling_mb']} MB" if obs.get("job_ceiling_mb") else ""), "purpose": config.PURPOSE_WORDS.get((doc.get("isolation") or {}).get("purpose"), ""), "held": obs.get("held") or "", "profile_time": profile_time_text(doc.get("profile_time")), "platform": doc.get("platform") or "",
            "release": (doc.get("release") or "?")[:9], "load": (f"{load:.2f}" if load is not None else "?") + (f" / {temp:.0f} C" if temp is not None else ""),
            "age": human_age(age) + (" STALE" if age is None or age > STALE_S else ""),
            "has_claude": has, "locked": bool(doc.get("locked")), "stale": age is None or age > STALE_S,
            "packages": len(doc.get("packages") or []), "classes": len(doc.get("classes") or []), "class_names": sorted(doc.get("classes") or [])}


# ---- the cards: the wizard's question-card look (a coloured rail down the left, a chip, labelled rows) ----------------------------------------------
BLUE, BLUE_CHIP, AMBER_CHIP, RED_CHIP = "primary", "primary_chip", "warn_chip", "bad_chip"          # roles: ct.theme decides the colours
RED, AMBER, DIM, BOLD = "bad", "warn", "muted", "strong"
ROWS = (("OS", "os"), ("CPU", "cpu"), ("MEMORY", "memory"), ("CLAUDE", "claude"), ("TODAY", "spend"), ("STATE", "state"), ("RELEASE", "release"), ("LOAD", "load"), ("READING", "age"))
HANG = 15           # the widest label (AVAILABILITY, PROFILE LIMIT) plus a gap: a longer label ran into its value (guard: tests/test_workerstate.py)


def fact_rows(r, upper=False):
    """The measurements of one worker as (label, value) rows, with what it is used for after its state when it has said (an older release does not)."""
    rows = [(label, (str(r.get(key, "-")).replace(" STALE", "") if key == "age" else str(r.get(key, "-")))) for label, key in ROWS]
    if r.get("ceiling"):                                                                   # the largest job file it accepts right now (it moves with its free disk and memory)
        rows.insert([k for _, k in ROWS].index("memory") + 1, ("LARGEST JOB" if upper else "Largest job", r["ceiling"]))
    if r.get("purpose"):
        rows.insert([k for _, k in ROWS].index("state") + 1 + bool(r.get("ceiling")), ("PURPOSE" if upper else "Purpose", r["purpose"]))
    if r.get("profile_time"):                                                          # how long a profile run may take here, and why that long
        rows.insert([k for _, k in ROWS].index("state") + 1 + bool(r.get("ceiling")) + bool(r.get("purpose")), ("PROFILE LIMIT" if upper else "Profile limit", r["profile_time"]))
    if r.get("platform") not in (None, "", "linux"):                                       # only when it is not the usual Linux worker
        rows.insert([k for _, k in ROWS].index("state") + 1, ("PLATFORM" if upper else "Platform", r["platform"]))
    if r.get("held"):                                                                   # a shared computer that is not available right now, as it last said
        rows.insert([k for _, k in ROWS].index("state") + 1 + bool(r.get("purpose")), ("AVAILABILITY" if upper else "Availability", "held: " + r["held"]))
    return rows


def installed_rows(r, plain=False):
    """What is installed on a worker, apart from the measurements: its classes BY NAME, each with a brief muted description beside it (so the count can be checked against `ct-run classes --worker NAME`),
    the commands dedicated to it (each on its own line), then a blank line and its package count. Names are values, never labels."""
    if r.get("packages") is None or r["stale"]:
        return []
    from .. import cards as card_mod
    desc = r.get("class_desc") or {}

    def one(text):
        return text if plain else card_mod_styled(text)

    names = r.get("class_names") or []
    width = min(max([len(n) for n in names] or [0]), 24)           # the description starts in one column; a name longer than that gets its description on the line below, so it moves nothing

    def klass(n):
        d = desc.get(n)
        if plain:
            return [f"{n.ljust(width)}  {d}" if d and len(n) <= width else n] + ([d] if d and len(n) > width else [])
        if not d:
            return [card_mod.Styled([(n, "strong")])]
        if len(n) <= width:
            return [card_mod.Styled([(n.ljust(width), "strong"), (d, "muted")])]
        return [card_mod.Styled([(n, "strong")]), card_mod.Styled([(d, "muted")])]
    dedicated = [x.strip() for x in (r.get("dedicated") or "").split(",") if x.strip()]
    lines = [v for n in names for v in klass(n)]
    rows = [("CLASSES" if i == 0 else "", v) for i, v in enumerate(lines)] or [("CLASSES", one("none"))]
    rows += [("DEDICATED" if i == 0 else "", one(n)) for i, n in enumerate(dedicated)]
    return rows + [("", "") if plain else "\n", ("PACKAGES", one(plural(r["packages"], "package")))]


def card_mod_styled(text):
    from .. import cards as card_mod
    return card_mod.Styled([(text, "strong")])


def cards(rows, width=80, color=True):
    """One framed card per worker, stacked, in the same outline as every other card: its name as the title, a tag for its state (LOCKED red, STALE yellow, otherwise WORKER), then labelled rows.
    Plain text when `color` is false (a pipe)."""
    if not color:
        room = max(20, min(width, 100) - 2 - HANG)
        out = []
        for r in rows:
            from .. import alias
            out += [f"{alias.show(r['name']).upper()}" + (" (LOCKED)" if r["locked"] else " (STALE)" if r["stale"] else "")]
            facts = fact_rows(r, upper=True)
            extra = installed_rows(r, plain=True)                          # what is set up here, apart from the measurements above it
            out += [f"  {label:<{HANG}}{value if len(value) <= room else value[:room - 1] + '…'}" if (label or value) else "" for label, value in facts + ([("", "")] if extra else []) + extra]
            out.append("")
        return out
    from .. import alias, cards as card_mod, ui as ui_mod
    out = []
    for r in rows:
        facts = fact_rows(r)
        extra = installed_rows(r)                                         # what is set up here: a blank line apart from the measurements, and bold
        facts += (["\n"] if extra else []) + extra
        tag, colour = ("LOCKED", ui_mod.RED) if r["locked"] else ("STALE", ui_mod.YELLOW) if r["stale"] else ("WORKER", ui_mod.PRIMARY)
        out += [""] + card_mod.card(alias.show(r["name"]), facts, tag=tag, color=colour, cols=width)
    return out


def dedicated_text(pins, name):
    """The commands the computer's ~/.tenta.toml dedicates to this worker, as a comma separated list ('nmap, sqlmap'), or '' when none. A command goes only to its worker; the worker still takes other work."""
    return ", ".join(sorted(c for c, w in pins.commands.items() if w == name))


def brief(c, room=44):
    """A class in a few words, for beside its name: what it runs (or what kind of job it is), cut with a mark when long."""
    text = " ".join(c["run"]) if c.get("run") else ("read-only Claude on files you attach" if c.get("kind") == "claude" else "profile one exact command" if c.get("kind") == "profile" else c.get("kind", ""))
    return text if len(text) <= room else text[:room - 1] + "…"
