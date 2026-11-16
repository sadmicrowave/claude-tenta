# The setup wizard (`cw-setup`)

## In plain words

**`cw-setup install`** does all of it from your own computer in one command: it asks the questions once, sets up both sides, sends the release to the server, runs the server's setup there (you type your sudo password once), adds the server's deploy keys to GitHub for you, and sends a first job. The sections below describe the pieces it is made of, which you can also run one at a time.

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
cw-setup install [--dry-run] [--yes] [--answers FILE] [--set section.key=value]... [--source DIR] [--wait-for-keys SECONDS] [--no-test]
cw-setup uninstall [--apply] [--yes] [--keep-data] [--remove-account]
cw-setup client [--dry-run] [--yes] [--answers FILE] [--set section.key=value]... [--queue-key FILE] [--results-key FILE]
cw-setup server [--apply] [--yes] [--answers FILE] [--set section.key=value]... [--source DIR] [--rev COMMIT] [--wait-for-keys SECONDS]
cw-setup audit
```

`server` is a preview unless `--apply`. `--yes` answers every question with its default (and fails, naming them, if a question has none). `--answers` is a TOML file with the
same tables as `cw.toml`; `--set` overrides it. Any schema setting can be given even if it has no question (for example `hardening.modules`).

### Questions

`src/cw/setup/questions.py` holds the questions. Each maps to one schema setting and is checked with that setting's own check, so the wizard cannot write a value
the loader would refuse. A question can depend on an earlier answer (the LAN questions only appear if the LAN is blocked). Suggestions come from the machine
(`detect.py`): hardware profile from memory, cores and battery. Only values that differ from the defaults are
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

### `cw-setup install`, in order

`src/cw/setup/install.py`.

1. **Connect and learn.** Asks for the server's address, the admin account and the SSH port; checks key login (a failure says to run `ssh-copy-id`); reads the server's facts through a read-only SSH view (`remote.py`), which become the suggestions
   (the hardware profile from memory and battery) and refuse an unsupported system before anything changes.
2. **Questions.** All of the client and server questions, once, on your computer. Every answer is then written to a complete answers file (`toml_out.dump(..., full=True)`), so the server never has to ask or guess.
3. **Your side.** The client half ([above](#client-half-in-order)), with the repositories created by `gh` if they are missing.
4. **Delivery.** The committed files of the checkout (`git archive HEAD`; uncommitted changes are not sent, and it says so) and the answers go through SSH into a private folder made with `mktemp` (mode 0700, name checked against a strict pattern before it is
   used or removed).
5. **The server half**, run as `sudo env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=<folder>/src python3 -m cw.setup server --apply --wait-for-keys N ...` over `ssh -t`, so sudo asks for its password on your terminal and only the server sees it. (No bytecode: root must not leave files in
   that folder that the administrator cannot remove.)
6. **Deploy keys, while it runs.** The server publishes the public halves to `<config>/deploy-keys/{queue,results}.pub` (mode 0644, public keys only). This side reads them over a second SSH connection and adds each with
   `gh repo deploy-key add` (queue read-only, results `--allow-write`, titled `<name>-server`), skipping any that are already on the repository (`keys.py`). Without `gh` it prints each key and the exact settings page. The server's own wait loop notices as soon as the
   repositories become reachable and carries on, so there is one sudo password and no second run.
7. **Cleanup and a first job.** The working folder is removed. `sys-status` is sent and waited for (the worker needs a minute after its first start to publish its class list, so it retries), then `cw-admin check` runs.

A preview (`--dry-run`) does steps 1 to 4 without creating repositories or changing your configuration, and runs the server half as a preview (no sudo, no changes).

### Exit codes

`cw-setup server` exits **10** when it stopped because the repositories cannot be reached yet (the deploy keys are not added); running it again continues. `cw-setup install` treats that as an error only if the keys were not added within `--wait-for-keys` seconds (default 900).

### `cw-setup uninstall`

`src/cw/setup/uninstall.py`. From your computer: `cw-setup uninstall` shows what it would remove (a preview that needs no password and changes nothing); `cw-setup uninstall --apply` asks once, then runs the removal on the server over `ssh -t` under `sudo`. The removal code comes from your
checkout (delivered like an install, then removed), never from the install being removed, so it works however old or broken that install is. On the server itself it is `python3 -m cw.setup uninstall --here [--apply]`.

It reads the install's own `cw.toml`, so it respects a different name or folders, and lists only what is actually there:

1. stops and disables the timers, stops the services, ends any job still running as the worker account;
2. removes the unit files and reloads systemd;
3. removes the sudoers rule, the IPv6 and user-namespace sysctl files, the decoy-watching audit rule (and reloads the audit rules), and the commands;
4. removes the code, configuration and state folders (`--keep-data` keeps the state folder: the clones, the security log, kept outputs);
5. with `--remove-account`, removes the worker account and its home folder.

**What stays, deliberately:** the protections (key-only SSH, the firewall, automatic security updates, the user-namespace restriction and the packages bubblewrap, auditd and ufw), because removing the worker does not make the machine less safe; the worker account (reinstalling reuses it)
unless you say otherwise; IPv6 stays off until the next restart if it was off (the file that kept it off is removed); your GitHub repositories; and your own computer's configuration. Running `cw-setup install` afterwards sets everything up again.

### What the questions and protections look like

`src/cw/setup/tui.py`. On a real terminal (colour on, not `--yes`, not piped) the wizard draws its own screens; anywhere else it prints plain text as it always did, so logs, tests and `NO_COLOR` are unaffected. `cw-setup demo` shows every screen on sample content and saves nothing.

- **Questions.** A progress bar on top (`cw-setup install  ━━━━╸────  8 of 19 done`), then up to four answered questions collapsed to one line each (`✓ Server address  worker.example`; older ones are counted: `… 6 earlier answers`), then the active
  question on a blue rail with a chip naming its section and its position, the plain-words reason, the input line, a hint, and **the keys, always listed underneath**.
- **Choices.** A question with a fixed set of answers lists each one with its number and what it means, the current one marked `▸`. Type the name, its number, or part of the name and press Tab.
- **Keys.** Enter or ↓ accepts and goes on. **↑ goes back to the previous question** with your earlier answer ready to change (what you had typed is kept even if you leave without pressing Enter). Tab completes a choice. ←/→, Home/End, Backspace, Delete, Ctrl-U (clear)
  and Ctrl-W (delete a word) edit the line. Ctrl-C stops cleanly. A question that stops applying because of an earlier change disappears from the flow, and its answer with it.
- **A last look.** After the final question a review screen lists every answer; Enter starts, ↑ goes back to change one. The install flow is one continuous walk: from its first question after the connection check, ↑ even returns to the connection questions
  (and it reconnects).
- **Hidden answers.** The alert address is typed hidden, shown as dots so you can see it was accepted, and listed as `set`. Pasting works, including when the terminal wraps a paste in escape sequences (bracketed paste), which the terminal's own line editor
  would have passed through as garbage.
- **Protections.** Each one that needs a decision is an amber frame with the title, why, the risk, what it is now, the exact steps, and Apply and Skip buttons. `←`/`→` or Tab choose, Enter confirms, `y` and `n` answer at once, `s` shows every line of every step, Ctrl-C stops
  cleanly. The two that could lock you out (`ssh-keys-only`, `ufw-lan-egress`) are marked "could lock you out"; the rest say "recommended". The frame is erased when you decide, and the result is printed as the usual one-line status.
- **No library.** Plain ANSI sequences (cursor up, erase to the end of the screen), and the keys are read in cbreak mode with bracketed paste turned on while a screen is up.
- **Tested** with a small virtual terminal that obeys the same sequences, so the tests check what is actually on screen after each key, and by driving the real thing through a pseudo-terminal with typed answers, arrow keys, Tab and a bracketed paste.
