# Backlog

Ideas for future features, one per line, newest first. Each becomes its own small branch when it is scoped. Nothing here is in progress.

In scope now: [Windows tools installer](design/WINDOWS-TOOLS-INSTALLER.md).

- 2026-11-20 (low priority) Paginated `ct-admin apply` review: a first question with `view changes` and `install` chips, then the CHANGES list as page one, then one DIFF block per page; and say on screen that DIFF covers only the files that run as root unless `--full`.
- 2026-11-20 An end-to-end UPGRADE route: install the previous release, then apply the current one (the first apply of the shared-computer release failed on all three workers because no test upgraded from an older release).
- 2026-11-20 Rename tenta to armada (ct- to ca-, arms arm1...): only when development is done.
- 2026-11-20 Mac as a worker: its own design (the platform seam only knows Linux and Windows names).
- 2026-11-20 A Special Use route that runs a class which uses a tool folder end to end (the route only checks the install).
- 2026-11-20 Clean up leftover files from earlier test runs (needs a say-so per file).
- 2026-11-20 Decide the minimum Python version (3.10 or 3.11).
