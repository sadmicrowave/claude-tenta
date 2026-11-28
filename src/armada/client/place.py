"""Placing a job: which of your workers gets it (the orchestrator's wiring, `armada.sched` is the decision).

With one worker nothing here runs: sending works as it always did. With several (the main one and those in workers.toml), `route` reads what each published (state and registry, over
each worker's OWN connection, side by side), asks `sched.choose`, says why, and hands back the Context of the chosen worker. `--worker NAME` forces one. Where a job went is written to
`placements.json` in the client home, so `arm-run result` and the `arm-admin` commands that take a job id find it on the right worker afterwards. Nothing is moved or copied once placed.
"""
import contextlib
import fcntl
import json
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .. import config, sched, transport
from . import refuse
from .repo import ClientError, ClientRepos


def entries(cfg):
    """[(name, cfg, repos)] for the main worker and every one in workers.toml; a configuration that cannot be read is skipped (the workers command reports it)."""
    from ..admin import workers as workers_mod
    out = [(cfg.identity.name, cfg, None)]
    wfile = Path(cfg.source).parent / "workers.toml" if cfg.source else None
    for name, path in (workers_mod.load_list(wfile) if wfile else {}).items():
        try:
            out.append((name, config.load(path, env={"ARM_CONFIG": ""}), None))
        except Exception:
            continue
    return out


def pickup_s(c, kind=None):
    """What this computer assumes a worker takes to notice a job (a worker that publishes its own figure overrides it): seconds for Direct, its idle pace otherwise."""
    return 5 if (kind or transport.for_config(c).kind) == "direct" else (int(c.schedule.idle_fetch_s) or 5)


def actual_connection(repos, configured):
    """The connection this computer REALLY sends over: where its copy of the queue points (that is where `git push` goes), not what its configuration says. They can differ (a worker switched
    in one place and not the other); the copy wins because it decides where a job lands. Falls back to the configuration when there is no copy to look at."""
    import subprocess
    try:
        url = subprocess.run(["git", "-C", str(repos.queue), "remote", "get-url", "origin"], capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return configured
    if not url:
        return configured
    return "github" if "github.com" in url else "direct"


PENDING_FOR_S = 900


def pending_sent(home, name, repos, now=None):
    """The capacity points of the jobs this computer sent to `name` in the last 15 minutes that have no final result yet (read from the record of placements and the results already downloaded): a light
    job counts 1, a medium 2, a heavy 4, by what that worker's own registry says; a record with no class counts 1."""
    from .repo import PENDING
    now = time.time() if now is None else now
    try:
        data = json.loads((Path(home) / "placements.json").read_text()) or {}
    except (OSError, ValueError, TypeError):
        return 0
    try:
        classes = (repos.registry_info() or {}).get("classes") or {}
    except Exception:
        classes = {}

    def weight(p):
        return sched.COST.get((classes.get(p.get("class")) or {}).get("cost"), 1) if p.get("class") else 1
    n = 0
    for jid, p in data.items():
        if p.get("worker") != name:
            continue
        if p.get("reserve"):                                          # a send in progress: counts until released, or for two minutes if that send died
            n += weight(p) if now - p.get("at", 0) <= RESERVE_FOR_S else 0
            continue
        if now - p.get("at", 0) > PENDING_FOR_S:
            continue
        res = repos.read_result(jid)
        if res is None or res.get("status") in PENDING:
            n += weight(p)
    return n


def channel_problem(c, repos):
    """(connection this computer sends over, connection the worker says it listens on) when they differ, else None: a job sent now would never be picked up. A worker that does not say (an older release) is not a mismatch."""
    from ..admin import workers as workers_mod
    sends = actual_connection(repos, transport.for_config(c).kind)
    listens = (workers_mod.read_state(repos.results) or {}).get("transport")
    return (sends, listens) if listens and listens != sends else None


def view_of(name, c, repos, fetch=None, now=None, home=None, silent_s=None):
    """What is known about one worker, after one look over its own connection. A look that fails is a worker that is not reachable now, with what was said."""
    from ..admin import workers as workers_mod
    configured = transport.for_config(c).kind
    kind = actual_connection(repos, configured)
    try:
        (fetch or (lambda r: r.pull_results()))(repos)
    except Exception as e:
        first = (str(getattr(e, "why", "") or e).strip().splitlines() or ["no answer"])[0][:120]
        return sched.worker_view(name, kind, workers_mod.read_state(repos.results), repos.registry_info(), None, pickup_s(c, kind), reachable=False, why_not=first, configured=configured)
    state = workers_mod.read_state(repos.results)
    return sched.worker_view(name, kind, state, repos.registry_info(), workers_mod.age_s(state, now) if state else None, pickup_s(c, kind), configured=configured,
                             sent_pending=pending_sent(home, name, repos, now) if home else 0,
                             silent_after_s=silent_s or sched.SILENT_S)


def _silent_s(ctx):
    hours = getattr(ctx, "silent_hours", 24)
    return (hours if isinstance(hours, (int, float)) else 24) * 3600


def gather(ents, repos_of, fetch=None, home=None, silent_s=None):
    """One view per worker, all looked at at the same time (a slow or dead one does not hold up the others)."""
    with ThreadPoolExecutor(max_workers=max(1, len(ents))) as pool:
        return list(pool.map(lambda e: view_of(e[0], e[1], repos_of(e), fetch, home=home, silent_s=silent_s), ents))


SNAPSHOT_FOR_S = 5


def gather_shared(ents, repos_of, fetch=None, home=None, silent_s=None):
    """`gather`, but a look taken in the last few seconds by another placement is reused (a burst of sends reads each worker once, not once per send). What is NOT shared is this computer's own record of
    what it has sent: that is worked out fresh, so every placement still sees the ones before it."""
    f = Path(home) / "views.json" if home else None
    names = sorted(e[0] for e in ents)
    now = time.time()
    if f:
        try:
            snap = json.loads(f.read_text())
            if now - snap["at"] <= SNAPSHOT_FOR_S and sorted(snap["views"]) == names:
                out = []
                for e in ents:
                    d = dict(snap["views"][e[0]])
                    if d.get("age_s") is not None:
                        d["age_s"] += now - snap["at"]
                    v = sched.Worker(**d)
                    v.silent_after_s = silent_s or sched.SILENT_S                   # the limit is today's setting, not the one when the look was taken
                    v.sent_pending = pending_sent(home, e[0], repos_of(e))
                    out.append(v)
                return out
        except (OSError, ValueError, KeyError, TypeError):
            pass
    views = gather(ents, repos_of, fetch, home=home, silent_s=silent_s)
    if f:
        try:
            keep = ("name", "connection", "reachable", "why_not", "state", "registry", "age_s", "pickup_s", "configured")
            tmp = f.with_name(f.name + f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps({"at": now, "views": {v.name: {k: getattr(v, k) for k in keep} for v in views}}))
            os.replace(tmp, f)
        except OSError:
            pass
    return views


def kind_of(cls, views):
    for v in views:
        c = (v.registry or {}).get("classes", {}).get(cls)
        if c:
            return c.get("kind", "command")
    return "command"


def decide(cls, views, prefs=None):
    return sched.choose(cls, kind_of(cls, views), views, prefs)


def rows(choice):
    """The explanation as card rows: for each worker its verdict (a fixed word, CHOSEN in a green chip), the worker's name, and the reason on the line below."""
    from .. import alias, cards
    out = []
    for i, (n, v, r) in enumerate(choice.lines):
        head = (["\n"] if i else []) + [(cards.Chip("CHOSEN") if v == "CHOSEN" else v.upper(), alias.show(n))]
        reasons = (getattr(choice, "reasons", None) or {}).get(n)
        if reasons:                                                           # a worker left out: each reason on its own line, then the codes
            out += head + [("", text) for _, text in reasons] + [("Code", ", ".join(code for code, _ in reasons))]
            continue
        reason, _, warning = r.partition(", WARNING: ")                       # a warning is a line of its own, in the warning colour, not the end of a sentence about capacity
        out += head + [("", reason)] + ([("", cards.Styled([("WARNING: " + warning, "warn")]))] if warning else [])
    return out


@contextlib.contextmanager
def _locked(home, name):
    """An exclusive lock between every arm-run on this computer (processes and threads) for one named purpose, so two of them never act on the same picture at once."""
    Path(home).mkdir(parents=True, exist_ok=True)
    with open(Path(home) / name, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _update(home, change):
    """Read-change-write placements.json under its own lock, so records written at the same moment are all kept."""
    f = Path(home) / "placements.json"
    with _locked(home, "placements.lock"):
        try:
            data = json.loads(f.read_text()) if f.exists() else {}
        except ValueError:
            data = {}
        change(data)
        tmp = f.with_name(f.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=1))
        os.replace(tmp, f)


def record(home, ids, worker, cls=None):
    def add(data):
        for jid in ids:
            data[jid] = {"worker": worker, "at": int(time.time()), **({"class": cls} if cls else {})}
        cutoff = time.time() - 14 * 86400                          # a record older than a fortnight is of no use: its result is long since pruned
        for k in [k for k, v in data.items() if v.get("at", 0) < cutoff]:
            del data[k]
    _update(home, add)


RESERVE_FOR_S = 120


def reserve(home, worker, cls=None):
    """Mark that a job of class `cls` is about to be sent to `worker`, counted as its load straight away (before the send finishes, so a placement made a moment later sees it). Returns a token for `release`."""
    token = "reserve-" + uuid.uuid4().hex[:10]
    _update(home, lambda data: data.__setitem__(token, {"worker": worker, "at": int(time.time()), "reserve": True, **({"class": cls} if cls else {})}))
    return token


def reserved(home, skip=None, now=None):
    """[(worker, class)] of the sends in progress on this computer (not stale, not `skip`, the caller's own token)."""
    now = time.time() if now is None else now
    try:
        data = json.loads((Path(home) / "placements.json").read_text()) or {}
    except (OSError, ValueError, TypeError):
        return []
    return [(p.get("worker"), p.get("class")) for k, p in data.items() if p.get("reserve") and k != skip and now - p.get("at", 0) <= RESERVE_FOR_S]


def choose_within_share(cls, views, prefs, share, ch):
    """(choice, why) for `ch`, the choice already made. A worker at its own limit is left out and the next best is tried; `why` is the sentence when every worker that could take the job is over its
    share (the caller queues it); None when the returned choice has room."""
    pool, first = list(views), None
    while True:
        over = share(ch.worker)
        if not over:
            return ch, None
        first = first or (ch, over[1])
        if over[0] != "worker" or len(pool) < 2:
            return first
        pool = [v for v in pool if v.name != ch.worker]
        nxt = decide(cls, pool)
        if nxt.worker is None or nxt.busy_all:
            return first
        ch = nxt


def release(home, token):
    if token:
        _update(home, lambda data: data.pop(token, None))


def worker_of(home, jid):
    try:
        return ((json.loads((Path(home) / "placements.json").read_text()) or {}).get(jid) or {}).get("worker")
    except (OSError, ValueError):
        return None


def route(ctx, cfg, cls, forced, *, entries_fn=None, repos_of=None, make_ctx=None, fetch=None, say=None, prefs=None, hold=False, share=None):
    """The Context to send a job of class `cls` with. With one worker, or when there is no configuration (a test), the one it was given. `forced` is a worker name (--worker).
    With `hold`, a job that fits no worker right now is not placed: the Context comes back with `.held` set to the explanation, nothing is reserved, and the caller queues it (queue.py)."""
    ents = (entries_fn or entries)(cfg) if cfg is not None else []
    if len(ents) < 2 and not forced:
        return ctx, None
    repos_of = repos_of or (lambda e: ctx.repos if e[0] == ents[0][0] else ClientRepos.from_config(e[1]))
    make_ctx = make_ctx or (lambda e: ctx if e[0] == ents[0][0] else type(ctx).from_config(e[1]))
    by_name = {e[0]: e for e in ents}
    from .. import alias
    forced = alias.resolve(forced, by_name)                            # an alias means its worker
    if forced:
        if forced not in by_name:
            raise refuse.error("placement.unknown_worker", ("Name", forced), ("Yours", ", ".join(by_name)))
        e = by_name[forced]
        wrong = channel_problem(e[1], repos_of(e))
        if wrong:                                                       # naming a worker does not make a job arrive: refused, not sent into the void
            raise refuse.error("placement.transport.mismatch", ("Worker", forced), connection=wrong[0], listens=wrong[1])
        return _with(make_ctx(e), ctx), None
    with _locked(ctx.home, "placements.decide.lock"):                  # looking, choosing and reserving are ONE step: eight sends at once must not all see the same idle workers
        views = gather_shared(ents, repos_of, fetch, home=ctx.home, silent_s=_silent_s(ctx))
        ch = decide(cls, views, prefs)
        if ch.worker is None:
            raise none_error(cls, ch)
        if hold and ch.busy_all:
            ctx.held = ch
            return ctx, ch
        if hold and share:                                           # checked in the same step as the reservation, so two sends at once cannot both take the last place
            ch, why = choose_within_share(cls, views, prefs, share, ch)
            if why:
                ctx.held, ctx.share_why = ch, why
                return ctx, ch
        token = reserve(ctx.home, ch.worker, cls)
    if say:
        say(ch)
    chosen = _with(make_ctx(by_name[ch.worker]), ctx)
    chosen.reservation = token
    chosen.choice = ch                                                 # kept for the audit trail (audit.placed)
    return chosen, ch


def none_error(cls, ch):
    """No worker can take the job: every worker and every reason it cannot, then how to see it again."""
    reasons = getattr(ch, "reasons", None) or {}
    steps = [f"{n}: {text}" for n, _, r in ch.lines for text in ([t for _, t in reasons[n]] if n in reasons else [r])]
    return refuse.error("placement.none_available", ("Command", cls), steps=steps + [f"arm-run where {cls} shows this again, arm-admin workers compares them."])


def _with(chosen, original):
    """The chosen worker's Context, counting the cap across the WHOLE fleet and keeping where results are looked up."""
    if chosen is not original:
        chosen.fleet, chosen.max_in_flight = original.fleet, original.max_in_flight
        chosen.max_per_worker, chosen.max_per_class = getattr(original, "max_per_worker", 0), getattr(original, "max_per_class", 0)
    chosen.home_main = original.home                                   # every placement is remembered, the main worker's too: it is how pending jobs are counted
    return chosen


def for_job(ctx, cfg, jid, make_ctx=None, entries_fn=None):
    """The Context of the worker a job was sent to (from placements.json), or the one given when it went to the main worker or nobody recorded it."""
    name = worker_of(ctx.home, jid)
    if not name or cfg is None or name == ctx.product:
        return ctx
    for n, c, _ in (entries_fn or entries)(cfg):
        if n == name:
            other = (make_ctx or (lambda c: type(ctx).from_config(c)))(c)
            other.fleet, other.max_in_flight, other.home_main = ctx.fleet, ctx.max_in_flight, ctx.home
            return other
    return ctx


PRUNE_EVERY_S = 3600


def prune_due(home, now=None):
    """Pruning the queue branch is housekeeping that rarely has anything to do: once an hour is enough, not once per send."""
    now = time.time() if now is None else now
    try:
        return now - float((Path(home) / "pruned.stamp").read_text()) >= PRUNE_EVERY_S
    except (OSError, ValueError):
        return True


def prune_done(home, now=None):
    try:
        Path(home).mkdir(parents=True, exist_ok=True)
        (Path(home) / "pruned.stamp").write_text(str(time.time() if now is None else now))
    except OSError:
        pass
