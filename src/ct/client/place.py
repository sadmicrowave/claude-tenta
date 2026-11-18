"""Placing a job: which of your workers gets it (the scheduler's wiring, `ct.sched` is the decision).

With one worker nothing here runs: sending works as it always did. With several (the main one and those in workers.toml), `route` reads what each published (state and registry, over
each worker's OWN connection, side by side), asks `sched.choose`, says why, and hands back the Context of the chosen worker. `--worker NAME` forces one. Where a job went is written to
`placements.json` in the client home, so `ct-run result` and the `ct-admin` commands that take a job id find it on the right worker afterwards. Nothing is moved or copied once placed.
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
from .repo import ClientError, ClientRepos


def entries(cfg):
    """[(name, cfg, repos)] for the main worker and every one in workers.toml; a configuration that cannot be read is skipped (the workers command reports it)."""
    from ..admin import workers as workers_mod
    out = [(cfg.identity.name, cfg, None)]
    wfile = Path(cfg.source).parent / "workers.toml" if cfg.source else None
    for name, path in (workers_mod.load_list(wfile) if wfile else {}).items():
        try:
            out.append((name, config.load(path, env={"CT_CONFIG": ""}), None))
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
    """How many jobs this computer sent to `name` in the last 15 minutes that have no final result yet (read from the record of placements and the results already downloaded)."""
    from .repo import PENDING
    now = time.time() if now is None else now
    try:
        data = json.loads((Path(home) / "placements.json").read_text()) or {}
    except (OSError, ValueError, TypeError):
        return 0
    n = 0
    for jid, p in data.items():
        if p.get("worker") != name:
            continue
        if p.get("reserve"):                                          # a send in progress: counts until released, or for two minutes if that send died
            n += now - p.get("at", 0) <= RESERVE_FOR_S
            continue
        if now - p.get("at", 0) > PENDING_FOR_S:
            continue
        res = repos.read_result(jid)
        if res is None or res.get("status") in PENDING:
            n += 1
    return n


def view_of(name, c, repos, fetch=None, now=None, home=None):
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
                             sent_pending=pending_sent(home, name, repos, now) if home else 0)


def gather(ents, repos_of, fetch=None, home=None):
    """One view per worker, all looked at at the same time (a slow or dead one does not hold up the others)."""
    with ThreadPoolExecutor(max_workers=max(1, len(ents))) as pool:
        return list(pool.map(lambda e: view_of(e[0], e[1], repos_of(e), fetch, home=home), ents))


def kind_of(cls, views):
    for v in views:
        c = (v.registry or {}).get("classes", {}).get(cls)
        if c:
            return c.get("kind", "command")
    return "command"


def decide(cls, views, prefs=None):
    return sched.choose(cls, kind_of(cls, views), views, prefs)


def rows(choice):
    """The explanation as card rows: one per worker, in the same words the comparison command uses."""
    return [(n, f"{v.upper() if v == 'CHOSEN' else v}: {r}") for n, v, r in choice.lines]


@contextlib.contextmanager
def _locked(home, name):
    """An exclusive lock between every ct-run on this computer (processes and threads) for one named purpose, so two of them never act on the same picture at once."""
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


def record(home, ids, worker):
    def add(data):
        for jid in ids:
            data[jid] = {"worker": worker, "at": int(time.time())}
        cutoff = time.time() - 14 * 86400                          # a record older than a fortnight is of no use: its result is long since pruned
        for k in [k for k, v in data.items() if v.get("at", 0) < cutoff]:
            del data[k]
    _update(home, add)


RESERVE_FOR_S = 120


def reserve(home, worker):
    """Mark that a job is about to be sent to `worker`, counted as its load straight away (before the send finishes, so a placement made a moment later sees it). Returns a token for `release`."""
    token = "reserve-" + uuid.uuid4().hex[:10]
    _update(home, lambda data: data.__setitem__(token, {"worker": worker, "at": int(time.time()), "reserve": True}))
    return token


def release(home, token):
    if token:
        _update(home, lambda data: data.pop(token, None))


def worker_of(home, jid):
    try:
        return ((json.loads((Path(home) / "placements.json").read_text()) or {}).get(jid) or {}).get("worker")
    except (OSError, ValueError):
        return None


def route(ctx, cfg, cls, forced, *, entries_fn=None, repos_of=None, make_ctx=None, fetch=None, say=None, prefs=None):
    """The Context to send a job of class `cls` with. With one worker, or when there is no configuration (a test), the one it was given. `forced` is a worker name (--worker)."""
    ents = (entries_fn or entries)(cfg) if cfg is not None else []
    if len(ents) < 2 and not forced:
        return ctx, None
    repos_of = repos_of or (lambda e: ctx.repos if e[0] == ents[0][0] else ClientRepos.from_config(e[1]))
    make_ctx = make_ctx or (lambda e: ctx if e[0] == ents[0][0] else type(ctx).from_config(e[1]))
    by_name = {e[0]: e for e in ents}
    if forced:
        if forced not in by_name:
            raise ClientError(f"There is no worker called {forced!r}", "Workers are the main one and those in ct-admin workers.", [f"Yours: {', '.join(by_name)}"])
        e = by_name[forced]
        return _with(make_ctx(e), ctx), None
    with _locked(ctx.home, "placements.decide.lock"):                  # looking, choosing and reserving are ONE step: eight sends at once must not all see the same idle workers
        views = gather(ents, repos_of, fetch, home=ctx.home)
        ch = decide(cls, views, prefs)
        if ch.worker is None:
            raise ClientError(f"No worker can take a {cls} job right now", "Each one, and why it cannot:", [f"{n}: {r}" for n, _, r in ch.lines] + ["ct-run where " + cls + " shows this again, ct-admin workers compares them."])
        token = reserve(ctx.home, ch.worker)
    if say:
        say(ch)
    chosen = _with(make_ctx(by_name[ch.worker]), ctx)
    chosen.reservation = token
    return chosen, ch


def _with(chosen, original):
    """The chosen worker's Context, counting the cap across the WHOLE fleet and keeping where results are looked up."""
    if chosen is not original:
        chosen.fleet, chosen.max_in_flight = original.fleet, original.max_in_flight
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
