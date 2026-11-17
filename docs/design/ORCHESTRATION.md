# Several workers: design notes

Status: **planned, not built.** These are requirements written down as they came up, so the multi-worker work starts from them. Today there is one
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
   A filter such as "only workers with Claude access" is the first use, and `cw-admin status` for one worker is the single-worker case of it.

## How this builds on what exists

- The worker already publishes `registry.json` (classes, capacity, memory, jail, trusted approver keys) to the results repository whenever it changes.
  A per-worker state snapshot extends that file, so the Mac can read every worker's state without logging in to any of them.
- The `Claude` row in `cw-admin status` (version, kind, last update) and `claude.auth` are the first environment facts to publish.
- Approvals, orders and registries are already signed with one key that every worker trusts (each worker keeps its own `approvers` file), so a
  signed message can be addressed to one worker or to all.
- Routing must be deterministic and explainable: when the scheduler picks (or refuses) a worker it says why, in the same terms as the comparison command.
