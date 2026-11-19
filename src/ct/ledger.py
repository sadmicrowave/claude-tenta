"""The Claude spend ledger: what Claude jobs used today on this worker, and a daily limit that pauses new ones.

A Claude job spends the owner's usage (an account token or an API key). Every Claude job is recorded when it STARTS (so a burst of jobs cannot all slip under the limit before any
finishes) and again when it ENDS with the turns and notional cost `claude -p --output-format json` reported. A day is a UTC calendar day: it is the same on every worker, wherever it is.
Dollars are only notional with an account token (a subscription is not billed per call), so the limits are on jobs and turns; the cost is recorded and shown, never enforced.

When a limit is reached the worker stops STARTING Claude jobs (they stay queued and start tomorrow, or when the limit is raised), says so once that day, and keeps running every other
kind of job: it is a pause, not a lockdown, and nothing is wrong. A limit of 0 means no limit. Standard library only.
"""
import json
from pathlib import Path

from .util import iso, now
from .words import plural

KEEP_DAYS = 35


def usage_of(stdout):
    """{turns, cost_usd, input_tokens, output_tokens} from the JSON `claude -p --output-format json` printed, or None when it is not that."""
    try:
        doc = json.loads(stdout)
    except (TypeError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    u = doc.get("usage") if isinstance(doc.get("usage"), dict) else {}
    out = {"turns": doc.get("num_turns"), "cost_usd": doc.get("total_cost_usd", doc.get("cost_usd")), "input_tokens": u.get("input_tokens"), "output_tokens": u.get("output_tokens")}
    out = {k: v for k, v in out.items() if isinstance(v, (int, float))}
    return out or None


class Ledger:
    def __init__(self, state, jobs_per_day=0, turns_per_day=0, clock=now):
        self.file = Path(state) / "ledger.jsonl"
        self.jobs_per_day, self.turns_per_day, self.clock = int(jobs_per_day or 0), int(turns_per_day or 0), clock

    def day(self):
        return iso(self.clock())[:10]

    def _rows(self):
        try:
            lines = self.file.read_text().splitlines()
        except OSError:
            return []
        rows = []
        for line in lines:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        return rows

    def _append(self, row):
        self.file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.file, "a") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
        self.file.chmod(0o600)

    def start(self, jid, cls_name):
        self._append({"day": self.day(), "at": iso(self.clock()), "job": jid, "class": cls_name, "event": "start"})

    def finish(self, jid, cls_name, usage, status=None):
        self._append({"day": self.day(), "at": iso(self.clock()), "job": jid, "class": cls_name, "event": "end", "status": status, **(usage or {})})

    def today(self):
        day, jobs, turns, cost = self.day(), 0, 0, 0.0
        for r in self._rows():
            if r.get("day") != day:
                continue
            if r.get("event") == "start":
                jobs += 1
            elif r.get("event") == "end":
                turns += int(r.get("turns") or 0)
                cost += float(r.get("cost_usd") or 0)
        return {"day": day, "jobs": jobs, "turns": turns, "cost_usd": round(cost, 4)}

    def exceeded(self):
        """A plain sentence saying which daily limit stops new Claude jobs, or None."""
        t = self.today()
        if self.jobs_per_day and t["jobs"] >= self.jobs_per_day:
            return f"the daily limit of {plural(self.jobs_per_day, 'Claude job')} is reached ({t['jobs']} started today, UTC)"
        if self.turns_per_day and t["turns"] >= self.turns_per_day:
            return f"the daily limit of {plural(self.turns_per_day, 'Claude turn')} is reached ({t['turns']} used today, UTC)"
        return None

    def summary(self):
        """What is published (worker-state.json): today's use, the limits and whether new Claude jobs are paused. Never job names."""
        why = self.exceeded()
        return {**self.today(), "limit_jobs": self.jobs_per_day, "limit_turns": self.turns_per_day, "paused": bool(why), "reason": why}

    def prune(self):
        """Drop rows older than KEEP_DAYS (called once a day by the step pass)."""
        rows = self._rows()
        if not rows:
            return 0
        cutoff = iso(self.clock().replace(microsecond=0) - __import__("datetime").timedelta(days=KEEP_DAYS))[:10]
        keep = [r for r in rows if r.get("day", "") >= cutoff]
        if len(keep) != len(rows):
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in keep))
            tmp.replace(self.file)
        return len(rows) - len(keep)
