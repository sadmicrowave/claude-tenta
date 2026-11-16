# Getting started

## What you need

- **Your computer** (macOS or Linux) with `git`, `ssh` and Python 3.11 or newer. The GitHub command-line tool `gh`, signed in (`gh auth login`), is strongly recommended: with it, setup creates the two private repositories and adds the
  server's deploy keys to them for you. Without it, setup tells you exactly what to click.
- **A spare computer that stays on**, running **Ubuntu 24.04 or newer, or Debian 12 or newer**, on your home network. An old laptop or a mini PC is plenty. It needs `python3` (3.11+), `sudo`, an SSH server, and a normal
  account for you on it (the *admin* account) that you can log in to **with an SSH key** (`ssh-copy-id admin@worker` once, if you have not). Everything else it needs (git, the registry reader, bubblewrap, the firewall tools) is installed for you.
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

## 2. Set everything up, from your computer

```bash
cw-setup install --dry-run     # optional: see everything it would do; nothing on the server changes
cw-setup install
```

It will:

1. connect to the server over SSH and look at it (it refuses an unsupported machine before changing anything);
2. ask its questions, once, with a default for each (the worker's address, your account name on it, your GitHub name, how strong the machine is, whether to turn IPv6 off, where alerts go...);
3. set up your side: your configuration, the two private repositories, the clones;
4. send the release to the server and run the server's setup there. **It asks for your sudo password on the server** (only the server sees it) and for a yes to each protection it offers, one at a time, with the plain
   reason and how it could lock you out;
5. while that runs, take the server's two public deploy keys and add them to GitHub for you (the queue key read-only, the results key with write access). Nothing is copied by hand;
6. send a first job through the whole loop and check the connection.

It is safe to run again: each step only does what is missing. The folder it used on the server (which held your answers) is removed when it finishes.

If something stops it, it says what and how to fix it, and running `cw-setup install` again continues from there.

### Doing it step by step instead

`cw-setup client` sets up your side only. On the server, from a copy of the checkout, `sudo env PYTHONPATH=src python3 -m cw.setup server --apply --wait-for-keys 900` does the server's side and waits for the deploy keys; `cw-setup client`
(run again once the server has published its keys) adds them. See the [setup wizard](components/setup.md).

## 3. Send a first job

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
| check the worker still matches what I chose | on the worker: `sudo env PYTHONPATH=/opt/claude-worker/lib python3 -m cw.setup audit` |

## If something goes wrong

Every command explains itself: what went wrong, why, how to fix it, and a command to copy. `cw-help` lists everything. The [components](components/overview.md) pages explain how each part works.
