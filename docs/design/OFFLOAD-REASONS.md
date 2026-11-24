# Why offload: a ranked ladder of reasons

Status: agreed 2026-11-23 (list item 2): the weights and the definition of "repeatable" below were accepted by the owner of the project, including rung 1 ranking above rung 2 when a job meets both.
A living document: new rungs are added as they are found; it is written so its plain-language parts can be reused for a public website.

## Why a ladder

"Slow" and "repeated" are not enough to show that offloading is worth the trouble: they give few examples from real use. Offloading pays for itself in several different ways, and
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
| 5 | **More to find** | | | |

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
what happens when one fails, how far more workers keep paying off (the measurement utility, list item 8, answers this per person).

### 4. Free your own computer

Long runs, and short runs that repeat, take your computer's CPU. **"Repeatable"** needs a definition; Agreed: a command shape the person has run **at least 3 times in 14 days**.
The two numbers are settings (`offload.repeat_runs`, `offload.repeat_days` in `ct.toml`, overridable in a folder's `.tenta.toml`), not constants in code. The measurement utility
later suggests values from the person's own log instead of a guess.

### 5. More to find

Keep a list here as ideas arrive; none is promised. Examples to test: privacy (work that should not leave a controlled device), cost (a worker that is cheaper to run than a
cloud service), energy or heat (work that should not warm your laptop), network position (work that is faster close to the data), time zone or schedule (work that must run overnight).

## How a job is judged

1. Describe the job: its class or command shape, how long it takes, how often it runs, whether it is pinned, whether it can be split.
2. Check every rung. Record, for each, met or not met and why (never stop at the first miss).
3. The headline reason is the highest-weighted rung met; the other met rungs are listed beneath it.
4. Show it where a person decides: `ct-run offload log top`, a profile result, and the placement explanation.

Candidacy today looks at rung 4 only (seconds and count). Evaluating all rungs is list item 8.

## Decisions

- 2026-11-23: the weights above stand. The definition of "repeatable" (3 runs in 14 days, as two settings) stands. Rung 1 ranks above rung 2.
