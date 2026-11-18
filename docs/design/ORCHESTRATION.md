# Several workers: design notes

Status: **started.** Built so far: each worker publishes `worker-state.json` and `ct-admin workers` compares workers from it (requirement 3, and the data requirement 2 needs). Not built: choosing a worker automatically, dedicated workers and candidate locks, keeping a batch on one worker.

Original status: **planned, not built.** These are requirements written down as they came up, so the multi-worker work starts from them. Today there is one
worker per install. The signed approvals, orders and registry (see `docs/components/setup.md`) already work from the administrator's own computer
for that reason: nothing about running several workers should need a login on each.

## Requirements

1. **A batch stays together.** A batch of jobs (`submit` with many files, split into independent jobs by `plan_batches`) is placed on **one** worker as a
   unit. Spreading one batch over several machines would mix their environments and make the results hard to compare. Different batches may go to
   different workers; one batch never does.

2. **Dedicated workers and a priority per command.** The administrator can reserve a worker for certain commands (for example a worker that exists only to
   build a clean testing environment) and set which workers are preferred for which classes. This is an administration setting kept with the registry and
   signed like the rest, not something decided by the workers.

   Choosing the best worker cannot use physical statistics alone (cores, memory, heat, load). It also needs the **install state**: which packages,
   which tools and which versions are present on each worker, and whether that is what the command needs. So a worker publishes a *state snapshot* and
   the scheduler works from candidate locks: a candidate's state is captured when it is considered, and the choice is only valid while that state still
   holds. (Otherwise a package installed or removed between choosing and starting would make the choice wrong.) The snapshot changes when packages
   change, so the registry's `packages` lists and the profile history are inputs to it.

3. **A command to compare workers.** One command that lists every registered worker with its hardware and its environment state, side by side: Claude
   access and its current state (signed in, which route, tool version, whether the tool is the older pinned release, last update check), packages,
   load, heat, free memory, lock state. It serves both for status comparison and for choosing a worker by hand when the automatic choice is not wanted.
   A filter such as "only workers with Claude access" is the first use, and `ct-admin status` for one worker is the single-worker case of it.

## How this builds on what exists

- The worker already publishes `registry.json` (classes, capacity, memory, jail, trusted approver keys) to the results repository whenever it changes.
  A per-worker state snapshot extends that file, so the Mac can read every worker's state without logging in to any of them.
- The `Claude` row in `ct-admin status` (version, kind, last update) and `claude.auth` are the first environment facts to publish.
- Approvals, orders and registries are already signed with one key that every worker trusts (each worker keeps its own `approvers` file), so a
  signed message can be addressed to one worker or to all.
- Routing must be deterministic and explainable: when the scheduler picks (or refuses) a worker it says why, in the same terms as the comparison command.

How a job reaches each worker (the transport) is a separate decision, recorded in `TRANSPORTS.md`: with the default **Direct** transport your computer delivers to each worker and does the scheduling, which is what this note assumes.

## Decisions for the scheduler (2026-11-18)

**1. One network can mix connections.** A worker's connection (GitHub, Direct, later a local git server) is a property of that worker, not of the network. Nothing needs forcing, because the code already works that way: every worker has its own configuration file with its own `transport.kind`, `fleet_of` builds one `ClientRepos` per worker from it, and `ClientRepos` reaches its store only through the `Transport` interface (`store_for(cfg)`), so "read this worker's `worker-state.json`" and "deliver this job" are the same two calls for every kind. The scheduler therefore asks each worker's own transport and never branches on kind.

What a mix does need, all of which the scheduler needs anyway:
- **Reachability differs by kind.** A worker on Direct is reachable only from the home network; a GitHub worker from anywhere. "Not reachable now" is a normal reason to leave a worker out, said in plain words (the same reason the comparison command shows), not an error.
- **Pick-up latency differs by kind.** Direct picks a job up within seconds; GitHub within the worker's polling pace. It is an input to the choice and to the wording of what happens next.
- **Wording and pace are per worker.** `view.set_pace` and `view.STORE` are process-wide settings made for the single worker a command talks to. A command that talks to several (the scheduler, `ct-admin workers`) must take them per worker. This is the one real piece of extra handling, found while checking; it has a failing-first test in the plan below.
- **Trust is shared and already uniform.** One approval key is accepted by every worker. Workers never talk to each other, so a compromised worker on one connection cannot reach the others: mixing does not make the network's security that of its weakest member. A class that must only run on a particular kind of connection says so in its worker preference (decision 3).

If a mix ever caused a conflict the design could not absorb, the fallback is a network-wide setting that refuses to add a worker on another connection; nothing in the design depends on it.

**2. Where it runs: at submit time, on your computer, from the last collected state, with no new service.** `ct-run submit` reads each reachable worker's published state (the cached copy when fresh, a read over its own connection when not), chooses, and delivers to that one worker. A computer that is asleep loses no job, because there is nothing waiting on it. A placement queue that holds jobs while every worker is busy would need a long-running service and is a possible later addition, not part of the first version.

**3. The first version.** Connections: all of them, through the interface. A batch stays on one worker (requirement 1). Per-class preferred and dedicated workers are signed settings in the registry (requirement 2), including "only on workers reached by X". Every pick, and every refusal, is explained in the comparison command's terms. When every worker is busy or unreachable, the job goes to the least-loaded reachable worker and queues there, with the reason shown; if none is reachable the error says which workers were tried and why each was left out.

**4. Never move or copy a placed job.** Once delivered, a job belongs to that worker until it ends (the one-job-one-result rule in `SINGLE-JOB-DEVICES.md`). A worker that goes offline holds its jobs; the scheduler does not re-send them elsewhere, because a job that already ran part-way must not run twice.

## Plan (each step starts with the test that fails first)

1. **State model.** A `WorkerView` per worker (name, connection, reachable and why not, capacity and load, heat, memory, lock state, environment facts, age of the reading) built from `worker-state.json` through the transport; fixtures recorded from the real worker.
2. **Per-worker words and pace.** Remove the process-wide state in `view`; a test that two workers on different connections in one command each get their own wording.
3. **Scoring and the choice.** A pure function `choose(job_class, workers, prefs) -> (worker, reasons)`; deterministic; tested with table cases (dedicated worker, busy, hot, locked, unreachable, batch stickiness, tie-breaks).
4. **Registry settings.** Signed per-class preferences and dedicated workers; `ct-admin` commands to set them.
5. **Submit and batches.** `ct-run submit` places a batch on one worker; the explanation is printed with the "queued" card.
6. **Comparison command.** `ct-admin workers` shows the reasoning column; the route-by-tier matrix gets rows for each route (one worker busy, one offline, mixed connections, laptop off the home network).

## A journey the tests must cover (added 2026-11-18)

**Move one registered worker to another connection and back, after the multi-worker environment is established** (for example the worker on GitHub to Direct and back to GitHub, while the others stay as they are). What it must prove:
- the other workers, their configurations and the scheduler's view of them do not change (the `add-worker` fingerprint rule, applied to a move);
- jobs already queued or running on that worker finish and are read back across the move, and no job is sent twice or lost;
- the scheduler's reading of that worker (connection, pick-up time, reachability) follows the move on the next look, with no restart;
- the host's job cap and the "where it answered" memory (`last-seen.json`) still hold for it;
- the same move works in both directions, and a second move right after the first.
Cells: unit (the real command with only the machines faked, two workers in one `workers.toml`), then the two real machines by hand.
