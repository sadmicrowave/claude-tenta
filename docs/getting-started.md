# Getting started

## What you need

- **Your computer** (macOS or Linux) with `git`, `ssh` and Python 3.11 or newer. The GitHub command-line tool `gh`, signed in, is optional but saves clicking.
- **A spare computer that stays on**, running **Ubuntu 24.04 or newer, or Debian 12 or newer**, on your home network. An old laptop or a mini PC is plenty. It needs `python3` (3.11+), `python3-yaml`, `git`, `sudo` and an SSH server,
  and a normal account for you on it (the *admin* account) with an SSH key installed.
- **A GitHub account.** The two repositories are created private under it.

Nothing needs to be reachable from the internet. The worker only makes outgoing connections to GitHub.

## 1. Install on your computer

```bash
git clone https://github.com/<project>/claude-worker.git
cd claude-worker
python3 -m venv ~/.cw-venv && ~/.cw-venv/bin/pip install -e .
export PATH="$HOME/.cw-venv/bin:$PATH"
cw-help
```

(`-e` keeps the commands tied to the checkout, which is where the worker's release comes from.)

## 2. Set up your side

```bash
cw-setup client
```

It asks for the worker's address, your account name on it, and your GitHub name. It creates the two private repositories, checks SSH, and clones them. Add `--dry-run` first to see everything it would do.

## 3. Set up the worker

Copy the checkout to the worker (or clone it there) and run the wizard as root. It shows everything first; add `--apply` to do it.

```bash
cd claude-worker
sudo env PYTHONPATH=src python3 -m cw.setup server           # a preview: changes nothing
sudo env PYTHONPATH=src python3 -m cw.setup server --apply
```

It will stop after it creates the worker's two repository keys and print two `gh repo deploy-key add ...` commands. Run them on your own computer, then run the server command again; it continues from there with the job registry, the protections
(one question each) and the timers.

## 4. Send a first job

```bash
cw-run classes
cw-run submit sys-status --wait
cw-admin check
cw-admin status
```

## Day to day

| I want to | Command |
|---|---|
| send a job | `cw-run submit <class> --wait` |
| see my jobs and anything waiting for me | `cw-run jobs` |
| approve a waiting job | `cw-admin approve <job>` |
| see how the worker is doing | `cw-admin status` |
| find out whether a slow command is worth sending | `cw-run offload fit -- <command>` |
| install a new release | `cw-admin diff`, then `cw-admin apply` |
| the worker locked itself | `cw-admin quarantine`, then `cw-admin unlock` |
| check the worker still matches what I chose | `sudo env PYTHONPATH=src python3 -m cw.setup audit` on the worker, from the checkout |

## If something goes wrong

Every command explains itself: what went wrong, why, how to fix it, and a command to copy. `cw-help` lists everything. The [components](components/overview.md) pages explain how each part works.
