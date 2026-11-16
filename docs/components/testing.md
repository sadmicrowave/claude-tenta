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
scripts/e2e-orbstack            # create the machine if needed, copy the tracked files in, run the scenario
scripts/e2e-orbstack --fresh    # start from a clean Ubuntu
scripts/e2e-orbstack --delete   # remove the machine
```

`tests/e2e/scenario.sh` can also be run as root on any fresh Ubuntu 24.04 or Debian 12 machine. It: installs the project with `pip`; runs `cw-setup client`; runs `cw-setup server` twice (the first run must stop for the deploy keys, the second must carry on);
checks the accounts, installed commands, composed registry, timers, password-less SSH, the firewall, groups and permissions and `cw-setup audit`; sends `sys-status` and waits for the result; checks `cw-admin check` and `cw-admin status`;
checks that `sudo` cannot be profiled and that a denied program cannot be installed; reads a decoy credential and expects a lockdown; and re-installs the release.

GitHub is simulated: `github.com` resolves to the machine itself, where an SSH user called `git` holds the two bare repositories, so every clone, fetch and push uses real keys, including the worker's own deploy keys.
The decoy-credential check needs kernel audit, which container-style machines (OrbStack) lack; there it reports SKIP and the hardening module explains why. Use a full VM to cover it.

### Privacy guard

`scripts/privacy-check` runs as a `pre-commit` and `commit-msg` hook and with `--history` before any push. It blocks private IPs, MAC addresses, e-mail addresses, `.local` host names, keys, tokens, home paths, and anything on a personal denylist kept outside the repository. A line may carry `privacy-ok` with a reason.
