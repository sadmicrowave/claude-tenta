"""The waiting list on this computer: a job that fits no worker right now waits here, in order, instead of being pushed onto the worker with the most room.

One file in the client home (`queue.json`, mode 0600), changed under its own lock, so it survives a restart of `arm-run` and a kill in the middle of a write. Nothing runs in the background: every
`arm-run` command calls `drain`, which sends the oldest highest-priority job that now fits a worker. Only the orchestrator's own choice queues: a job sent with `--worker` or by a folder's pin goes
where it was told, as before. Dependencies between jobs, a daemon and fair share are not here (see docs/design/ORCHESTRATOR-ROADMAP.md, R1).
"""
import contextlib
import fcntl
import json
import os
import time
from pathlib import Path

from ..words import plural
from . import audit, place

PRIORITIES = ("low", "normal", "high")
DEFAULT = "normal"
WAITED_LONG_S = 3600                    # a job waiting longer than this is said so on every command, not left to be found
EXPIRE_S = 7 * 86400                    # a job that has waited this long ends, with the reason, instead of waiting for ever
SENDING_STALE_S = 120                   # a send marked `sending` for longer than this died (same figure as a dead reservation, place.RESERVE_FOR_S)


def _file(home):
    return Path(home) / "queue.json"


def _read(home):
    try:
        data = json.loads(_file(home).read_text())
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _change(home, change):
    """Read-change-write under the queue's own lock; the file is replaced whole and is private to the person."""
    with place._locked(home, "queue.lock"):
        items = _read(home)
        out = change(items)
        items = items if out is None else out
        _write_private(_file(home), json.dumps(items, indent=1))
        return items


def rank(item):
    """Sort key: highest priority first, then the one that has waited longest."""
    p = item.get("priority")
    return (-(PRIORITIES.index(p) if p in PRIORITIES else 1), item.get("at", 0), item.get("id", ""))


def waiting(home):
    return sorted(_read(home), key=rank)


def find(home, jid):
    return next((i for i in _read(home) if i.get("id") == jid), None)


def _payload(home, jid):
    return Path(home) / "queue" / f"{jid}.json"


def _write_private(path, text):
    """A file only the person can read, replaced whole and flushed to disk (a kill leaves the old file or the new one, never half)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def job_of(home, item):
    """The job to send: its own file (attached files can be large; the index stays small and is rewritten often)."""
    return json.loads(_payload(home, item["id"]).read_text())


def ids(home):
    return {i.get("id") for i in _read(home)}


def add(home, jid, cls, priority, job, why, now=None):
    """Queue a job. The payload file is written first, so an index entry never points at nothing. A job id already waiting is refused."""
    if jid in ids(home):
        raise ValueError(f"a job called {jid} is already waiting")
    _write_private(_payload(home, jid), json.dumps(job))
    entry = {"id": jid, "class": cls, "priority": priority if priority in PRIORITIES else DEFAULT, "at": int(time.time() if now is None else now), "why": why}
    _change(home, lambda items: items.append(entry))
    return entry


def remove(home, jid):
    _change(home, lambda items: [i for i in items if i.get("id") != jid])
    with contextlib.suppress(OSError):
        _payload(home, jid).unlink()


def set_priority(home, jid, priority):
    """True if the job was waiting (a job being sent right now cannot be changed)."""
    hit = []

    def put(items):
        for i in items:
            if i.get("id") == jid and not i.get("sending"):
                i["priority"] = priority
                hit.append(1)
    _change(home, put)
    return bool(hit)


def cancel(home, jid):
    """True if the job was waiting and is now gone. A job whose send is in progress is not cancelled: it is already on its way."""
    item = find(home, jid)
    if not item or item.get("sending"):
        return False
    remove(home, jid)
    return True


def claim(home, jid, worker, now=None):
    """Mark a job as being sent to `worker` (a local write: the normal path costs no network call). False if another drain already took it."""
    won = []

    def put(items):
        for i in items:
            if i.get("id") == jid and not i.get("sending"):
                i["sending"] = {"worker": worker, "since": int(time.time() if now is None else now)}
                won.append(1)
    _change(home, put)
    return bool(won)


def unclaim(home, jid):
    def put(items):
        for i in items:
            if i.get("id") == jid:
                i.pop("sending", None)
    _change(home, put)


def note(home, jid, why):
    """Say why a job is still waiting (what the last look at the workers found)."""
    def put(items):
        for i in items:
            if i.get("id") == jid:
                i["why"] = why
    _change(home, put)


def age_text(seconds):
    s = max(0, int(seconds))
    return f"{s}s" if s < 90 else f"{s // 60} min" if s < 5400 else f"{s // 3600} h" if s < 172800 else plural(s // 86400, "day")


def why_of(choice):
    """The short reason a job could not be placed now, from the orchestrator's own explanation."""
    if choice.worker is None:
        return "no worker can take it: " + "; ".join(f"{n}: {r}" for n, _, r in choice.lines)[:200]
    return f"every worker is busy (the best, {choice.worker}, has no room)"


@contextlib.contextmanager
def _drain_lock(home):
    """Only one drain at a time on this computer; a second one does not wait, it has nothing to add (the first is already sending)."""
    Path(home).mkdir(parents=True, exist_ok=True)
    with open(Path(home) / "queue.drain.lock", "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _real_text(e):
    return (str(getattr(e, "why", "") or getattr(e, "title", "") or e).strip().splitlines() or ["send failed"])[0][:200]


def _recover(ctx, items, by_name, repos_of, sent, dropped, now):
    """After a crash only: a job marked `sending` for longer than a send takes. Ask the ONE worker it was being sent to whether it arrived; if so, it is placed, else it waits again."""
    for it in items:
        s = it.get("sending")
        if not s or now - s.get("since", 0) < SENDING_STALE_S:
            continue
        e = by_name.get(s.get("worker"))
        try:
            arrived = bool(e) and repos_of(e).job_known(it["id"])
        except Exception as ex:                                                 # cannot tell now: leave it marked, look again next time
            note(ctx.home, it["id"], "a send was cut short and the worker could not be asked whether it arrived: " + _real_text(ex))
            continue
        if arrived:
            place.record(ctx.home, [it["id"]], s["worker"], it["class"])
            audit.placed(ctx, [it["id"]], it["class"], s["worker"], None, it.get("priority"), "a send was cut short; the worker has the job")
            remove(ctx.home, it["id"])
            sent.append((it["id"], s["worker"]))
        else:
            unclaim(ctx.home, it["id"])


def drain(ctx, cfg, *, entries_fn=None, repos_of=None, fetch=None, send=None, check=None, share=None, now=None, dropped=None):
    """Send every waiting job that now fits a worker, highest priority then oldest first; returns [(job id, worker)] for the ones placed. A job that still fits nowhere stays, with the reason of this look.
    A job that has waited past EXPIRE_S, or whose command no worker offers any more, ends: it is removed and appended to `dropped` as (job id, reason)."""
    if not _read(ctx.home) or cfg is None:
        return []
    ents = (entries_fn or place.entries)(cfg)
    if not ents:
        return []
    repos_of = repos_of or (lambda e: ctx.repos if e[0] == ents[0][0] else place.ClientRepos.from_config(e[1]))
    from . import submit as sub
    send, check = send or sub.push_job, check or _cap_check
    dropped = [] if dropped is None else dropped
    now = time.time() if now is None else now
    by_name, sent = {e[0]: e for e in ents}, []
    from . import cap
    share = share or cap.checker_for(ctx)                                       # None with no limit set
    by_worker, by_class = {}, {}                                                # what this drain has sent so far: a look at the workers may not show it yet
    with _drain_lock(ctx.home) as mine:
        if not mine:
            return []
        _recover(ctx, waiting(ctx.home), by_name, repos_of, sent, dropped, now)
        for it in waiting(ctx.home):
            if it.get("sending"):
                continue
            if now - it.get("at", 0) > EXPIRE_S:
                remove(ctx.home, it["id"])
                dropped.append((it["id"], f"it waited {age_text(now - it['at'])} and no worker had room"))
                continue
            with place._locked(ctx.home, "placements.decide.lock"):             # the same step as a live placement: looking, choosing and reserving are one
                views = place.gather_shared(ents, repos_of, fetch, home=ctx.home, silent_s=place._silent_s(ctx))
                if views and all(v.reachable for v in views) and not any(it["class"] in ((v.registry or {}).get("classes") or {}) for v in views):
                    remove(ctx.home, it["id"])
                    dropped.append((it["id"], f"no worker offers {it['class']} any more"))
                    continue
                ch = place.decide(it["class"], views)
                if ch.worker is None or ch.busy_all:
                    note(ctx.home, it["id"], why_of(ch))
                    continue
                if share:                                                       # over its share: it keeps its place and the next job gets a look (this is what stops one batch taking every place)
                    cls = it["class"]
                    ch, over = place.choose_within_share(cls, views, None, lambda w: share(w, cls, by_worker.get(w, 0), by_class.get(cls, 0)), ch)
                    if over:
                        note(ctx.home, it["id"], over)
                        continue
                token = place.reserve(ctx.home, ch.worker, it["class"])
            try:
                if not claim(ctx.home, it["id"], ch.worker, now):               # someone cancelled it, or it is already being sent
                    continue
                check(ctx, 1)
                send(repos_of(by_name[ch.worker]), it["id"], job_of(ctx.home, it))
                place.record(ctx.home, [it["id"]], ch.worker, it["class"])
                audit.placed(ctx, [it["id"]], it["class"], ch.worker, ch, it.get("priority"), it.get("why"))
                from . import health
                if health.is_repeatable(next((v.registry for v in views if v.name == ch.worker), None), it["class"]):
                    health.keep(ctx.home, it["id"], job_of(ctx.home, it))
                    from . import retry
                    retry.plan_sent(ctx.home, next((v.registry for v in views if v.name == ch.worker), None), it["class"], [it["id"]])
                remove(ctx.home, it["id"])
                sent.append((it["id"], ch.worker))
                by_worker[ch.worker] = by_worker.get(ch.worker, 0) + 1
                by_class[it["class"]] = by_class.get(it["class"], 0) + 1
            except Exception as ex:                                             # the send failed (the cap, a locked or unreachable worker): it waits, with the real error
                unclaim(ctx.home, it["id"])
                note(ctx.home, it["id"], _real_text(ex))
            finally:
                place.release(ctx.home, token)
    return sent


def _cap_check(ctx, adding):
    from . import cap
    cap.check(ctx.fleet(), ctx.max_in_flight, adding)
