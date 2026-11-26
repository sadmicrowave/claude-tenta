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
| `submit <class> [-p name=value]... [--prompt T] [--file F] [--dir D --include G] [--wait] [--timeout S]` | validate the parameters against the worker's published registry, push `jobs/<id>.json` to the `jobs` branch, optionally wait |
| `result <job> [--wait]` | read a result; `--wait` polls (exit 0 only for status `ok`, 3 if the worker is locked, 1 on timeout) |
| `jobs [-n N]` | recent jobs and those waiting for approval, with the command to approve each |
| `classes` | the classes the worker published, their cost, tier and limits, plus pending proposals |
| `prune [--days N]` | squash old job files out of the `jobs` branch |
| `offload log hook\|top\|show\|clear` | the command log (below) |
| `offload fit -- <cmd>` | registered / excluded / retry-after-upgrade / unknown; exit 0 / 3 / 5 / 4 |
| `offload history` | every fit check you or Claude made |
| `offload profile [options] -- <cmd>` | queue a profiling run of one exact command |
| `offload propose <profile-job>` | turn a profile result into a registry proposal (a branch) or an exclusion |
| `offload exclusions` | commands that were tested and do not fit |

Status and errors go to standard error; standard output carries only what a script wants (a job id, a result, JSON).

### Pushing without a working tree

The client never edits a checkout. `ClientRepos.push_files` builds a commit with git plumbing on a temporary index and pushes it with a plain (never forced) push, retrying after a re-fetch.
Job files go to the `jobs` branch, exclusions to the `data` branch, proposals to `registry/<name>` branches.

### The command log

`offload log hook pre|post` is meant to be called by Claude Code's Bash hooks. It records each command and how long it took in `<client home>/command-log.jsonl` (mode 0600, never pushed).
A command that looks like it carries a secret is not recorded. `offload log top` ranks normalised "shapes" (tool, subcommand, flag names; paths and values are dropped) of commands that
could suit the worker: not network or version control, not a package manager, not privileged or interactive, not compound, and slow enough (median 30 s by default) to repay the 15 to 60
seconds of job overhead, or repeated (`offload.repeat_runs` runs, 3 by default, within `offload.repeat_days` days, 14 by default) with a total time of at least that long. Each row says which reason on the ladder makes it worth offloading ("why it pays"); `--all` also lists every reason's verdict for every shape. `offload measure` reads the same log and suggests `offload.repeat_days` (the typical time your repeated commands took to reach `offload.repeat_runs` runs), with a table of how many commands would be candidates at other settings; it changes nothing and prints the one `ct-setup install --set` command that applies the suggestion. With too little data it says so and suggests nothing. `offload recommend` looks at the commands you pinned to a worker (`ct-run use <worker> --command <class>`) and, from the finished jobs this computer has seen (`runs.jsonl`, recorded when you run `ct-run jobs` or `ct-run result`), says whether another worker ran one clearly faster (median at least `offload.recommend_gain` percent lower, each worker with `offload.recommend_runs` successful runs). It never guesses from hardware and never moves a pin: it prints the `ct-run use` line that would. After `ct-run jobs`, `result` or `submit`, one hint line says so when a pinned command has a clearly faster worker, once per suggestion and at most every `offload.recommend_hint_hours` hours (24); `offload.recommend_hint = false` turns it off.

### Profiling and proposals

A profile run (`kind: profile`, class `profile-run` in the core pack) names exactly one command as an argument list, says what it does and what to expect, and estimates time and memory. It always
needs approval. The worker looks the command up, runs it under hard ceilings (a fraction of RAM, a temperature ceiling, a disk ceiling), measures it and publishes a verdict (`fits`,
`does-not-fit`, `inconclusive`) with the reasons and a drafted registry entry with limits set from the measurement plus a margin. `offload propose` turns a fit into a branch for
the administrator, or a misfit into an exclusion; `ct-admin adopt` does the review and install in one guided step.

### Claude Code integration

A Claude Code skill that tells Claude when a command is worth offloading, and how to check with `ct-run offload fit`, is planned. The command log hooks above already work with Claude Code's Bash hooks.
