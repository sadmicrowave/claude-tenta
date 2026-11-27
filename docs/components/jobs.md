# Jobs: the life of one job

## In plain words

A job is a small file that says "run this class of work with these inputs". You send it with `ct-run submit`. Within a minute the worker
notices it, checks it against its list of allowed classes, and does one of three things: **refuses** it (with a plain reason), **holds** it for your
approval, or **queues** it. Queued jobs start when the machine has room: not too many at once, not too busy, not too hot. When a job finishes, its
output is checked, trimmed to what the class says may leave, and published for you to read with `ct-run result`.

Some classes run on their own (`auto`). Others always wait for you (`approve`): you read exactly what would run, then approve it. A job you are
testing for the first time (a *profile run*) always waits.

## Technical specification

### The job file

JSON at `jobs/<id>.json` on the queue repository's `jobs` branch.

| Field | Meaning |
|---|---|
| `id` | lowercase letters, digits and dashes, 4 to 64 characters, equal to the file name |
| `class` | a class in the installed registry |
| `params` | the class's declared parameters, each checked against its type (`enum`, `int [lo, hi]` or `regex`, fully anchored); unknown or missing ones are refused |
| `created` | ISO timestamp with a time zone; a job older than 24 hours, or more than 5 minutes in the future, is refused (replay protection) |
| `files` | optional small text files for classes with `accepts_files`; plain relative names (no dot-names, so a job cannot plant a settings file), bounded count and size |
| `argv`, `what`, `expect`, `est_*`, `packages`, `flags` | for a profile run: exactly one command as an argument list, what it does and what is expected, and honest estimates |

Commands are **argument lists built from the class's template**; there is no shell and no string interpolation.

### Passes

| Pass | When | What it does |
|---|---|---|
| `fetch` | timer, default every 5 s | pulls both repositories, publishes anything pending, snapshots each new job file once, validates, and rejects, holds or queues it |
| `step` | timer, default every 5 s | starts queued jobs oldest first while capacity, load and temperature allow, and never while locked down |
| `exec <id>` | started by `step`, one process per job | runs the job, measures it, shapes and scans the output, publishes the result |

### Smart sleep

A worker with nothing to do stops asking GitHub every pass. The timers still fire every `schedule.fetch_s` seconds (a pass that skips costs almost nothing), but once nothing has been queued, running or waiting (for approval, say) for `schedule.idle_after_min` minutes (default 10), the fetch pass only really runs every `schedule.idle_fetch_s` seconds (default 90). After `schedule.sleep_after_min` minutes with nothing to do (default 120) it goes into **sleep mode**: the fetch pass only really runs every `schedule.sleep_fetch_s` seconds (default 300, five minutes; `0` means no sleep mode). Any work ends the idleness at once and the normal pace returns: a queued, running or waiting job, a lockdown (so a signed unlock is seen quickly), or a new job found by one of the slow checks. The cost is that a job sent to an idle worker waits at most `idle_fetch_s` seconds before it is noticed; a job sent to a sleeping worker waits at most `sleep_fetch_s`, and then the worker is active again (every `schedule.fetch_s`, default 5 s). `idle_fetch_s = 0` turns smart sleep off, sleep mode included. `ct-admin wake` ends idle or sleep at once without waiting. `status` says whether a worker is active, idle or asleep and how often it is asking.

### Status values

`rejected` (refused, with a note) · `awaiting_approval` · `queued` · `running` · `ok` · `error` · `quarantined` (output withheld by the guard) ·
`killed` (stopped by a limit or by you) · `cancelled` (never started). A job in `awaiting_approval`, `queued` or `running` has no final result yet.

### Admission

- Each class has a `cost` (`light` 1, `medium` 2, `heavy` 4 capacity points). Running jobs may add up to `hardware.capacity`; a heavy job takes a capacity-4 machine alone.
- A job is not started while the 1-minute load is above `hardware.max_load` or the hottest core is above `hardware.max_temp_c`, and a class may set its own `max_temp_c`.
- While the worker is locked down, `fetch` does not process jobs at all.

### Approval

`tier: approve` classes, profile runs, and any class a policy rule says needs approval wait. `ct-admin approve` shows the job, computes the SHA-256 of the
job file it showed you, and sends `approve <id> --sha256 <digest>`. The worker approves only if its own snapshot has that digest. `ct-admin review` records
what you saw, so `ct-admin approve --all` can approve several jobs at once, but only those that are unchanged since you viewed them.

### Execution limits

A fixed program search path (never the worker's home), a hard timeout (`timeout_s`, never more than an hour), memory (`max_rss_mb`), disk (`max_disk_mb`), a
cap on the size of any one file written, and no network unless the class declares `network: true` (which also forces approval under the core policy).
Every job runs inside a **jail** ([security](security.md)): its own process tree and host name, system folders read-only, only its own work folder writable, an empty temporary home, and none of the install's configuration visible. A job whose class does not declare `network: true` also has no network. If the jail cannot be built, the job is **refused**, never run unjailed (`isolation.require_netns`).

### What is published

Only the fields the class declares (`output_keys`), capped in length; for a failed command only its error lines, never stack traces. The full output of a
failed command stays on the worker (mode 0600, 30 days) and is read with `ct-admin output <job>`. Before anything is published it is scanned by the guard
([security](security.md)).

### Packages

A class (or profile run) may list `apt:`, `npm:` or `pip:` packages. They must be pinned to an exact version unless the install turns pinning off, are fetched
into a throwaway tools folder with install scripts disabled, and never put on the search path of other jobs.

## Files a class was profiled with

When a profiling run is sent with `--dir`, `--include` or `--file`, the packaging (folder, patterns, files, relative to where the command was typed) is kept on this computer. `ct-admin adopt` writes it into the proposed class as a `submit:` section. The worker never acts on it; it only publishes it. `ct-run submit CLASS` then packages the same files when no `--dir`, `--file` or `--include` is given. Anything typed wins, `--no-pack` sends none, and a recipe with an absolute folder or `..` is refused (`pack.recipe.unsafe`). A folder outside the one you were in is not saved, and the profile output says so.

## Adopting a profile result automatically

At a terminal, `ct-run result` of a profile run whose verdict is `fits` adopts it right away: it runs `ct-admin adopt JOB --tier T --yes` on this computer, so the class is added to the registry and installed with **your own approval key**. A light or medium class is adopted with tier `auto` (it runs as soon as it is submitted); a heavy one is adopted with tier `approve` (it waits for your OK each time it runs). Each result is offered once, whatever came of it, and never in a pipe or script. Switch it off with `offload.auto_adopt = false` (`ct-setup install --set offload.auto_adopt=false`); then `ct-admin adopt` is yours to run. The tighter conditions (who or what may trigger it) belong to the AI layer and are on the backlog.

## How long a profile run may take

A profile run's time limit is not a fixed number. It is **your estimate x this worker's speed factor x the margin (3)**, never below 30 s:

- **Your estimate** is the `--est-time-s` you already give.
- **The speed factor** starts at 1. The install times four steps that depend only on the computer (compile, import, hash, git) and compares them with a reference table recorded on our own fleet (`scripts/record-calibration`); that gives a first factor. Every profile run that finishes cleanly then moves it toward what it showed (its measured seconds over your estimate), by at most a doubling per run, ignoring a run far from the others.
- **The owner's ceiling** (`max_timeout_s` of `profile-run`) applies only on a computer whose purpose is `shared`. A dedicated computer is limited by memory and heat, not by a fixed time.
- **A run that only ran out of time** is reported as `needs-longer`, not `does-not-fit`, so it is never recorded as an exclusion. The result shows the next limit (double, up to a shared computer's ceiling) and the exact command to send it again.

`ct-admin workers` and the worker's own `status` show the line, for example `estimate x 1.59 x 3; no ceiling; from install, 4 of 4 steps + 2 profile runs`. The worker also publishes it as `profile_time` in `registry.json`, which `ct-run offload profile` checks a `--timeout-s` against before sending.

