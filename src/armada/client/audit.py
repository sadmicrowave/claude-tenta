"""The audit trail (R5): where every job ran, why, and what happened, kept after `placements.json` has forgotten it.

One file in the client home (`audit.jsonl`, mode 0600), one JSON object per line, only ever appended to. An entry is older than `audit.keep_days` (default 90) and is dropped by a trim at most once an
hour; nothing else changes or removes a line. Events: `placed` (worker, priority, the explanation of every worker), `lost` (a worker went silent), `again` (a lost job sent again),
`retry` (R3, the next attempt) and `outcome` (the result's status, once). A job sent again has a new id (`-again`, `-try2`); `chain` finds them all from any one. Never raises: a trail that cannot
be written must not stop the job it describes. Nothing is sent anywhere and nothing is signed (see docs/design/ORCHESTRATOR-ROADMAP.md, R5).
"""
import json
import os
import re
import time
from pathlib import Path

from . import place
from .repo import PENDING

TRIM_EVERY_S = 3600
KEEP_DAYS = 90
# an outcome is any result whose status is not one of repo.PENDING (the same test as health._final), so a status the client has never seen is still recorded
_SUFFIX = re.compile(r"(-again\d*|-try\d+)$")


def _file(home):
    return Path(home) / "audit.jsonl"


def home_of(ctx):
    """The client home every placement is remembered in: the main worker's, also from the Context of another worker."""
    return getattr(ctx, "home_main", None) or ctx.home


def keep_days(ctx):
    n = getattr(ctx, "audit_keep_days", KEEP_DAYS)
    return n if isinstance(n, int) and n > 0 else KEEP_DAYS


def explain(choice):
    """The explanation as plain data: [[worker, verdict, reason...]], every worker the choice looked at, with every reason a worker was left out."""
    out = []
    for n, v, r in getattr(choice, "lines", None) or []:
        reasons = (getattr(choice, "reasons", None) or {}).get(n)
        out.append([n, v, *([text for _, text in reasons] if reasons else [r])])
    return out


def append(home, event, jid, now=None, keep=KEEP_DAYS, **fields):
    """Add one line. `fields` with a None value are left out. Never raises."""
    try:
        now = time.time() if now is None else now
        line = json.dumps({"at": int(now), "event": event, "job": jid, **{k: v for k, v in fields.items() if v not in (None, "", [])}}, separators=(",", ":")) + "\n"
        f = _file(home)
        with place._locked(home, "audit.lock"):
            f.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(f, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, line.encode())
            finally:
                os.close(fd)
            os.chmod(f, 0o600)
            _trim(home, now, keep)
    except (OSError, TypeError, ValueError):
        pass


def _trim(home, now, keep):
    """Drop the lines older than `keep` days, at most once an hour (called with the lock held)."""
    stamp = Path(home) / "audit.stamp"
    try:
        if now - float(stamp.read_text()) < TRIM_EVERY_S:
            return
    except (OSError, ValueError):
        pass
    cutoff = now - keep * 86400
    rows = read(home)
    f = _file(home)
    tmp = f.with_name(f"{f.name}.{os.getpid()}.tmp")
    tmp.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows if r.get("at", 0) >= cutoff))
    tmp.chmod(0o600)
    os.replace(tmp, f)
    stamp.write_text(str(now))


def read(home):
    """Every entry, oldest first. A line that is not valid JSON (a write cut short) is skipped, not an error."""
    out = []
    try:
        for line in _file(home).read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("job"):
                out.append(row)
    except OSError:
        pass
    return out


def placed(ctx, ids, cls, worker, choice=None, priority=None, why=None, now=None):
    """A job was handed to `worker`. `choice` is the explanation (None when the person named the worker); `priority` is how it waited, if it did; `why` a line of words for what the table cannot say."""
    rows = explain(choice) if choice is not None else None
    for jid in ids:
        append(home_of(ctx), "placed", jid, now, keep_days(ctx), **{"class": cls}, worker=worker, priority=priority, rows=rows, why=why or (None if choice is not None else "the worker was named, not chosen"))


def lost(ctx, jid, worker, cls, why, again=None, now=None):
    append(home_of(ctx), "lost", jid, now, keep_days(ctx), worker=worker, **{"class": cls}, why=why, again=again)


def retried(ctx, jid, new, cls, attempt, of, why, now=None):
    append(home_of(ctx), "retry", jid, now, keep_days(ctx), **{"class": cls}, next=new, attempt=attempt, of=of, why=why)


def outcomes(ctx, results, now=None):
    """The outcome of each finished job in `results` ({job id: result}) that was placed and has none yet. Returns how many were written. Never raises."""
    try:
        home, n = home_of(ctx), 0
        rows = read(home)
        placed_ids = {r["job"] for r in rows if r["event"] == "placed"}
        done = {r["job"] for r in rows if r["event"] == "outcome"}
        for jid, res in (results or {}).items():
            status = (res or {}).get("status")
            if jid in placed_ids and jid not in done and status and status not in PENDING:
                append(home, "outcome", jid, now, keep_days(ctx), status=status, finished=res.get("finished"), **{"class": res.get("class")})
                n += 1
        return n
    except (OSError, TypeError, ValueError, AttributeError):
        return 0


def root(jid):
    """The id a job started as: `-again` and `-try2` tails taken off, however many."""
    while True:
        new = _SUFFIX.sub("", jid)
        if new == jid:
            return jid
        jid = new


def chain(rows, jid):
    """Every entry about `jid` and the jobs it was sent again as (or came from), oldest first."""
    want = root(jid)
    return [r for r in rows if root(r["job"]) == want or root(r.get("next") or r["job"]) == want or root(r.get("again") or r["job"]) == want]


def story(rows):
    """One job's entries as [(when, event, worker, text)] for the card: where, why, then what happened."""
    out = []
    for r in rows:
        ev = r["event"]
        if ev == "placed":
            text = r.get("why") or "; ".join(f"{x[0]} {x[1]}: {' / '.join(x[2:])}" for x in r.get("rows") or [] if x[1] == "CHOSEN") or "placed"
        elif ev == "outcome":
            text = r.get("status", "")
        elif ev == "retry":
            text = f"attempt {r.get('attempt')} of {r.get('of')} queued as {r.get('next')}: {r.get('why', '')}"
        else:
            text = r.get("why", "") + (f" (sent again as {r['again']})" if r.get("again") else "")
        out.append((r["at"], ev, r.get("worker"), text))
    return out
