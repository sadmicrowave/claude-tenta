"""What the client shows: a job's result, the job list, the class list, and the live wait. Functions return lines so they can be tested;
colour is added only for a terminal (see cw.ui)."""
import json
import os
import shutil
import sys
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path

from .. import cards, ui
from ..ui import BOLD, CYAN, DIM, GREEN, ITALIC, RED, YELLOW, paint
from .repo import PENDING

STATUS_COLOR = {"queued": CYAN, "running": CYAN, "ok": GREEN, "error": RED, "rejected": RED, "killed": RED, "quarantined": RED, "limit": YELLOW,
                "cancelled": DIM, "awaiting_approval": YELLOW}
STATUS_MEANING = (("queued", "submitted, the worker has not picked it up yet"), ("running", "approved, being run on the worker right now"),
                  ("ok", "finished fine"), ("error", "the command ran and failed, or could not start"), ("rejected", "the worker refused it (see the note)"),
                  ("limit", "stopped at its time, memory or heat limit"), ("killed", "stopped by a safety check"),
                  ("cancelled", "you cancelled it"), ("awaiting_approval", "waiting for you:  approve <job>"))
NOTE_STYLE = {"queued": (YELLOW,), "error": (RED,), "rejected": (RED,), "killed": (RED,), "quarantined": (RED,), "limit": (YELLOW,)}
VERDICT_COLOR = {"fits": GREEN, "does-not-fit": RED, "inconclusive": YELLOW}
MARGIN = "\t"            # one tab: where a row's note starts, under the columns
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def human_age(when, now=None):
    if not when:
        return "-"
    sec = int(((now or datetime.now(timezone.utc)) - when).total_seconds())
    return "just now" if sec < 60 else f"{sec // 60} min" if sec < 5400 else f"{sec // 3600} h {sec % 3600 // 60:02d} min" if sec < 172800 else f"{sec // 86400} days"


def human_dur(sec):
    """'45 s', '12 min', '3 h 05 min', '2 days 4 h': short enough for a table column."""
    sec = int(sec)
    if sec < 90:
        return f"{sec} s"
    m = sec // 60
    if m < 90:
        return f"{m} min"
    h, m = divmod(m, 60)
    if h < 48:
        return f"{h} h {m:02d} min"
    days, h = divmod(h, 24)
    return f"{days} days {h} h"


def parse_when(text):
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None


def round_trip(res, received=None):
    """(total, waited, ran, back) in seconds for a finished job: from the client sending it to the client having its result in hand, and where the time went:
    waiting to be picked up (and approved), running, and the result coming back (the worker publishing it to GitHub and this computer fetching it). `received` is the
    moment of seeing it; it defaults to now. Without it there is no honest end to the trip, so `back` is None and the total stops at the worker's publish time.
    None when the result does not say when it was sent (an older result) or never started. Never negative: two clocks differ."""
    sent, started, done = parse_when(res.get("created")), parse_when(res.get("started")), parse_when(res.get("finished"))
    if not (sent and started and done):
        return None
    secs = lambda a, b: max(0, int(round((b - a).total_seconds())))      # noqa: E731
    end = received if received is not None else datetime.now(timezone.utc)
    seen = parse_when(res.get("seen"))                                    # when the worker first saw it, if it says (newer workers do)
    return secs(sent, end), secs(sent, started), secs(started, done), max(0, secs(sent, end) - secs(sent, done)), (min(secs(sent, seen), secs(sent, started)) if seen else None)


def trip_rows(trip, label=str):
    """The round trip as four labelled rows, so each part reads on its own line."""
    total, waited, ran, back, noticed = trip
    wait_rows = ([(label("noticing"), f"{human_dur(noticed)}  until the worker's next look at GitHub found it"),
                  (label("starting"), f"{human_dur(waited - noticed)}  your approval, a free slot, and the next start pass")] if noticed is not None
                 else [(label("waiting"), f"{human_dur(waited)}  until the worker started it (noticing it, and your approval)")])
    return [(label("total"), f"{human_dur(total)}  from sending the job to having its result here")] + wait_rows + [
        (label("running"), f"{human_dur(ran)}  on the worker"), (label("coming back"), f"{human_dur(back)}  the result published to GitHub and fetched here")]


# ---- one result ----
def describe_profile(pr):
    """What was run and why, in the words the requester gave, for the top of a profile result."""
    import shlex
    L = ["", paint(f"{'Command':<22}", BOLD) + " " + paint(" ".join(shlex.quote(str(a)) for a in pr.get("argv", [])), BOLD, GREEN)]
    for label, key in (("What it does", "what"), ("Why", "why"), ("Expected", "expect"), ("Reads/writes/contacts", "side_effects"), ("Bad signs", "watch_for")):
        if pr.get(key):
            L.append(paint(f"{label:<22}", BOLD) + " " + pr[key])
    for flag, meaning in (pr.get("flags") or {}).items():
        L.append(paint(f"{'Flag ' + flag:<22}", BOLD) + " " + meaning)
    if pr.get("files"):
        L.append(paint(f"{'Input files':<22}", BOLD) + " " + ", ".join(pr["files"]))
    return "\n".join(L) + "\n"


def format_result(res, admin="cw-admin", suggest_name=None, received=None):
    """A result as the lines the person reads (to stderr, with the OUTPUT block printed separately by the caller). Returns (head, output, tail)."""
    status = res["status"]
    head = [f"\n{paint('JOB', BOLD, CYAN)}     {res['id']}",
            f"{paint('STATUS', BOLD, CYAN)}  {paint(status, BOLD, STATUS_COLOR.get(status, '0'))}" + (f"   {paint(res['note'], DIM)}" if res.get("note") else "")]
    if res.get("profile"):
        head.append(describe_profile(res["profile"]))
    head.append(paint("OUTPUT", BOLD, CYAN))
    tail, m = [], res.get("metrics")
    trip = round_trip(res, received)
    if m:
        tail.append(f"\n{paint('MEASURED', BOLD, CYAN)}")
        temp = f"{m['peak_temp_c']} C" if m.get("peak_temp_c") else "n/a"
        for label, val in (("time", f"{m.get('wall_s')} s wall, {m.get('cpu_s')} s cpu"), ("memory", f"peak {m.get('peak_rss_mb')} MB of {m.get('mem_total_mb')} MB"),
                           ("heat", f"peak {temp}"), ("disk", f"{m.get('disk_mb')} MB")):
            tail.append(f"  {paint(f'{label:<12}', DIM)}{val}")
    if trip:
        tail.append(f"\n{paint('ROUND TRIP', BOLD, CYAN)}")
        tail += [f"  {paint(f'{label:<12}', DIM)}{val}" for label, val in trip_rows(trip)]
    pr = res.get("profile")
    if pr:
        v = pr["verdict"]
        tail.append(f"\n{paint('VERDICT', BOLD, CYAN)}  {paint(v, BOLD, VERDICT_COLOR.get(v, '0'))}" + (f"   cost {pr['cost']}" if pr.get("cost") else ""))
        tail += [f"  - {why}" for why in pr.get("reasons", [])]
        est = pr.get("estimates") or {}
        if est and m:
            actual = {"time_s": (m.get("wall_s"), " s"), "mem_mb": (m.get("peak_rss_mb"), " MB"), "cpu_cores": (m.get("cores_busy"), " cores busy"), "disk_mb": (m.get("disk_mb"), " MB")}
            tail.append(f"\n{paint('ESTIMATE VS ACTUAL', BOLD, CYAN)}")
            for k, (val, unit) in actual.items():
                if k in est and val is not None:
                    note = ""
                    if val > est[k] * 1.5 and val - est[k] >= 1:
                        note = paint(f"   x{val / max(est[k], 0.01):.1f} over the estimate", YELLOW)
                    elif val < est[k] * 0.5 and est[k] - val >= 1:
                        note = paint("   well under the estimate", DIM)
                    tail.append(f"  {paint(f'{k.split(chr(95))[0]:<8}', DIM)}estimated ~{est[k]:g}{unit}, actual {val:g}{unit}{note}")
        if pr["verdict"] == "fits" and suggest_name:
            tail += [f"\n{paint('NEXT', BOLD, CYAN)}  {paint('register it and install it on the worker, asking you once', DIM)}\n",
                     paint(f"{admin} adopt {res['id']} --name {suggest_name}", BOLD, GREEN) + "\n", paint("The name is a suggestion; change it if you like.", DIM) + "\n"]
        elif pr["verdict"] in ("fits", "does-not-fit"):
            tail += [f"\n{paint('NEXT', BOLD, CYAN)}  {paint('record that it does not fit, so it is never offloaded', DIM)}\n", paint(f"cw-run offload propose {res['id']}", BOLD, GREEN) + "\n"]
    return head, res.get("output") or "", tail


def result_card(res, admin="cw-admin", suggest_name=None, received=None):
    """A result as the three things a person looks at, kept apart: `head` (who and what: the job, its status as a chip, its command and verdict), `output` (the job's own
    output, to be drawn in a box in the status colour) and `meta` (how long it took and what it used: quiet, beneath). Plus the command that usually comes next."""
    import shlex
    status, m, pr, trip = res["status"], res.get("metrics"), res.get("profile"), round_trip(res, received)
    rows = ([("Note", res["note"])] if res.get("note") else [])
    if pr:
        rows += [("Command", " ".join(shlex.quote(str(a)) for a in pr.get("argv", [])))]
        rows += [(label, pr[key]) for label, key in (("What it does", "what"), ("Why", "why"), ("Expected", "expect"), ("Reads/writes/contacts", "side_effects"), ("Bad signs", "watch_for")) if pr.get(key)]
        rows += [(f"Flag {flag}", meaning) for flag, meaning in (pr.get("flags") or {}).items()] + ([("Input files", ", ".join(pr["files"]))] if pr.get("files") else [])
    run, run_label = None, "Next"
    if pr:
        rows += [cards.Heading("Verdict"), ("Verdict", pr["verdict"] + (f"   cost {pr['cost']}" if pr.get("cost") else ""))] + [f"- {why}" for why in pr.get("reasons", [])]
        if pr["verdict"] == "fits" and suggest_name:
            run, run_label = f"{admin} adopt {res['id']} --name {suggest_name}", "Register it and install it on the worker, asking you once"
        elif pr["verdict"] in ("fits", "does-not-fit"):
            run, run_label = f"cw-run offload propose {res['id']}", "Record that it does not fit, so it is never offloaded"
    measured = []
    if m:
        temp = f"{m['peak_temp_c']} C" if m.get("peak_temp_c") else "n/a"
        measured = [("Time", f"{m.get('wall_s')} s wall, {m.get('cpu_s')} s cpu"), ("Memory", f"peak {m.get('peak_rss_mb')} MB of {m.get('mem_total_mb')} MB"), ("Heat", f"peak {temp}"), ("Disk", f"{m.get('disk_mb')} MB")]
    colour = STATUS_COLOR.get(status, CYAN)
    return {"head": dict(title=res["id"], details=rows, tag=status, color=colour),
            "output": {"lines": (res.get("output") or "").rstrip("\n").split("\n") if res.get("output") else ["(no output)"], "tag": status, "color": colour},
            "meta": [("Measured", measured), ("Round trip", trip_rows(trip, str.capitalize) if trip else [])], "run": run, "run_label": run_label}


def print_result(res, as_json=False, out=None, err=None, **kw):
    out, err = out or sys.stdout, err or sys.stderr
    if as_json:
        print(json.dumps(res, indent=2), file=out)
    elif getattr(out, "isatty", lambda: False)() and ui._color_on(out):              # a person watching: the job, its output in a box in the status colour, the numbers quietly beneath
        r = result_card(res, **kw)
        lines = [""] + cards.card(r["head"]["title"], r["head"]["details"], tag=r["head"]["tag"], color=r["head"]["color"])
        lines += [""] + cards.card("Output", [cards.Block(r["output"]["lines"])], tag=r["output"]["tag"], color=r["output"]["color"], framed=True)
        lines += cards.meta(r["meta"])
        if r["run"]:
            lines += cards.commands(r["run"], r["run_label"])
        print("\n".join(lines) + "\n", file=out)
    else:
        head, output, tail = format_result(res, **kw)
        print("\n".join(head), file=err)
        print(output, file=out)
        print("\n".join(tail), file=err)
    return 0 if res["status"] == "ok" else 1


# ---- the job list ----
def run_durations(results):
    """{job id: how long it ran}: finished - started; a job still running shows the time so far; the rest have none."""
    found = {}
    for jid in results:
        d = results[jid]
        start = parse_when(d.get("started") or "")
        if start is None or d.get("status") in PENDING:
            continue
        end = parse_when(d.get("finished") or "") or (datetime.now(timezone.utc) if d.get("status") == "running" else None)
        if end is not None and end >= start:
            found[d.get("id", jid)] = human_dur((end - start).total_seconds())
    return found


def status_rows(results, created_of, n=15, extra=()):
    """(when, status, id, note, class) for the n most recent jobs, newest first (n=None: all). Sorted by the job's own finish or start time,
    not by file time (a fresh checkout gives every file the same time). `created_of(jid)` gives a waiting job's submission time."""
    rows = []
    for jid, d in results.items():
        when = parse_when(d.get("finished") or d.get("started") or "")
        if when is None and d.get("status") in PENDING:
            when = created_of(d.get("id", jid))
        rows.append((when, d.get("status", "?"), d.get("id", jid), d.get("note") or "", d.get("class") or ""))
    rows += list(extra)
    floor = datetime.min.replace(tzinfo=timezone.utc)
    rows.sort(key=lambda r: r[0] or floor, reverse=True)
    return rows if n is None else rows[:n]


def queued_rows(known, listing, within_h=24, at=None, admin="cw-admin"):
    """Jobs on the jobs branch with no result yet: submitted, and not picked up by the worker (it looks about once a minute). Only recent
    ones, because a result that was pruned would otherwise make an old job look queued. `listing` is [(job id, job dict)]."""
    at, rows = at or datetime.now(timezone.utc), []
    for jid, job in listing:
        created = parse_when(job.get("created", ""))
        if jid in known or not created or (at - created).total_seconds() > within_h * 3600:
            continue
        mins = int((at - created).total_seconds() // 60)
        note = (f"the worker has not picked this up in {mins} min. It normally takes under a minute: is it running? ({admin} check)" if mins >= 5
                else "waiting for the worker to pick it up (it looks about once a minute)")
        rows.append((created, "queued", jid, note, job.get("class") or ""))
    return rows


def approve_hint(jid, pad, hints, out):
    """Where a job's approval can be given, one command per line (the commands in green), under its row in the job list."""
    print(pad + paint("approve it:", DIM, stream=out), file=out)
    width = max(len(h) for h, _ in hints)
    for where, cmd in hints:
        print(f"{pad}    {paint(where.ljust(width), DIM, stream=out)} {paint('>', DIM, stream=out)} {paint(cmd.format(jid), BOLD, GREEN, stream=out)}", file=out)


def render_status(rows, results, label_of, hints, admin="cw-admin", n=15, out=None, now=None):
    """The job list: the legend, the jobs waiting for you, then the table. Returns nothing; prints to `out`."""
    out = out or sys.stdout
    P = lambda *a, **k: print(*a, file=out, **k)   # noqa: E731
    waiting = [r for r in rows if r[1] == "awaiting_approval"]
    shown = rows if n is None else rows[:n]
    P(paint("Your most recent jobs, newest first.", BOLD, stream=out))
    P(f"\n{paint('WHEN', BOLD, stream=out)}{'':<6}your local time, when the job finished (or was submitted, if it has not run)")
    P(f"{paint('DURATION', BOLD, stream=out)}{'':<2}how long the run took (so far, if it is still running)")
    P(f"{paint('NAME', BOLD, stream=out)}{'':<6}the name chosen when it was profiled, or the class it ran")
    P(paint("STATUS", BOLD, stream=out))
    for word, meaning in STATUS_MEANING:
        P(f"  {paint(f'{word:<20}', BOLD, STATUS_COLOR.get(word, '0'), stream=out)}{meaning}")
    P(paint("\nSee one job in full:  cw-run result <job>", DIM, stream=out))
    P()
    if waiting:
        P(paint("WAITING FOR YOU", BOLD, CYAN, stream=out) + "  " + paint("these will not run until you approve them", DIM, stream=out))
        w = max(len(r[2]) for r in waiting)
        P(paint(f"  {'JOB':<{w}}  {'NAME':<28}WAITING", BOLD, stream=out))
        for when, _, jid, _, cls in waiting[:8]:
            P(f"  {paint(f'{jid:<{w}}', BOLD, YELLOW, stream=out)}  {label_of(jid, cls)[:27]:<28}{human_age(when, now)}")
        P("\n" + paint("Approve one (it then waits and prints the result)", BOLD, stream=out) + "\n")
        for r in waiting[:8]:
            P(paint(f"{admin} approve {r[2]}", BOLD, GREEN, stream=out))
        P()
    if not shown:
        P("No results yet.")
        return
    width = max(len(r[2]) for r in shown)
    took = run_durations(results)
    P(paint(f"{'WHEN':<17}  {'STATUS':<17}  {'DURATION':<10}  {'JOB':<{width}}  NAME", BOLD, stream=out))
    room = max(shutil.get_terminal_size((100, 24)).columns - 8 - 1, 40)      # a tab is 8 columns
    for when, status, jid, note, cls in shown:
        stamp = when.astimezone().strftime("%Y-%m-%d %H:%M") if when else "-"
        P(f"{stamp:<17}  {paint(f'{status:<17}', BOLD, STATUS_COLOR.get(status, '0'), stream=out)}  {took.get(jid, '-'):<10}  {jid:<{width}}  {label_of(jid, cls)}")
        if status == "awaiting_approval":
            approve_hint(jid, MARGIN, hints, out)                        # the stored note is a run-on sentence; show the commands instead
        elif note:
            style = NOTE_STYLE.get(status)
            for line in textwrap.wrap(note, room):
                P(MARGIN + (paint(line, ITALIC, *style, stream=out) if style else paint(line, DIM, stream=out)))


def render_classes(info, pending=(), excluded=0, out=None, product="the worker"):
    out = out or sys.stdout
    P = lambda *a, **k: print(*a, file=out, **k)   # noqa: E731
    P(paint(f"What {product} can run for you today.", BOLD, stream=out))
    P(paint(f"Installed as of {info['updated']}; {info['mem_total_mb']} MB RAM, {info['capacity']} capacity point(s).", DIM, stream=out))
    P(f"\n{paint('COST', BOLD, stream=out)}   how heavy it is on {product}")
    P(f"{paint('TIER', BOLD, stream=out)}   auto = runs as soon as you submit it,  approve = waits for your OK first\n")
    rows = []
    for name, c in sorted(info["classes"].items()):
        what = " ".join(c["run"]) if c.get("run") else ("read-only Claude on files you attach" if c["kind"] == "claude" else "profile one exact command (always needs your approval)")
        rows.append((name, c["cost"], c["tier"], what + (f"   params: {', '.join(c['params'])}" if c.get("params") else "")))
    w = max([len(r[0]) for r in rows] + [5])
    P(paint(f"{'CLASS':<{w}}  {'COST':<7}  {'TIER':<8}  RUNS", BOLD, stream=out))
    for name, cost, tier, what in rows:
        P(f"{paint(f'{name:<{w}}', BOLD, GREEN, stream=out)}  {cost:<7}  {tier:<8}  {what}")
    if pending:
        P(f"\nProposed, NOT installed yet (apply them): {', '.join(sorted(pending))}")
    if excluded:
        P(f"\nExcluded after profiling: {excluded}")


# ---- waiting for a result ----
PACE = {"idle": 90, "sleep": 300}          # how often a worker checks while idle / asleep; set from the configuration (see set_pace)


def set_pace(schedule):
    """Take the worker's polling pace from the configuration, so what a person is told to expect is what the worker does."""
    PACE.update(idle=int(schedule.idle_fetch_s) or 5, sleep=int(schedule.sleep_fetch_s) or int(schedule.idle_fetch_s) or 5)


def pace_text():
    """What to expect before a job is noticed: a busy worker looks every few seconds, an idle one less often, a sleeping one least."""
    return f"a busy worker looks every few seconds, an idle one every {PACE['idle']} s, a sleeping one every {max(1, PACE['sleep'] // 60)} min"


def phase_of(res, jid, admin="cw-admin", approved=False, product="the worker", waited=0):
    """(what to show, colour) for the one-line progress, from the latest published result (None = not picked up yet). `waited` is how many seconds the job has
    been waiting, so a job nobody picks up is described honestly instead of promising "within a minute" forever."""
    status = (res or {}).get("status")
    late = waited >= 120
    if approved and status in (None, "queued", "awaiting_approval"):
        return (f"approved, but {product} has not picked it up after {waited // 60} min: {admin} status shows why" if late else f"approved: waiting for {product} to pick it up"), (RED if late else CYAN)
    if not res and late:
        return f"sent {waited // 60} min ago and {product} has not picked it up: run `{admin} status` to see whether it is running", RED
    if not res:
        return f"sent: waiting for {product} to notice it (an idle worker looks every {PACE['idle']} s)", YELLOW
    if status == "queued":
        return f"queued on {product}: waiting for a free slot" + (f" ({waited // 60} min so far)" if waited >= 60 else ""), YELLOW
    if status == "awaiting_approval":
        return f"waiting for your approval ({admin} approve {jid})", YELLOW
    if status == "running":
        return (f"fetching packages on {product}" if "fetching packages" in (res.get("note") or "") else f"running on {product}"), CYAN
    return status or "waiting", CYAN


class Progress:
    """One live line on the terminal (spinner, phase, elapsed time). Silent when stderr is not a terminal."""

    def __init__(self, stream=None, clock=time.time):
        self.stream, self.clock = stream or sys.stderr, clock
        self.on = self.stream.isatty() and os.environ.get("CW_COLOR", "auto") != "never"
        self.t0, self.i, self.shown = clock(), 0, False

    def draw(self, text, color):
        if not self.on:
            return
        el = int(self.clock() - self.t0)
        try:
            width = os.get_terminal_size(self.stream.fileno()).columns - 1          # the terminal this line is drawn on, not the default of a pipe
        except (OSError, ValueError, AttributeError):
            width = shutil.get_terminal_size((80, 24)).columns - 1
        clock_text = f"  {el // 60}:{el % 60:02d}"
        spin = SPIN[self.i % len(SPIN)]
        chip, rest = (None, text)
        if color == YELLOW:                                   # a waiting line: its first word is a CAPITALS chip, so the eye lands on the status first
            word, _, rest = text.partition(" ")
            chip = f" {word.rstrip(':').upper()} "
        room = width - len(clock_text) - len(spin) - 1 - (len(chip) + 1 if chip else 0)
        rest = rest if len(rest) <= room else rest[:max(1, room - 1)] + "…"                 # one line, always: a long phase is cut with a mark, never wrapped
        self.i += 1
        if chip and self.stream.isatty():
            body = paint(spin + " ", BOLD, color, stream=self.stream) + paint(chip, "wait_chip", stream=self.stream) + " " + paint(rest + clock_text, BOLD, color, stream=self.stream)
            self.stream.write("\r" + body + "\x1b[K")
            self.stream.flush()
            self.shown = True
            return
        plain = (f"{spin} {chip.strip() + ' ' if chip else ''}{rest}") + clock_text
        self.stream.write("\r" + paint(plain, BOLD, color, stream=self.stream) + "\x1b[K")
        self.stream.flush()
        self.shown = True

    def clear(self):
        if self.on and self.shown:
            self.stream.write("\r\x1b[K")
            self.stream.flush()
            self.shown = False
