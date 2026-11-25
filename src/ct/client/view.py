"""What the client shows: a job's result, the job list, the class list, and the live wait. Functions return lines so they can be tested;
colour is added only for a terminal (see ct.ui)."""
import json
import os
import shutil
import sys
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path

from .. import alias, cards, ui
from ..jobs import MAX_AGE_S
from ..ui import BOLD, CYAN, DIM, GREEN, ITALIC, RED, YELLOW, chip, paint
from .repo import PENDING
from ..words import plural

STATUS_COLOR = {"queued": CYAN, "running": CYAN, "ok": GREEN, "error": RED, "rejected": RED, "killed": RED, "quarantined": RED, "limit": YELLOW,
                "cancelled": DIM, "awaiting_approval": YELLOW, "expired": DIM}
STATUS_MEANING = (("queued", "submitted, the worker has not picked it up yet"), ("running", "approved, being run on the worker right now"),
                  ("ok", "finished fine"), ("error", "the command ran and failed, or could not start"), ("rejected", "the worker refused it (see the note)"),
                  ("limit", "stopped at its time, memory or heat limit"), ("killed", "stopped by a safety check"),
                  ("cancelled", "you cancelled it"), ("expired", "waited more than a day for approval; send it again"), ("awaiting_approval", "waiting for you:  approve <job>"))
NOTE_STYLE = {"queued": (YELLOW,), "error": (RED,), "rejected": (RED,), "killed": (RED,), "quarantined": (RED,), "limit": (YELLOW,)}
VERDICT_COLOR = {"fits": GREEN, "does-not-fit": RED, "inconclusive": YELLOW, "needs-longer": YELLOW}
MARGIN = "\t"            # one tab: where a row's note starts, under the columns
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def human_age(when, now=None):
    if not when:
        return "-"
    sec = int(((now or datetime.now(timezone.utc)) - when).total_seconds())
    return "just now" if sec < 60 else f"{sec // 60} min" if sec < 5400 else f"{sec // 3600} h {sec % 3600 // 60:02d} min" if sec < 172800 else f"{plural(sec // 86400, 'day')}"


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
    return f"{plural(days, 'day')} {h} h"


def parse_when(text):
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None


def round_trip(res, received=None):
    """(total, waited, ran, back) in seconds for a finished job: from the client sending it to the client having its result in hand, and where the time went:
    waiting to be picked up (and approved), running, and the result coming back (the worker publishing it and this computer fetching it). `received` is the
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
    store = STORE["name"]
    wait_rows = ([(label("noticing"), f"{human_dur(noticed)}  until the worker's next look {'at' if store == 'GitHub' else 'in'} {store} found it"),
                  (label("starting"), f"{human_dur(waited - noticed)}  your approval, a free slot, and the next start pass")] if noticed is not None
                 else [(label("waiting"), f"{human_dur(waited)}  until the worker started it (noticing it, and your approval)")])
    return [(label("total"), f"{human_dur(total)}  from sending the job to having its result here")] + wait_rows + [
        (label("running"), f"{human_dur(ran)}  on the worker"), (label("coming back"), f"{human_dur(back)}  the result published to {store} and fetched here")]


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


def format_result(res, admin="ct-admin", suggest_name=None, received=None, rerun=None):
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
                     chip(f"{admin} adopt {res['id']} --name {suggest_name}") + "\n", paint("The name is a suggestion; change it if you like.", DIM) + "\n"]
        elif pr["verdict"] in ("fits", "does-not-fit"):
            tail += [f"\n{paint('NEXT', BOLD, CYAN)}  {paint('record that it does not fit, so it is never offloaded', DIM)}\n", chip(f"ct-run offload propose {res['id']}") + "\n"]
        elif pr["verdict"] == "needs-longer":
            cmd, label, steps = needs_longer_next(pr, rerun)
            tail += [f"\n{paint('NEXT', BOLD, CYAN)}  {paint(label or steps[0], DIM)}\n"] + ([chip(cmd) + "\n"] if cmd else [f"  - {x}" for x in steps[1:]])
    if rerun and str(res.get("refusal") or "").startswith("files.unpack."):
        tail += [f"\n{paint('NEXT', BOLD, CYAN)}  {paint('send it again without compression', DIM)}\n", chip(rerun) + "\n"]
    return head, res.get("output") or "", tail


def needs_longer_next(pr, rerun):
    """(command, label, steps) for a profile run that stopped only for time: send it again with the next limit, or, at a shared computer's ceiling, say what that ceiling is."""
    if pr.get("next_limit_s"):
        label = f"Profile it again with a limit of {pr['next_limit_s']} s"
        return (rerun, label, []) if rerun else (None, "", [f"{label}: add --timeout-s {pr['next_limit_s']} to the command you ran."])
    return None, "", [f"A shared computer holds a profile run to {pr.get('ceiling_s'):g} s. If this computer is not shared, say so: ct-setup install --worker <its name> --set isolation.purpose=tenta-only"]


def result_card(res, admin="ct-admin", suggest_name=None, received=None, rerun=None):
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
            run, run_label = f"ct-run offload propose {res['id']}", "Record that it does not fit, so it is never offloaded"
        elif pr["verdict"] == "needs-longer":
            run, run_label, steps = needs_longer_next(pr, rerun)
            rows += [f"- {x}" for x in steps]
    if res.get("refusal"):                                                      # which rule refused it: the last row, so it can be looked up (docs/reference/refusals.md)
        rows += [("Code", res["refusal"])]
        if rerun and str(res["refusal"]).startswith("files.unpack."):
            run, run_label = rerun, "Send it again without compression"
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
        print(json.dumps(res, indent=2), file=out)                                  # output: data (--json)
    elif getattr(out, "isatty", lambda: False)() and ui._color_on(out):              # a person watching: the job, its output in a box in the status colour, the numbers quietly beneath
        r = result_card(res, **kw)
        lines = [""] + cards.card(r["head"]["title"], r["head"]["details"], tag=r["head"]["tag"], color=r["head"]["color"])
        if res.get("output"):                                                       # no empty section: a job that never ran has a reason in its card above, not a box that says "(no output)"
            lines += [""] + cards.card("Output", [cards.Block(r["output"]["lines"])], tag=r["output"]["tag"], color=r["output"]["color"], framed=True)
        lines += cards.meta(r["meta"])
        if r["run"]:
            lines += cards.commands(r["run"], r["run_label"])
        ui.show(lines + [""], stream=out)
    else:
        head, output, tail = format_result(res, **kw)
        print("\n".join(head), file=err)                                                 # output: plain text for a pipe
        print(output, file=out)                                                           # output: data (the job's own output, alone on stdout)
        print("\n".join(tail), file=err)                                                 # output: plain text for a pipe
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


def status_rows(results, created_of, n=15, extra=(), now=None):
    """(when, status, id, note, class) for the n most recent jobs, newest first (n=None: all). Sorted by the job's own finish or start time,
    not by file time (a fresh checkout gives every file the same time). `created_of(jid)` gives a waiting job's submission time."""
    rows = []
    for jid, d in results.items():
        when = parse_when(d.get("finished") or d.get("started") or "")
        if when is None and d.get("status") in PENDING:
            when = created_of(d.get("id", jid))
        status, note = d.get("status", "?"), d.get("note") or ""
        if status == "awaiting_approval" and when is not None and ((now or datetime.now(timezone.utc)) - when).total_seconds() > MAX_AGE_S:      # the worker may not have looked yet: a job this old cannot be approved, so it is not listed as waiting
            status, note = "expired", "it waited more than a day for approval, so it can no longer run; send it again"
        rows.append((when, status, d.get("id", jid), note, d.get("class") or ""))
    rows += list(extra)
    floor = datetime.min.replace(tzinfo=timezone.utc)
    rows.sort(key=lambda r: r[0] or floor, reverse=True)
    return rows if n is None else rows[:n]


def queued_rows(known, listing, within_h=24, at=None, admin="ct-admin"):
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


STATUS_ROLE = {"ok": "ok", "error": "bad", "rejected": "bad", "killed": "bad", "quarantined": "bad", "limit": "warn", "awaiting_approval": "warn", "queued": "primary", "running": "primary", "cancelled": "muted", "expired": "muted"}


job_blocks = cards.job_blocks                                                       # one definition for every list of jobs


def render_status(rows, results, label_of, hints, admin="ct-admin", n=15, out=None, now=None, where=None):
    """The job list as cards: the jobs waiting for you (with the commands that approve them), then your recent jobs, then what the status words mean."""
    out = out or sys.stdout
    waiting = [r for r in rows if r[1] == "awaiting_approval"]
    shown = rows if n is None else rows[:n]
    if waiting:
        ui.say("Waiting for you", *job_blocks([(r[2], [("CLASS", label_of(r[2], r[4])), ("WAITING", human_age(r[0], now))]) for r in waiting[:8]]),
               "These will not run until you approve them. Approving waits for the result and prints it.", tag="APPROVE", color=ui.YELLOW, stream=out,
               run=[f"{admin} approve {r[2]}" for r in waiting[:8]], run_label="Approve one")
    if not shown:
        ui.say("Your jobs", "No results yet.", tag="NONE", color=ui.PRIMARY, stream=out)
        return
    took = run_durations(results)
    entries = []
    for when, status, jid, note, cls in shown:
        stamp = when.astimezone().strftime("%Y-%m-%d %H:%M %Z") if when else "-"
        rows_ = [("STATUS", cards.Styled([(status.upper(), STATUS_ROLE.get(status)), (f"  {stamp}  ·  {took.get(jid, '-')}", "muted")])), *([("CLASS", label_of(jid, cls))] if not jid.startswith(f"{cls}-") else [])]          # a generated id already starts with its class
        if where is not None:
            rows_.append(("WORKER", where.get(jid, "")))
        if note and status != "awaiting_approval":
            rows_.append(("NOTE", note))
        entries.append((jid, rows_))
    ui.say("Your most recent jobs, newest first", *job_blocks(entries),
           "The time is your local time: when the job finished (or was submitted, if it has not run), then how long the run took (so far, if it is still running).", tag="JOBS", color=ui.PRIMARY, stream=out)
    ui.say("What the status words mean", *[(word, meaning) for word, meaning in STATUS_MEANING], "See one job in full:  ct-run result <job>", tag="LEGEND", color=ui.PRIMARY, stream=out)


def _what(c):
    return " ".join(c["run"]) if c.get("run") else ("read-only Claude on files you attach" if c["kind"] == "claude" else "profile one exact command (always needs your approval)")


TIER_WORDS = {"auto": "runs as soon as you submit it", "approve": "waits for your approval"}


def when_text(iso_text):
    """'2026-11-17T04:40:58Z' as '2026-11-17 04:40 UTC' (a time always says its zone)."""
    try:
        return datetime.strptime(iso_text, "%Y-%m-%dT%H:%M:%SZ").strftime("%Y-%m-%d %H:%M") + " UTC"
    except (TypeError, ValueError):
        return None


def class_rows(name, c, runs_on=None, installed=False):
    """One class as card rows: fixed labels, and everything a person named (the class, the workers) as a value, so a long name wraps in its own space and moves nothing else."""
    rows = [(cards.Chip("CLASS", "primary"), cards.Styled([(name, "strong")])), ("RUNS", _what(c))]
    rows += [("PARAMS", ", ".join(c["params"]))] if c.get("params") else []
    rows += [("TOOLS" if i == 0 else "", t) for i, t in enumerate(c.get("tools") or [])]                     # folders of tools that already exist on the worker, one per line
    if runs_on:                                                         # each worker on its own line, under the value column: a long name wraps alone and nothing is run together
        rows += [("RUNS ON" if i == 0 else "", w) for i, w in enumerate(runs_on)]
    rows += [("COST", f"{c['cost']}, {TIER_WORDS.get(c['tier'], c['tier'])}")]
    if installed:
        rows += [("INSTALLED", when_text(c.get("installed")) or "not recorded (this worker runs an older release)")]
    return rows


def _list(rows_by_class):
    out = []
    for i, rows in enumerate(rows_by_class):
        out += (["\n"] if i else []) + rows
    return out


def render_classes(info, pending=(), excluded=0, out=None, product="the worker"):
    """One worker's own list: the worker's own facts first (what it has, when its set of classes last changed), then each class with the day IT was installed."""
    out = out or sys.stdout
    facts = [("MEMORY", f"{info['mem_total_mb']} MB"), ("CAPACITY", plural(info["capacity"], "capacity point")), ("CLASSES", f"{len(info['classes'])} installed"),
             ("REGISTRY", f"last changed {when_text(info.get('updated')) or info.get('updated')}")]
    notes = [*([f"Proposed, NOT installed yet (apply them): {', '.join(sorted(pending))}"] if pending else []), *([f"Excluded after profiling: {excluded}"] if excluded else [])]
    ui.say("What this worker can run for you today", ("Worker", alias.show(product)), *facts, "\n", *_list([class_rows(n, c, installed=True) for n, c in sorted(info["classes"].items())]), *(["\n", *notes] if notes else []),
           tag="CLASSES", color=ui.PRIMARY, stream=out)


def render_fleet_classes(classes, unreachable=(), folder_worker=None, out=None):
    """Every class any of your workers offers, and WHICH workers offer it. `classes` is {name: {"info": the class as one worker published it, "offered_by": [workers], "dedicated": worker or None}}.
    A class dedicated to one worker says so, and says when that worker does not offer it (a job for it would have nowhere to run)."""
    out = out or sys.stdout
    blocks = []
    for name, c in sorted(classes.items()):
        on = [alias.show(w) for w in sorted(c["offered_by"])] or ["none"]
        if c["dedicated"]:                                              # the class goes only there: that worker alone, with a yellow DEDICATED chip after its name (and a red one when it does not offer the class)
            pieces = [(alias.show(c["dedicated"]), None), ("DEDICATED", "warn_chip")] + ([] if c["dedicated"] in c["offered_by"] else [("NOT OFFERED", "bad_chip")])
            on = [cards.Styled(pieces)]
        blocks.append(class_rows(name, c["info"], on))
    notes = ["RUNS ON lists the workers that offer the class. A dedicated class goes only to its worker (ct-admin workers use WORKER --command CLASS).",
             "One worker's own list, with its limits: ct-run classes --worker NAME."]
    notes += [f"Jobs from this folder go to {alias.show(folder_worker)} unless a class is set to another worker."] if folder_worker else []
    notes += [f"{w} could not be read ({why}), so what it offers is not listed." for w, why in unreachable]
    ui.say("What your workers can run for you today", *_list(blocks), "\n", *notes, tag="CLASSES", color=ui.PRIMARY, stream=out)


# ---- waiting for a result ----
STORE = {"name": "GitHub"}                  # what jobs and results pass through, in the words of a message (set with the pace, see set_pace)
PACE = {"idle": 90, "sleep": 300}          # how often a worker checks while idle / asleep; set from the configuration (see set_pace)


def set_pace(schedule, direct=False, store=None):
    """Take the worker's polling pace from the configuration, so what a person is told to expect is what the worker does."""
    PACE.update(idle=int(schedule.idle_fetch_s) or 5, sleep=int(schedule.sleep_fetch_s) or int(schedule.idle_fetch_s) or 5)
    if direct:                                                      # a Direct worker does not slow down when idle (see Settings in ct.cli)
        PACE.update(idle=5, sleep=5)
    STORE["name"] = store or ("the mailbox" if direct else "GitHub")        # what the worker and this computer pass jobs and results through, for the words of a message


def pace_text():
    """What to expect before a job is noticed: a busy worker looks every few seconds, an idle one less often, a sleeping one least."""
    if PACE["idle"] <= 5 and PACE["sleep"] <= 5:
        return "the worker looks every few seconds, busy or idle"
    return f"a busy worker looks every few seconds, an idle one every {PACE['idle']} s, a sleeping one every {max(1, PACE['sleep'] // 60)} min"


def late_after():
    """Seconds after which an unnoticed job is called late: the slowest pace the worker keeps (a sleeping one), plus a minute, and never under two minutes."""
    return max(120, max(PACE.values()) + 60)


def phase_of(res, jid, admin="ct-admin", approved=False, product="the worker", waited=0):
    """(what to show, colour) for the one-line progress, from the latest published result (None = not picked up yet). `waited` is how many seconds the job has
    been waiting, so a job nobody picks up is described honestly instead of promising "within a minute" forever."""
    status = (res or {}).get("status")
    late = waited >= late_after()
    if approved and status in (None, "queued", "awaiting_approval"):
        return (f"approved, but {alias.show(product)} has not picked it up after {waited // 60} min: `{admin} wake` looks now, `{admin} status` shows why" if late else f"approved: waiting for {alias.show(product)} to pick it up"), (RED if late else CYAN)
    if not res and late:
        return f"sent {waited // 60} min ago and {alias.show(product)} has not picked it up: `{admin} wake` looks now, `{admin} status` shows why", RED
    if not res:
        return f"sent: waiting for {alias.show(product)} to notice it (an idle worker looks every {PACE['idle']} s)", YELLOW
    if status == "queued":
        return f"queued on {alias.show(product)}: waiting for a free slot" + (f" ({waited // 60} min so far)" if waited >= 60 else ""), YELLOW
    if status == "awaiting_approval":
        return f"waiting for your approval ({admin} approve {jid})", YELLOW
    if status == "running":
        return (f"fetching packages on {alias.show(product)}" if "fetching packages" in (res.get("note") or "") else f"running on {alias.show(product)}"), CYAN
    return status or "waiting", CYAN


CHIP_FOR = {YELLOW: "wait_chip", CYAN: "primary_chip", RED: "bad_chip", GREEN: "ok_chip"}      # the chip colour for each live-line colour (yellow has its own gold)


class Busy:
    """A live "working" line for a step that has no output of its own (a round trip to the worker): the phase and the elapsed time, redrawn while it lasts, gone when it ends. Silent when stderr is
    not a terminal. (Without it a slow connection looked like a hung screen: nothing at all for half a minute.)"""

    def __init__(self, text, color=None, stream=None, every=0.25):
        self.text, self.color, self.every = text, color or YELLOW, every
        self.prog = Progress(stream=stream)
        self._stop = None
        self._thread = None

    def __enter__(self):
        if self.prog.on:
            import threading
            self._stop = threading.Event()
            self.prog.draw(self.text, self.color)

            def run():
                while not self._stop.wait(self.every):
                    self.prog.draw(self.text, self.color)
            self._thread = threading.Thread(target=run, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc):
        if self._stop is not None:
            self._stop.set()
            self._thread.join(1)
            self.prog.clear()
        return False


class Progress:
    """One live line on the terminal (spinner, phase, elapsed time). Silent when stderr is not a terminal."""

    def __init__(self, stream=None, clock=time.time):
        self.stream, self.clock = stream or sys.stderr, clock
        self.on = self.stream.isatty() and os.environ.get("CT_COLOR", "auto") != "never"
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
        word, _, rest = text.partition(" ")                  # every live line starts with its status as a CAPITALS chip, so the eye lands on it first
        chip = f" {word.rstrip(':,;.').upper()} "
        chip_role = CHIP_FOR.get(color, "primary_chip")
        room = width - len(clock_text) - len(spin) - 1 - (len(chip) + 1 if chip else 0)
        rest = rest if len(rest) <= room else rest[:max(1, room - 1)] + "…"                 # one line, always: a long phase is cut with a mark, never wrapped
        self.i += 1
        if self.stream.isatty():
            body = paint(spin + " ", BOLD, color, stream=self.stream) + paint(chip, chip_role, stream=self.stream) + " " + paint(rest + clock_text, BOLD, color, stream=self.stream)
            self.stream.write("\r" + body + "\x1b[K")                                         # output: the live status line
            self.stream.flush()
            self.shown = True
            return
        plain = (f"{spin} {chip.strip() + ' ' if chip else ''}{rest}") + clock_text
        self.stream.write("\r" + paint(plain, BOLD, color, stream=self.stream) + "\x1b[K")      # output: the live status line
        self.stream.flush()
        self.shown = True

    def clear(self):
        if self.on and self.shown:
            self.stream.write("\r\x1b[K")                                                     # output: the live status line
            self.stream.flush()
            self.shown = False
