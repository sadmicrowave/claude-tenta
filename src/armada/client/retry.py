"""Retries on this computer (docs/design/SEVERAL-WORKERS.md, "Retries and timeouts (R3)").

A class the administrator marked `repeatable` may also say `retries: N` (and `retry_delay_s`). A job of such a class that ends `killed` (it was stopped from outside: a restart, a stop) is queued again, at most N
more times, once the delay has passed. Nothing else is retried: a job that ended with an error ran to its end and the person decides what to do; a job that ended `timeout` ran too long and would only do so again;
`limit` is a stop for memory, heat or disk. Like the lost-job check in health.py, nothing runs in the background: the check is made at the start of each arm-run command, so the delay is "at least this long".

The one thing stored is the plan of each job (`retries.json` in the client home, mode 0600): attempt, attempts allowed, delay, and `next` (the id of the try that followed) or `done`. The plan stays while the
placement does, so the result card can say "attempt 2 of 3".
"""
import contextlib
import json
import time
from pathlib import Path

from ..jobs import ID_RE
from . import audit, health, place, queue
from .repo import PENDING

RETRY_STATUSES = ("killed",)           # a stop from outside: the job did not run to its own end, and nothing says running it again would end differently
MAX_RETRIES = 5
DEFAULT_DELAY_S = 60


def _file(home):
    return Path(home) / "retries.json"


def _read(home):
    try:
        data = json.loads(_file(home).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _update(home, change):
    with place._locked(home, "retries.lock"):
        data = _read(home)
        change(data)
        queue._write_private(_file(home), json.dumps(data, indent=1))


def settings(info, cls):
    """(retries, delay_s) from the worker's published registry; (0, delay) when the class is not repeatable (a retry runs the job again, so it needs the same promise a lost job does)."""
    c = (((info or {}).get("classes") or {}).get(cls) or {})
    n = c.get("retries")
    if not c.get("repeatable") or isinstance(n, bool) or not isinstance(n, int) or n <= 0:
        return 0, DEFAULT_DELAY_S
    d = c.get("retry_delay_s")
    return min(n, MAX_RETRIES), (d if isinstance(d, int) and not isinstance(d, bool) and d > 0 else DEFAULT_DELAY_S)


def plan_sent(home, info, cls, ids):
    """After a send: the plan of each job when its class says to retry (attempt 1 of retries + 1)."""
    n, delay = settings(info, cls)
    if n:
        _update(home, lambda data: [data.setdefault(jid, {"attempt": 1, "of": n + 1, "delay": delay}) for jid in ids if ID_RE.match(str(jid))])


def carry(home, old, new):
    """A lost job sent again (health.py) is the same attempt: its plan moves to the new id."""
    def go(data):
        if old in data and not data[old].get("next"):
            data[new] = {k: v for k, v in data[old].items() if k != "done"}
            data[old]["done"] = True
    _update(home, go)


def attempt_of(home, jid):
    """(attempt, attempts allowed) for the result card, or None when the job has no plan."""
    p = _read(home).get(jid)
    return (p["attempt"], p["of"]) if p and p.get("of", 1) > 1 else None


def pending(home):
    """The ids whose result is still to be looked at for a retry: their copy of the job must stay."""
    return {jid for jid, p in _read(home).items() if not p.get("next") and not p.get("done")}


def _new_id(home, jid, attempt):
    tail = f"-try{attempt}"
    taken = queue.ids(home) | set(health._placements(home)) | set(_read(home))
    base = jid[:64 - len(tail)].rstrip("-")
    new, n = base + tail, 1
    while new in taken:
        n += 1
        new = base[:64 - len(tail) - len(str(n))].rstrip("-") + tail + str(n)
    return new


def _finished_at(res):
    from .view import parse_when
    t = parse_when(res.get("finished") or "")
    return t.timestamp() if t else None


def _forget_orphans(home):
    placed = health._placements(home)
    gone = [jid for jid in _read(home) if jid not in placed]
    if gone:
        _update(home, lambda data: [data.pop(j, None) for j in gone])


def sweep(ctx, cfg, *, entries_fn=None, repos_of=None, now=None):
    """Look at the jobs with a plan still open. A final result that may be retried (and the delay has passed) queues the job again; any other final result closes the plan.
    Returns [(job id, new id, attempt of the new one, attempts allowed, status, why not)] for what happened now: `new id` is None when the job was not retried (`why not` says why)."""
    now = time.time() if now is None else now
    _forget_orphans(ctx.home)
    todo = pending(ctx.home)
    if not todo or cfg is None:
        return []
    ents = (entries_fn or place.entries)(cfg)
    by_name = {e[0]: e for e in ents}
    repos_of = repos_of or (lambda e: ctx.repos if e[0] == ents[0][0] else place.ClientRepos.from_config(e[1]))
    placed, plans, out = health._placements(ctx.home), _read(ctx.home), []
    for jid in sorted(todo):
        e = by_name.get((placed.get(jid) or {}).get("worker"))
        res = None
        if e:
            with contextlib.suppress(Exception):
                res = repos_of(e).read_result(jid)
        if res is None or res.get("status") in PENDING:
            continue                                                       # no final result yet (a lost job is health.py's)
        p, status = plans[jid], res.get("status")
        if status not in RETRY_STATUSES:
            _update(ctx.home, lambda d, j=jid: d[j].update(done=True) if j in d else None)
            continue
        if p["attempt"] >= p["of"]:
            _update(ctx.home, lambda d, j=jid: d[j].update(done=True) if j in d else None)
            out.append((jid, None, p["attempt"], p["of"], status, f"all {p['of']} attempts were used"))
            continue
        fin = _finished_at(res)
        if fin is not None and now < fin + p["delay"]:
            continue                                                       # the delay has not passed: looked at again by the next arm-run command
        job = health.kept(ctx.home, jid)
        if job is None:
            _update(ctx.home, lambda d, j=jid: d[j].update(done=True) if j in d else None)
            out.append((jid, None, p["attempt"], p["of"], status, "this computer has no copy of the job to send again"))
            continue
        with place._locked(ctx.home, "retry.lock"):                         # another arm-run may have got here first
            if _read(ctx.home).get(jid, {}).get("next") or jid not in _read(ctx.home):
                continue
            new = _new_id(ctx.home, jid, p["attempt"] + 1)
            why = f"attempt {p['attempt'] + 1} of {p['of']}: {jid} ended {status}"
            queue.add(ctx.home, new, placed[jid].get("class") or job.get("class"), queue.DEFAULT, job, why, now=now)     # queued first: a crash before the plan is written repeats at most this one
            _update(ctx.home, lambda d, j=jid, n=new, q=p: (d[j].update(next=n), d.update({n: {"attempt": q["attempt"] + 1, "of": q["of"], "delay": q["delay"]}})))
            health.drop(ctx.home, jid)
            audit.retried(ctx, jid, new, placed[jid].get("class") or job.get("class"), p["attempt"] + 1, p["of"], why, now)
        out.append((jid, new, p["attempt"] + 1, p["of"], status, ""))
    return out
