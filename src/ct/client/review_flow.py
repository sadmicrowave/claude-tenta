"""The interactive offload review (slice 7 of theme/offload-value; design: docs/design/WORKSPACE-JOBS.md, "The offload review flow").

`ct-run offload review` lists what the command log says a worker could take (slice 6); this module then asks, for each command in turn, "register it as a worker class?".
A yes profiles the command on EVERY worker that can be asked, adopts it where it fits, and says plainly where it did not and why. The review picks no worker: a class
registered on several workers is placed per job by the orchestrator (ct.sched reads each worker's registry), so "registered here" is the whole eligibility.

Safety is the old one: each y/n is the approval of that one command (no yes to all), a profile run still goes through the worker's own approval, adopting follows
`offload.auto_adopt`, packs only get stricter, nothing that looks like a secret, network, git, a package manager or a privileged tool is ever offered.

Every input (the recommendations, the workers, the clock, the answers, the fleet that really sends things) is passed in, so the logic is tested without a network."""
import json
import os
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import alias, cards, sched, ui as _ui
from ..packages import card_safe
from ..words import plural
from . import cmdlog, offload as off

ANSWERS_FILE = "review-answers.json"
RELEASE_KEYS = ("profile_time", "cache")        # a worker's registry.json carries both once it runs the release this flow needs (the file recipe and the profile time it publishes)
DAY = 86400
PICKUP_S = 120                                    # how long past a run's own limit we wait for the worker to notice it and for the result to arrive
YES, NO, NEVER = ("y", "yes"), ("n", "no"), ("never",)


class Failed(Exception):
    """A worker could not take the job; the text is the tool's own."""


@dataclass
class Target:
    """One worker as the review sees it: whether it can be asked now, why not, what it offers and what was excluded on it."""
    name: str
    state: str                  # ready | offline | silent | locked | old
    why: str
    registry: dict
    excluded: list = field(default_factory=list)


@dataclass
class Job:
    shape: str
    argv: list
    folder: str
    est_s: float
    name: str
    packages: bool = False      # put the packages the folder's marker files propose (pinned only) on the run


@dataclass
class Item:
    rec: dict
    argv: list
    ready: list                 # workers to profile on
    have: list                  # workers that already offer it
    held: list                  # [(worker, state, why)]: workers that cannot be asked, or where it did not fit before


@dataclass
class Plan:
    items: list
    skipped: list               # [(shape, why)]


@dataclass
class Outcome:
    worker: str
    status: str                 # registered | profiled | does-not-fit | needs-longer | not-sent | not-approved | no-result | adopt-failed
    text: str = ""
    jid: str = ""
    run: str = ""


@dataclass
class Result:
    item: Item
    outcomes: list
    name: str = ""


# ---- what was answered ----
class Answers:
    """The commands the person said no to, on this computer only (mode 0600): `not-now` is quiet for `offload.review_ask_again_days`, `never` until undone."""

    def __init__(self, home, clock=time.time):
        self.path, self.clock, self.damaged = Path(home) / ANSWERS_FILE, clock, False

    def _read(self):
        try:
            d = json.loads(self.path.read_text())
            if not isinstance(d, dict):
                raise ValueError("not a table")
            return d
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            self.damaged = True               # a damaged file is an empty one that the person is told about: the review is not stopped by it
            return {}

    def _write(self, d):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(d, f, indent=1)
        os.replace(tmp, self.path)

    def decline(self, shape, kind):
        d = self._read()
        d[shape] = {"kind": kind, "at": int(self.clock())}
        self._write(d)

    def silenced(self, shape, ask_again_days):
        e = self._read().get(shape)
        if not isinstance(e, dict):
            return None
        if e.get("kind") == "never":
            return "never"
        if e.get("kind") == "not-now" and self.clock() - e.get("at", 0) < ask_again_days * DAY:
            return "not-now"
        return None

    def rows(self):
        """[(number, shape, kind, when)] oldest first: the numbers are what `--forget` takes."""
        d = self._read()
        return [(i, s, e.get("kind", "?"), e.get("at", 0)) for i, (s, e) in enumerate(sorted(d.items(), key=lambda kv: kv[1].get("at", 0) if isinstance(kv[1], dict) else 0), 1) if isinstance(e, dict)]

    def forget(self, which):
        d = self._read()
        shape = next((s for n, s, _, _ in self.rows() if str(n) == str(which) or s == which), None)
        if shape is None:
            return False
        del d[shape]
        self._write(d)
        return True


# ---- which workers can be asked ----
def worker_state(view):
    """(state, why) for a sched.Worker: ready, or the way it cannot be asked now, said so the person can act on it."""
    if not view.reachable:
        return "offline", f"cannot be reached: {card_safe(view.why_not or 'no answer')}"
    if view.age_s is None or view.age_s > sched.STALE_S:
        return "silent", ("has not reported yet" if view.age_s is None else f"has not reported for {int(view.age_s // 60)} min") + "; it may be stopped, so it was not asked"
    if view.doc("locked", default=False):
        return "locked", "is locked: it takes no jobs until you unlock it"
    reg = view.registry or {}
    missing = [k for k in RELEASE_KEYS if k not in reg]
    if missing:
        return "old", "runs an older release that cannot take the recipe"
    return "ready", ""


# ---- what to ask about ----
def _unsafe(example, own_tools):
    if cmdlog.SECRET.search(example) or off.SECRET.search(example):
        return "it looks like it carries a secret"
    try:
        c = cmdlog.classify(example, own_tools)
    except Exception:                                      # a line the shape reader cannot judge is not offered
        return "it could not be read as one command"
    if c is None:
        return "it could not be read as one command"
    return c[2]


def plan(recs, targets, answers, *, own_tools, now, ask_again_days):
    items, skipped = [], []
    for r in recs:
        shape, example = r["shape"], r["example"]
        bad = _unsafe(example, own_tools)
        if bad:
            skipped.append((shape, f"a worker is never offered this: {bad}"))
            continue
        said = answers.silenced(shape, ask_again_days)
        if said:
            skipped.append((shape, "you said never" if said == "never" else f"you said not now (asked again after {plural(ask_again_days, 'day')})"))
            continue
        try:
            argv = shlex.split(example)
        except ValueError:
            skipped.append((shape, "it could not be read as one command"))
            continue
        ready, have, held = [], [], []
        for t in targets:
            fit = off.fit_lookup(argv, t.registry, t.excluded)
            if fit["verdict"] == "registered":
                have.append(t.name)
            elif fit["verdict"] in ("excluded", "retry"):
                held.append((t.name, "excluded", f"did not fit when it was tried here: {card_safe(str(fit.get('reasons', '')))}"))
            elif t.state == "ready":
                ready.append(t.name)
            else:
                held.append((t.name, t.state, t.why))
        if ready:
            items.append(Item(r, argv, ready, have, held))
        elif held:
            skipped.append((shape, "; ".join(f"{w}: {why}" for w, _, why in held)))
        else:
            skipped.append((shape, "already offered by every worker"))
    return Plan(items, skipped)


# ---- the profile run for one worker ----
MEM_PLACEHOLDER_MB = 500          # the command log records time, never memory: this number is a placeholder, and the run's own text says so to whoever approves it


def profile_args(job, jid=None, dir_arg=None):
    """The `ct-run offload profile` arguments for one job, exactly as typed (the live fleet parses them with the real parser, so the run is the one a person would have sent).
    The details an approver reads are required by the real command; the ones the log cannot know are said to be placeholders, never presented as measured."""
    a = ["offload", "profile", *(["--id", jid] if jid else []), "--name", job.name, "--what", f"{job.shape}, as you ran it",
         "--expect", f"about {_span(job.est_s)} per run, from your own command history; memory was not recorded, so the {MEM_PLACEHOLDER_MB} MB below is a placeholder, not a measurement",
         "--est-time-s", f"{job.est_s:g}", "--est-mem-mb", str(MEM_PLACEHOLDER_MB),
         "--why", "your command history shows it is slow and repeated, and you chose to register it"]
    folder = dir_arg or job.folder
    if folder:
        a += ["--dir", folder]
        if job.packages:
            a += ["--use-detected-packages"]
    return a + ["--", *job.argv]


def adopt_tier(res):
    return "approve" if (res.get("profile") or {}).get("cost") == "heavy" else "auto"


# ---- what is shown ----
def _say(*a, **k):
    import sys
    k.setdefault("stream", sys.stderr)
    _ui.say(*a, **k)


def _span(seconds):
    seconds = float(seconds)
    return f"{seconds:.0f} s" if seconds < 60 else f"{seconds / 60:.0f} min" if seconds < 5400 else f"{seconds / 3600:.1f} h"


def _plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


def _held_rows(held):
    """Rows for the workers a command was held back from. Workers that cannot be asked at all are said once, before the list (`show_unavailable`), not on every command."""
    out = []
    for w, st, why in held:
        if st == "excluded":
            out += [("Not asked", alias.show(w)), ("Why", card_safe(why))]
    return out


def show_unavailable(targets):
    """The workers that cannot be asked now, each with its reason and (for an old release) the command that fixes it. Said once, never silently skipped."""
    down = [t for t in targets if t.state != "ready"]
    if not down:
        return
    rows, runs = [], []
    for t in down:
        rows += [("Worker", alias.show(t.name)), ("Why", card_safe(t.why))]
        if t.state == "old":
            runs.append(f"ct-admin apply --worker {t.name}")
    _say("Workers that cannot be asked now", *rows, "They are left out of this review. Fix them, then run the review again: it offers only what they are missing.", tag="NOTE", color=_ui.YELLOW,
         **({"run": runs, "run_label": "Update an old worker"} if runs else {}))


def show_plain(pl, folder_of, ask_again_days, targets=()):
    """No terminal (a pipe, a script, a run Claude started): the list and the exact commands, nothing asked and nothing registered."""
    show_unavailable(targets)
    if not pl.items:
        _say("Nothing to register right now", *_skipped_rows(pl) or ["Every recommended command is already offered, or was answered."], tag="NOTE", color=_ui.YELLOW)
        return
    cmds, taken = [], set()
    for it in pl.items:
        name = off.suggest_name(it.argv, taken)
        taken.add(name)
        job = Job(it.rec["shape"], it.argv, folder_of(it.rec), max(1.0, float(it.rec["median"])), name)
        cmds.append("ct-run " + " ".join(shlex.quote(x) for x in profile_args(job)))
    _say("Commands you can register as worker classes", "Nothing was asked or registered: this is not a terminal. Each command below test-runs the command on a worker; that run needs your approval, then the result says how to adopt it.",
         tag="REVIEW", color=_ui.CYAN, run=cmds, run_label="Run the ones you want, one at a time")
    if pl.skipped:
        _say("Left out of the list", *_skipped_rows(pl), tag="NOTE", color=_ui.YELLOW)


def _skipped_rows(pl):
    out = []
    for shape, why in pl.skipped:
        out += [("Command", shape), ("Why", card_safe(why))]
    return out


def ask_choice(ask, question):
    """One question with y / n / never. Enter skips this command for now. An answer that is none of those is asked again once, then the command is skipped: never taken as a yes."""
    for _ in range(2):
        got = ask(_ui.choice_prompt(question)).strip().lower()
        if got in YES:
            return "yes"
        if got in NO:
            return "no"
        if got in NEVER:
            return "never"
        if got == "":
            return "skip"
        _say("That is not an answer", "Type y, n or never. Press Enter to skip this one for now.", tag="NOTE", color=_ui.YELLOW)
    return "skip"


def _outcome_rows(outcomes):
    groups = {"registered": "Registered on", "profiled": "Profiled, adopt it yourself on", "does-not-fit": "Does not fit on", "needs-longer": "Ran out of time on", "not-sent": "Not sent to",
              "not-approved": "Not approved on", "no-result": "No result yet from", "adopt-failed": "Not adopted on"}
    rows, runs = [], []
    for o in outcomes:
        rows.append((groups[o.status], alias.show(o.worker)))
        if o.text:
            rows.append(("Why" if o.status not in ("registered", "profiled") else "Note", card_safe(o.text)))
        if o.run:
            runs.append(o.run)
    return rows, runs


# ---- the loop ----
def review(recs, targets, fleet, answers, *, ask, is_tty, own_tools, now, ask_again_days, folder_of, proposal_of=None):
    """Ask about each recommended command, one at a time; returns a Result per command the person said yes to."""
    pl = plan(recs, targets, answers, own_tools=own_tools, now=now, ask_again_days=ask_again_days)
    if answers.damaged:
        from . import refuse
        e = refuse.error("review.answers.unreadable")
        _say(e.title, cards.Why(e.why or ""), tag="NOTE", color=_ui.YELLOW, steps=e.steps or None)
    if not is_tty():
        show_plain(pl, folder_of, ask_again_days, targets)
        return []
    show_unavailable(targets)
    if pl.skipped:
        _say("Left out of the list", *_skipped_rows(pl), tag="NOTE", color=_ui.YELLOW)
    if not pl.items:
        _say("Nothing to register right now", "Every recommended command is already offered, was answered, or no worker can be asked.", tag="NOTE", color=_ui.YELLOW)
        return []
    taken = {c for t in targets for c in ((t.registry or {}).get("classes") or {})}
    out = []
    try:
        for it in pl.items:
            r = it.rec
            folder = proposed = folder_of(r)
            lines, usable = (proposal_of(folder) if (proposal_of and folder) else ([], False))
            _say("Register this command as a worker class?", ("Command", " ".join(it.argv)), ("Ran", f"{_plural(r['count'], 'time')}, {_span(r['total'])} in all, {_span(r['median'])} each"),
                 ("Test-run on", ", ".join(alias.show(w) for w in it.ready)), *([("Already on", ", ".join(alias.show(w) for w in it.have))] if it.have else []), *_held_rows(it.held),
                 *([("Folder", folder)] if folder else []), *lines,
                 cards.Why("Yes sends a test run of this one command to each worker named above; each worker's own approval comes next, and nothing is installed unless the run fits."),
                 tag="REGISTER", color=_ui.CYAN)
            got = ask_choice(ask, "Register it as a worker class?")
            if got == "never":
                answers.decline(r["shape"], "never")
                continue
            if got == "no":
                answers.decline(r["shape"], "not-now")
                continue
            if got == "skip":
                continue
            if folder:
                typed = ask(_ui.choice_prompt("Folder to send with it (Enter keeps it, - sends none, or type another)", hint="")).strip()
                folder = None if typed == "-" else (typed or folder)
            name = off.suggest_name(it.argv, taken)
            taken.add(name)
            job = Job(r["shape"], it.argv, folder, max(1.0, float(r["median"])), name, bool(usable and folder == proposed))
            out.append(run_one(it, job, fleet))
    except (EOFError, KeyboardInterrupt):
        _say("Stopped", "Nothing more was asked or sent. What was already sent keeps running; the answers you gave are kept.", tag="NOTE", color=_ui.YELLOW)
    return out


def run_one(it, job, fleet):
    outcomes, sent = [], []
    for w in it.ready:
        try:
            sent.append((w, fleet.profile(w, job)))
        except Failed as e:
            outcomes.append(Outcome(w, "not-sent", str(e)))
    _say("Profiling on your workers", ("Command", " ".join(job.argv)), "Each run goes through the worker's own approval, then is measured there; the wait is as long as the worker's own time limit.", tag="PROFILING", color=_ui.CYAN)
    approved = []
    for w, jid in sent:
        if fleet.approve(w, jid) != 0:
            outcomes.append(Outcome(w, "not-approved", "the run was not approved, so it was not started", jid, f"ct-admin approve {jid} --worker {w}"))
        else:
            approved.append((w, jid))
    for w, jid in approved:
        res = fleet.result(w, jid, fleet.limit_s(w, job.est_s))
        if res is None:
            outcomes.append(Outcome(w, "no-result", "no result arrived in time; the run may still finish", jid, f"ct-run result {jid} --wait"))
            continue
        pr = res.get("profile") or {}
        v = pr.get("verdict")
        why = card_safe("; ".join(pr.get("reasons") or []) or res.get("note") or "no reason was given")
        if v == "fits":
            got = fleet.adopt(w, res, job)
            if got == "adopted":
                outcomes.append(Outcome(w, "registered", "", jid))
            elif got == "manual":
                outcomes.append(Outcome(w, "profiled", "it fits; adopting is left to you", jid, f"ct-admin adopt {jid} --tier {adopt_tier(res)} --worker {w}"))
            else:
                outcomes.append(Outcome(w, "adopt-failed", card_safe(str(got)), jid, f"ct-admin adopt {jid} --tier {adopt_tier(res)} --worker {w}"))
        elif v == "needs-longer":
            outcomes.append(Outcome(w, "needs-longer", why, jid))
        else:
            outcomes.append(Outcome(w, "does-not-fit", why, jid))
    outcomes.sort(key=lambda o: [x for x in it.ready].index(o.worker))
    rows, runs = _outcome_rows(outcomes)
    held = _held_rows(it.held)
    won = any(o.status == "registered" for o in outcomes)
    _say("Registered as a worker class" if won else "Not registered on any worker", ("Class", job.name), *rows, *held,
         *(["Where it is registered, the orchestrator picks the worker for each job; where it is not, that worker is simply not a choice."] if won else []),
         tag="DONE" if won else "NOTE", color=_ui.GREEN if won else _ui.YELLOW, **({"run": runs, "run_label": "To go on from here"} if runs else {}))
    return Result(it, outcomes, job.name)


# ---- the live fleet ----
class LiveFleet:
    """What really sends, approves, waits and adopts, one worker's Context at a time (the same code `ct-run offload profile` and `ct-admin adopt` run)."""

    def __init__(self, ctx, cfg, ctx_of=None):
        self.ctx, self.cfg = ctx, cfg
        self._ctx_of = ctx_of
        self._made = {}

    def _for(self, worker):
        if worker not in self._made:
            self._made[worker] = self._ctx_of(worker) if self._ctx_of else self.ctx
        return self._made[worker]

    def profile(self, worker, job):
        from . import cli, submit as sub
        from .repo import ClientError
        wctx = self._for(worker)
        jid = sub.new_job_id("profile", wctx.repos.result_ids())
        folder = job.folder
        args = profile_args(job, jid, dir_arg="." if folder else None)
        ap, _ = cli.build_parser()
        i = args.index("--")
        a = ap.parse_args(args[:i])
        a.argv, a.raw = args[i + 1:], args
        a.wait = False
        before = os.getcwd()
        try:
            if folder:
                os.chdir(folder)                     # the recipe is saved relative to where the command was typed: from the folder, so the class keeps it
            cli.cmd_profile(wctx, a)
        except ClientError as e:
            raise Failed(card_safe(f"{e.title}. {e.why or ''}".strip())) from e
        except OSError as e:
            raise Failed(card_safe(str(e))) from e
        finally:
            os.chdir(before)
        if not wctx.commands.get(jid):               # cmd_profile returns quietly when the command is already registered or excluded there: nothing was sent
            raise Failed("nothing was sent: the worker already answers for this command")
        return jid

    def approve(self, worker, jid):
        wctx = self._for(worker)
        if (wctx.repos.worker_state() or {}).get("profile_approval") == "none":
            return 0                                 # this worker starts profile runs without approval, inside its jail
        return self.ctx.run_admin(["approve", jid, "--worker", worker]) if self.ctx.run_admin else 1

    def limit_s(self, worker, est_s):
        pt = ((self._for(worker).repos.registry_info() or {}).get("profile_time")) or {}
        return max(30.0, float(est_s) * float(pt.get("factor") or 1.0) * float(pt.get("margin") or 3))

    def result(self, worker, jid, limit_s):
        from . import cli, view
        wctx = self._for(worker)
        end, prog = wctx.clock() + limit_s + PICKUP_S, view.Progress(clock=wctx.clock)
        try:
            while wctx.clock() < end:
                wctx.repos.pull_results()
                res = wctx.repos.read_result(jid)
                if res and res["status"] not in cli.PENDING:
                    return res
                prog.draw(f"{alias.show(worker)}: waiting for the profile run", _ui.CYAN)
                wctx.sleep(2)
        finally:
            prog.clear()
        return None

    def adopt(self, worker, res, job):
        wctx = self._for(worker)
        tier = adopt_tier(res)
        wctx.adopted.remember(res["id"], tier)       # so `ct-run result` does not offer the same adoption a second time
        if not (wctx.auto_adopt and self.ctx.run_admin and self.ctx.is_tty()):
            return "manual"
        rc = self.ctx.run_admin(["adopt", res["id"], "--tier", tier, "--yes", "--worker", worker])
        return "adopted" if rc == 0 else f"ct-admin adopt stopped with exit code {rc}; its own message is above"


def live_targets(ctx, cfg, now=None):
    """[(Target)] for the main worker and every one in workers.toml, from one look at each (a slow or dead one does not hold up the others)."""
    from . import place
    from .repo import ClientRepos
    ents = place.entries(cfg) if cfg is not None and hasattr(cfg, "identity") else [(ctx.product, None, None)]
    repos_of = lambda e: ctx.repos if e[0] == ents[0][0] else ClientRepos.from_config(e[1])      # noqa: E731
    if cfg is None or not hasattr(cfg, "identity"):
        ents = [(ctx.product, None, None)]
        views = [sched.worker_view(ctx.product, "direct", None, ctx.repos.registry_info(), None, 5)]
    else:
        views = place.gather(ents, repos_of, home=ctx.home)
    out = []
    for e, v in zip(ents, views):
        st, why = worker_state(v)
        try:
            ex = off.read_exclusions(repos_of(e)).get("excluded", [])
        except Exception:
            ex = []
        out.append(Target(e[0], st, why, v.registry or {}, ex))
    return out


def folder_map(records, recs, home=None):
    """{shape: folder}: where each recommended command was last run, kept only when it is a real folder that is not the home folder or the root of the disk."""
    skip = {Path("/").resolve(), Path(home or Path.home()).resolve()}
    last = {}
    for r in sorted(records, key=lambda x: x.get("ts") or 0):
        if r.get("cmd") and r.get("cwd"):
            last[cmdlog.example_of(r["cmd"])] = r["cwd"]
    out = {}
    for rec in recs:
        d = last.get(rec["example"])
        try:
            if d and Path(d).is_dir() and Path(d).resolve() not in skip:
                out[rec["shape"]] = d
        except OSError:
            pass
    return out
