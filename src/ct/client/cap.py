"""The host job cap: how many jobs this computer will have running at once across all its workers.

Each worker limits itself (its capacity, its own limits), but a session on THIS computer that sends jobs to several workers, or a script in a loop, can pile
them up faster than anyone looks. `limits.max_in_flight` (default 8; 0 means no cap) is checked by `ct-run submit` before anything is sent: the jobs this computer sent that are
queued or running on any worker you administer, plus the ones about to be sent, must not pass it. A job waiting for YOUR approval is not counted: it is not running, and the wait is
the brake. A job with no result yet counts only for 24 hours, so one that never came back cannot block sending forever. When a worker cannot be read, the last downloaded results
are used (this is a brake, not a gate on the network). Standard library only.
"""
import json
import time
from pathlib import Path

from . import refuse
from .repo import PENDING

RUNNING = tuple(s for s in PENDING if s != "awaiting_approval")
MAX_AGE_S = 24 * 3600


def jobs_sent(repos):
    """{job id: created epoch seconds} of the job files on the jobs branch (what this computer sent and has not had pruned)."""
    return {jid: created for jid, (created, _) in jobs_sent_detail(repos).items()}


def jobs_sent_detail(repos):
    """{job id: (created epoch seconds, class)} of the same files; the class is "" when the file does not say."""
    out = {}
    names = [n for n in repos.git(repos.queue, "ls-tree", "-r", "--name-only", f"origin/{repos.jobs_branch}", check=False).stdout.split("\n") if n]
    texts = repos.show_files(f"origin/{repos.jobs_branch}", names)                 # one git process for all of them
    for path in names:
        txt = texts.get(path) or ""
        try:
            from datetime import datetime
            doc = json.loads(txt)
            cls = doc.get("class") or (doc.get("job") or {}).get("class") or ""
            out[Path(path).stem] = (datetime.fromisoformat(doc["created"].replace("Z", "+00:00")).timestamp(), cls if isinstance(cls, str) else "")
        except Exception:
            out[Path(path).stem] = (time.time(), "")                       # a file we cannot date counts as new: the cap errs on the side of fewer jobs
    return out


def in_flight(repos, now=None):
    """The ids of jobs sent through `repos` that are queued or running (or not yet seen) and younger than a day."""
    return list(in_flight_detail(repos, now))


def in_flight_detail(repos, now=None):
    """{job id: class} of those jobs (class "" when unknown)."""
    now = time.time() if now is None else now
    try:
        repos.fetch_queue(max_age=15)                        # a brake, not a gate: a look made moments ago (the placement's, the send's) is good enough
        repos.pull_results(max_age=15)
    except Exception:
        pass                                                         # the last downloaded state is good enough for a brake
    live = {}
    for jid, (created, cls) in jobs_sent_detail(repos).items():
        if now - created > MAX_AGE_S:
            continue
        res = repos.read_result(jid)
        if res is None or res.get("status") in RUNNING:
            live[jid] = cls
    return live


def check(fleet, cap, adding, now=None):
    """Raise ClientError when sending `adding` more jobs would put more than `cap` in flight across `fleet` ([(name, repos)]). Returns the count in flight."""
    if not cap:
        return 0
    per = {name: in_flight(repos, now) for name, repos in fleet}
    total = sum(len(v) for v in per.values())
    if total + adding > cap:
        where = ", ".join(f"{name}: {len(v)}" for name, v in per.items() if v) or "none"
        raise refuse.error("placement.capacity.in_flight", ("In flight", str(total)), ("Cap", str(cap)), adding=adding, where=where)
    return total


def share_problem(per, worker, cls, per_worker, per_class, extra_worker=0, extra_class=0, reserved=()):
    """Why a job of class `cls` bound for `worker` is over its share, as (kind, sentence) with kind "worker" or "class", or None when it fits. `per` is {worker: {job id: class}} (in_flight_detail
    of each), `per_worker` the most jobs in flight on one worker, `per_class` the most of one class across every worker (0 = no limit for either). `extra_*` are jobs of this batch or drain already
    decided but not yet visible, `reserved` the (worker, class) of sends in progress on this computer. A job over its share is queued, never refused: the limit is a turn, not a ban."""
    here = len(per.get(worker, {})) + extra_worker + sum(1 for w, _ in reserved if w == worker)
    if per_worker and here >= per_worker:
        return "worker", f"{worker} already has {here} in flight, its limit is {per_worker} (limits.max_per_worker)"
    same = sum(1 for jobs in per.values() for c in jobs.values() if c == cls) + extra_class + sum(1 for _, c in reserved if c == cls)
    if per_class and cls and same >= per_class:
        return "class", f"{same} of {cls} are already in flight, the limit for one command is {per_class} (limits.max_per_class)"
    return None


def checker_for(ctx, now=None):
    """None when neither limit is set (nothing is read, nothing changes); else `check(worker, cls, extra_worker=0, extra_class=0, skip=None)` -> share_problem's answer. The workers are looked at
    ONCE, the first time it is asked (a drain over fifty waiting jobs is one look, not fifty); sends in progress on this computer are read fresh each time, so two sends at the same moment see each other.
    `skip` is the caller's own reservation token (it must not count itself)."""
    from . import place
    per_worker, per_class = getattr(ctx, "max_per_worker", 0), getattr(ctx, "max_per_class", 0)
    if not per_worker and not per_class:
        return None
    seen = {}

    def check(worker, cls, extra_worker=0, extra_class=0, skip=None):
        if "per" not in seen:
            seen["per"] = {name: in_flight_detail(repos, now) for name, repos in ctx.fleet()}
        return share_problem(seen["per"], worker, cls, per_worker, per_class, extra_worker, extra_class, place.reserved(ctx.home, skip))
    return check
