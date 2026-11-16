# Jobs: the life of one job

## In plain words

A job is a small file that says "run this class of work with these inputs". You send it with `cw-run submit`. Within a minute the worker
notices it, checks it against its list of allowed classes, and does one of three things: **refuses** it (with a plain reason), **holds** it for your
approval, or **queues** it. Queued jobs start when the machine has room: not too many at once, not too busy, not too hot. When a job finishes, its
output is checked, trimmed to what the class says may leave, and published for you to read with `cw-run result`.

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
| `prompt` | for `claude` classes only, 1 to 8000 characters |
| `argv`, `what`, `expect`, `est_*`, `packages`, `flags` | for a profile run: exactly one command as an argument list, what it does and what is expected, and honest estimates |

Commands are **argument lists built from the class's template**; there is no shell and no string interpolation.

### Passes

| Pass | When | What it does |
|---|---|---|
| `fetch` | timer, default every 30 s | pulls both repositories, publishes anything pending, snapshots each new job file once, validates, and rejects, holds or queues it |
| `step` | timer, default every 10 s | starts queued jobs oldest first while capacity, load and temperature allow, and never while locked down |
| `exec <id>` | started by `step`, one process per job | runs the job, measures it, shapes and scans the output, publishes the result |

### Status values

`rejected` (refused, with a note) · `awaiting_approval` · `queued` · `running` · `ok` · `error` · `quarantined` (output withheld by the guard) ·
`killed` (stopped by a limit or by you) · `cancelled` (never started). A job in `awaiting_approval`, `queued` or `running` has no final result yet.

### Admission

- Each class has a `cost` (`light` 1, `medium` 2, `heavy` 4 capacity points). Running jobs may add up to `hardware.capacity`; a heavy job takes a capacity-4 machine alone.
- A job is not started while the 1-minute load is above `hardware.max_load` or the hottest core is above `hardware.max_temp_c`, and a class may set its own `max_temp_c`.
- While the worker is locked down, `fetch` does not process jobs at all.

### Approval

`tier: approve` classes, profile runs, and any class a policy rule says needs approval wait. `cw-admin approve` shows the job, computes the SHA-256 of the
job file it showed you, and sends `approve <id> --sha256 <digest>`. The worker approves only if its own snapshot has that digest. `cw-admin review` records
what you saw, so `cw-admin approve --all` can approve several jobs at once, but only those that are unchanged since you viewed them.

### Execution limits

A fixed program search path (never the worker's home), a hard timeout (`timeout_s`, never more than an hour), memory (`max_rss_mb`), disk (`max_disk_mb`), a
cap on the size of any one file written, and no network unless the class declares `network: true` (which also forces approval under the core policy).
If the machine cannot build an empty network namespace, a no-network job is **refused**, not run with the network (`isolation.require_netns`).

### What is published

Only the fields the class declares (`output_keys`), capped in length; for a failed command only its error lines, never stack traces. The full output of a
failed command stays on the worker (mode 0600, 30 days) and is read with `cw-admin output <job>`. Before anything is published it is scanned by the guard
([security](security.md)).

### Packages

A class (or profile run) may list `apt:`, `npm:` or `pip:` packages. They must be pinned to an exact version unless the install turns pinning off, are fetched
into a throwaway tools folder with install scripts disabled, and never put on the search path of other jobs.
