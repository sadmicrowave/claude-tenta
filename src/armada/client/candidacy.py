"""Is a command worth running on a worker? Judged against EVERY rung of the offload ladder (docs/design/OFFLOAD-REASONS.md), never stopping at the first miss.

`judge` is pure: it takes the facts about one command and returns one verdict per rung (met / not met / unknown, each with its reason) and the headline, the
highest-weighted rung that is met. Rung 2 (fewer AI tokens) cannot be judged until the AI layer exists, so it says so and is never guessed. The weights live in WEIGHTS only.
"""
import math
import time
from statistics import median

WEIGHTS = {1: 100, 2: 80, 3: 60, 4: 40}
NAMES = {1: "Worker-specific work", 2: "Fewer AI tokens", 3: "Faster", 4: "Free your own computer"}
MET, NOT_MET, UNKNOWN = "met", "not met", "unknown"


def runs_in_window(records, days, now=None):
    """How many of the log's records fall in the last `days` days (each record has an integer `ts`, seconds)."""
    now = time.time() if now is None else now
    cutoff = now - days * 86400
    return sum(1 for r in records if (r.get("ts") or 0) >= cutoff)


def _rung(n, verdict, reason):
    return {"rung": n, "name": NAMES[n], "weight": WEIGHTS[n], "verdict": verdict, "reason": reason}


def _worker_specific(f):
    if f.get("pinned"):
        return _rung(1, MET, "you pinned it to a worker")
    if f.get("class_bound"):
        return _rung(1, MET, "its class belongs to one worker")
    return _rung(1, NOT_MET, "it is not tied to a particular worker")


def _fewer_tokens(f):
    return _rung(2, UNKNOWN, "cannot be judged yet: needs the AI layer")


def _faster(f):
    if f.get("batchable") or f.get("splittable"):
        return _rung(3, MET, "it can be split into independent pieces that run at once")
    return _rung(3, NOT_MET, "nothing says it can be split into independent pieces")


def _free_computer(f, repeat_runs, min_seconds):
    med, runs = f.get("median_s"), f.get("runs") or 0
    if med is None:
        return _rung(4, UNKNOWN, "no timing recorded yet")
    if med >= min_seconds:
        return _rung(4, MET, f"a typical run takes {med:.0f} s (limit {min_seconds:.0f} s)")
    total = med * runs
    if runs >= repeat_runs and total >= min_seconds:
        return _rung(4, MET, f"it ran {runs} times, {total:.0f} s in all (repeats from {repeat_runs} runs)")
    if runs >= repeat_runs:
        return _rung(4, NOT_MET, f"it repeats ({runs} runs) but only {total:.0f} s in all, under {min_seconds:.0f} s")
    return _rung(4, NOT_MET, f"too quick ({med:.0f} s) and run {runs} of {repeat_runs} times needed")


def judge(facts, repeat_runs, min_seconds):
    """facts: runs (in the window), median_s (or None), pinned, class_bound, batchable, splittable. Returns {'rungs': [...], 'headline': rung number or None}."""
    rungs = [_worker_specific(facts), _fewer_tokens(facts), _faster(facts), _free_computer(facts, repeat_runs, min_seconds)]
    met = [r["rung"] for r in rungs if r["verdict"] == MET]
    return {"rungs": rungs, "headline": max(met, key=WEIGHTS.get) if met else None}


def tightest_span_days(stamps, k):
    """The shortest time, in days, in which the command was run `k` times in a row (k consecutive runs), or None when it was run fewer than k times."""
    ts = sorted(t for t in stamps if t)
    if k < 1 or len(ts) < k:
        return None
    return min(ts[i + k - 1] - ts[i] for i in range(len(ts) - k + 1)) / 86400


def suggest_days(per_shape_stamps, k):
    """(days, how many commands it is based on): the typical time, over the commands that reached `k` runs, to run one k times, rounded up to a whole day (at least 1).
    (None, 0) when no command has reached k runs: nothing is suggested from nothing."""
    spans = [d for d in (tightest_span_days(t, k) for t in per_shape_stamps) if d is not None]
    return (max(1, math.ceil(median(spans))), len(spans)) if spans else (None, 0)
