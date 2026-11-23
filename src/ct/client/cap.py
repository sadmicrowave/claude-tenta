"""The host job cap: how many jobs this computer will have running at once across all its workers.

Each worker limits itself (its capacity, one Claude job at a time per class), but a session on THIS computer that sends jobs to several workers, or a script in a loop, can pile
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
    out = {}
    names = [n for n in repos.git(repos.queue, "ls-tree", "-r", "--name-only", f"origin/{repos.jobs_branch}", check=False).stdout.split("\n") if n]
    texts = repos.show_files(f"origin/{repos.jobs_branch}", names)                 # one git process for all of them
    for path in names:
        txt = texts.get(path) or ""
        try:
            from datetime import datetime
            out[Path(path).stem] = datetime.fromisoformat(json.loads(txt)["created"].replace("Z", "+00:00")).timestamp()
        except Exception:
            out[Path(path).stem] = time.time()                       # a file we cannot date counts as new: the cap errs on the side of fewer jobs
    return out


def in_flight(repos, now=None):
    """The ids of jobs sent through `repos` that are queued or running (or not yet seen) and younger than a day."""
    now = time.time() if now is None else now
    try:
        repos.fetch_queue(max_age=15)                        # a brake, not a gate: a look made moments ago (the placement's, the send's) is good enough
        repos.pull_results(max_age=15)
    except Exception:
        pass                                                         # the last downloaded state is good enough for a brake
    live = []
    for jid, created in jobs_sent(repos).items():
        if now - created > MAX_AGE_S:
            continue
        res = repos.read_result(jid)
        if res is None or res.get("status") in RUNNING:
            live.append(jid)
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
