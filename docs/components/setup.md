# The setup wizard (`cw-setup`)

## In plain words

`cw-setup` does the setup for you by asking plain questions and then doing the work. There are two halves, because there are two computers.

- **On your computer, `cw-setup client`:** asks for the worker's address, your account name on it, and your GitHub name; checks SSH works; creates the two private
  repositories under your GitHub account (using the `gh` command if you have it, otherwise it tells you exactly what to click); and clones them.
- **On the worker, `cw-setup server`:** shows you everything it is about to do, then (with `--apply`) creates the accounts and folders, installs the software, makes
  the worker's two repository keys and **stops** so you can add them to GitHub, then continues: it installs the job list, applies the protections one at a time
  and starts the timers. Run it again any time; it only does what is missing.
- **`cw-setup audit`:** a read-only check that the worker still matches what you chose.

Every answer has a safe default, and nothing is guessed: if a question needs an answer and has none, it tells you which.

## Technical specification

### Commands

```
cw-setup client [--dry-run] [--yes] [--answers FILE] [--set section.key=value]... [--queue-key FILE] [--results-key FILE]
cw-setup server [--apply] [--yes] [--answers FILE] [--set section.key=value]... [--source DIR] [--rev COMMIT]
cw-setup audit
```

`server` is a preview unless `--apply`. `--yes` answers every question with its default (and fails, naming them, if a question has none). `--answers` is a TOML file with the
same tables as `cw.toml`; `--set` overrides it. Any schema setting can be given even if it has no question (for example `hardening.modules`).

### Questions

`src/cw/setup/questions.py` holds the questions. Each maps to one schema setting and is checked with that setting's own check, so the wizard cannot write a value
the loader would refuse. A question can depend on an earlier answer (the LAN questions only appear if the LAN is blocked). Suggestions come from the machine
(`detect.py`): hardware profile from memory, cores and battery; thermal support from the sensors present. Only values that differ from the defaults are
written to `cw.toml` (`toml_out.py`).

### Server half, in order

1. **Check the machine** (Ubuntu 24.04+ or Debian 12+, because the worker needs Python 3.11 or newer; systemd, PyYAML, root for a real run).
2. **Configuration**: ask, validate (including that named hardening modules exist), write `<config>/cw.toml`.
3. **Accounts and folders**: the admin account must already exist; the worker is created with no shell; folders get their modes; `secrets.env` is created `0640` root:worker.
4. **Code**: `install_release` from the checkout the wizard is running from (units, wrappers, packs, sudoers rule, IPv6 setting).
5. **Repository keys**: one ed25519 key per repository for the worker, an SSH config with one host alias per repository, and the exact `gh repo deploy-key add` commands
   (queue read-only, results with `--allow-write`). If the server cannot reach a repository yet, it stops here and says so; running again continues.
6. **Clones**, as the worker.
7. **Job registry**: composed from the packs and your queue repository ([packs](packs.md)); an empty queue repository is fine.
8. **Hardening**: each module in `hardening.modules`, one question each ([hardening](hardening.md)).
9. **Timers**: `fetch` and `step`, and `update` if `updates.mode = "signed-tags"`.

### Client half, in order

Tools check (`git`, `ssh`, optional `gh`) → configuration written to `~/.config/claude-worker/cw.toml` → SSH key login test → repositories created private (or the manual steps are
printed) → deploy keys added if their files were passed → clones → the queue repository's `main` is seeded with an empty `registry.yaml` for **your own** classes (never overwritten if it exists).

### Everything goes through a `Host`

`src/cw/setup/host.py` is the one place the wizard touches a machine (`run`, `read`, `write`, `exists`, `which`). Tests use a fake that records, which is how the dry run and the unit tests can
show and check every step. The end-to-end test uses the real one ([testing](testing.md)).
