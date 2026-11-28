"""Telling a human, and locking the worker when something is wrong.

Two separate events, on purpose:
  * a QUARANTINE / BREACH alert says what was withheld and why (one plain sentence, never the matched text), and
  * a LOCKDOWN alert says the whole machine is now locked, once, when the lockdown starts.
While locked, reminders go out on a schedule (default 1, 6, 24 hours, then daily, at normal priority) because a quiet phone must not
mean "all is well". Alerts stay short: they carry no class names, rule names, file paths or addresses; those are in the log.
"""
import json
import subprocess
import urllib.request
from pathlib import Path

from .guard import why_text
from .util import human_hours, iso, now, parse_iso

DEFAULT_NEXT_STEPS = "Review: arm-admin quarantine\nThen clear: arm-admin unlock"


class Notifier:
    """Sends an alert to the log, to syslog and, if configured, to an https push endpoint (ntfy style). A failing channel never raises."""

    def __init__(self, url="", token="", opener=None, log=None, syslog=True, product="armada"):
        self.url, self.token, self.product = url or "", token or "", product
        self._open = opener or (lambda req: urllib.request.urlopen(req, timeout=8).close())
        self._log = log or (lambda msg: None)
        self._syslog = syslog

    def send(self, title, body, fields=None, tags="rotating_light", priority="urgent"):
        """`fields` is [(label, value)]: the phone gets one labelled line each. The log keeps one short line."""
        push = "\n".join(f"{k}: {v}" for k, v in fields if v) if fields else body
        self._log(f"SECURITY ALERT: {title}: {body}")
        if self._syslog:
            try:
                subprocess.run(["logger", "-p", "auth.alert", "-t", self.product, f"{title}: {body}"], timeout=5)
            except (OSError, subprocess.SubprocessError):
                pass
        if not self.url.startswith("https://"):
            return False
        headers = {"Title": title.encode("ascii", "replace").decode(), "Priority": priority, "Tags": tags}
        if self.token:                       # a protected or self-hosted topic: the topic name is then not the only secret
            headers["Authorization"] = "Bearer " + self.token
        try:
            self._open(urllib.request.Request(self.url, data=push.encode("utf-8"), method="POST", headers=headers))
            return True
        except Exception as e:               # an alert channel being down must never break a pass
            self._log(f"alert push failed: {e}")
            return False


def reminders_due(elapsed_h, remind_at, repeat_h):
    """How many reminders should have been sent by now."""
    n = sum(1 for h in remind_at if elapsed_h >= h)
    if remind_at and elapsed_h >= remind_at[-1] and repeat_h > 0:
        n += int((elapsed_h - remind_at[-1]) // repeat_h)
    return n


class Lockdown:
    """The lock file, its reminders, and the two alerts. While `locked()` is true no job may start."""

    def __init__(self, state, notifier, seclog, product="armada", next_steps=DEFAULT_NEXT_STEPS,
                 remind_at=(1, 6, 24), repeat_h=24, clock=now):
        self.state, self.notifier, self.seclog = Path(state), notifier, seclog
        self.product, self.next_steps = product, next_steps
        self.remind_at, self.repeat_h, self.clock = list(remind_at), repeat_h, clock

    @property
    def file(self):
        return self.state / "lockdown"

    def locked(self):
        try:
            return json.loads(self.file.read_text())
        except OSError:
            return None
        except ValueError:
            return {"reason": "unreadable lockdown file"}

    def public_status(self):
        """What may be published (security.json): that it is locked and since when, never details."""
        st = self.locked()
        doc = {"locked": bool(st), "updated": iso(self.clock())}
        if st:
            doc.update({k: st.get(k) for k in ("since", "job", "class", "rules", "kind")})
        return doc

    def engage(self, kind, job, rules, cls=None, detail=None):
        """Record the event, lock, and alert. `kind` is 'quarantine' (a result was withheld) or 'breach' (a decoy credential was exposed
        or read). The lockdown alert is sent only when this event starts the lockdown. Returns the event."""
        previous = self.locked()
        event = {"kind": kind, "job": job, "class": cls, "rules": sorted(rules)}
        self.seclog.append({**event, **({"detail": detail} if detail else {})})
        since = (previous or {}).get("since") or iso(self.clock())     # a second event does not restart the reminder clock
        self.file.write_text(json.dumps({**event, "since": since, **({"detail": detail} if detail else {})}))
        what = ("a job's output was withheld by the security guard" if kind == "quarantine" and cls is not None
                else "a decoy credential was exposed in a job's output" if kind == "breach" and cls is not None
                else "something read a decoy credential file")
        fields = [("What", what), ("Why", why_text(rules))] + ([("Job", job)] if cls is not None else [])
        self.notifier.send(f"{self.product} {kind.upper()}", f"{what}. Review it: arm-admin quarantine.", fields=fields,
                           tags="warning" if kind == "quarantine" else "rotating_light")
        if previous:
            self.notifier._log("lockdown already active; not announcing it again")
        else:
            self.notifier.send(f"{self.product} LOCKDOWN", f"locked down after {kind} (job {job}). No new jobs will start.",
                               fields=[("State", "LOCKED DOWN: no new jobs will start; running jobs finish"),
                                       ("Next", self.next_steps.replace("\n", "; "))], tags="lock")
        return event

    def clear(self):
        """Unlock. Returns the state that was cleared, or None if it was not locked."""
        st = self.locked()
        if not st:
            return None
        self.file.unlink(missing_ok=True)
        (self.state / "lockdown-reminders").unlink(missing_ok=True)
        self.seclog.append({"kind": "unlock", "was": st.get("job")})
        return st

    def remind_if_due(self):
        """Call every pass while locked. Sends at most one reminder per call and never repeats one already sent. Returns True if it sent one."""
        st = self.locked()
        since = parse_iso((st or {}).get("since"))
        if not st or since is None:
            return False
        elapsed_h = (self.clock() - since).total_seconds() / 3600
        mark = self.state / "lockdown-reminders"
        try:
            sent = json.loads(mark.read_text()) if mark.exists() else {}
        except ValueError:
            sent = {}
        done = sent.get("sent", 0) if sent.get("since") == st["since"] else 0
        due = reminders_due(elapsed_h, self.remind_at, self.repeat_h)
        if due <= done:
            return False
        mark.write_text(json.dumps({"since": st["since"], "sent": due}))      # recorded first: a failed push must not repeat every pass
        age = human_hours(elapsed_h)
        self.notifier.send(f"{self.product} STILL LOCKED", f"still locked down after {age}",
                           fields=[("State", f"still LOCKED DOWN after {age}: no jobs are running"), ("Why", why_text(st.get("rules") or [])),
                                   ("Next", self.next_steps.replace("\n", "; "))], tags="hourglass", priority="default")
        return True
