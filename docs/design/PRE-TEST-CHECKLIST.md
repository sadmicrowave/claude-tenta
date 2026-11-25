# Pre-test checklist

**Goal.** Before any run that costs minutes (probes, end-to-end routes, a probe on a real machine), one cheap checklist verifies that every environment piece the run needs is in place, and fails at once with the one command that fixes it, so a long run never starts on a broken environment.
**Done when.** `tests/preflight.py --for <kind>` exists for `unit`, `probe`, `e2e` and `real HOST`; each script that starts such a run calls it first and stops on a problem; each problem names its fix command; a test fails if a script that starts a run skips the checklist.
**Not in scope.** What the tests assert; the change planner (`tests/impact.py`) and theme gates.

## What was already checked
- `scripts/t`, `scripts/release`, `tests/support.py`: Python packages from `pyproject.toml`, `git`, `ssh-keygen` (kind `unit`).
- `scripts/probe`, `scripts/e2e-*`: only that the `docker` / `orb` command exists. A stopped Docker or OrbStack gave a long, confusing failure.

## Checks per kind
| kind | checks |
|---|---|
| unit | packages, `git`, `ssh-keygen` (unchanged) |
| probe | `docker` on the PATH, the Docker daemon answers (`docker info`), free disk at least 5 GB |
| e2e | `orb` and `orbctl` on the PATH, OrbStack answers (`orb status`), fewer than the cap of test machines already exist, free disk at least 10 GB, `tar` and `git` present |
| real HOST | `ssh` on the PATH, the host answers within 5 s with key login only (`ssh -o BatchMode=yes true`) |

Every problem is one line: what is wrong, the real error text where there is one, and the command to run. The checks take under a few seconds in all; a run is never started on a failed checklist.
