# Getting started

## What you need

- **Your computer** (macOS or Linux) with `git`, `ssh` and Python 3.11 or newer. The GitHub command-line tool `gh`, signed in (`gh auth login`), is strongly recommended: with it, setup creates the two private repositories and adds the
  server's deploy keys to them for you. Without it, setup tells you exactly what to click.
- **A spare computer that stays on**, running **Ubuntu 24.04 or newer, or Debian 12 or newer**, on your home network. An old laptop or a mini PC is plenty. It needs `python3` (3.11+), `sudo`, an SSH server, and a normal
  account for you on it (the *admin* account) that you can log in to **with an SSH key** (`ssh-copy-id admin@worker` once, if you have not). Everything else it needs (git, the registry reader, bubblewrap, the firewall tools) is installed for you.
- **A GitHub account.** The two repositories are created private under it.

Nothing needs to be reachable from the internet. With the GitHub connection the worker only makes outgoing connections to GitHub; with Direct it makes none and your computer connects to it instead (see [Connections](components/transports.md)).

## 1. Install on your computer

```bash
git clone https://github.com/<project>/claude-tenta.git
cd claude-tenta
python3 -m venv ~/.ct-venv && ~/.ct-venv/bin/pip install -e .
~/.ct-venv/bin/ct-setup install
```

(`-e` keeps the commands tied to the checkout, which is where the worker's release comes from.) That one command is the only time you type a path: the install links `ct-run`, `ct-admin`, `ct-setup` and `ct-help` into `~/.local/bin` (never over a file already there) and, if that folder is not on your PATH, offers to add one marked line to your shell's startup file. Open a new terminal tab afterwards and the commands work from anywhere. (A terminal that was already open does not see the change; `source ~/.zshrc` or a new tab does.)

## 2. Set everything up, from your computer

```bash
ct-setup demo                  # optional: see the question and protection screens on sample content; nothing is saved
ct-setup install --dry-run     # optional: see everything it would do; nothing on the server changes
ct-setup install
```

It will:

1. connect to the server over SSH and look at it (it refuses an unsupported machine before changing anything);
2. ask its questions, once, with a default for each (the worker's address, your account name on it, your GitHub name, how strong the machine is, whether to turn IPv6 off, where alerts go, how Claude jobs sign in...). The keys are listed under each question: Enter goes on, the up arrow goes back to change an earlier answer, Tab completes a choice. Only the questions without a safe default are asked; press `A` on the last screen (or use `--advanced`) to change the rest. That screen lists every answer before anything happens;
3. set up your side: your configuration, the two private repositories, the clones;
4. send the release to the server and run the server's setup there. **It asks for your sudo password on the server** (only the server sees it) and for a yes to each protection it offers, one at a time, with the plain
   reason and how it could lock you out;
5. while that runs, take the server's two public deploy keys and add them to GitHub for you (the queue key read-only, the results key with write access). Nothing is copied by hand;
6. send a first job through the whole loop and check the connection.

It is safe to run again: each step only does what is missing. The folder it used on the server (which held your answers) is removed when it finishes.

If something stops it, it says what and how to fix it, and running `ct-setup install` again continues from there.

### Doing it step by step instead

`ct-setup client` sets up your side only. On the server, from a copy of the checkout, `sudo env PYTHONPATH=src python3 -m ct.setup server --apply --wait-for-keys 900` does the server's side and waits for the deploy keys; `ct-setup client`
(run again once the server has published its keys) adds them. See the [setup wizard](components/setup.md).

## 3. Send a first job

```bash
ct-run classes
ct-run submit sys-status --wait
ct-admin check
ct-admin status
```

## Day to day

| I want to | Command |
|---|---|
| send a job | `ct-run submit <class> --wait` |
| see my jobs and anything waiting for me | `ct-run jobs` |
| approve a waiting job | `ct-admin approve <job>` |
| see how the worker is doing | `ct-admin status` |
| find out whether a slow command is worth sending | `ct-run offload fit -- <command>` |
| install a new release | `ct-admin diff`, then `ct-admin apply` |
| the worker locked itself | `ct-admin quarantine`, then `ct-admin unlock` |
| check the worker still matches what I chose | on the worker: `sudo env PYTHONPATH=/opt/claude-tenta/lib python3 -m ct.setup audit` |

## Removing it

```bash
ct-setup uninstall             # a preview: lists exactly what would be removed, changes nothing
ct-setup uninstall --apply     # asks once, then removes it (your sudo password, on the server)
```

The protections stay (key-only SSH, the firewall, automatic security updates), and so do your GitHub repositories and your computer's configuration. `ct-setup install` sets it all up again.
See [the setup wizard](components/setup.md#ct-setup-uninstall) for the options (`--keep-data`, `--remove-account`).

## If something goes wrong

Every command explains itself: what went wrong, why, how to fix it, and a command to copy. `ct-help` lists everything. The [components](components/overview.md) pages explain how each part works.
