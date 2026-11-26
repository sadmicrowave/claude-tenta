"""The operator's status panel (R6): workers, the waiting list, what is running and what finished lately, on one screen.

It reads only what R1 (queue.json), R2 (the published worker states, the silent rule) and R5 (audit.jsonl) already keep on this computer: no login to any worker.
`collect` gathers it into plain data (testable, no terminal); `lines` draws it to fit a given width, bars included; `show` prints the card.
"""
import time

from .. import alias, diffview, theme, ui
from . import audit, queue

RUNNING_MAX_S = 86400            # a job placed longer ago than this with no outcome is not "running": it is lost or unseen, and R2 says so elsewhere
RECENT = 6                       # how many finished jobs the panel lists
GOOD = ("ok",)


def _age(seconds):
    s = max(0, int(seconds))
    return "now" if s < 60 else f"{s // 60} min" if s < 7200 else f"{s // 3600} h" if s < 172800 else f"{s // 86400} d"


def collect(home, rows, now=None, recent=RECENT):
    """The panel as data. `rows` is [(name, row)] from workers.row_for for every worker (row None when it has published nothing)."""
    now = time.time() if now is None else now
    log = audit.read(home)
    done = {r["job"]: r for r in log if r["event"] == "outcome"}
    running = [r for r in log if r["event"] == "placed" and r["job"] not in done and now - r["at"] <= RUNNING_MAX_S]
    by_worker = {}
    for r in running:
        by_worker[r.get("worker")] = by_worker.get(r.get("worker"), 0) + 1
    waiting = queue.waiting(home)
    depth = {p: sum(1 for i in waiting if i.get("priority", queue.DEFAULT) == p) for p in reversed(queue.PRIORITIES)}       # high first
    oldest = max((now - i.get("at", now) for i in waiting), default=0)
    finished = sorted(done.values(), key=lambda r: -r["at"])[:recent]
    placed = {r["job"]: r for r in log if r["event"] == "placed"}
    return {
        "workers": [{"name": alias.show(n), "state": _state(r), "bad": _state(r) != "ok", "load": r.get("load_value") if r else None, "cores": r.get("cores_value") if r else None,
                     "running": by_worker.get(n, 0), "age": r["age"].replace(" SILENT", "").replace(" STALE", "") if r else "-"} for n, r in rows],
        "queue": {"depth": depth, "total": len(waiting), "oldest": _age(oldest) if waiting else ""},
        "running": [{"job": r["job"], "class": r.get("class", ""), "worker": alias.show(r.get("worker", "")), "for": _age(now - r["at"])} for r in running[-RECENT:][::-1]],
        "recent": [{"job": r["job"], "class": r.get("class") or (placed.get(r["job"]) or {}).get("class", ""), "worker": alias.show((placed.get(r["job"]) or {}).get("worker", "")), "status": r.get("status", ""),
                    "ago": _age(now - r["at"])} for r in finished],
    }


def _state(r):
    if not r:
        return "no state"
    if r["silent"]:
        return "SILENT"
    if r["stale"]:
        return "STALE"
    if r["paused"]:
        return "PAUSED"
    if r["locked"]:
        return "LOCKED"
    return "ok"


def bar(value, total, room):
    """A bar `room` characters wide (never wider): filled in proportion to value/total, at least one cell for a non-zero value, empty for zero."""
    room = max(0, room)
    if room == 0:
        return ""
    cells = 0 if not value or total <= 0 else min(room, max(1, round(room * value / total)))
    return "█" * cells + "░" * (room - cells)


def _cut(text, room):
    text = str(text)
    return text if len(text) <= room else text[:max(1, room - 1)] + "…"


def lines(data, room, color=True):
    """The panel as lines that are never wider than `room` (colour roles through ct.theme; the plain text of a line is what is measured)."""
    out = []
    sgr = theme.sgr if color else (lambda text, *roles: text)
    heading = lambda t: out.append(sgr(t.upper(), "label"))               # noqa: E731
    bar_room = max(4, min(24, room - 22))
    heading("Workers")
    if not data["workers"]:
        out.append(sgr("no workers listed", "muted"))
    for w in data["workers"]:
        head = f"{_cut(w['name'], max(6, room - 14))}  "
        state = w["state"]
        out.append(head + sgr(_cut(state, room - len(head)), "bad" if w["bad"] else "ok"))
        detail = f"  {w['running']} running · reading {w['age']}"
        out.append(sgr(_cut(detail, room), "muted"))
        if w["load"] is not None and w["cores"]:
            used = f"  load {w['load']:.1f} of {w['cores']}"
            out.append(sgr(_cut(used, room), "muted"))
            out.append("  " + sgr(bar(w["load"], w["cores"], bar_room), "accent"))
    out.append("")
    q = data["queue"]
    heading(f"Waiting ({q['total']})")
    if not q["total"]:
        out.append(sgr("nothing waits", "muted"))
    else:
        peak = max(q["depth"].values())
        for p, n in q["depth"].items():
            out.append(f"{p:<7}{n:>3}  " + sgr(bar(n, peak, max(4, min(24, room - 14))), "primary"))
        out.append(sgr(_cut(f"oldest has waited {q['oldest']}", room), "muted"))
    out.append("")
    heading("Running")
    if not data["running"]:
        out.append(sgr("nothing running", "muted"))
    for j in data["running"]:
        out.append(_cut(f"{j['class']} {j['job']}", room))
        out.append(sgr(_cut(f"  on {j['worker']} for {j['for']}", room), "muted"))
    out.append("")
    heading("Recent results")
    if not data["recent"]:
        out.append(sgr("no results seen yet", "muted"))
    for j in data["recent"]:
        out.append(sgr(_cut(j["status"], 12), "ok" if j["status"] in GOOD else "bad") + " " + _cut(f"{j['class']} {j['job']}", max(4, room - 14)))
        out.append(sgr(_cut(f"  on {j['worker'] or '?'}, {j['ago']} ago", room), "muted"))
    return out


def show(data, stream=None):
    """The card for a person at a terminal; plain lines when piped."""
    import sys
    stream = stream or sys.stdout
    color = ui._color_on(stream)
    ui.document("Your workers and jobs", "\n".join(lines(data, diffview.room(True) if color else 10 ** 6, color=color)), tag="STATUS", stream=stream)
