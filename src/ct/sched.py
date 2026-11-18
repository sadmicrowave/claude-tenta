"""Choosing which worker should run a job (docs/design/ORCHESTRATION.md).

Pure: it takes what each worker published (its state document and its registry) and says which worker should get a job of a given class, and WHY, for every worker, in plain words.
Nothing here connects to anything, so the same function serves the submit command, the explain command and the tests, and a given set of readings always gives the same answer.

A worker is left out for a reason a person can act on (unreachable, locked, no isolation, does not offer the class, reserved for other work, no Claude access). The ones left are ranked:
the administrator's preference for that class first, then a fresh reading before a stale one, then the most free capacity, then the shortest wait for the worker to notice the job, then the
lightest load per core, then the name (so a tie is never random). Standard library only.
"""
from dataclasses import dataclass, field

STALE_S = 7200                     # a reading older than this is still used, but ranked after every fresh one. A healthy idle worker publishes about every half hour, so a shorter limit would call every idle worker stale
LIVE_S = 120                       # the worker's own running and waiting counts are believed only this long: they are published rarely, and a count that old says nothing about now
COST = {"light": 1, "medium": 2, "heavy": 4}


@dataclass
class Worker:
    """What is known about one worker right now. Build it with `worker_view`; every field has a safe default for "not known"."""
    name: str
    connection: str = "direct"                 # github | direct (later: lan)
    reachable: bool = True
    why_not: str = ""                          # when not reachable: what was seen (said as it is)
    state: dict = None                         # worker-state.json, or None when none was published
    registry: dict = None                      # registry.json (the classes it offers), or None
    age_s: float = None                        # how old the reading is
    pickup_s: int = 5                          # how long, at most, until it notices a job (its idle pace)
    sent_pending: int = 0                      # capacity POINTS of the jobs THIS computer sent to it that have no final result yet (a light job 1, medium 2, heavy 4): the only live measure of its load here
    configured: str = None                     # what this computer's configuration SAYS the connection is, when that differs from the one its copies really use

    def doc(self, *keys, default=None):
        cur = self.state or {}
        for k in keys:
            cur = cur.get(k) if isinstance(cur, dict) else None
        return default if cur is None else cur

    @property
    def listens_on(self):
        """What the worker says it listens on (it knows); the configuration of this computer only says what this computer believes."""
        return self.doc("transport") or self.connection

    @property
    def wait_s(self):
        """The longest a new job waits to be noticed: what the worker publishes, else what this computer assumes."""
        return int(self.doc("pickup_max_s") or self.pickup_s)

    @property
    def mismatch(self):
        said = []
        if self.configured and self.configured != self.connection:
            said.append(f"this computer's configuration says {self.configured}, but its copies of the queue and results use {self.connection}")
        if self.connection != self.listens_on:
            said.append(f"this computer sends over {self.connection}, but the worker listens on {self.listens_on}")
        return "; ".join(said)

    @property
    def offered(self):
        return (self.registry or {}).get("classes") or {name: {} for name in (self.doc("classes", default=[]))}

    @property
    def capacity(self):
        return int(self.doc("hardware", "capacity", default=0) or (self.registry or {}).get("capacity") or 0)

    @property
    def busy(self):
        """Jobs on it now, as far as this computer can tell: the jobs it sent that have not finished, plus the worker's own count while that count is fresh (the worker publishes it rarely)."""
        own = 0
        if self.age_s is not None and self.age_s <= LIVE_S:
            own = int(self.doc("observed", "running", default=0) or 0) + int(self.doc("observed", "waiting", default=0) or 0)
        return max(own, self.sent_pending)

    @property
    def headroom(self):
        return self.capacity - self.busy

    def cost_of(self, cls):
        """Capacity points a job of this class takes on THIS worker, from its own published registry (a worker may weigh a class differently)."""
        return COST.get((self.offered.get(cls) or {}).get("cost", "medium"), COST["medium"])

    def fits(self, cls):
        return self.headroom >= self.cost_of(cls)

    @property
    def per_core(self):
        load, cores = self.doc("observed", "load_1m"), self.doc("hardware", "cores") or 1
        return (load / cores) if load is not None else 0.0

    @property
    def stale(self):
        return self.age_s is None or self.age_s > STALE_S


@dataclass
class Prefs:
    """What the administrator decided (signed settings in the registry): workers reserved for certain classes, an order of preference per class, and classes tied to a kind of connection."""
    dedicated: dict = field(default_factory=dict)         # worker -> classes it is reserved for (it gets nothing else)
    preferred: dict = field(default_factory=dict)         # class -> [workers, best first]
    connections: dict = field(default_factory=dict)       # class -> [connection kinds allowed]


@dataclass
class Choice:
    worker: str = None
    lines: list = field(default_factory=list)             # [(worker, verdict, reason)], every worker, in a stable order
    busy_all: bool = False                                # the best one has no free capacity: the job queues there

    def explain(self):
        return [(n, v, r) for n, v, r in self.lines]


def _left_out(w, cls, kind, prefs):
    """The reason this worker cannot take the job, or None."""
    if not w.reachable:
        return f"cannot be reached now ({w.why_not or 'no answer'})"
    if w.state is None:
        return "has published no state (an older release, or not set up yet), so nothing can be said about it"
    if w.doc("locked", default=False):
        return "is LOCKED (a security lockdown): unlock it after you have looked"
    if not w.doc("isolation", "ready", default=False):
        return "has its job isolation OFF"
    reserved = [c for c in prefs.dedicated.get(w.name, ())]
    if w.name in prefs.dedicated and cls not in reserved:
        return f"is reserved for {', '.join(reserved) or 'other work'}"
    if cls not in w.offered:
        return f"does not offer the class {cls}"
    allowed = prefs.connections.get(cls)
    if allowed and w.listens_on not in allowed:
        return f"is reached over {w.listens_on}, and {cls} is limited to {' or '.join(allowed)}"
    if kind == "claude":
        c = w.doc("claude", default={}) or {}
        if c.get("auth", "none") == "none":
            return "has no Claude sign-in"
        if (c.get("tool") or {}).get("kind") == "missing":
            return "has no Claude tool installed"
        if w.doc("claude_paused", default=False):
            return "has paused Claude jobs for today (its daily limit)"
    return None


def _rank(w, cls, prefs):
    order = prefs.preferred.get(cls) or []
    return (order.index(w.name) if w.name in order else len(order), w.stale, not w.fits(cls), -w.headroom, w.wait_s, w.per_core, w.name)


def _why_chosen(w, best_rank, others, cls):
    bits = []
    if w.name in best_rank["preferred"]:
        bits.append("your preferred worker for this class")
    need = w.cost_of(cls)
    bits.append((f"{max(w.headroom, 0)} of {w.capacity} capacity points free, the job needs {need}" + ("" if w.fits(cls) else " so it will wait for running jobs")) if w.capacity else "capacity unknown")
    bits.append(f"notices a job within {w.wait_s} s")
    if w.mismatch:
        bits.append(f"WARNING: {w.mismatch}")
    if w.stale:
        bits.append(f"(its reading is {int((w.age_s or 0) // 60)} min old)")
    return ", ".join(bits)


def choose(cls, kind, workers, prefs=None):
    """The worker that should run a job of class `cls` (registry kind `kind`), and a reason for every worker. Deterministic: the order of `workers` never matters."""
    prefs = prefs or Prefs()
    ordered = sorted(workers, key=lambda w: w.name)
    out, eligible = {}, []
    for w in ordered:
        why = _left_out(w, cls, kind, prefs)
        if why:
            out[w.name] = ("left out", why)
        else:
            eligible.append(w)
    if not eligible:
        return Choice(None, [(w.name, *out[w.name]) for w in ordered])
    eligible.sort(key=lambda w: _rank(w, cls, prefs))
    best = eligible[0]
    order = prefs.preferred.get(cls) or []
    for i, w in enumerate(eligible):
        if w is best:
            out[w.name] = ("CHOSEN", _why_chosen(w, {"preferred": order[:1]}, eligible, cls))
        else:
            out[w.name] = ("not chosen", _worse_than(w, best, order, cls) + (f" (WARNING: {w.mismatch})" if w.mismatch else ""))
    return Choice(best.name, [(w.name, *out[w.name]) for w in ordered], busy_all=not best.fits(cls))


def _worse_than(w, best, order, cls):
    a, b = (order.index(w.name) if w.name in order else len(order)), (order.index(best.name) if best.name in order else len(order))
    if a != b:
        return f"{best.name} is preferred for this class"
    if w.stale != best.stale:
        return "its reading is old, and a fresh one was available"
    if w.fits(cls) != best.fits(cls):
        return f"the job would have to wait for running jobs here ({max(w.headroom, 0)} of {w.capacity} capacity points free, it needs {w.cost_of(cls)}), and it has room for it now on {best.name}"
    if w.headroom != best.headroom:
        return f"{best.name} has more capacity free ({max(best.headroom, 0)} against {max(w.headroom, 0)})"
    if w.wait_s != best.wait_s:
        return f"{best.name} notices a job sooner ({best.wait_s} s against {w.wait_s} s)"
    if w.per_core != best.per_core:
        return f"{best.name} is less loaded"
    return f"equal in every way; {best.name} comes first by name"


def worker_view(name, connection, state, registry, age_s, pickup_s, reachable=True, why_not="", configured=None, sent_pending=0):
    return Worker(name=name, connection=connection, state=state, registry=registry, age_s=age_s, pickup_s=pickup_s, reachable=reachable, why_not=why_not, configured=configured, sent_pending=sent_pending)
