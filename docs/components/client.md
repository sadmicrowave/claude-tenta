# Sending work: `ct-run`

## In plain words

`ct-run` is what you use on your own computer to hand work to the worker and read what comes back.

- `ct-run submit sys-status --wait` sends a job and waits for the answer.
- `ct-run jobs` shows your recent jobs, and the ones waiting for your approval.
- `ct-run classes` shows what the worker is willing to run.
- `ct-run offload ...` helps you decide **what is worth sending**. It can quietly learn which of your commands are slow and repeated, check whether a command is already
  allowed or was already tested and found unsuitable, and test-run a new command on the worker (you approve that test) to measure how heavy it is. If it is a good fit, it
  becomes a *proposal* for you to review; if not, it is remembered so it is never tried again.

Nothing in `ct-run` can change what the worker is allowed to do. It only sends requests and proposals; you decide in `ct-admin`.

## Technical specification

### Commands

| Command | What it does |
|---|---|
| `setup` | clone the two repositories into the client home (`~/.ct` by default) |
| `submit <class> [-p name=value]... [--prompt T] [--file F] [--dir D --include G --exclude G] [--resend-all] [--wait] [--timeout S]` | validate the parameters against the worker's published registry, push `jobs/<id>.json` to the `jobs` branch, optionally wait. A folder goes as a list of file hashes plus only the files the worker lacks, so a repeat run sends what changed (`--resend-all` sends every file again; see [jobs](jobs.md), "Repeat runs") |
| `result <job> [--wait]` | read a result; `--wait` polls (exit 0 only for status `ok`, 3 if the worker is locked, 1 on timeout) |
| `jobs [-n N]` | recent jobs and those waiting for approval, with the command to approve each |
| `classes` | the classes the worker published, their cost, tier and limits, plus pending proposals |
| `prune [--days N]` | squash old job files out of the `jobs` branch |
| `offload log hook\|top\|show\|clear` | the command log (below) |
| `offload fit -- <cmd>` | registered / excluded / retry-after-upgrade / unknown; exit 0 / 3 / 5 / 4 |
| `offload history` | every fit check you or Claude made |
| `offload profile [options] -- <cmd>` | queue a profiling run of one exact command |
| `offload propose <profile-job>` | turn a profile result into a registry proposal (a branch) or an exclusion |
| `offload review` | what your own command log says a worker could take, ranked; at a terminal then one y/n per command to register it on your workers (below) |
| `offload exclusions` | commands that were tested and do not fit |

Status and errors go to standard error; standard output carries only what a script wants (a job id, a result, JSON).

### Pushing without a working tree

The client never edits a checkout. `ClientRepos.push_files` builds a commit with git plumbing on a temporary index and pushes it with a plain (never forced) push, retrying after a re-fetch.
Job files go to the `jobs` branch, exclusions to the `data` branch, proposals to `registry/<name>` branches.

### The command log

`offload log hook pre|post` is meant to be called by Claude Code's Bash hooks. It records each command and how long it took in `<client home>/command-log.jsonl` (mode 0600, never pushed).
A command that looks like it carries a secret is not recorded. `offload log top` ranks normalised "shapes" (tool, subcommand, flag names; paths and values are dropped) of commands that
could suit the worker: not network or version control, not a package manager, not privileged or interactive, not several real commands in one line, and slow enough (median 30 s by default) to be worth the 15 to 60
seconds of job overhead, or repeated (`offload.repeat_runs` runs, 3 by default, within `offload.repeat_days` days, 14 by default) with a total time of at least that long. Each row says which reason on the ladder makes it worth offloading (its "offload reason"); `--all` also lists every reason's verdict for every shape. While this computer is busy (5-minute load average per core at least `offload.busy_load_ratio`, 1 by default) a command that usually takes `offload.busy_min_seconds` (10) or more also counts, with the headline "Your computer is busy"; that is a live reading shown as a note, never stored in the log. `offload measure` reads the same log and suggests `offload.repeat_days` (the typical time your repeated commands took to reach `offload.repeat_runs` runs), with a table of how many commands would be candidates at other settings; it changes nothing and prints the one `ct-setup install --set` command that applies the suggestion. With too little data it says so and suggests nothing.

A compound line such as `cd x && scripts/t a b 2>&1 | tail -5` is judged by its real command. The line is split at `&&`, `||`, `;`, `|` and newlines outside quotes (a heredoc body is data and is dropped), plumbing segments (`cd`, `tail`, `head`, `grep`, `sed`, `wc`, `sort`, `tee`, `echo`, `sleep`, loop keywords and the like) are dropped, and the seconds of the whole row belong to the one segment left. A segment that is network, version control, a package manager, privileged or one of this install's own commands excludes the whole line, so `cd x && git push` is never a candidate. With two or more real commands left the time cannot be split honestly between them, so the line is skipped ("compound command", with the reason shown by `--all`) instead of credited to one. Code typed on the command line or fed on standard input (`python3 -c`, `python3 - <<EOF`) has no name to offload and is skipped. A project script keeps its name and its folder only when the folder is a generic one (`scripts/t`, `bin/run`), so different scripts are different shapes; any other folder is never kept. The example printed under each shape is the real segment only, never the whole line.

### The review summary

`offload review` (and a card at the end of `ct-setup install`, on a terminal only) reads the command log and says, for example, "In the last 13 days you ran 253 commands, 3.0 h in all, that tenta could take", then lists the top commands best first.
It reads only the local log and sends nothing; it shows shapes, never paths or values.

- **What the hours are.** The recorded seconds of the commands that pass the candidate rules above, in the window (`offload.review_days`, 14 by default; a younger log is read whole). It is the time they took on this computer, never a promise that a worker is quicker (a small worker can be slower): the gain is a free computer. Runs with no recorded timing are counted aside and said.
- **Order.** The highest-weighted reason met first (docs/design/OFFLOAD-REASONS.md), then the most time. `offload.review_top` (5) are listed; the totals cover every candidate.
- **Too little to say.** Fewer than `offload.review_min_commands` (20) commands in the window, or a log younger than `offload.review_min_days` (3), shows no hours. An empty log says so. A log with nothing a worker could take says why (the two most common reasons).
- **The y/n list.** At a terminal the same command goes on to the list below (the end of `ct-setup install` asks once whether to go through it now).

### Registering the recommendations (one y/n each)

For each recommended command, best first, `ct-run offload review` asks `Register it as a worker class? [y/n/never]`. That one answer is the approval of that one command: there is no "yes to all".

- **yes** sends a test run of that command to **every worker that can be asked** (the folder it last ran in is confirmed first: Enter keeps it, `-` sends none, or type another; the packages the folder's marker files propose are shown on the same card and the yes covers exactly those). Each worker's own approval comes next (`ct-admin approve`, unchanged), the run is measured there, and a worker where it **fits** gets the class adopted (`offload.auto_adopt`; off: the card prints the exact `ct-admin adopt` command). The class has the same name on every worker. The review picks no worker for later jobs: where the class is registered, the orchestrator chooses per job (availability and its other criteria), so "registered on this worker" is all it needs.
- **What the approver reads.** The run says it comes from your command history; the time is the median of the recorded runs, and the memory figure is a labelled placeholder (the log does not record memory).
- **A worker that cannot be asked** (unreachable, no report for over two hours, locked, or on a release older than this one) is named once, with its reason and, for an old release, `ct-admin apply --worker NAME`. Nothing is skipped silently; after fixing it, run the review again: it offers only what the worker lacks.
- **A command that fails on some workers** is registered where it passed and reported where not, with the tool's own text (does not fit: the limit it hit; ran out of time: the longer limit; not approved; not sent; no result in time, with the command to read it later).
- **A command already registered** everywhere is not offered; one registered on some workers is offered for the others only. A command excluded on a worker is not tried there again.
- **Never offered:** a command that is network, git, a package manager, privileged, one of tenta's own, or that looks like it carries a secret.
- **Enter** skips a command for this time only. **n** (not now) is not asked again for `offload.review_ask_again_days` (30); **never** is not asked again until undone. `ct-run offload review --declined` lists the answers and `ct-run offload review --forget N` undoes one. They are kept in `review-answers.json` in this computer's client folder (private to you).
- **Without a terminal** (a pipe, a script, a run started by Claude) it prints the list and the exact `ct-run offload profile` commands and asks or registers nothing.
- **Workers to update:** a worker must run this release (it publishes `profile_time` and `cache`) to be asked; update with `ct-admin apply`.

### Known problems

| Problem | What you see | What to run |
| ------- | ------------ | ----------- |
| The command log cannot be read (wrong owner or mode) | `review.log.unreadable` with the system's own message; at the end of an install, a note instead (the install still succeeds) | make the file readable, or `ct-run offload log clear` |
| No hooks installed yet | "Nothing recorded yet" | install the Claude Code hooks that call `ct-run offload log hook pre` and `post` |
| No worker yet | the summary still shows: it reads only this computer's log | none |

### Profiling and proposals

A profile run (`kind: profile`, class `profile-run` in the core pack) names exactly one command as an argument list, says what it does and what to expect, and estimates time and memory. It always
needs approval. The worker looks the command up, runs it under hard ceilings (a fraction of RAM, a temperature ceiling, a disk ceiling), measures it and publishes a verdict (`fits`,
`does-not-fit`, `inconclusive`) with the reasons and a drafted registry entry with limits set from the measurement plus a margin. `offload propose` turns a fit into a branch for
the administrator, or a misfit into an exclusion; `ct-admin adopt` does the review and install in one guided step.

### Claude Code integration

A Claude Code skill that tells Claude when a command is worth offloading, and how to check with `ct-run offload fit`, is planned. The command log hooks above already work with Claude Code's Bash hooks.
