# Testing

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
scripts/e2e-base [image]          # (re)build the cached base machine ct-cache-<release> (done automatically when missing)
scripts/e2e-orbstack              # one cold route-free run on a fresh machine (slow; for checking the base itself)
scripts/e2e-matrix                # the scenario on Ubuntu 26.04 and 24.04 and Debian 12 and 13 at once
scripts/e2e-clean [--list|--all]  # delete every test machine this project made (the caches stay unless --all)
```

The scenario is split into independent **routes** in `tests/e2e/routes/` (`install`, `direct`, `switch`, `move`, `rename`, `uninstall`), sharing `tests/e2e/lib.sh`. Each does its own baseline install, so the routes run side by side and the wall time is the slowest route, not the sum. The slow, identical preparation (system packages, git, a stand-in for GitHub) is done once into a cached base machine that every run clones in a second; the cache is keyed by release. The routes that are about something else (`move`, `rename`, `switch`, `uninstall`) start from a **shared baseline**: `scripts/e2e-routes` builds it once per run (`tests/e2e/baseline.sh`: the checkout installed, a GitHub install that works, a first job) and clones it for each of them, while `install` and `direct`, which need a fresh machine, run alongside. Measured: the install took 54 s alone but 324 s when six routes each did it at once on one host, so six in parallel gave no gain; with the shared baseline the whole run went from 437 s to 172 s. Every step prints its seconds, and each route ends with its slowest steps, so the next saving is measured, not guessed.

GitHub is simulated: `github.com` resolves to the machine itself, where an SSH user called `git` holds the two bare repositories, so every clone, fetch and push uses real keys, including the worker's own deploy keys.
The decoy-credential check needs kernel audit, which container-style machines (OrbStack) lack; there it reports SKIP. The kernel probe (below) and `ct-setup tripwire-test` cover it on a real machine.

### The tripwire on a real machine

The end-to-end test runs in a container, which has no kernel audit, so it cannot test the first link of the tripwire (auditd logging a read of a decoy file). `ct-setup tripwire-test` tests the whole chain on a real worker: run as root on it, it reads one decoy file, waits for the lockdown, checks it, and unlocks again. It asks first, because it really does lock the worker for a minute and send the security alert, and it refuses when auditd is not running, when canary-watch is not monitoring, or when the worker is already locked.

### Continuous integration

`.github/workflows/ci.yml` runs the unit tests on Python 3.11, 3.12 and 3.13, checks that the generated reference pages are current, and runs the privacy check over the whole history. A `release-X.Y.Z` tag must also match the version in `pyproject.toml` and `src/ct/__init__.py`. The end-to-end test needs a full machine and is run by hand with `scripts/e2e-orbstack`.

### Privacy guard

`scripts/privacy-check` runs as a `pre-commit` and `commit-msg` hook and with `--history` before any push. It blocks private IPs, MAC addresses, e-mail addresses, `.local` host names, keys, tokens, home paths, and anything on a personal denylist kept outside the repository. A line may carry `privacy-ok` with a reason.


**Supported releases and how each was checked.** Ubuntu 24.04 and 26.04, Debian 12 and 13 pass the whole GitHub scenario (install, jobs, rename, move to Direct, uninstall, reinstall) in OrbStack; Direct passes on Ubuntu 26.04. OrbStack machines share one kernel, so the tripwire (auditd) and the systemd sandbox check are only exercised on real machines or full VMs (the Mini, a VMware Fusion VM). Found by the matrix: Debian 13's bubblewrap 0.12 cannot open `/usr` under `RestrictSUIDSGID`, so the services no longer set it.


**Test machines are never left behind.** `scripts/e2e-orbstack` deletes its machine when the run ends, whether it passed, failed or was interrupted, after copying the scenario's output files and the services' journal to `/tmp/ct-e2e-logs/<machine>/`; `--keep` is the only way to leave one. It refuses to start when 4 test machines already exist. `scripts/e2e-matrix` cleans up after itself, and `scripts/e2e-clean` removes every machine named `ct-e2e*`, `ct-mx*`, `ct-u*` or `cw-e2e*` (a machine OrbStack lists but can no longer address is deleted by its id). `tests/e2e/smoke.sh` and `smoke-fail.sh` test the harness itself (`CT_E2E_SCENARIO=smoke scripts/e2e-orbstack`). OrbStack machines are on a virtual network inside your computer; they are visible to the add-worker scan as such.


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
| `ct-setup transport direct` | yes (history copy checked with real git) | yes | |
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
| `ct-setup uninstall` preview run as the admin (a normal account) | yes (real `Host` on an unsearchable folder) | yes (`uninstall`, on all four releases) | |
| A worker's address changed (`ct-admin locate`, the monitoring pass) | yes (ladder, matcher, real `ssh-keyscan` and `known_hosts` output on every release, saving the address) | gap: needs two addresses on one machine | by hand: change the VM's address |
| Move ONE registered worker to another connection (GitHub to Direct and back) after several workers exist | gap: the single-install move is covered, not a second worker with its own config, `workers.toml`, the fleet and the scheduler | gap: needs two machines | by hand: the Mac mini and the VM |
| Several workers: a job is placed by the scheduler (`ct-run submit`, `ct-run where`, `--worker`) | yes (`test_sched` rules, `test_place` wiring with real files; the real state builder) | | confirmed by hand on the Mac mini and the VM (found the GitHub/Direct mismatch) |
| A job placed on another worker: result, approval, stop (`placements.json`) | yes | gap | by hand: result read back from the VM |
| Several workers: one unreachable, locked, or lacking the class | yes | gap | VM off: by hand |
| Many `ct-run submit` at once from one computer (a script, two sessions) | yes (`test_concurrent_submit`: real git, 8 threads at once; `ManyPlacementsAtOnce`) | by hand: `scripts/loadtest` on the real workers (found the lost jobs and the herd) | `scripts/loadtest --jobs 8` |
| Arrow keys and Ctrl-C in a real terminal | yes (virtual terminal) | | a pseudo-terminal check, not yet kept in the repo |


**Environment probes.** `scripts/probe [name]` runs `tests/probes/<name>.sh` in a throwaway container (`docker run --rm`, the checkout mounted read-only at `/src`): about 20 seconds, nothing to clean up. A probe proves ONE fact about a real operating system that a unit test cannot, using the project's own code. `ownership` creates the two accounts (the worker and the mailbox account) and shows: git refuses a repository owned by another account (the premise); the mailbox shell serves it anyway, for that one repository and no other; and root copying history between repositories owned by others works with the project's helper and not without it. It was written from two bugs the full end-to-end runs found, and it fails on the old code for both. Needs Docker (OrbStack provides it); `CT_PROBE_IMAGE` picks the release (default Ubuntu 24.04).

**More probes.** `release` runs on all four supported releases at once (`scripts/probe --releases release`) and shows that the packages and tools the installer needs exist there, at the versions the code assumes (git 2.32 or newer, Python 3.11 or newer, bubblewrap, systemd directives). `kernel` is read-only and runs on a real machine over SSH (`scripts/probe --on user@host kernel`): the AppArmor restriction and profile, user namespaces, timer accuracy, the services' sandbox settings. It is how the timer `AccuracySec` that systemd silently ignored (an inline comment) was found on a real VM. `record` is not a test: `scripts/record-fixtures` runs it per release and saves what the real tools printed.

**Recorded real outputs.** `tests/fixtures/real/<release>/` holds the real output of `os-release`, `sudo --version` (sudo-rs on Ubuntu 26.04), git, Python, systemd, bubblewrap, OpenSSH, git's refusal of another account's repository, and the whole candidacy-check script. `tests/test_real_outputs.py` runs the parsers over them, so a parser is tested against what a release really prints, not against its author's idea of it. Re-record with `scripts/record-fixtures` when a release is added. Recording found that git 2.53 (Ubuntu 26.04) does not refuse the foreign repository the way 2.43 does, so the ownership bug cannot show there.

**Change-based planning.** `scripts/test-plan [base]` lists, for the files changed (including uncommitted and new ones), the scoped unit modules, the probes and the routes worth running, from the table in `tests/impact.py`. `tests/test_impact.py` fails when any file of the project has no rule, or a rule names something that does not exist; a file the table does not know plans everything. The full unit suite stays the gate before a push.

**Load test.** `scripts/loadtest [--jobs 8] [--waves 1]` sends a burst of simultaneous `ct-run submit` of the harmless `sys-status` class across every worker you administer, then reads each job back and reports where it went and how long each stage took (pick-up, queue, run). The host cap (`limits.max_in_flight`, default 8) applies. It is how three real faults were found: concurrent sends sharing one temporary git index (jobs reported sent but missing, and lock errors), every simultaneous placement seeing the same idle workers (all sent to one), and a staleness limit shorter than the workers' own publishing interval.

## What the first load tests taught (2026-11-18, the Mac mini on GitHub and the VM on Direct)

- **Load has to come from this computer.** A worker publishes its own running and waiting counts about every half hour, so for scheduling they are almost always stale. The live signal is the jobs this computer sent that have no final result yet (`placements.json`, with the results already downloaded). A worker's own count is believed for two minutes only.
- **Choose and reserve in one step.** Eight simultaneous sends each looked, saw the same idle workers and chose the same one. Looking, choosing and writing a reservation are now one locked step (`placements.decide.lock`); the reservation counts as load at once and is replaced by the real record when the send finishes, or expires in two minutes if it died.
- **Sends must not share state.** Every send used one temporary git index file, so concurrent sends lost each other's work. Each send now has its own, retries with a random pause, and reads back that the job is on the branch afterwards.
- **What the worker does is the truth.** The worker publishes the connection it listens on and its longest wait; this computer's configuration is only a belief and is flagged when it disagrees.
- **Measured, 8 simultaneous light jobs:** all 8 delivered; split 4 and 4 between the VM (Direct) and the Mini (GitHub); a whole burst read back in 37 s. A Mini that has been asleep for a long time notices a job in about 80 s (up to 300 s); once awake, in 4 to 9 s. Direct notices in 3 to 7 s.
- **Not weighed yet:** the cost of a job (light, medium, heavy), memory and heat, a worker's history.

## Which worker a command acts on (2026-11-18)

The rule: **a command that is about one worker is never scheduled, and an administration command that acts on workers acts on every worker unless `--worker NAME` narrows it.** Only your own jobs (`ct-run submit` without `--here`) are placed by the scheduler. Checks that are about one worker (the check after `ct-admin apply`, the wizard's first job) send with `--here`, which sends to the worker the configuration is for and never chooses. Found in the first real `ct-admin apply` on two workers: the check on the Mac mini passed, but its test job had been placed on the VM.

Every `ct-admin` command is classified in `tests/test_admin.py` (`EveryCommandKnowsWhichWorkersItActsOn`), so a new one cannot be added without saying which it is: **fleet** (`apply`, `diff`, `check`, `status`, `quarantine`, `unlock`: every worker in turn, each with its own questions, one failing does not stop the rest, the exit code is the worst), **pick** (`update`, `clean`, `adopt`, `retire`, `action`, `proposals`: the main worker, or the one named), **own flag** (`wake`, `audit`, `tripwire-test`, `locate`), **by nature** (`watch`, `workers`), **by job** (`approve`, `review`, `cancel`, `kill`, `output`: they follow the job to where it was sent) and **local** (`theme`, `action-hash`).

## Cost-aware placement (2026-11-18)

A worker's capacity is in points (a light job 1, a medium 2, a heavy 4, the worker's own registry decides per class). The load this computer has put on a worker is the points of the jobs it sent that have not finished, and a reservation for a job being sent weighs what its class weighs. The choice asks first "does the job fit now?" (free points at least what it needs), then who has the most free points, then the shortest wait, then the lighter load, then the name. A job that fits nowhere queues on the worker with the most room, and the explanation says it will wait for running jobs. So a heavy job goes to the worker that can start it, and a burst of heavy jobs goes one per worker.

## What the later load tests and the first real `ct-admin apply` taught (2026-11-18)

- **Measured with both workers current** (8 simultaneous light jobs, twice): all 16 delivered, 4 and 4 each time. The VM (Direct) noticed a job in 4 to 7 s and finished in 4 to 8 s; the Mac mini (GitHub) noticed in 7 to 11 s and finished in 11 to 24 s.
- **A worker announces the connection it really fetches over** (where its queue copy points), and separately what its own configuration says when they differ. The Mac mini announced `direct` because its configuration said so while it still fetched from GitHub, a half-moved install. The scheduler says so in the explanation, with the command that repairs it (`ct-setup install` again).
- **Where a send's time goes.** Profiled alone, one send took 8.6 s: 5.3 s of it was the host job cap reading every job file on the queue branch with its own git process (it grew with every job sent), and the rest was about eleven fetches and pulls, several repeating one made a second earlier. Now: one git process for all the files, a look made moments ago is reused by the parts that only need a recent picture (never by the lock check before sending), and pruning runs at most hourly. One send is now 4 to 7 s. A burst is still bounded by pushes to the same queue branch taking turns: 8 at once take about 17 s to send.
- **The update banner** said "a newer release is available" to a worker that was AHEAD of the newest tag. It is now shown only when git proves the worker is behind, never when unsure, and once per run.

## Folder settings and dedicated commands (design, 2026-11-19; replaces the first proposal)

**The need.** A developer works on project A today and offloads to a worker; tomorrow project B needs a different test environment, so a separate virtual machine is built for it. A penetration tester keeps one machine for attack-style commands and wants everything else to go to other workers. The scheduler chooses the freest worker for anything, so the wrong job can land on the wrong machine.

**The design (the person's, replacing a project-name scheme).** A folder remembers its own setting, the way a CLAUDE.md does: a plain, editable **`.tenta.toml`** in the working folder (the nearest one at or above where you run `ct-run`, as `.git` is found). One command writes it, so a person, a script or Claude (the planned skill) can set or change it in one line instead of walking through setup again:

- `ct-run use WORKER`: every job from this folder goes to that worker. `ct-run use` alone shows what is set; `ct-run use --clear` removes it.
- `ct-run use WORKER --command CLASS`: only that job class goes to that worker (the penetration tester's `nmap`); the rest of the folder stays with the scheduler or the folder worker. `--clear --command CLASS` removes one.
- An explicit `--worker` on `ct-run submit` always wins; `--here` is untouched. A worker the file names that is no longer yours is an error naming the file and the fix, never a silent fall back. `ct-run where CLASS` says which file and line of reasoning sent it. Sending says `PINNED` with the file. The file is a placement preference of the sender's own folder, not a security boundary.

**Slices.** (1, built) the file, `ct-run use`, routing and `ct-run where`. (2) Dedication from the worker's side, for a command that must go to one worker from every folder, and a worker that takes only certain commands: `ct-admin workers dedicate WORKER --command CLASS` and `release`, stored with the administrator's configuration (what `sched.Prefs.dedicated` already models), shown in `ct-admin workers` and in `ct-run classes` as a "Dedicated to" column so it is visible where the command is listed. (3) `ct-setup install` for a new computer asks "dedicate it to a purpose?" after the profile question (default no) and writes the same setting. (4) End-to-end route with two folders on two workers and the three-worker load test with a dedicated worker.
