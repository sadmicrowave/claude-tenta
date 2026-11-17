# Devices are single-job off-loaders, not parallel agents

Status: **decided; items 1, 2 and 3 below are built (0.2 hardening slice); 4 to 6 are not.** A device that runs Claude commands (a worker) does one thing per order: it runs ONE job and reports ONE result back. It does not decide to do more work, start helpers, loop, or make a working branch of its own. All sequencing, retries and follow-ups belong to the host's engine, where a person can see them and where the usage they cost is counted.

Why: a Claude job spends the owner's usage (an account token or an API key). A device that can fan out or loop spends it without oversight, and several devices multiply that.

## What already holds

- One job is one `claude -p` call, with `--max-turns` and a time limit from its class, run in the job jail (no network except what Claude needs, no secrets, no credentials for GitHub or anything else, so it cannot push anywhere).
- The only way out is the result, published by the worker itself through its own results repository after the output guard. The device makes no branch and no pull request.
- Claude classes are `tier: approve`: each job waits for the owner's approval, pinned to the exact job file. A loop on the host therefore needs a person in it.
- The shipped class lists only read tools (Read, Grep, Glob).

## What did not hold, the plan, and what is built

1. **Helpers (sub-agents) are not switched off.** *(built)* The worker names the tools that need no permission (`--allowedTools`), but does not forbid the tool that starts helper agents. A helper has its own turns and runs alongside the main one, so `--max-turns` does not bound the whole. Plan: also pass `--disallowedTools` for the helper-starting tools on every Claude job, and refuse to register a class that lists them. The installed Claude is a pinned older release on some machines, so the worker checks the flag exists first and refuses the class, with a plain message, when it does not.
2. **The tool list is free text.** *(built)* An administrator could register a class with write or shell tools. Plan: registry validation rejects the dangerous ones (helpers, shell, edit, web fetch) unless the class says `reviewed: true`, and the approval screen shows the tool list in its own colour.
3. **Several Claude jobs may run at once** *(built)* if capacity allows (two medium jobs fit in four points). Plan: a class setting `max_running` (1 by default for Claude classes) that the step pass enforces.
4. **No spend ledger.** `claude -p --output-format json` reports its turns and cost. Plan: record them in the result's measurements, show them in `ct-admin status`, and add a daily limit per worker (jobs and turns; dollars are only notional with an account token) that pauses new Claude jobs and alerts, like a lockdown without the alarm.
5. **A write-capable class has to hand its work back as data.** Not a branch: a patch or a file set inside the result, size-limited and scanned by the output guard, applied by the host after review.
6. **The host's engine** (the Claude session that sends jobs) needs its own budget: a cap on how many jobs one session may send without a person, because a session that reads a result and sends the next job is the loop this is meant to prevent. Approval per job is the first brake; a signed "approve this many jobs for this long" would be the convenience, never an unbounded one.

Order: 1, 3 and 2 first (small, close the real gaps), then 4, then 5 and 6 with the several-workers scheduler.
