# The orchestrator: roadmap

Status: written 2026-11-24 (list item 5), from the owner's own words of 2026-11-16 to 2026-11-23. The older name for the orchestrator is retired everywhere; the code module keeps its short name `ct.sched`.
Written so each slice can be built in its own session from this page alone. Slice order R1 to R7 approved by the owner 2026-11-25.

## What the orchestrator is

Your computer is the orchestrator host. It knows every worker you administer, decides which one runs each job, keeps jobs from piling on one worker, and is where you review, approve and
register things, never by logging in to each worker. One worker or many; one transport or several.

## Already decided, and reused here (not redesigned)

- **Why a job is worth offloading:** the ranked ladder in [OFFLOAD-REASONS.md](OFFLOAD-REASONS.md) (worker-specific 100, fewer AI tokens 80, faster 60, free your computer 40). It judges a job, not a worker.
- **Which worker takes it:** `ct.sched.choose` through `place.decide`: leave out workers that cannot (every reason reported), then rank by preference, fresh reading, free capacity points, shortest wait, lightest load, name. See [SEVERAL-WORKERS.md](SEVERAL-WORKERS.md).
- **Whether a pin is still the best worker:** `ct-run offload recommend` and its hint (item 8b), from your own finished jobs.

## Inventory: your words against the code (7 messages, 14 asks: 9 built, 3 partial, 2 missing)

| When | You asked for | State | Where it is, or what is missing |
| ---- | ------------- | ----- | ------------------------------- |
| 11-16 | One or many workers registered with a manager that weighs their spec limits | built | `ct-admin workers`, `worker-state.json`, `sched._left_out` (OS, CPU age, memory, isolation, locked) |
| 11-16 | Prioritise and queue across workers; none overloaded, not all on one | partial | capacity points, reservation lock and `limits.max_in_flight` spread load; there is **no queue on this computer** and no priority: a job that fits nowhere is sent to the worker with most room and waits there |
| 11-16 | A rename script reaches every worker | built | fleet commands in `ct-admin` (`apply`, `check`, ...) |
| 11-16 | Review, approve, register from the orchestrator host | built | `ct-admin approve/review/adopt`, which follow the job to its worker |
| 11-17 | A worker named by its id alone, short commands | built | aliases (`alias.py`), `--worker`, `ct-run use` |
| 11-17 | `wake` one or all workers over the install link | built | `ct-admin wake` |
| 11-18 | Track machine states | partial | readings with age; stale ones rank last (`STALE_S`), but nothing notices a worker has gone silent or recovers its jobs |
| 11-18 | Track queues | partial | this computer counts the jobs it sent with no result (`placements.json`), pruned after 14 days; no durable record of queue or outcome |
| 11-18 | Decide who best takes the load | built | `sched.choose` |
| 11-19 | Aliases as local masks the orchestrator works through | built | `alias.py` |
| 11-22 | Offload gain as a weighted ladder, list grows | built | item 8, `candidacy.py` |
| 11-22 | Recommend a better worker for a pinned command | built | item 8b, `recommend.py` |
| 11-23 | Orchestrator reports completion back; the AI never polls | missing | needs the AI layer |
| 11-23 | Fewer AI tokens (short result, fetch the rest) | missing | needs the AI layer; the ladder's rung 2 says "cannot be judged yet" |

Also found missing while inventorying: no retry of a failed job (only per-class timeouts, `jobs.job_timeout`), no limit per person, no history of placements beyond 14 days.

## Slices, in the proposed order

Each is a feature of `theme/orchestrator`, one session, one small branch. Not in scope for every slice: the AI layer, any new transport, anything outside the orchestrator host's own files and the worker fields it already reads.

### R1. A durable job queue with priorities (`feat/orchestrator-queue`; built 2026-11-25, see SEVERAL-WORKERS.md)
**Goal.** A job that fits no worker right now waits on this computer, in order, and is sent when one has room, instead of being pushed onto the worker with the most room. Survives a restart of `ct-run`.
**Done when.** A local queue file (mode 0600, written under the same lock as `placements.json`) holds waiting jobs with class, priority (`low`, `normal`, `high`; default `normal`) and age; `ct-run submit --priority`; `ct-run jobs` shows waiting jobs and why; a drain step places the oldest highest-priority job whenever capacity frees (on every `ct-run` command, never a background process); a job never waits forever without being said; tests failing on the old code, including a queue that survives a kill.
**Not in scope.** Dependencies between jobs, a background daemon, fair share (R4).

### R2. Worker health and failover (`feat/orchestrator-health`)
**Goal.** A worker that has gone silent is named, no new job goes to it, and the jobs it was holding are not left hanging.
**Done when.** A reading older than a limit (setting, default 1 day) marks the worker `silent` with its age; a job sent to a silent worker with no result is marked `lost` with the reason and, if it is safe to repeat (class flag), re-queued through R1; the card says which; `ct-admin workers` shows the state; tests for each state and for a worker that comes back.
**Not in scope.** Restarting or repairing the worker, heartbeats beyond what workers already publish.

### R3. Retries and timeouts per job class (`feat/orchestrator-retries`)
**Goal.** A transient failure is retried a limited number of times, and a job that runs too long is stopped and reported, per class.
**Done when.** Class settings `retries` (default 0) and a retry delay; a retry only for classes marked safe to repeat; the result card says "attempt 2 of 3"; the existing `timeout_s` and `max_timeout_s` are the stop rule and are shown in `ct-admin class show`; tests.
**Not in scope.** Retrying a job that ran to a clean error (the person decides), changing how approvals work.

### R4. Fair share and limits (`feat/orchestrator-fair-share`; built 2026-11-26: `limits.max_per_worker` and `limits.max_per_class`, `cap.share_problem`, checked at submit and in the queue drain; a job over its share waits with the reason)
**Goal.** One big batch cannot starve everything else, and no worker or person goes over a stated limit.
**Done when.** `limits.max_in_flight` gains a per-worker and a per-class form; a batch over its share queues behind others (R1) and the card says so; tests with a burst plus a single job.
**Not in scope.** Billing, accounts for several people (this is one person's fleet).

### R5. Audit trail (`feat/orchestrator-audit`)
**Goal.** Every placement and outcome is kept long enough to answer "where did it run, why, and what happened".
**Done when.** An append-only local log (mode 0600, trimmed by age, setting) with job, class, worker, the explanation rows, priority, attempts and outcome; `ct-run offload log`-style command to read it; the 14-day `placements.json` prune no longer loses the answer; tests.
**Not in scope.** Sending the log anywhere, signing it.

### R6. Operator status view (`feat/orchestrator-status`)
**Goal.** One terminal view of workers, queue and recent outcomes (the terminal dashboard idea).
**Done when.** `ct-admin status` panel: worker states, queue depth by priority, running and recent results, with bars that fit the terminal width (the cards guard draws it at 8 widths); reads R1, R2 and R5 only.
**Not in scope.** A web page, live refresh beyond a repeat flag.

### R7. Completion reporting and the token-saving foundation (after the AI layer)
**Goal.** The orchestrator reports a finished job back so the AI never adds a poll, and a long result becomes a short note plus the way to fetch the rest (ladder rung 2).
**Blocked on** the AI layer; design it with that layer, not before.

## Later release (listed, not designed)

Metrics export (queue depth, run times, failure rates); maintenance mode for a worker (drain before an update); dependencies between jobs, only once real use asks for them.

## Open for the owner

1. (Done 2026-11-25: R1 to R7 order approved.)
2. The module keeps the name `ct.sched`; renaming it to `ct.orchestrator` is mechanical and can be its own chore if wanted.
