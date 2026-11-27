# Why offload: a ranked ladder of reasons

Status: agreed 2026-11-23 (list item 2): the weights and the definition of "repeatable" below were accepted by the owner of the project, including rung 1 ranking above rung 2 when a job meets both.
A living document: new rungs are added as they are found; it is written so its plain-language parts can be reused for a public website.

## Why a ladder

"Slow" and "repeated" are not enough to show that offloading is worth the trouble: they give few examples from real use. Offloading helps in several different ways, and
a job can qualify for more than one. Each way is a **rung**. Each rung has a weight. A job's headline reason is the **highest-weighted rung it meets**, and it keeps being
evaluated against **every** rung: a job that fails one rung is never dismissed before the others are read.

Every rung is also a **lane**: a direction to design further, asking what else a person gets there besides "it ran somewhere else".

The tool is a command-line application. Nothing here is specific to one AI model or company; where an AI model is involved, any model can be used.

## The rungs

| # | Rung | Plain-language value | Signal the tool has today | Weight |
| - | ---- | -------------------- | ------------------------- | --------------- |
| 1 | **Worker-specific work**: the job must run on one particular worker | It could not run on your own computer at all, or should not | the person pins it (`ct-run use`, `.tenta.toml`); the class belongs to one worker | 100 |
| 2 | **Fewer AI tokens**: work taken away from the model | The model reads a short result instead of doing or waiting on the long work | none yet (needs the AI layer; see the token-saving foundation, list item 5) | 80 |
| 3 | **Faster**: parallel work across devices | Many pieces at once finish sooner than one after another | batchable classes, files split into independent jobs | 60 |
| 4 | **Free your own computer**: long or repeated work leaves your CPU alone | Your computer stays quick while the work runs elsewhere | the command log: seconds and how often (`ct-run offload log top`) | 40 |
| 5 | **Your computer is busy**: it is already loaded, so offloading keeps it responsive | Several AI sessions and test suites at once slow an older computer to a crawl | a live reading: 5-minute load average per core (`offload.busy_load_ratio`); `ct-run offload log top` | 50 |
| 6 | **More to find** | | | |

### 1. Worker-specific work (three kinds)

- **Must run somewhere else:** another operating system or distribution, for example a real Linux for a Linux build or a clean OS for an end-to-end test.
- **Run while you are away:** an always-on device finishes and keeps results while your computer is off or asleep.
- **A special environment kept off your computer:** security testing tools, a specific coding environment, anything you would rather not install or run next to your own work.

Lane questions: a catalogue of ready-made classes per operating system (a consumer category: "things your Mac cannot do"); a worker that wakes on demand; environments that are
rebuilt clean for every job.

### 2. Fewer AI tokens

An AI model should not spend its tokens waiting on, or reading the full output of, work a worker can do and summarise. The saving is largest when a long result is replaced by a
short note and a way to fetch the rest. Requirements already agreed: a setting at four levels (global, workspace, job, worker); a one-line note on every shortened result saying
how to fetch the full one; the worker keeps the full result for a period; the orchestrator learns from each run. Not tied to any one model: a thin layer per model later.

### 3. Faster

More workers do more at once, up to a point: sharding a job into independent pieces, then putting the results back together reliably. Lane questions: how pieces are chosen,
what happens when one fails, how far more workers keep helping (the measurement utility, list item 8, answers this per person).

### 4. Free your own computer

Long runs, and short runs that repeat, take your computer's CPU. **"Repeatable"** needs a definition; Agreed: a command shape the person has run **at least 3 times in 14 days**.
The two numbers are settings (`offload.repeat_runs`, `offload.repeat_days` in `ct.toml`, overridable in a folder's `.tenta.toml`), not constants in code. The measurement utility
later suggests `repeat_days` as the typical time the person's repeated commands took to reach `repeat_runs` runs (`repeat_runs` itself is only shown as a grid: the log cannot say what it should be) instead of a guess.

### 5. Your computer is busy (built 2026-11-26, slice 2 of `theme/offload-value`)

Older computers cannot run several AI sessions and test suites at once, and they are who this tool is for (on the owner's Mac the load average reached 203 on 8 cores while tests, a virtual machine and Finder ran together). **Weight 50, decided by the owner 2026-11-26**: below the structural reasons (a property of the job), above rung 4 (a measured fact now beats a guess about the future); when a command is both repeated and the computer is busy, the headline names the busy computer.

- **Signal:** the 5-minute load average divided by the number of cores (`src/ct/client/hostload.py`); the 5-minute window means a short spike does not count and no state is kept. Windows, or a failed read, is "cannot be judged".
- **Settings:** `offload.busy_load_ratio` (default 1.0: one waiting task per core) and `offload.busy_min_seconds` (default 10).
- **How it combines with the log:** the rung is a live reading, never stored. It is met only while the computer is busy now, for a command that could run on a worker and whose usual run takes at least `busy_min_seconds`. It can lift a command that no other rung picks (the floor keeps a one-second command from being "worth offloading"). `log top` says "your computer is busy now" as a note under the table; `offload measure` ignores it.
- **Not built:** offloading automatically when busy; it only reports the reason.

### 6. More to find

Keep a list here as ideas arrive; none is promised. Examples to test: privacy (work that should not leave a controlled device), cost (a worker that is cheaper to run than a
cloud service), energy or heat (work that should not warm your laptop), network position (work that is faster close to the data), time zone or schedule (work that must run overnight).

**Rung 5 "your computer is busy" was a proposal here and is now built (weight 50; see section 5).** The original proposal, kept for the record: Offloading is also worth doing when your own computer is already working hard: its load (run queue or CPU use) is high against its core count, so a job started there would slow both the job and everything else you are doing. The signal is cheap and local (host load divided by core count, read when the job is about to start). It is a **proposal only**: the owner sets the weight and where it sits in the ladder, and it is not built until then. Open questions for that decision: what load-per-core counts as busy, how long it must stay busy, and whether it can lift a job that no other rung picks.

**More to find (ideas, none promised):**
- Run the test suite for branch X on a worker while you keep editing branch Y here (the snapshot of X is taken once, so later edits do not change the run; see [workspace jobs](WORKSPACE-JOBS.md)).
- A pre-push gate on a worker: the checks that should pass before a push run on a worker against a snapshot of exactly what would be pushed, and the push waits for the short answer.
- A scanner, a build or a document generator you run on a schedule: any command on any folder, not only tests (the workspace job is command-agnostic).

### Evidence from real use (2026-11-26)

The project owner's own command log held **742 runs of the test script (8.9 hours in all) and 340 end-to-end runs (3.9 hours)**, about 12.8 hours of work that a worker could have done, yet `ct-run offload log top` listed **no candidates**. The commands are written as compound lines such as `cd folder && command | tail`, and the candidacy check skips any line with more than one segment (`src/ct/client/cmdlog.py:91`, "compound command (offload a single command)"). A tool that shows no value from a person's own history on day one gives them no reason to continue. Fixing it is slice 1 of `theme/offload-value`; showing the value is the review flow described in [workspace jobs](WORKSPACE-JOBS.md).

## How a job is judged

1. Describe the job: its class or command shape, how long it takes, how often it runs, whether it is pinned, whether it can be split.
2. Check every rung. Record, for each, met or not met and why (never stop at the first miss).
3. The headline reason is the highest-weighted rung met; the other met rungs are listed beneath it.
4. Show it where a person decides: `ct-run offload log top`, a profile result, and the placement explanation.

Candidacy today looks at rung 4 only (seconds and count). Evaluating all rungs is list item 8.

Note (2026-11-26): rung 4's command-log signal missed compound commands; fixed in slice 1 (`fix/offload-sees-compound-commands`). Measured on the same log afterwards: of the 743 rows that mention the test script, 171 are runs of it (the rest are file edits, commits and waiting loops that only name it), and the end-to-end script's 17 runs surface too. Runs started in the background log about 2 s, so most of the time sits in the waiting loops that follow them (a possible later slice: credit a loop's time to the command it waits for).

## Decisions

- 2026-11-23: the weights above stand. The definition of "repeatable" (3 runs in 14 days, as two settings) stands. Rung 1 ranks above rung 2.

## Candidacy over every rung, and the measurement utility (list item 8; design 2026-11-24)

Branch `feat/candidacy-measurement` (and the slice branches `feat/candidacy-show-rungs`, `feat/offload-measure`), features of `theme/orchestrator`. Weights and "repeatable" are decided above and are not reopened here.

**Goal.** Judge a command against every rung of the ladder (never stopping at the first miss), name the headline reason, and give the person a measurement utility that suggests `offload.repeat_runs` / `offload.repeat_days` from their own command log.
**Done when.**
- `offload.repeat_runs` (default 3) and `offload.repeat_days` (default 14) exist in `ct.toml`, validated (a folder override in `.tenta.toml` is a later slice: that file holds only worker pins today), and the "repeatable" test in `cmdlog` reads them instead of constants.
- One pure function takes a command's facts (shape, median seconds, run count in the window, pinned or class-bound, batchable, split-able) and returns, for every rung, `met | not met | cannot be judged yet` with a reason; the headline is the highest-weighted rung met, the rest listed beneath it. Rung 2 (fewer AI tokens) returns "cannot be judged yet: needs the AI layer", never a guess. The weights are read from one table.
- `ct-run offload log top` and the placement explanation show the headline and the other met rungs; `--all` shows each rung's verdict.
- `ct-run offload measure` reads the command log and prints suggested values with the data behind them (how many shapes would qualify at 2, 3, 4 runs and at 7, 14, 30 days), and changes nothing itself: it prints the one `ct-setup install --set` command that applies a suggestion (the person's run of it is the confirmation); nothing is invented when the log is too short (it says how much more is needed).
- Tests first, failing on the old code; real output of each card at the narrow and wide widths; the refusal catalog and generated reference updated if any refusal is added.
**Not in scope.** The AI layer and token measurement; sharding jobs (rung 3 signal only reads existing `batchable`); any change to where a job is placed; new rungs; the GitHub or transport code.
**Slices.** (1) settings + the pure rung evaluator with tests (`src/ct/client/candidacy.py`; rung 4 counts short runs that repeat only when their total is at least the long-run limit, so a trivial command run often does not qualify: my proposal, not yet confirmed); (2) show it in `log top` (an "offload reason" column, and under `--all` a table of every rung; the placement explanation is a later slice: it needs a worker pick, not a command shape); (3) `offload measure`.

## Workspace jobs and the offload review (design 2026-11-26)

Branch `docs/offload-workspace-design`, slice 0 of `theme/offload-value`. The full design is [WORKSPACE-JOBS.md](WORKSPACE-JOBS.md); the decisions that touch this ladder:

- **Any command on any folder.** A "workspace job" sends a folder (git or not) and runs one command, so rungs 1, 3 and 4 are available to a linter, a build, a scanner or a data job as much as to a test suite.
- **Review after setup.** The tool reads the person's command log, shows the saved hours, recommends commands ranked by the rung each meets, and asks yes or no per command. A yes profiles the command on every worker; each worker where it succeeds is a candidate and the orchestrator picks per job. The yes or no is the per-command approval; there is no blanket yes.
- **A usage-history hint** (rate-limited, default on, with an off switch) points at the review when the log shows a worthwhile command.
- **Environment cleanup** is a setting (`packages.keep_days`, default 30), not a constant: an unused environment is removed after that many days and rebuilt on the next use, and the result says so.
- **Not decided here:** the weight of the "your computer is busy" rung.
