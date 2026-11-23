# Profile runs on the Direct connection

Decision (2026-11-22): on the Direct connection a profile run does not need approval, because only a holder of the mailbox key can queue a job, and on Direct that is the person's own computer. On GitHub it still does: anyone who can write to the queue repository can put a job file there, so the approval signature is the control (spot-check finding F4).

The setting: `transport.profile_approval` in the worker's `ct.toml`.

- `required` (the default for now): a profile run waits for `ct-admin approve`, as before.
- `none`: only valid with `transport.kind = "direct"` (any other connection is refused by the configuration check). The worker then starts a profile run without approval **only while the job really is confined**: its jail isolates and is ready (bubblewrap or unshare, in full isolation mode). A worker with no jail, a broken jail, a relaxed isolation mode, or a Windows worker (a Job Object stops the process tree but isolates neither files nor network) still waits for approval whatever the setting says.
- The worker publishes what it will really do as `profile_approval` in its state document (`none` only when it would start one unapproved); the client uses it to say "sent, no approval needed" instead of asking you to approve.
- Nothing else loses its approval: classes with `tier: approve` and root-level actions are unaffected.

Why the default stays `required` for now: the other protections from the spot-check are not in yet (rotating a worker's mailbox key, and a disk ceiling on what a worker accepts). The default changes to `none` for Direct when they are.

Not in this slice: making the descriptive fields of a profile run (`--what`, `--expect`, the estimates) optional when no one approves it.
