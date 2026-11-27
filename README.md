# Armada

Offload slow or heavy jobs from your laptop to a spare Linux machine on your home network, using two private GitHub repositories as the mailbox.

- **Only runs what you allow.** Jobs are picked from a short list of classes with fixed commands, typed parameters and limits.
- **No network for jobs.** Jobs run in an empty network namespace, under time, memory and temperature limits.
- **Everything is scanned on the way out.** A result that looks like it contains a private address, a key or a token is withheld, the worker locks itself down, and your phone gets a short alert.
- **You approve what is risky, exactly as you saw it.** An approval is tied to the SHA-256 of the job file.
- **Set up by a wizard.** It asks plain questions, writes one config file, and applies the protections as part of setup, not as a repair afterwards.
- **Extensible and safe to extend.** Add job classes and off-limits rules as packs; rules can only get stricter.
- **Nothing is reachable from the internet.** The worker only makes outgoing connections to GitHub.

```bash
git clone https://github.com/<project>/armada.git && cd armada
python3 -m venv ~/.ct-venv && ~/.ct-venv/bin/pip install -e .
~/.ct-venv/bin/ct-setup install        # asks its questions, sets up both computers, adds the deploy keys to GitHub for you, runs a first job, and puts ct-run, ct-admin... on your PATH (open a new terminal tab)
```

Start with [Getting started](docs/getting-started.md). The [documentation index](docs/README.md) explains how each part works, with a plain summary and a technical specification for each.

**Status: early.** The whole loop runs end to end on a real Ubuntu 24.04 machine (see [testing](docs/components/testing.md)), but there are no tagged releases yet, it has been tried on very little hardware, and
two parts of the design are still open (a `systemd-run` isolation method and a privileged-helper mechanism; see the [security review](docs/REVIEW.md)). Read the [security model](docs/components/security.md) before you rely on it.

Requires Python 3.11+ on both computers, and Ubuntu 24.04+ or Debian 12+ on the worker. Standard library only on the server, plus PyYAML (`python3-yaml`) for the registry.

No migration is supported before the first release: uninstall a previous install with its own version (`ct-setup uninstall --apply`), then install.

Licensed under the Apache License 2.0. See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

## The offload skill

`skills/tenta-offload/SKILL.md` teaches a coding assistant when to send work to the worker and how (check what is registered, profile an unknown command, attach only the files a job needs, never approve anything itself). Install it by linking the folder into the assistant's skills folder (the path below is the one the common assistants read):

```bash
mkdir -p ~/.claude/skills && ln -s "$PWD/skills/tenta-offload" ~/.claude/skills/tenta-offload
```
