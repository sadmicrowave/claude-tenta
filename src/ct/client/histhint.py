"""The usage-history hint: after a `ct-run` job command, one line when the person's own command log (recorded and imported) holds commands with an offload reason, pointing at
`ct-run offload review`. A command every known worker already answers for is not counted. It reuses the recommend hint's rules (default on, an off switch, at most one line per `offload.history_hint_hours`, never fails the command) and its state file
style. Every input (the log's review, the remembered answers, the state, the clock) is passed in, so the logic is tested without a terminal or a network."""
import hashlib
import shlex
from pathlib import Path

from . import offload, recommend, review_flow

STATE_FILE = "history-hint.json"
CHECK_SECONDS = 3600          # the log is looked at no more than once an hour, whether or not a line was said


class State(recommend.HintState):
    """What this hint last said (`history-hint.json`, local, mode 0600): when, and a fingerprint of the set of commands it named."""
    def __init__(self, home):
        self.path = Path(home) / STATE_FILE


def fingerprint(shapes):
    return hashlib.sha256("\n".join(sorted(shapes)).encode()).hexdigest()[:16]


def settled(example, views):
    """True when every worker whose registry is known already answers for this command (registered there, or tried and excluded), so the review would offer nothing. `views` is
    [(registry info or None, exclusions)] read from local files; a worker that published nothing is unknown, and unknown keeps the command in."""
    try:
        argv = shlex.split(example)
    except ValueError:
        return False
    if not views or any(not info for info, _ in views):
        return False
    return all(offload.fit_lookup(argv, info, ex)["verdict"] in ("registered", "excluded") for info, ex in views)


def worth(recs, answers, own_tools, ask_again_days, views=()):
    """The recommended commands the review would still offer: not one a worker is never offered, not one the person answered not now (inside the ask-again days) or never, and not
    one every known worker already answers for."""
    return [r for r in recs if not review_flow._unsafe(r["example"], own_tools) and not answers.silenced(r["shape"], ask_again_days) and not settled(r["example"], views)]


def pick(review, answers, state, now, hours, own_tools, ask_again_days, views=()):
    """(facts, new state): `facts` is {kinds, seconds, untimed} for the one line to say now, or None. Said only when the review has something (state `ok`), when at least
    `hours` have passed since the last line, and when the set of commands differs from the one last named (a set that went away is forgotten, so its return is said again)."""
    new = dict(state)
    recs = worth(review.get("recs") or [], answers, own_tools, ask_again_days, views) if review.get("state") == "ok" else []
    if not recs:
        new["said"] = ""
        return None, new
    fp = fingerprint(r["shape"] for r in recs)
    if now - state.get("at", 0) < hours * 3600 or state.get("said") == fp:
        return None, new
    new.update(at=now, said=fp)
    return {"kinds": len(recs), "seconds": sum(r["total"] for r in recs), "untimed": sum(r["untimed"] for r in recs)}, new


def mark_seen(home, review, answers, now, own_tools, ask_again_days, views=()):
    """The review was just shown: the set it showed need not be hinted at. Never raises."""
    try:
        recs = worth(review.get("recs") or [], answers, own_tools, ask_again_days, views) if review.get("state") == "ok" else []
        st = State(home)
        d = st.read()
        d["said"] = fingerprint(r["shape"] for r in recs) if recs else ""
        d.setdefault("at", 0)
        st.write(d)
    except Exception:
        pass
