"""Would a pinned command run better on another worker? Answered only from this computer's own finished jobs (docs/design/OFFLOAD-REASONS.md, item 8b).

`RunLog` keeps one line per finished job (local, mode 0600, trimmed). `recommend` is pure: rows and pins in, one verdict per pinned command out. It never guesses from
hardware and never changes a pin; the person applies the printed `ct-run use` line themselves. Standard library only.
"""
import json
import os
import time
from pathlib import Path
from statistics import median

KEEP_DAYS = 60
OK = "ok"                                                       # the worker's own word for a job that ran to a clean end


def _secs(res, parse_when):
    a, b = parse_when(res.get("started")), parse_when(res.get("finished"))
    return max(0, int(round((b - a).total_seconds()))) if a and b else None


class RunLog:
    def __init__(self, home):
        self.path = Path(home) / "runs.jsonl"

    def read(self):
        out = []
        try:
            for line in self.path.read_text().splitlines():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
        except OSError:
            pass
        return out

    def observe(self, results, worker_of, parse_when, now=None):
        """Record each finished job in `results` ({job id: result}) once. `worker_of(jid)` names the worker it ran on (None: unknown, skipped). Never raises:
        a history problem must not break the command that looked at the jobs."""
        try:
            now = time.time() if now is None else now
            rows = self.read()
            seen = {r["job"] for r in rows}
            fresh = []
            for jid, res in results.items():
                secs, worker = _secs(res, parse_when), worker_of(jid)
                if jid in seen or secs is None or not worker or not res.get("class") or res.get("status") not in (OK, "error", "failed", "killed"):
                    continue
                fresh.append({"job": jid, "class": res["class"], "worker": worker, "secs": secs, "ok": res.get("status") == OK,
                              "at": int(parse_when(res.get("finished")).timestamp())})
            cutoff = now - KEEP_DAYS * 86400
            fresh = [r for r in fresh if r["at"] >= cutoff]
            if not fresh:
                return 0
            keep = [r for r in rows + fresh if r.get("at", 0) >= cutoff]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
            tmp.write_text("".join(json.dumps(r) + "\n" for r in keep))
            tmp.chmod(0o600)
            tmp.replace(self.path)
            return len(fresh)
        except OSError:
            return 0

    def clear(self):
        self.path.unlink(missing_ok=True)


def recommend(rows, pins, min_runs=3, min_gain_pct=25):
    """One verdict per pinned command (`pins`: {class: worker}), in class order. Only successful runs count, and the median of them, never the mean.
    Verdicts: `better` (another worker's median is at least `min_gain_pct` percent lower, with `min_runs` runs on both), `keep` (the pinned worker is as fast,
    or nearly), `more_runs` (a worker has too few runs to compare; `need` names who, and how many it has), `no_runs` (the pinned worker has none). Each verdict
    carries the medians behind it."""
    out = []
    for cls in sorted(pins):
        pinned = pins[cls]
        by = {}
        for r in rows:
            if r.get("class") == cls and r.get("ok"):
                by.setdefault(r["worker"], []).append(r["secs"])
        med = {w: median(v) for w, v in by.items() if len(v) >= min_runs}
        v = {"class": cls, "pinned": pinned, "runs": {w: len(s) for w, s in sorted(by.items())}, "median": dict(sorted(med.items()))}
        if pinned not in med:
            v.update(verdict="no_runs" if pinned not in by else "more_runs", need={w: len(s) for w, s in by.items() if len(s) < min_runs} or {pinned: len(by.get(pinned, []))})
        else:
            best = min((w for w in med if w != pinned), key=lambda w: (med[w], w), default=None)
            gain = round(100 * (med[pinned] - med[best]) / med[pinned]) if best is not None and med[pinned] > 0 else 0
            if best is not None and gain >= min_gain_pct:
                v.update(verdict="better", worker=best, gain_pct=gain)
            else:
                short = {w: len(s) for w, s in by.items() if w != pinned and len(s) < min_runs}
                v.update(verdict="more_runs" if short else "keep", need=short)
        out.append(v)
    return out


class HintState:
    """What the hint last said (`recommend-hint.json`, local, mode 0600): when, and which suggestion per command. Unreadable or missing means nothing said yet."""
    def __init__(self, home):
        self.path = Path(home) / "recommend-hint.json"

    def read(self):
        try:
            d = json.loads(self.path.read_text())
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def write(self, d):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(d))
            tmp.chmod(0o600)
            tmp.replace(self.path)
        except OSError:
            pass


def hint(verdicts, state, now, hours=24):
    """The one verdict worth a hint line now, or None, with the new state. Only a `better` verdict counts; it is shown once per suggestion (pinned worker and faster
    worker), and no two hints come closer than `hours`. A command whose suggestion went away forgets it, so a later return of the same suggestion is said again."""
    seen = {v["class"]: f"{v['pinned']}>{v['worker']}" for v in verdicts if v["verdict"] == "better"}
    said = {c: k for c, k in state.get("said", {}).items() if seen.get(c) == k}
    new = {"at": state.get("at", 0), "said": said}
    if now - state.get("at", 0) < hours * 3600:
        return None, new
    for v in verdicts:
        if v["verdict"] == "better" and said.get(v["class"]) != seen[v["class"]]:
            said[v["class"]] = seen[v["class"]]
            new["at"] = now
            return v, new
    return None, new
