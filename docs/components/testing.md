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

The scenario is split into independent **routes** in `tests/e2e/routes/` (`install`, `direct`, `switch`, `move`, `rename`, `uninstall`), sharing `tests/e2e/lib.sh`. Each does its own baseline install, so the routes run side by side and the wall time is the slowest route, not the sum. The slow, identical preparation (system packages, git, a stand-in for GitHub) is done once into a cached base machine that every run clones in a second; the cache is keyed by release. Every step prints its seconds, and each route ends with its slowest steps, so the next saving is measured, not guessed.

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
| Arrow keys and Ctrl-C in a real terminal | yes (virtual terminal) | | a pseudo-terminal check, not yet kept in the repo |


**Environment probes.** `scripts/probe [name]` runs `tests/probes/<name>.sh` in a throwaway container (`docker run --rm`, the checkout mounted read-only at `/src`): about 20 seconds, nothing to clean up. A probe proves ONE fact about a real operating system that a unit test cannot, using the project's own code. `ownership` creates the two accounts (the worker and the mailbox account) and shows: git refuses a repository owned by another account (the premise); the mailbox shell serves it anyway, for that one repository and no other; and root copying history between repositories owned by others works with the project's helper and not without it. It was written from two bugs the full end-to-end runs found, and it fails on the old code for both. Needs Docker (OrbStack provides it); `CT_PROBE_IMAGE` picks the release (default Ubuntu 24.04).

**More probes.** `release` runs on all four supported releases at once (`scripts/probe --releases release`) and shows that the packages and tools the installer needs exist there, at the versions the code assumes (git 2.32 or newer, Python 3.11 or newer, bubblewrap, systemd directives). `kernel` is read-only and runs on a real machine over SSH (`scripts/probe --on user@host kernel`): the AppArmor restriction and profile, user namespaces, timer accuracy, the services' sandbox settings. It is how the timer `AccuracySec` that systemd silently ignored (an inline comment) was found on a real VM. `record` is not a test: `scripts/record-fixtures` runs it per release and saves what the real tools printed.

**Recorded real outputs.** `tests/fixtures/real/<release>/` holds the real output of `os-release`, `sudo --version` (sudo-rs on Ubuntu 26.04), git, Python, systemd, bubblewrap, OpenSSH, git's refusal of another account's repository, and the whole candidacy-check script. `tests/test_real_outputs.py` runs the parsers over them, so a parser is tested against what a release really prints, not against its author's idea of it. Re-record with `scripts/record-fixtures` when a release is added. Recording found that git 2.53 (Ubuntu 26.04) does not refuse the foreign repository the way 2.43 does, so the ownership bug cannot show there.

**Change-based planning.** `scripts/test-plan [base]` lists, for the files changed (including uncommitted and new ones), the scoped unit modules, the probes and the routes worth running, from the table in `tests/impact.py`. `tests/test_impact.py` fails when any file of the project has no rule, or a rule names something that does not exist; a file the table does not know plans everything. The full unit suite stays the gate before a push.
