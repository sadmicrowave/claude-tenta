# Profile runs on the Direct connection

Decision (2026-11-22): on the Direct connection a profile run does not need approval, because only a holder of the mailbox key can queue a job, and on Direct that is the person's own computer. On GitHub it still does: anyone who can write to the queue repository can put a job file there, so the approval signature is the control (spot-check finding F4).

The setting: `transport.profile_approval` in the worker's `ct.toml`.

- `required` (the default for now): a profile run waits for `ct-admin approve`, as before.
- `none`: only valid with `transport.kind = "direct"` (any other connection is refused by the configuration check). The worker then starts a profile run without approval **only while the job really is confined**: its jail isolates and is ready (bubblewrap or unshare, in full isolation mode). A worker with no jail, a broken jail, a relaxed isolation mode, or a Windows worker (a Job Object stops the process tree but isolates neither files nor network) still waits for approval whatever the setting says.
- The worker publishes what it will really do as `profile_approval` in its state document (`none` only when it would start one unapproved); the client uses it to say "sent, no approval needed" instead of asking you to approve.
- Nothing else loses its approval: classes with `tier: approve` and root-level actions are unaffected.

The record of what the key sent (slice 3, `feat/direct-job-record`): a Direct worker writes one line per job the first time it looks at the job file, before anything runs, to `received-jobs.jsonl` in its state folder (`src/ct/received.py`): when, job id, class, kind, the SHA-256 and size of the exact file, and what it did (`queued`, `awaiting_approval`, `started_unapproved`, `rejected` with the refusal code, `error`). No parameters, file names or contents. Read it with `ct-worker received` on the worker or `ct-admin received` from your computer (read-only, no password). A job you did not send means the key was copied: `ct-setup rotate-key`. The file is written by the worker account, so a job already running as that account could alter it: it is evidence of use, not a tamper-proof log. It is capped at two files of about 512 KB.

One key per worker: the key lives in each worker's own `client_home` (`<client_home>/mailbox/key`, `Direct.client_key`) and `ct-setup rotate-key` replaces only that worker's key, so a stolen key reaches one worker.

Default: the default on Direct is `none` (decided, list item 3a) now that all four protections exist: one key per worker, `rotate-key`, the worker-side record, and no unapproved profile run without a working jail (never on Windows). A profile run on a Windows worker always waits for approval, so Windows classes are still measured through approved profile runs and registered by the approval key's signature.

Not in this slice: making the descriptive fields of a profile run (`--what`, `--expect`, the estimates) optional when no one approves it.
