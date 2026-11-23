# Profile time limits that follow the device

Status: scoped 2026-11-23 (list items 3 and 8 meet here; item 1, the unit-suite class, waits on it).

## Problem

A profile run is limited in time by a fixed cap in the worker's registry (`max_timeout_s` of `profile-run`, 600 s on the
fleet). That number was chosen by us, so it means nothing on someone else's device: a command that needs 17 minutes on a fast
computer cannot be measured on a slower one, and the person cannot know the device's limits until profiles have been done on it.

## What a time limit is for

Memory, disk and temperature are already enforced by their own limits. Time answers one question only: how long is the owner
willing to have this device busy with one measured run? That is a consent, not a capability, so it is not a number we pick.

## Rules

1. **The limit comes from the person's own estimate.** `--est-time-s` is already required. The limit of a run is
   estimate x the worker's speed factor x the profiling margin (3 today), never below the existing floor. A run cannot take
   longer than the person said it would, times the margin.
2. **The worker learns its speed factor.** A prior from the install (below), then replaced by evidence from its own finished
   profile runs (measured wall time against estimate). Stored in the worker's own state, per device.
3. **The owner's ceiling applies where the device is shared.** On a dedicated or special-use worker nothing else needs it, so
   rule 1 is the only limit. On a shared computer the owner's ceiling (today's 600 s) stays as the upper bound. The owner can
   change it; `ct-admin status` shows the value and where it came from.
4. **A limit hit is an answer.** A run that stops at its time limit while inside its resource limits returns the verdict
   `needs-longer`: what ran, what it used, the next limit (double, up to the ceiling) and the command that sends it. When the
   next limit is over the ceiling, the result prints the command the owner runs to raise it.
5. **The client refuses first.** The worker publishes `profile_time` (speed factor, its basis, ceiling and where it came from);
   the client checks a request against it before sending (the cap check already exists and reads the published cap).

## The install as the first measurement

The install is a free first sample on every worker, on the real hardware. It is network- and cache-dependent, so only its
local phases are used: byte-compiling the package, importing it, hashing a fixed amount of data, creating and cloning a fixed
git fixture. The server half records each phase's seconds in the worker's state. The release ships a table of reference times
per phase, recorded on our own fleet (the real Mac mini and the OrbStack machines) as fixtures, with a script to re-record
them. The ratio of this worker's phases to the reference is the speed-factor prior, published with its basis ("install, 4
phases") and a wide margin. Evidence from real runs replaces it as it arrives. The reference table is a prior, never the truth;
a baseline from our fleet says nothing about an end user's range, so the prior is only a starting point.

## Slices (each its own small branch; later ones stack on earlier only where files overlap)

| # | Goal | Done when | Files (security path?) |
| - | ---- | --------- | ---------------------- |
| 1 | The worker publishes `profile_time`, and a profile run's limit is estimate x factor x margin (factor 1 until measured), ceiling only on a shared worker | a worker test per rule 1 and 3 failing on the old code; the client cap check reads the new block | worker.py, jobs.py, profiling.py (yes: manual merge) |
| 2 | Install phase timings recorded, the reference table as recorded fixtures with a re-record script, the factor prior | tests over recorded phase files; a real install on the Mac mini shows the timings | setup/server.py, a new `calibration.py` (hardening/server: manual merge) |
| 3 | Evidence: each finished profile updates the factor (bounded step, outliers ignored) | tests with sequences of runs; the factor converges and one outlier moves it little | worker.py, profiling.py (yes) |
| 4 | The `needs-longer` verdict, the next limit and the command | the result card, a test of the exact wording and the command; a run that stops by time inside its limits ends this way | profiling.py, client/view.py (yes for profiling.py) |
| 5 | `ct-admin status` shows speed factor, basis and ceiling; docs | status output checked on a real worker | admin/cli.py, docs |

## Not in this design

- Approving or adopting profile results automatically (list item 3 proper).
- The measurement utility that recommends a better worker for a pinned job (list item 8).
- Giving the unit suite its own longer run time (list item 1): it uses this once slice 1 and 4 exist.

## Decisions (user, 2026-11-23)

- The install is the calibration, with local phases only and reference times recorded on the dev fleet.
- When the next limit is over the ceiling, the result prints the command the owner runs to raise it.
