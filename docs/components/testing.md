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

The scenario is split into independent **routes** in `tests/e2e/routes/` (`install`, `direct`, `switch`, `move`, `rename`, `uninstall`, `shared`, `special`, `upgrade`), sharing `tests/e2e/lib.sh`. Each does its own baseline install, so the routes run side by side and the wall time is the slowest route, not the sum. The slow, identical preparation (system packages, git, a stand-in for GitHub) is done once into a cached base machine that every run clones in a second; the cache is keyed by release. The routes that are about something else (`move`, `rename`, `switch`, `uninstall`) start from a **shared baseline**: `scripts/e2e-routes` builds it once per run (`tests/e2e/baseline.sh`: the checkout installed, a GitHub install that works, a first job) and clones it for each of them, while `install` and `direct`, which need a fresh machine, run alongside. Measured: the install took 54 s alone but 324 s when six routes each did it at once on one host, so six in parallel gave no gain; with the shared baseline the whole run went from 437 s to 172 s. Every step prints its seconds, and each route ends with its slowest steps, so the next saving is measured, not guessed.

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
| A fix the candidacy check tells a person to type (add the account to the sudo group, install sudo or Python) | yes (the exact text per system, `FixesAreExactForTheSystem`) | yes (`fixes`: run exactly as printed, as that account, on all four releases; the old `su -c` form is shown to fail on Debian) | the real Debian 13 VM confirmed the premise (no /usr/sbin on the account's PATH) |
| Where a job goes from a folder or a computer (`.tenta.toml`, `~/.tenta.toml`, `ct-run use`, `ct-admin workers use`, the question after a new worker) | yes (`test_here`: overlap warnings, override/retain, precedence, a dedicated command leaves its worker free; `test_place.Dedicated`; `test_addworker.OfferDedication`) | no (placement is a decision made on the administrator's computer from files; no machine involved) | not needed |
| A worker's alias (`~/.ct/aliases.toml`, `ct-admin workers rename`: one worker, the fleet wizard, mask, clear; typing an alias anywhere a worker is named; every card shows a worker through `alias.show`) | yes (`test_alias`: store, resolve, collisions, wizard, routing by alias, file keeps the real name) | no (an alias is local to this computer; nothing reaches a worker) | the real fleet is renamed by hand once, then every command read |
| `ct-run jobs` with several workers (every worker's jobs together, newest first, with the worker; one that cannot be read is named) | yes (`test_place.TheJobsList`) | no | seen on the real fleet: it used to list only the main worker's jobs, so jobs sent to the others never appeared |
| A class's tool folders on a Special Use worker (`isolation.mode`, `isolation.tool_roots`, `tools:` in a class) | yes (`test_config`, `test_registry.ToolFolders`, `test_worker.ToolFolders`: order, full isolation refuses, missing folder, a link out of the roots, published fields) | yes (`tooldirs`: a tool runs in the real jail, order decides, an unnamed folder is invisible, a named one is read-only; run on a real worker with `scripts/probe --on`, a container cannot build the jail) | proved on Debian 13, Ubuntu 24.04 and Ubuntu 26.04 workers |
| What the computer is used for (`isolation.purpose`, the setup question, which protections are the default, who each protection affects, tool folders asked for Special Use) | yes (`test_setup.WhatTheComputerIsUsedFor`, the wizard tests count the new question) | no | by hand: `ct-setup demo` shows the screens; a real install on a spare VM for each purpose is still to do |
| A computer other people use: no new job while someone is logged in or outside its working hours, said in the card and ranked by the scheduler (`[availability]`) | yes (`test_worker.SharedComputer`: a logged-in person holds jobs and the owner's account does not count, a window incl. one crossing midnight, nothing changes by default; `test_sched.SharedComputers`; `test_config`; `test_admin.WorkerCards`) | `who`'s output format was read on a real worker (first column = the account) | to try: a spare PC with a second user logging in |
| A shared computer's resource limits on the jobs (`availability.cpu_weight`, `io_weight`, `memory_max_percent`, `nice` in the step unit) | yes (`test_render`: nothing set = the unit byte for byte as before, set = the four lines in order after `Type=oneshot`, only the step unit; `test_config`) | `systemd-analyze verify` of the rendered unit on the three workers (systemd 255, 257, 259) shows no unknown or invalid key | to try: a busy job on a spare PC while a person works at it |
| Installing on a computer other people use leaves every machine-wide setting as it was, a second user still logs in by password, the whole-machine protections are recorded declined, a job runs, and the worker account sees who is logged in (`isolation.purpose = shared`) | yes (`test_setup`, `test_runner.WhoIsLoggedIn`: recorded `loginctl` and `who` output from the three workers and Ubuntu 26.04) | yes (`scripts/e2e-routes shared`: the fingerprint of sshd, firewall, sudoers, groups, IPv6 and automatic updates before and after; found that Ubuntu 26.04 has no utmp, so `who` shows nobody) | the route is the real-machine proof |
| Installing as a Special Use computer (`isolation.purpose = special-use` with a tool folder) records relaxed isolation and the folder, leaves machine-wide settings as they were, declines the whole-machine protections, runs a job and passes the audit | yes (`test_setup`, `test_registry`, `test_runner`) | yes (`scripts/e2e-routes special`) | a class that uses the tool folder is not run end to end yet |
| A worker installed by an OLDER release is upgraded by the update tool it already has (the path `ct-admin apply` takes), then shared-computer limits are switched on and off | yes (`test_render`: no template placeholder an older updater cannot fill; `test_update.StepLimitsDropin`) | yes (`scripts/e2e-routes upgrade`: installs release `E2E_FROM_REV`, default f4c9ae706, then the current one with the old tool; checks the loop, a job, a no-op second run, and the drop-in against real systemd; fails with the real error on the broken release) | no route yet for a Direct install or for skipping several releases |
| Before approving a job of a class with tool folders: which tool really runs (`workerstate.tool_check`, the review card, `ct-admin class show`) and the relaxed state in status, workers cards and the scheduler | yes (`test_workerstate.ToolCheck`: order, system fallback, not found, a folder not allowed; `test_admin.BeforeApprovingAToolClass`; `test_sched.ToolFolderClasses`; `test_cli.Status`) | the folder checks are plain file tests, proved by `tooldirs` for the jail itself | to see on the real fleet after `ct-admin apply` (the workers run an older release today) |
| Workers on different releases (a worker installed from an unpublished checkout while the others were applied from the release branch) | yes (`ReleaseMismatch`, `TheCheckoutIsTheReleaseTheOthersGet`: the card, the watch alert once, apply refusing to replace an unpublished commit, one release decided once for the fleet) | | the real fleet: tenta3 (installed from a branch) beside the two applied from main |
| `ct-setup uninstall` preview run as the admin (a normal account) | yes (real `Host` on an unsearchable folder) | yes (`uninstall`, on all four releases) | |
| A worker's address changed (`ct-admin locate`, the monitoring pass) | yes (ladder, matcher, real `ssh-keyscan` and `known_hosts` output on every release, saving the address) | gap: needs two addresses on one machine | by hand: change the VM's address |
| Move ONE registered worker from GitHub to Direct after several workers exist | yes, the part on your computer (`test_move_one_worker`: real files and git clones; only the named worker's configuration and its two clones change, every other file is byte for byte the same, the scheduler's view follows; the server half is `test_migrate`). Moving back is `ct-setup transport github` (`test_move_one_worker.BackToGitHub`: the history is pushed to stand-in GitHub repositories, the installer is asked to set the worker up again, a GitHub with commits the clones lack stops it with nothing changed, a failed reinstall leaves this computer as it was) | gap: needs two machines | by hand: the Mac mini and the VM |
| Several workers: a job is placed by the scheduler (`ct-run submit`, `ct-run where`, `--worker`) | yes (`test_sched` rules, `test_place` wiring with real files; the real state builder) | | confirmed by hand on the Mac mini and the VM (found the GitHub/Direct mismatch) |
| A job placed on another worker: result, approval, stop (`placements.json`) | yes | gap | by hand: result read back from the VM |
| Several workers: one unreachable, locked, or lacking the class | yes | gap | VM off: by hand |
| Many `ct-run submit` at once from one computer (a script, two sessions) | yes (`test_concurrent_submit`: real git, 8 threads at once; `ManyPlacementsAtOnce`) | by hand: `scripts/loadtest` on the real workers (found the lost jobs and the herd) | `scripts/loadtest --jobs 8` |
| `ct-admin apply`, `diff`, `check`, `status`, `quarantine`, `unlock` with several workers (every worker in turn, or `--worker`) | yes (`FleetWideCommands`, and the classification of all 25 commands) | | confirmed by hand: `ct-admin diff` across the Mac mini and the VM |
| A check that is about one worker (the install check, the wizard's first job) runs on THAT worker | yes (`test_pinned_checks`, a guard on every internal send, and `test_pinned_end_to_end`: the real `ct-run submit` on two real git queues with the scheduler really choosing) | two real machines: not yet | found by hand: the Mac mini passed its check on the VM's job |
| A heavy job goes to the worker it fits on now; a burst of heavy jobs goes one per worker | yes (`CostAware`, `PendingJobsCountInCapacityPoints`) | gap | |
| A submit split into several jobs stays on ONE worker | yes (`ABatchStaysOnOneWorker`: real `--dir` split, two real git queues) | gap | |
| A worker's own configuration disagrees with the connection it really fetches over | yes (`transport_facts`, the explanation) | | found by hand on the Mac mini |
| The "newer release" banner for a worker ahead of, or behind, the newest tag | yes (`Pending`, `is_behind`, once per run) | | found by hand: a worker on main was told a tag was newer |
| One send's time and the host cap's cost as jobs accumulate | yes (`ReadingTheJobsBranchCostsOneProcessNotOnePerJob`, `NoRepeatedNetworkTrips`) | by hand: `scripts/loadtest`, and the profile of one send | `scripts/loadtest --jobs 8` |
| Arrow keys and Ctrl-C in a real terminal | yes (virtual terminal) | | a pseudo-terminal check, not yet kept in the repo |


**Pre-test checklist.** Before any run that costs minutes, `tests/preflight.py --for unit|probe|e2e|real HOST` checks the environment and stops with one line per problem and the command that fixes it (Docker or OrbStack not running, too many test machines, low disk, a real machine that does not answer a key login). `scripts/t`, `scripts/probe`, `scripts/e2e-*` call it first; `test_preflight` fails if one skips it. Design: `docs/design/PRE-TEST-CHECKLIST.md`.

**Environment probes.** `scripts/probe [name]` runs `tests/probes/<name>.sh` in a throwaway container (`docker run --rm`, the checkout mounted read-only at `/src`): about 20 seconds, nothing to clean up. A probe proves ONE fact about a real operating system that a unit test cannot, using the project's own code. `ownership` creates the two accounts (the worker and the mailbox account) and shows: git refuses a repository owned by another account (the premise); the mailbox shell serves it anyway, for that one repository and no other; and root copying history between repositories owned by others works with the project's helper and not without it. It was written from two bugs the full end-to-end runs found, and it fails on the old code for both. Needs Docker (OrbStack provides it); `CT_PROBE_IMAGE` picks the release (default Ubuntu 24.04).

**More probes.** `release` runs on all four supported releases at once (`scripts/probe --releases release`) and shows that the packages and tools the installer needs exist there, at the versions the code assumes (git 2.32 or newer, Python 3.11 or newer, bubblewrap, systemd directives). `kernel` is read-only and runs on a real machine over SSH (`scripts/probe --on user@host kernel`): the AppArmor restriction and profile, user namespaces, timer accuracy, the services' sandbox settings. It is how the timer `AccuracySec` that systemd silently ignored (an inline comment) was found on a real VM. `record` is not a test: `scripts/record-fixtures` runs it per release and saves what the real tools printed.

**Recorded real outputs.** `tests/fixtures/real/<release>/` holds the real output of `os-release`, `sudo --version` (sudo-rs on Ubuntu 26.04), git, Python, systemd, bubblewrap, OpenSSH, git's refusal of another account's repository, and the whole candidacy-check script. `tests/test_real_outputs.py` runs the parsers over them, so a parser is tested against what a release really prints, not against its author's idea of it. Re-record with `scripts/record-fixtures` when a release is added. Recording found that git 2.53 (Ubuntu 26.04) does not refuse the foreign repository the way 2.43 does, so the ownership bug cannot show there.

**Change-based planning.** `scripts/test-plan [base]` lists, for the files changed (including uncommitted and new ones), the scoped unit modules, the probes and the routes worth running, from the table in `tests/impact.py`. `tests/test_impact.py` fails when any file of the project has no rule, or a rule names something that does not exist; a file the table does not know plans everything. The full unit suite stays the gate before a push.

**Load test.** `scripts/loadtest [--jobs 8] [--waves 1]` sends a burst of simultaneous `ct-run submit` of the harmless `sys-status` class across every worker you administer, then reads each job back and reports where it went and how long each stage took (pick-up, queue, run). The host cap (`limits.max_in_flight`, default 8) applies. It is how three real faults were found: concurrent sends sharing one temporary git index (jobs reported sent but missing, and lock errors), every simultaneous placement seeing the same idle workers (all sent to one), and a staleness limit shorter than the workers' own publishing interval.
