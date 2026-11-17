"""The workers this computer administers, and a side-by-side comparison of them.

Each worker is a complete install with its own configuration file (its own queue and results repositories, its own server). The list is a small file next to
the main configuration: `workers.toml`, one `[workers.<name>]` table per extra worker holding the path of its configuration. The worker in the main
configuration is always the first row, so a computer with one worker sees a one-row comparison and nothing changes for it.

The comparison reads each worker's published `worker-state.json` (see cw.workerstate), so it needs no login to any worker.
"""
import json
import re
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

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
    lines = ["# Workers this computer administers besides the one in the main configuration. Managed by `cw-admin workers add` and `remove`.\n"]
    for name in sorted(workers):
        lines.append(f'\n[workers.{name}]\nconfig = "{workers[name]}"\n')
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("".join(lines))


def add(path, name, config_path):
    if not NAME_RE.match(name or ""):
        raise ValueError("a worker name is lowercase letters, digits and dashes, starting with a letter")
    cfg_file = Path(config_path).expanduser()
    if not cfg_file.is_file():
        raise ValueError(f"there is no configuration file at {cfg_file}")
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
    return True, f"{auth}: {version}{older}"


def human_age(seconds):
    if seconds is None:
        return "?"
    m = int(seconds // 60)
    return "just now" if m < 1 else f"{m} min ago" if m < 120 else f"{m // 60} h ago"


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
    return {"name": name, "os": f"{o.get('id', '?')} {o.get('version', '')}".strip(),
            "cpu": f"{hw.get('cores', '?')} cores" + ("" if hw.get("native_claude", True) else ", old CPU"),
            "memory": f"{mem_free / 1024:.1f} of {mem_total / 1024:.1f} GB free" if mem_total and mem_free is not None else "?",
            "claude": text, "state": "LOCKED" if doc.get("locked") else ("jail ON" if (doc.get("isolation") or {}).get("ready") else "jail OFF"),
            "release": (doc.get("release") or "?")[:9], "load": (f"{load:.2f}" if load is not None else "?") + (f" / {temp:.0f} C" if temp is not None else ""),
            "age": human_age(age) + (" STALE" if age is None or age > STALE_S else ""),
            "has_claude": has, "locked": bool(doc.get("locked")), "stale": age is None or age > STALE_S,
            "packages": len(doc.get("packages") or []), "classes": len(doc.get("classes") or [])}


COLUMNS = (("WORKER", "name"), ("OS", "os"), ("CPU", "cpu"), ("MEMORY", "memory"), ("CLAUDE", "claude"), ("STATE", "state"), ("RELEASE", "release"),
           ("LOAD / HEAT", "load"), ("READING", "age"))


def table(rows):
    """Plain text lines: a header and one aligned row per worker."""
    widths = [max(len(h), *(len(str(r[k])) for r in rows)) if rows else len(h) for h, k in COLUMNS]
    fmt = lambda cells: "  " + "  ".join(str(c).ljust(w) for c, w in zip(cells, widths)).rstrip()      # noqa: E731
    return [fmt([h for h, _ in COLUMNS])] + [fmt([r[k] for _, k in COLUMNS]) for r in rows]
