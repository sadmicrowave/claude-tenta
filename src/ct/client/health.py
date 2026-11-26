"""Worker health on this computer (docs/design/SEVERAL-WORKERS.md, "Worker health and lost jobs (R2)").

A job is LOST when it was sent to a worker, has no final result, is older than `health.silent_hours`, and that worker is silent (its reading is that old) or cannot be reached now. Lost is worked
out from the placement record, the result and the reading each time; the one thing stored is the action taken, on the placement (`lost`, and `again` when it was sent again). A worker that comes back
and publishes the result makes the job final again and the lost mark is no longer shown.

A class the administrator marked `repeatable` keeps a private copy of each job it was sent (`sent/<id>.json` in the client home, mode 0600) until the result is final, so a lost job can be queued
again through the waiting list (R1) on ANY connection: nothing is read back from a worker. Nothing here runs in the background and the normal path costs no network call: the placement file is read
locally, and the workers are looked at only when a job is old enough to be a candidate, and at most once an hour.
"""
import contextlib
import json
import time
from pathlib import Path

from ..jobs import ID_RE
from . import place, queue
from .repo import PENDING

LOOK_EVERY_S = 3600                     # the workers are looked at for this at most once an hour
SHOW_LOST_FOR_S = 7 * 86400             # a lost job with still no result is listed this long


def _dir(home):
    return Path(home) / "sent"


KEEP_MAX_BYTES = 256 * 1000 * 1000      # a bigger job (a large file set) is not copied: the card then says why it could not be sent again


def keep(home, jid, job):
    """The copy of a job of a repeatable class, private to the person; removed when the result is final or the job is sent again. False when it was not kept (too big, or an id that is not a job id)."""
    text = json.dumps(job)
    if len(text) > KEEP_MAX_BYTES or not ID_RE.match(str(jid)):
        return False
    queue._write_private(_dir(home) / f"{jid}.json", text)
    return True


def keep_sent(home, info, cls, ids, batches, job):
    """After a send: a copy of each job (with its own share of the files) when the class is repeatable; a copy stays on this computer until the result is final, since a silent worker cannot be asked."""
    if is_repeatable(info, cls):
        for jid, chunk in zip(ids, batches):
            keep(home, jid, {**job, **({"files": chunk} if chunk else {})})


def kept(home, jid):
    if not ID_RE.match(str(jid)):
        return None
    try:
        return json.loads((_dir(home) / f"{jid}.json").read_text())
    except (OSError, ValueError):
        return None


def drop(home, jid):
    with contextlib.suppress(OSError):
        (_dir(home) / f"{jid}.json").unlink()


def is_repeatable(info, cls):
    """From the worker's published registry: did the administrator mark this class safe to repeat?"""
    return bool((((info or {}).get("classes") or {}).get(cls) or {}).get("repeatable"))


def _placements(home):
    try:
        return json.loads((Path(home) / "placements.json").read_text()) or {}
    except (OSError, ValueError, TypeError):
        return {}


def _final(res):
    return res is not None and res.get("status") not in PENDING


def _candidates(home, limit_s, now):
    """Local only: jobs placed longer ago than the limit that carry no lost mark yet."""
    return {jid: p for jid, p in _placements(home).items()
            if not p.get("reserve") and not p.get("lost") and p.get("worker") and now - p.get("at", 0) > limit_s}


def _look_due(home, now):
    try:
        return now - float((Path(home) / "health.stamp").read_text()) >= LOOK_EVERY_S
    except (OSError, ValueError):
        return True


def _mark(home, jid, lost, again=None):
    place._update(home, lambda data: data[jid].update({"lost": lost, **({"again": again} if again else {})}) if jid in data else None)


def _new_id(home, jid):
    """`<id>-again` (then -again2 ...), kept inside the 64 characters a job id may have: the worker refuses a longer one."""
    taken = queue.ids(home) | set(_placements(home))
    n = 1
    while True:
        tail = "-again" + (str(n) if n > 1 else "")
        new = jid[:64 - len(tail)].rstrip("-") + tail
        if new not in taken:
            return new
        n += 1


def _forget_orphans(home):
    """A copy whose placement was pruned (14 days) has no result to wait for any more."""
    placed = _placements(home)
    with contextlib.suppress(OSError):
        for f in _dir(home).glob("*.json"):
            if f.stem not in placed:
                f.unlink()


def sweep(ctx, cfg, *, entries_fn=None, repos_of=None, fetch=None, now=None):
    """Mark the jobs that were lost, and queue again the ones that may be repeated. Returns [(job id, worker, why, again, outcome)] for the jobs marked NOW (`again` is the new id or None; `outcome` is again, no_copy or not_repeatable)."""
    now = time.time() if now is None else now
    limit_s = place._silent_s(ctx)
    _forget_orphans(ctx.home)
    cand = _candidates(ctx.home, limit_s, now)
    if not cand or cfg is None:
        return []
    ents = (entries_fn or place.entries)(cfg)
    if len(ents) < 2:
        return []
    by_name = {e[0]: e for e in ents}
    repos_of = repos_of or (lambda e: ctx.repos if e[0] == ents[0][0] else place.ClientRepos.from_config(e[1]))
    open_ = {}                                                         # what a result already here says costs nothing: a job with a final result is done, keep nothing for it
    for jid, p in list(cand.items()):
        e = by_name.get(p["worker"])
        res = None
        with contextlib.suppress(Exception):
            res = repos_of(e).read_result(jid) if e else None
        if _final(res):
            drop(ctx.home, jid)
            cand.pop(jid)
        else:
            open_[jid] = p
    if not open_ or not _look_due(ctx.home, now):
        return []
    queue._write_private(Path(ctx.home) / "health.stamp", str(now))
    views = {v.name: v for v in place.gather_shared(ents, repos_of, fetch, home=ctx.home, silent_s=limit_s)}
    out = []
    for jid, p in open_.items():
        v, e = views.get(p["worker"]), by_name.get(p["worker"])
        if v is None or e is None:
            continue
        res = None
        with contextlib.suppress(Exception):                           # the look just pulled this worker's results (a worker that did not answer has none to read)
            res = repos_of(e).read_result(jid)
        if _final(res):
            drop(ctx.home, jid)
            continue
        if not v.reachable:
            why = f"{p['worker']} cannot be reached ({v.why_not or 'no answer'}) and the job is {queue.age_text(now - p['at'])} old with no result"
        elif v.silent:
            why = f"{p['worker']} has published nothing for {queue.age_text(v.age_s)} and the job is {queue.age_text(now - p['at'])} old with no result"
        else:
            continue                                                    # the worker is alive: a long job, or one waiting its turn there
        with place._locked(ctx.home, "health.lock"):                    # another ct-run may have got here first: the one who marks it is the one who queues it
            if (_placements(ctx.home).get(jid) or {}).get("lost"):
                continue
            again, job = None, kept(ctx.home, jid)                      # only a repeatable class had a copy kept when it was sent
            if job is not None:
                again = _new_id(ctx.home, jid)
                queue.add(ctx.home, again, p.get("class"), queue.DEFAULT, job, f"{p['worker']} went silent, so {jid} is sent again", now=now)   # added first: a crash before the mark can only repeat a repeatable job
                drop(ctx.home, jid)
            outcome = "again" if again else ("no_copy" if is_repeatable(v.registry, p.get("class")) else "not_repeatable")
            _mark(ctx.home, jid, {"why": why, "at": int(now), "outcome": outcome}, again)
        out.append((jid, p["worker"], why, again, outcome))
    return out


def lost_now(home, final_ids, now=None):
    """[(job id, worker, class, why, again, outcome)] of the jobs marked lost that still have no final result (`final_ids`: the ids that have one), for the jobs list."""
    now = time.time() if now is None else now
    return [(jid, p.get("worker"), p.get("class"), p["lost"].get("why", ""), p.get("again"), p["lost"].get("outcome", "again" if p.get("again") else "not_repeatable")) for jid, p in sorted(_placements(home).items(), key=lambda kv: kv[1].get("at", 0))
            if p.get("lost") and jid not in final_ids and now - p["lost"].get("at", 0) <= SHOW_LOST_FOR_S]


def forget_final(home, final_ids):
    """The jobs list has seen these results: their copies have no more use, so they do not stay on disk until the limit."""
    for jid in final_ids:
        if jid in _placements(home):
            drop(home, jid)
