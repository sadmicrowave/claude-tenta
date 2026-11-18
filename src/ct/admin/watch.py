"""ct-admin watch: this computer collects what each worker published and raises the alerts, so a worker never has to reach out.

A Direct worker is outbound-silent (its firewall lets nothing out), so it cannot post an alert to a phone. Each worker already publishes `security.json` (locked or clear,
since when) and `worker-state.json` (when it last reported) into its results repository, and your computer already reads that repository. `watch` makes one pass over every
worker you administer, compares what it reads with what it saw last time, and alerts on a CHANGE only: a worker locking down, a reminder while it stays locked (the worker's own
schedule: 1, 6, 24 hours, then daily), unlocking, going silent, becoming unreachable, and coming back. Alerts carry no job names, rules or paths, as the worker's own never do.

One slow or dead worker must not slow the others or hammer them: every worker has its own next-check time, and a worker that fails to answer is asked less often each time
(doubling up to a ceiling) and once more at the normal pace when it answers again. With many workers a pass costs one short fetch per worker that is due, nothing more.
The memory is one small file (`watch.json`) next to your other client state.
"""
import json
import time
from pathlib import Path

from ..alerts import reminders_due
from ..util import human_hours, iso, parse_iso
from . import workers as workers_mod

BASE_S, CEILING_S = 60, 900          # how often a healthy worker is asked, and the longest wait after repeated failures
FAILURES_BEFORE_ALERT = 3            # one dropped connection is not news
REMIND_AT, REPEAT_H = (1, 6, 24), 24


def next_delay(failures, base=BASE_S, ceiling=CEILING_S):
    """Seconds until the worker is asked again: the normal pace while it answers, doubling with each failure up to the ceiling."""
    return base if failures <= 0 else min(base * (2 ** failures), ceiling)


def decide(name, security, state, fetched_ok, prev, now):
    """(events, new memory) for one worker. `security` and `state` are the published documents ({} / None when absent), `fetched_ok` says whether the fetch worked, `prev` is what
    was remembered. An event is (kind, title, text); only changes produce one."""
    mem = {"failures": 0, "locked_since": None, "reminders": 0, "silent": False, "unreachable": False, "paused": False, **(prev or {})}
    events = []
    if not fetched_ok:
        mem["failures"] += 1
        if mem["failures"] >= FAILURES_BEFORE_ALERT and not mem["unreachable"]:
            mem["unreachable"] = True
            events.append(("unreachable", f"{name} cannot be reached", f"{name} did not answer {mem['failures']} times in a row."))
        mem["next_at"] = now + next_delay(mem["failures"])
        return events, mem
    if mem["unreachable"]:
        events.append(("recovered", f"{name} is reachable again", f"{name} answered."))
    mem["failures"], mem["unreachable"] = 0, False
    mem["next_at"] = now + next_delay(0)
    locked = bool((security or {}).get("locked"))
    if locked and not mem["locked_since"]:
        mem["locked_since"] = security.get("since") or iso(now)
        since = parse_iso(mem["locked_since"])
        mem["reminders"] = reminders_due((now - since.timestamp()) / 3600, REMIND_AT, REPEAT_H) if since else 0       # a lock first noticed late is announced once, not announced and then reminded at once
        events.append(("locked", f"{name} LOCKED DOWN", f"{name} locked itself down; no new jobs will start. Review it: ct-admin quarantine."))
    elif locked:
        since = parse_iso(mem["locked_since"])
        due = reminders_due((now - since.timestamp()) / 3600 if since else 0, REMIND_AT, REPEAT_H)
        if due > mem["reminders"]:
            mem["reminders"] = due
            events.append(("reminder", f"{name} STILL LOCKED", f"{name} has been locked down for {human_hours((now - since.timestamp()) / 3600) if since else 'a while'}. Review it: ct-admin quarantine."))
    elif mem["locked_since"]:
        mem["locked_since"], mem["reminders"] = None, 0
        events.append(("unlocked", f"{name} unlocked", f"{name} is accepting jobs again."))
    age = workers_mod.age_s(state, now) if state else None
    silent = bool(state) and (age is None or age > workers_mod.STALE_S)
    if silent and not mem["silent"]:
        events.append(("silent", f"{name} has gone quiet", f"{name} has not reported for {human_hours(age / 3600) if age else 'a long time'}: it may be off, or its timers stopped."))
    if mem["silent"] and not silent and state:
        events.append(("back", f"{name} is reporting again", f"{name} published a fresh reading."))
    mem["silent"] = silent
    paused = bool((((state or {}).get("observed") or {}).get("claude_today") or {}).get("paused"))
    if paused and not mem["paused"]:
        events.append(("paused", f"{name} paused its Claude jobs", f"{name} reached its daily Claude limit; new Claude jobs wait until tomorrow or until the limit is raised. Nothing is wrong. See: ct-admin status"))
    mem["paused"] = paused
    return events, mem


class Memory:
    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        tmp.replace(self.path)


def one_pass(fleet, memory, notifier, now=None, fetch=None, say=None, force=False, on_ok=None, on_fail=None):
    """Ask every worker that is due, alert on changes, and remember. Returns [(worker, kind)] of what was raised. `fetch(repos)` refreshes the results clone (default: pull_results)."""
    now = time.time() if now is None else now
    fetch = fetch or (lambda repos: repos.pull_results())
    data, raised = memory.load(), []
    for name, repos in fleet:
        prev = data.get(name) or {}
        if not force and prev.get("next_at", 0) > now:
            continue
        try:
            fetch(repos)
            ok = True
        except Exception:
            ok = False
        hint = ""
        try:                                                      # best effort, never able to break a pass: remember where a worker answered; when it does not, say where it is now if that is cheap to know
            if ok and on_ok:
                on_ok(name, repos)
            elif not ok and on_fail:
                hint = on_fail(name, repos) or ""
        except Exception:
            pass
        events, mem = decide(name, repos.security() if ok else None, workers_mod.read_state(repos.results) if ok else None, ok, prev, now)
        data[name] = mem
        for kind, title, text in events:
            if kind == "unreachable" and hint:
                text = f"{text} {hint}"
            notifier.send(title, text, fields=[("Worker", name), ("What", text)],
                          tags="rotating_light" if kind in ("locked", "unreachable", "silent") else "white_check_mark" if kind in ("unlocked", "recovered", "back") else "hourglass",
                          priority="urgent" if kind in ("locked",) else "default")
            raised.append((name, kind))
            if say:
                say(name, kind, text)
    memory.save(data)
    return raised
