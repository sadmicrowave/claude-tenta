# Devices are single-job off-loaders, not parallel agents

Status: **decided and built.** A device that runs commands (a worker) does one thing per order: it runs ONE job and reports ONE result back. It does not decide to do more work, start helpers, loop, or make a working branch of its own. All sequencing, retries and follow-ups belong to the host's engine, where a person can see them.

## What holds

- One job is one command from its class with a time limit, run in the job jail (no secrets, no credentials for GitHub or anything else, so it cannot push anywhere).
- The only way out is the result, published by the worker itself through its own results repository after the output guard. The device makes no branch and no pull request.
- A class may hand work back as data: `returns: patch` (`src/ct/patch.py`, `ct-run patch`); the comparison is plain Python so nothing runs git inside a folder a job could write to; binary files, links and oversize patches are left out with a note; the patch passes the output guard; the host re-checks it and applies it only on request after showing it.
- The host has a cap on jobs in flight across workers: `limits.max_in_flight`, default 8, 0 = no cap, checked by `ct-run submit` and `ct-run offload profile` across every worker in workers.toml; jobs waiting for your approval are not counted (`src/ct/client/cap.py`). A signed "approve this many jobs for this long" convenience is not built.
