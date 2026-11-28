# Several workers: placement, testing and folder settings

## In plain words

Two kinds of tests keep the project honest. **Unit tests** run in seconds on any computer and check each part against pretend machines. The **end-to-end test** builds a real, throwaway Ubuntu machine, runs the setup wizard on it for real, sends a job through the
whole loop and checks the safety features, so we find out what the pretend machines cannot tell us.

## Technical specification

### Unit tests

```
python3 -m unittest discover -s tests
```

Standard library only, plus PyYAML for the registry tests (the tests skip cleanly without it). Principles:

- **Real git, fake machine.** Tests use real temporary git repositories for the queue and results, with fakes only for what touches the machine: the command runner, the temperature sensors, the process spawner, SSH, `systemctl`, `visudo`.
  The whole job lifecycle runs without root, a network or special hardware.
- **No test touches the real home folder.** Importing `tests/support.py` points `HOME` at a throwaway folder.
- **Documentation and tests use reserved example values only** (`example.com`, `192.0.2.0/24`); the privacy guard enforces it.
- `scripts/gen-docs --check` runs as a test, so the generated reference pages cannot go out of date.

### End-to-end routes

```
scripts/e2e-routes                # every route in parallel, each in its own machine cloned from a cached base; one line per route; machines deleted as each ends
scripts/e2e-routes direct move    # only these
scripts/e2e-base [image]          # (re)build the cached base machine arm-cache-<release> (done automatically when missing)
scripts/e2e-orbstack              # one cold route-free run on a fresh machine (slow; for checking the base itself)
scripts/e2e-matrix                # the scenario on Ubuntu 26.04 and 24.04 and Debian 12 and 13 at once
scripts/e2e-clean [--list|--all|--all-runs]  # delete THIS run id's machines (--list: everyone's, with owners; --all: + the shared caches; --all-runs: everyone's)
```

The scenario is split into independent **routes** in `tests/e2e/routes/` (`install`, `direct`, `switch`, `move`, `rename`, `uninstall`), sharing `tests/e2e/lib.sh`. Each does its own baseline install, so the routes run side by side and the wall time is the slowest route, not the sum. The slow, identical preparation (system packages, git, a stand-in for GitHub) is done once into a cached base machine that every run clones in a second; the cache is keyed by release. The routes that are about something else (`move`, `rename`, `switch`, `uninstall`) start from a **shared baseline**: `scripts/e2e-routes` builds it once per run (`tests/e2e/baseline.sh`: the checkout installed, a GitHub install that works, a first job) and clones it for each of them, while `install` and `direct`, which need a fresh machine, run alongside. Measured: the install took 54 s alone but 324 s when six routes each did it at once on one host, so six in parallel gave no gain; with the shared baseline the whole run went from 437 s to 172 s. Every step prints its seconds, and each route ends with its slowest steps, so the next saving is measured, not guessed.

GitHub is simulated: `github.com` resolves to the machine itself, where an SSH user called `git` holds the two bare repositories, so every clone, fetch and push uses real keys, including the worker's own deploy keys.
The decoy-credential check needs kernel audit, which container-style machines (OrbStack) lack; there it reports SKIP. The kernel probe (below) and `arm-setup tripwire-test` cover it on a real machine.

### The tripwire on a real machine

The end-to-end test runs in a container, which has no kernel audit, so it cannot test the first link of the tripwire (auditd logging a read of a decoy file). `arm-setup tripwire-test` tests the whole chain on a real worker: run as root on it, it reads one decoy file, waits for the lockdown, checks it, and unlocks again. It asks first, because it really does lock the worker for a minute and send the security alert, and it refuses when auditd is not running, when canary-watch is not monitoring, or when the worker is already locked.

### Continuous integration

`.github/workflows/ci.yml` runs the unit tests on Python 3.11, 3.12 and 3.13, checks that the generated reference pages are current, and runs the privacy check over the whole history. A `release-X.Y.Z` tag must also match the version in `pyproject.toml` and `src/armada/__init__.py`. The end-to-end test needs a full machine and is run by hand with `scripts/e2e-orbstack`.

### Privacy guard

`scripts/privacy-check` runs as a `pre-commit` and `commit-msg` hook and with `--history` before any push. It blocks private IPs, MAC addresses, e-mail addresses, `.local` host names, keys, tokens, home paths, and anything on a personal denylist kept outside the repository. A line may carry `privacy-ok` with a reason.


**Supported releases and how each was checked.** Ubuntu 24.04 and 26.04, Debian 12 and 13 pass the whole GitHub scenario (install, jobs, rename, move to Direct, uninstall, reinstall) in OrbStack; Direct passes on Ubuntu 26.04. OrbStack machines share one kernel, so the tripwire (auditd) and the systemd sandbox check are only exercised on real machines or full VMs (the Mini, a VMware Fusion VM). Found by the matrix: Debian 13's bubblewrap 0.12 cannot open `/usr` under `RestrictSUIDSGID`, so the services no longer set it.


**Test machines are never left behind.** `scripts/e2e-orbstack` deletes its machine when the run ends, whether it passed, failed or was interrupted, after copying the scenario's output files and the services' journal to `/tmp/arm-e2e-logs/<run id>/<machine>/`; `--keep` is the only way to leave one. `scripts/e2e-matrix` cleans up after itself, and `scripts/e2e-clean` removes only this run id's machines (see docs/components/testing.md, "Two runs at once") (a machine OrbStack lists but can no longer address is deleted by its id). `tests/e2e/smoke.sh` and `smoke-fail.sh` test the harness itself (`ARM_E2E_SCENARIO=smoke scripts/e2e-orbstack`). OrbStack machines are on a virtual network inside your computer; they are visible to the add-worker scan as such.


**Snapshot runs.** `scripts/t --snapshot [modules]` runs against a copy of the working tree made when the run starts, so edits made while a long run is going cannot change what it tests, and it never has to be stopped to "start again with the newest code": start the next run alongside or after it.


## Test tiers, and the routes each covers

**The order of proof.** Logic and every route a user takes are proven by unit tests first: through the real command line (`tests/test_addworker.py` `TheRealCommand` is the pattern: the real command and the real installer, with only the machine and the network faked). An end-to-end run (`scripts/e2e-orbstack`, the release matrix) then confirms what only a real machine can show: another account, a kernel, a firewall, the network. It is not used to find logic bugs: a bug it finds first gets the unit test that reproduces it, seen failing, and only then the fix. A fake that stands in for real code is trusted only while one test runs the real thing.

**Route by tier** (unit = real command, fake machine; e2e = OrbStack scenarios; by hand = a real machine). `gap` is the work list.

| Route a user takes | Unit | End to end | By hand |
| --- | --- | --- | --- |
| Fresh install, GitHub | yes | yes | the first worker |
| Fresh install, Direct | yes (installer and server) | yes (`direct.sh`) | a VM |
| Install again, same answers | yes | yes | |
| Install again, other connection (wizard) | yes (clones repointed, deploy-key loop skipped) | yes | |
| `arm-setup transport direct` | yes (history copy checked with real git) | yes | |
| Cross-account git ownership (mailbox account, worker's repository) | env passing only | yes (real accounts found it) | probe: `scripts/probe ownership` (real accounts, in seconds) |
| What each supported release prints and provides (sudo-rs, git, Python, systemd) | yes (recorded real outputs, `test_real_outputs.py`) | yes (matrix) | probe: `scripts/probe --releases release` |
| Timer accuracy, AppArmor and sandbox settings on a real kernel | plan and template guard | gap: OrbStack has none | probe: `scripts/probe --on user@host kernel` |
| Rename, uninstall and reinstall | yes | yes | |
| `add-worker`, new machine | yes (real command) | gap: needs two machines | a VM |
| `add-worker` again for a registered worker | yes (real command) | gap | pending |
| `add-worker` with an unreadable worker file | yes | | |
| A failed protection or an unreadable mailbox stops the job loop | yes | gap | a VM |
| A block taller than the terminal, an error that wraps | yes (scrolling virtual terminal) | | a real window |
| Worker with IPv6 off and a name that still resolves to it | yes (settings, installer) | gap | timing only on a real network |
| Real kernel features (AppArmor profile, tripwire, sandbox check) | plan only | gap: OrbStack has none | a VM |
| `add-worker` when every machine found is already a worker | yes (`TheScreens`: listed to set up again, not the default) | | the real network (confirmed by hand) |
| A command offered to copy (chip, copy note, real clipboard) | yes (chip on every path; clipboard program faked) | | confirmed by hand on a Mac; Linux desktop and over SSH not tried |
| The machine-kind question (`hardware.profile`) wording and choices | yes (no jargon, era, processor, memory) | | read in the wizard: not yet |
| `arm-setup uninstall` preview run as the admin (a normal account) | yes (real `Host` on an unsearchable folder) | yes (`uninstall`, on all four releases) | |
| A worker's address changed (`arm-admin locate`, the monitoring pass) | yes (ladder, matcher, real `ssh-keyscan` and `known_hosts` output on every release, saving the address) | yes (route `locate`: a second address on a dummy card, the real sshd, a hashed known_hosts entry, saving the address, a second run, another machine refused) | |
| Move ONE registered worker to another connection (GitHub to Direct and back) after several workers exist | gap: the single-install move is covered, not a second worker with its own config, `workers.toml`, the fleet and the orchestrator | gap: needs two machines | by hand: the Mac mini and the VM |
| Several workers: a job is placed by the orchestrator (`arm-run submit`, `arm-run where`, `--worker`) | yes (`test_sched` rules, `test_place` wiring with real files; the real state builder) | | confirmed by hand on the Mac mini and the VM (found the GitHub/Direct mismatch) |
| A job placed on another worker: result, approval, stop (`placements.json`) | yes | gap | by hand: result read back from the VM |
| Several workers: one unreachable, locked, or lacking the class | yes | gap | VM off: by hand |
| Several workers: one silent for a day, a job lost on it, the worker coming back | yes (`test_health`: the placement reasons, the real wiring, a repeatable class queued again once, others only marked lost, one look an hour, the copy kept at a send from the waiting list) | gap: a real worker left silent past the limit; the copy at a real Direct and GitHub send | stop a test worker's timers for the limit, by hand |
| Many `arm-run submit` at once from one computer (a script, two sessions) | yes (`test_concurrent_submit`: real git, 8 threads at once; `ManyPlacementsAtOnce`) | by hand: `scripts/loadtest` on the real workers (found the lost jobs and the herd) | `scripts/loadtest --jobs 8` |
| Arrow keys and Ctrl-C in a real terminal | yes (virtual terminal) | | a pseudo-terminal check, not yet kept in the repo |


**Environment probes.** `scripts/probe [name]` runs `tests/probes/<name>.sh` in a throwaway container (`docker run --rm`, the checkout mounted read-only at `/src`): about 20 seconds, nothing to clean up. A probe proves ONE fact about a real operating system that a unit test cannot, using the project's own code. `ownership` creates the two accounts (the worker and the mailbox account) and shows: git refuses a repository owned by another account (the premise); the mailbox shell serves it anyway, for that one repository and no other; and root copying history between repositories owned by others works with the project's helper and not without it. It was written from two bugs the full end-to-end runs found, and it fails on the old code for both. Needs Docker (OrbStack provides it); `ARM_PROBE_IMAGE` picks the release (default Ubuntu 24.04).

**More probes.** `release` runs on all four supported releases at once (`scripts/probe --releases release`) and shows that the packages and tools the installer needs exist there, at the versions the code assumes (git 2.32 or newer, Python 3.11 or newer, bubblewrap, systemd directives). `kernel` is read-only and runs on a real machine over SSH (`scripts/probe --on user@host kernel`): the AppArmor restriction and profile, user namespaces, timer accuracy, the services' sandbox settings. It is how the timer `AccuracySec` that systemd silently ignored (an inline comment) was found on a real VM. `record` is not a test: `scripts/record-fixtures` runs it per release and saves what the real tools printed.

**Recorded real outputs.** `tests/fixtures/real/<release>/` holds the real output of `os-release`, `sudo --version` (sudo-rs on Ubuntu 26.04), git, Python, systemd, bubblewrap, OpenSSH, git's refusal of another account's repository, and the whole candidacy-check script. `tests/test_real_outputs.py` runs the parsers over them, so a parser is tested against what a release really prints, not against its author's idea of it. Re-record with `scripts/record-fixtures` when a release is added. Recording found that git 2.53 (Ubuntu 26.04) does not refuse the foreign repository the way 2.43 does, so the ownership bug cannot show there.

**Change-based planning.** `scripts/test-plan [base]` lists, for the files changed (including uncommitted and new ones), the scoped unit modules, the probes and the routes worth running, from the table in `tests/impact.py`. `tests/test_impact.py` fails when any file of the project has no rule, or a rule names something that does not exist; a file the table does not know plans everything. The full unit suite stays the gate before a push.

**The waiting list (R1).** With several workers, a job the orchestrator places itself that fits no worker right now (the best one has no free capacity, or none can take it) is not pushed onto the worker with the most room: it waits on this computer in `queue.json` in the client home (mode 0600, changed under its own lock, so it survives a restart or a kill). `arm-run submit --priority low|normal|high` (default normal) sets its place in line; the job id is printed at once and `arm-run jobs` / `arm-run result` say it is waiting, since when, and why. Nothing runs in the background: every `arm-run` command first sends the waiting jobs that now fit, highest priority then oldest first, through the same look-choose-reserve step as a live placement. Attached files live in their own 0600 file per job (`queue/<id>.json`), the index stays small. One drain runs at a time (a local non-blocking lock; a second skips), and a job is marked `sending` with its worker before the send, so no job is sent twice and the normal path costs no extra network call; only a mark older than two minutes (a send that died) is checked, against that one worker. A send that fails (cap, locked or unreachable worker) leaves the job waiting with the real error; one that has waited over an hour is said so on every command, and one that has waited 7 days, or whose command no reachable worker offers any more, ends with the reason. `arm-run queue` lists the waiting jobs, `arm-run queue cancel <id>` and `arm-run queue priority <id> <level>` change them (a job already being sent cannot be cancelled). A job sent with `--worker`, `--here` or a folder's pin is never queued. With one worker nothing changes. Code: `armada.client.queue`; tests: `test_queue`.

**Worker health and lost jobs (R2, design 2026-11-26).** A worker whose latest reading is older than `health.silent_hours` (setting, default 24) is `silent`: the orchestrator gives it no new job (a catalog reason, shown by `arm-run where` like every other), and `arm-admin workers` shows `silent` with the age. A worker that has never published is not silent, it has no reading yet (unchanged). The older, softer `STALE_S` (2 h, ranked after fresh ones) stays. A job is `lost` when it was sent to a worker, has no final result, is older than the same limit, and that worker is silent or unreachable now. Lost is worked out from the placement record, the result and the worker's reading each time; it is not a stored state, so a worker that comes back and publishes the result simply makes the job final again. What IS stored is the one action taken: the placement gets `lost` (reason, time) and, when it was re-queued, `again` (the new job id), so a job is re-queued once. A class says it is safe to repeat with `repeatable: true` in its registry entry (administrator-signed like every class field, default false, shown by `arm-admin class show`). Only for those classes does this computer keep the job body: when the job is sent over ANY connection (Direct or GitHub, the reason the copy is local and not read back from a worker's jobs branch), `sent/<id>.json` in the client home (mode 0600, same as the waiting list's files) holds it until the result is final, the job is lost and re-queued, or its placement is pruned. A lost job of a repeatable class is re-queued through R1 as `<id>-again` (priority normal, reason "the worker went silent"); one that is not repeatable is only marked lost, with the command to send it again by hand. Nothing runs in the background and the normal path costs no network call: each `arm-run` command checks the placement file locally, and only when a job is old enough to be a candidate (and at most once an hour) looks at the workers. Not here: restarting or repairing a worker, heartbeats beyond what workers publish. Code: `armada.client.health`; tests: `test_health`.

**Retries and timeouts (R3, design 2026-11-26).** Two class settings in the registry, administrator-signed like the rest: `retries` (0 to 5, default 0) and `retry_delay_s` (1 to 3600, default 60); `retries` is refused unless the class is `repeatable: true`, since a retry runs the job again. What is retried: ONLY a job whose result is `killed` (stopped from outside: a restart or a stop). An `error` ran to its end and the person decides; a `timeout` ran too long and would only do so again; a `limit` (memory, heat, disk) is a stop for a reason that stays. The stop rule is the existing `timeout_s` (default 60 s) and, for a profile class, the ceiling `max_timeout_s` (default 1800 s); a job over it is stopped and reported `timeout`; both are shown by `arm-admin class show`, with the retries. Where it runs: the plan of each job (attempt, attempts allowed, delay, and the id of the try that followed) is kept in `retries.json` in the client home (mode 0600) when the job is sent through the orchestrated path (the same place the repeat copy of R2 is made, so it works for Direct and GitHub); a retry is queued through R1 as `<id>-try2` (then `-try3`), with the reason "attempt 2 of 3: <id> ended killed", and carries its attempt so the result card says "attempt 2 of 3" (the last one adds that it is not tried again). Nothing runs in the background: the check is made at the start of every client command, after the lost-job check and before the waiting list is sent, so the delay is "at least this long"; the copy of a job stays until its retry is decided. A job whose copy is missing, or whose attempts are used, is not retried and the client says why. Not here: retrying a clean error, changing approvals (a retry of a class that needs approval waits for it like any job). Code: `armada.client.retry`; tests: `test_retry`.

**The audit trail (R5, design 2026-11-26).** `audit.jsonl` in the client home (mode 0600), one JSON line per event, only ever appended to; the setting `audit.keep_days` (1 to 3650, default 90, longer than the 14 days `placements.json` remembers) drops older lines in a trim that runs at most once an hour, so nothing else changes or removes a line. Events: `placed` (worker, priority when it waited, the explanation of every worker with every reason it was left out, or "the worker was named, not chosen" for `--worker`), `lost` (R2: the worker, the reason, the id it was sent again as), `retry` (R3: the attempt and the new id) and `outcome` (the result's status, written once, and only for a job that has a `placed` line). A job sent again has a new id (`-again`, `-try2`); `arm-run audit <job>` shows the whole chain from any of its ids, `arm-run audit` the latest jobs, `--json` the raw lines. Written from the orchestrated send path, the waiting-list drain and the places that see results (`arm-run result` and `jobs`); a plain single-worker send is not placed by the orchestrator and is not in the trail (same limit as R2 and R3). A trail that cannot be written never stops the job. Not here: sending the log anywhere, signing it. Code: `armada.client.audit`; tests: `test_audit`.

**Load test.** `scripts/loadtest [--jobs 8] [--waves 1]` sends a burst of simultaneous `arm-run submit` of the harmless `sys-status` class across every worker you administer, then reads each job back and reports where it went and how long each stage took (pick-up, queue, run). The host cap (`limits.max_in_flight`, default 8) applies. It is how three real faults were found: concurrent sends sharing one temporary git index (jobs reported sent but missing, and lock errors), every simultaneous placement seeing the same idle workers (all sent to one), and a staleness limit shorter than the workers' own publishing interval.

## What the first load tests taught (2026-11-18, the Mac mini on GitHub and the VM on Direct)

- **Load has to come from this computer.** A worker publishes its own running and waiting counts about every half hour, so for scheduling they are almost always stale. The live signal is the jobs this computer sent that have no final result yet (`placements.json`, with the results already downloaded). A worker's own count is believed for two minutes only.
- **Choose and reserve in one step.** Eight simultaneous sends each looked, saw the same idle workers and chose the same one. Looking, choosing and writing a reservation are now one locked step (`placements.decide.lock`); the reservation counts as load at once and is replaced by the real record when the send finishes, or expires in two minutes if it died.
- **Sends must not share state.** Every send used one temporary git index file, so concurrent sends lost each other's work. Each send now has its own, retries with a random pause, and reads back that the job is on the branch afterwards.
- **What the worker does is the truth.** The worker publishes the connection it listens on and its longest wait; this computer's configuration is only a belief and is flagged when it disagrees.
- **Measured, 8 simultaneous light jobs:** all 8 delivered; split 4 and 4 between the VM (Direct) and the Mini (GitHub); a whole burst read back in 37 s. A Mini that has been asleep for a long time notices a job in about 80 s (up to 300 s); once awake, in 4 to 9 s. Direct notices in 3 to 7 s.
- **Not weighed yet:** the cost of a job (light, medium, heavy), memory and heat, a worker's history.

## Which worker a command acts on (2026-11-18)

The rule: **a command that is about one worker is never scheduled, and an administration command that acts on workers acts on every worker unless `--worker NAME` narrows it.** Only your own jobs (`arm-run submit` without `--here`) are placed by the orchestrator. Checks that are about one worker (the check after `arm-admin apply`, the wizard's first job) send with `--here`, which sends to the worker the configuration is for and never chooses. Found in the first real `arm-admin apply` on two workers: the check on the Mac mini passed, but its test job had been placed on the VM.

Every `arm-admin` command is classified in `tests/test_admin_workers.py` (`EveryCommandKnowsWhichWorkersItActsOn`), so a new one cannot be added without saying which it is: **fleet** (`apply`, `diff`, `check`, `status`, `quarantine`, `unlock`: every worker in turn, each with its own questions, one failing does not stop the rest, the exit code is the worst), **pick** (`update`, `clean`, `adopt`, `retire`, `action`, `proposals`: the main worker, or the one named), **own flag** (`wake`, `audit`, `tripwire-test`, `locate`), **by nature** (`watch`, `workers`), **by job** (`approve`, `review`, `cancel`, `kill`, `output`: they follow the job to where it was sent) and **local** (`theme`, `action-hash`).

## Cost-aware placement (2026-11-18)

A worker's capacity is in points (a light job 1, a medium 2, a heavy 4, the worker's own registry decides per class). The load this computer has put on a worker is the points of the jobs it sent that have not finished, and a reservation for a job being sent weighs what its class weighs. The choice asks first "does the job fit now?" (free points at least what it needs), then who has the most free points, then the shortest wait, then the lighter load, then the name. A job that fits nowhere queues on the worker with the most room, and the explanation says it will wait for running jobs. So a heavy job goes to the worker that can start it, and a burst of heavy jobs goes one per worker.

## What the later load tests and the first real `arm-admin apply` taught (2026-11-18)

- **Measured with both workers current** (8 simultaneous light jobs, twice): all 16 delivered, 4 and 4 each time. The VM (Direct) noticed a job in 4 to 7 s and finished in 4 to 8 s; the Mac mini (GitHub) noticed in 7 to 11 s and finished in 11 to 24 s.
- **A worker announces the connection it really fetches over** (where its queue copy points), and separately what its own configuration says when they differ. The Mac mini announced `direct` because its configuration said so while it still fetched from GitHub, a half-moved install. The orchestrator says so in the explanation, with the command that repairs it (`arm-setup install` again).
- **Where a send's time goes.** Profiled alone, one send took 8.6 s: 5.3 s of it was the host job cap reading every job file on the queue branch with its own git process (it grew with every job sent), and the rest was about eleven fetches and pulls, several repeating one made a second earlier. Now: one git process for all the files, a look made moments ago is reused by the parts that only need a recent picture (never by the lock check before sending), and pruning runs at most hourly. One send is now 4 to 7 s. A burst is still bounded by pushes to the same queue branch taking turns: 8 at once take about 17 s to send.
- **The update banner** said "a newer release is available" to a worker that was AHEAD of the newest tag. It is now shown only when git proves the worker is behind, never when unsure, and once per run.

## Folder settings and dedicated commands (design, 2026-11-19; replaces the first proposal)

**The need.** A developer works on project A today and offloads to a worker; tomorrow project B needs a different test environment, so a separate virtual machine is built for it. A penetration tester keeps one machine for attack-style commands and wants everything else to go to other workers. The orchestrator chooses the freest worker for anything, so the wrong job can land on the wrong machine.

**The design (the person's, replacing a project-name scheme).** A folder remembers its own setting, the way an assistant's instructions file does: a plain, editable **`.armada.toml`** in the working folder (the nearest one at or above where you run `arm-run`, as `.git` is found). One command writes it, so a person, a script or an assistant (the skill) can set or change it in one line instead of walking through setup again:

- `arm-run use WORKER`: every job from this folder goes to that worker. `arm-run use` alone shows what is set; `arm-run use --clear` removes it.
- `arm-run use WORKER --command CLASS`: only that job class goes to that worker (the penetration tester's `nmap`); the rest of the folder stays with the orchestrator or the folder worker. `--clear --command CLASS` removes one.
- An explicit `--worker` on `arm-run submit` always wins; `--here` is untouched. A worker the file names that is no longer yours is an error naming the file and the fix, never a silent fall back. `arm-run where CLASS` says which file and line of reasoning sent it. Sending says `PINNED` with the file. The file is a placement preference of the sender's own folder, not a security boundary.

**The computer's file, and what wins.** `~/.armada.toml` is the same file for the whole computer, as a global instructions file is for folders; `arm-admin workers use WORKER --command CLASS` writes it (`--clear`, with a worker, releases every command dedicated to it). `arm-run use --global` writes the same file. Most specific wins: the folder's command, then the computer's command (a command dedicated to one worker everywhere), then the folder's worker, then the computer's. `arm-admin workers` shows a `Dedicated` row on each worker; `arm-run classes` gets a `goes to` column when anything is set.

**Overlaps, decided with the person.** `arm-run use W --command C` when the folder's worker is already W warns that it changes nothing and does not write (`--force` adds it anyway). `arm-run use W` when commands in the file go to other workers lists them with their workers and asks: override (they follow W) or retain (default; a command's own worker beats the folder's). With nobody to answer they are retained; `--override` and `--retain` decide without asking.

**Built.** The file and `arm-run use`; the overlap rules; `~/.armada.toml` and `arm-admin workers use`; the `Dedicated` row and `goes to` column; the question after a new worker is added ("Should some commands always go to it?", default no); unit tests for each; the orchestrator test with a reserved worker. **The direction (corrected 2026-11-19):** the *command* is dedicated, never the machine: "this command can only run on this machine", so the machine stays free for every other command. The first build also had a reverse form ("this machine takes only these commands", `--only`, `reserved`); it was removed. `sched.Prefs.dedicated` still exists in the orchestrator but nothing sets it. Not signed and not enforced by the worker: a placement preference of the sender.


## Worker aliases (design, 2026-11-19)

A name you give a worker for the `arm-` commands, kept only on your computer in `~/.armada/aliases.toml` (`arm3 = "lab-vm"`); the worker, its hostname and its install are not touched. `arm-admin workers rename WORKER NEW` names one; `arm-admin workers rename` with nothing else is the fleet wizard (each worker in turn: Enter keeps, `-` clears); `--clear` removes one; `--mask` / `--show-real` choose whether `lab-vm  (arm3)` or only `lab-vm` is shown. Typing: the alias or the real name works wherever a worker is named (`--worker`, `arm-run use`, `arm-admin workers use`, `arm-run classes --worker`, `arm-setup --worker`). Showing: every card shows a worker through `alias.show`. Files such as `.armada.toml` keep the real name, so renaming an alias breaks nothing. A name cannot be the real name or the alias of another worker. `arm-setup rename` is a different, heavy command: it renames the install itself (server folders, services, GitHub repositories). Limit: a worker draws its own `status`, so its first lines still show its real name.

## The status panel (R6)

`arm-admin status` begins with one card, "Your workers and jobs", drawn once for the whole fleet (the per-worker reports follow it as before). It reads only this computer's own files, so it asks no worker and needs no login:
the published worker states (state ok / SILENT / STALE / PAUSED / LOCKED by the same rules as `arm-admin workers`, with a load bar against the cores), the waiting list by priority (R1, bars scaled to the fullest priority, the oldest wait),
the jobs placed in the last 24 hours with no outcome yet ("running", R5), and the latest finished results (R5). `arm-admin status --every SECONDS` draws that card alone again and again until Ctrl-C; nothing else is live.
Bars and lines are cut to the width of the terminal (the narrow-width guard draws the card at eight widths). Code: `armada.client.overview` (`collect` is plain data, `lines` draws it). Limit: a plain single-worker send is not in the trail (as for R2, R3, R5), so it is not shown as running.

