# Running it: `ct-admin`

## In plain words

`ct-admin` is how you, the administrator, look after the worker from your own computer. It connects over SSH with your key (it never asks you to type a password into it;
`sudo` asks you itself when something needs it). Its commands are grouped by what you are doing:

- **Worker:** `status` (is it healthy, what is it doing, what did it do lately) and `check` (does the connection work).
- **Jobs:** `review` shows waiting jobs; `approve` approves the one you looked at; `cancel` and `kill` stop a job.
- **Security:** `quarantine` shows results the guard held back, `unlock` clears a lockdown once you have looked, `output` shows the full output of a failed command.
- **Registry:** `adopt` turns a tested command into a job class; `retire` removes a class; `proposals` lists, merges and tidies waiting changes.
- **Release:** `diff` shows what installing a version would change; `apply` installs it, checks the worker still works, and offers to put the old version back if not.

Anything that changes something shows you what it will do and asks first.

## Technical specification

### How it talks to the worker

`admin/ssh.py`: `ssh` with `BatchMode` for captured commands (fails fast if key login is not set up) and a terminal for interactive ones (so `sudo` can ask for the password).
A connection may be shared for at most 60 seconds so that one command that makes several calls asks once; nothing remembers a password or keeps a long-lived connection. A host
beginning with `-` is refused. The worker's commands are run as `<name> <command>` through the worker's own `sudo` entry point; the read-only ones (`status`, `show`, `review`,
`quarantine`, `output`) are allowed without a password when `access.sudo = "readonly-nopasswd"`, everything else asks.

### Reviewing and approving

`review` fetches the job files and the published results, prints what each would do, and records `{job id: SHA-256 of the file you were shown}` in `<client home>/viewed.json`.
`approve <id>` prints the job, shows its digest, asks, and sends `approve <id> --sha256 <digest>`. `approve --all` takes only the jobs that are viewed **and unchanged**
(it lists each as READY or SKIPPED with the reason). If a job's file is gone from the queue repository but the worker kept its own snapshot, approval happens on the worker, so what you
read is exactly what it holds. In a terminal, approving then waits and prints the result (`--no-follow` returns at once).

### Registry changes

`adopt <profile-job>` previews a proposal from a profile result, asks once, writes the branch, squash-merges it into `main`, then runs `apply`. `action add|remove|list` registers, takes away and lists root-level actions; `action add <definition>` registers a command that must run as root the same way (it fetches the program's fingerprint from the worker, shows a RUNS AS ROOT card, asks once, merges, signs and installs; see [Root-level actions](privileged.md)). `theme` shows the colour roles and where the theme came from ([Colours](theme.md)). `retire <class>` shows the class and its
comments, asks, removes it on a branch, merges it and applies. `proposals` lists branches of the queue repository with whether each is already merged; `proposals merge <branch>` (or `--all`)
shows the commits, builds **one** squash commit authored as the branch's author with a `Merged-Branch` trailer, **validates a registry change before pushing**, pushes `main`, and deletes the
branch; `proposals tidy` deletes merged branches. The `profile-run` class cannot be retired.

### Comparing workers

`ct-admin wake` and `ct-admin audit` reach every worker, or one with `--worker NAME`, over the SSH login `ct-admin check` uses; the worker is named once, in `ct-admin workers add`, and its address, account and settings come from its own configuration, so no paths or options are typed. `wake` (no password) ends idle and sleep at once: the worker looks for jobs now and returns to its quick pace. `audit` runs the read-only configuration check as root (`sudo ct-update --audit`), so the worker's sudo asks for your password. The `SLOW` banner and the waiting line point at `ct-admin wake`; a job counts as late only after the slowest pace the worker keeps (a sleeping worker, `schedule.sleep_fetch_s`) plus a minute.

`ct-admin workers` lists every worker this computer administers, side by side: operating system, processor (and whether it is too old for the current Claude Code), free memory, Claude access and its state (sign-in route, tool version, "older release, pinned"), lockdown and isolation, release, load and heat, and how old the reading is. It reads each worker's published `worker-state.json` from its results repository, so it needs no login to any worker, and an old reading is marked STALE (over 2 hours) and, past `health.silent_hours` (default 24), SILENT: the orchestrator then places no new job on it, and a job it was holding with no result is called lost (see docs/design/SEVERAL-WORKERS.md, R2). `ct-admin class set CLASS --repeatable on` marks a class safe to run twice, so a lost job of that class is queued again on another worker; `ct-admin class show` says which. `--retries N` (0 to 5, needs `--repeatable on`) and `--retry-delay SECONDS` (default 60) also run again a job that was stopped from outside (it ended `killed`), and the result card says "attempt 2 of 3"; `class show` lists the stop rule (`timeout_s`, `max_timeout_s`) and the retry rule (docs/design/SEVERAL-WORKERS.md, R3). The worker publishes the part that describes it (release, OS, processor, Claude, the packages its classes need, trusted approver keys, locked or not) whenever that changes, and the live readings (load, heat, memory) at most every half hour. `--claude` keeps only workers whose Claude access works and that are not locked; `--json` prints the same facts for a script or a future orchestrator; `--no-refresh` skips pulling from GitHub. The worker in your main configuration is always the first row; others are added with `ct-admin workers add <name> --config <file>` (each is a complete install with its own configuration, queue and results repositories) and removed with `workers remove <name>`. **Every worker has its own private queue and results repositories, never shared** (a deploy key covers a whole repository, so two workers on one repository could read or forge each other's jobs): `workers add`, `ct-setup install` and `ct-setup add-worker` refuse a configuration whose queue or results repository another worker already uses, or whose two repositories are one, and `ct-admin workers` says so for a hand-edited list. No setting turns this off. `workers add <name> --config <file> --github` also creates the worker's two private repositories with `gh` (when missing), tags every repository the tool creates with the GitHub topic `claude-tenta` so one search groups the fleet, and clones them; a worker added by `ct-setup add-worker` gets the names `<install name>-<worker>-queue` and `-results` by default. `workers remove <name> [--repos keep|archive|delete]` never touches the repositories silently: at a terminal it names them and asks (default keep; delete asks again), without a terminal it keeps them, and a repository another worker lists is never offered. This is the first step of running several workers: see `docs/design/ORCHESTRATION.md`.

### Release

`diff [rev]` and `apply [rev]` use a bare clone of the release source (`updates.source`, branch `updates.branch`) kept in the client home. A commit must be on that branch to be installable.
`diff` lists the changed files with the root-run ones first and flagged. `apply` shows the diff, asks, runs `sudo ct-update --rev <commit>` on the worker, then:

1. reads the worker's state in one call: the three timers, failed units, the installed revision, and that the installed registry is valid;
2. if code changed, submits the `sys-status` class as a real end-to-end test job;
3. if anything fails, says so and, in a terminal, offers to install the previous revision (`--yes` or no terminal never rolls back by itself; it prints the command).

`--force` installs even when nothing differs; `--no-verify` skips the checks.

### Exit codes and output

Errors use exit 2 with a title, the reason, steps and a command to copy. A "no" answer is exit 1. Status messages go to standard error.
