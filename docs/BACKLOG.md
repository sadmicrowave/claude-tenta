# Backlog

Ideas for future features, one per line, newest first. Each becomes its own small branch when it is scoped. Nothing here is in progress.

In scope now: [Windows tools installer](design/WINDOWS-TOOLS-INSTALLER.md).

- 2026-11-20 The server half shows `ssh ctadmin@None` in its "real reason" hint (the server's own configuration has no host): print the address the person used.
- 2026-11-20 The installer's SSH session from a computer to an OrbStack machine stalled for an hour after `ufw --force enable` (outbound deny for 192.168.0.0/16): give the session keep-alives and a visible timeout so a hang says what it is waiting for.
- 2026-11-20 The test machines' cached image points github.com at a local stand-in (127.0.0.1): say so in the testing docs, since anything meant to reach real GitHub must remove it first.
- 2026-11-20 (low priority) Paginated `ct-admin apply` review: a first question with `view changes` and `install` chips, then the CHANGES list as page one, then one DIFF block per page; and say on screen that DIFF covers only the files that run as root unless `--full`.
- 2026-11-20 An end-to-end UPGRADE route: install the previous release, then apply the current one (the first apply of the shared-computer release failed on all three workers because no test upgraded from an older release).
- 2026-11-20 Rename tenta to armada (ct- to ca-, arms arm1...): only when development is done.
- 2026-11-20 Mac as a worker: its own design (the platform seam only knows Linux and Windows names).
- 2026-11-20 A Special Use route that runs a class which uses a tool folder end to end (the route only checks the install).
- 2026-11-20 Clean up leftover files from earlier test runs (needs a say-so per file).
- 2026-11-20 Decide the minimum Python version (3.10 or 3.11).

## The "Candidacy check" card lists Linux checks before a Windows computer is recognised
`ct-setup add-worker <windows address>` first prints the Linux check list (Ubuntu, systemd, sudo ...) and "It only reads", and only then says "This is a Windows computer" and offers to install its tools. Recognise the greeting before the summary card and show a Windows-specific one. Found on the real Windows 11 VM.

## Worker choice: count the whole route-trip cost, as a tie-break below the immediate factors
`sched._rank` orders: preferred/dedicated, stale, held, fits now, most capacity free, `wait_s` (time until the worker NOTICES a job: 5 s assumed for Direct, the idle fetch interval for GitHub), load per core, name. In a fleet mixing Direct and GitHub workers, one more free capacity point beats a much faster route, and `wait_s` ignores the rest of the trip (push, result coming back by polling, a slow link) and is assumed, not measured. Change: replace `wait_s` with an estimate of the full trip (pickup + result return, from the settings and recent measured round trips); rank it above "most capacity free" only when the job fits on both, keep preferred/dedicated, freshness, held and "fits now" above it; show both estimates in the "why chosen" text. Needs its own scoped branch (it reorders real job placement).
