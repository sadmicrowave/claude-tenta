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

### End-to-end test

```
scripts/e2e-orbstack            # create a fresh machine, copy the tracked files in, run the scenario, save its logs, DELETE the machine
scripts/e2e-orbstack --keep     # leave the machine running afterwards, to look around (then scripts/e2e-clean)
scripts/e2e-clean               # delete every test machine this project ever made (--list only lists them)
scripts/e2e-orbstack --delete   # remove the machine
scripts/e2e-matrix              # the scenario on Ubuntu 26.04 and 24.04 and Debian 12 and 13 at once, one pass/fail line each (CT_E2E_IMAGE picks one release for e2e-orbstack)
```

`tests/e2e/scenario.sh` can also be run as root on any fresh Ubuntu 24.04 or Debian 12 machine. It: installs the project with `pip`; runs `ct-setup client`; runs `ct-setup server` twice (the first run must stop for the deploy keys, the second must carry on);
checks the accounts, installed commands, that the worker reports its isolation as ON, that `job-isolation` is satisfied, composed registry, timers, password-less SSH, the firewall, groups and permissions and `ct-setup audit`; sends `sys-status` and waits for the result; checks `ct-admin check` and `ct-admin status`;
checks that `sudo` cannot be profiled and that a denied program cannot be installed; reads a decoy credential and expects a lockdown; and re-installs the release.

GitHub is simulated: `github.com` resolves to the machine itself, where an SSH user called `git` holds the two bare repositories, so every clone, fetch and push uses real keys, including the worker's own deploy keys.
The decoy-credential check needs kernel audit, which container-style machines (OrbStack) lack; there it reports SKIP and the hardening module explains why. Use a full VM to cover it.

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
