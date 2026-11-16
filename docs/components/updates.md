# Releases and updates

## In plain words

The worker's software is installed from the project's public repository, **only when you say so** (or, if you choose, only from releases signed by a key you trust). `cw-admin diff`
shows what would change, flagging anything that runs as root. `cw-admin apply` installs it, checks the worker still works by running a real test job, and offers to go back to the previous
version if it does not. A new version never replaces working files in a half-finished state, and a change to the worker's services is kept only if a test proves jobs can still run under it.

Your own job classes live in your private repository, separate from the project, so updating the software never touches them.

## Technical specification

### The update tool

`sudo <name>-update --rev <commit>` · `--verified` · `--local <release checkout> <commit>`. The wrapper (`/usr/local/sbin/<name>-update`) runs from `/` with a cleared environment and a fixed
`PATH` and `PYTHONPATH`, then `python3 -m cw.update`.

| Mode | Source | Allowed when |
|---|---|---|
| `--rev <commit>` | a bare clone of `updates.source` kept on the server | the commit is on `updates.branch` (an unmerged or abandoned commit cannot be installed) |
| `--verified` | the newest tag named `release-*` whose signature verifies | the tag is signed by a key in `<config>/allowed_signers`; unsigned or untrusted tags are skipped and said so |
| `--local` | a checkout you give it | used by the wizard and tests |

The release is exported with `git archive`, so no `.git` folder, hook or config comes with it.

### What an install does, in order

1. Render every generated file from the configuration and **fail closed** before writing anything if any value does not match its pattern.
2. Write each file atomically (a temporary file in the same folder, then a rename) with its mode: the `cw` package into `<code>/lib/cw`, the wrappers, the systemd units, the packs the configuration names into `<config>/packs/`, the sudoers rule (checked with `visudo` first and never installed unchecked), and
   the IPv6 sysctl file when `network.ipv6 = "disabled"` (removed, and the setting restored, when enabled).
3. Remove files the new release no longer has, in `lib/cw` and in the packs it ships, so a renamed module cannot leave stale code behind. Packs it does not ship are left alone.
4. If a unit file changed, run the **sandbox test** against the new step unit: transient units carrying exactly those settings start the dispatcher, build a no-network namespace, write the state and home folders,
   reach the repositories over SSH, read the sensors, log and read the package database. If anything fails, the previous unit files are put back, nothing is reloaded, and the revision is not recorded.
5. `systemctl daemon-reload`, then record the installed revision in `<config>/installed-rev`.
6. Install the registry: compose packs and your registry, validate, install packages first, keep `registry.yaml.previous`, write ([packs](packs.md)).

A second install of the same release changes nothing and does not re-test or reload.

### Update modes

`updates.mode = "manual"` (default): you run `cw-admin apply`. `"signed-tags"`: the `update` timer (every `schedule.update_min` minutes) runs `--verified`.
Pushing to the repository alone can therefore never change what runs on the worker in that mode.

### Rolling back

`cw-admin apply <previous commit> --no-verify` installs an older revision. The registry is recomposed from the packs and the queue repository's current `main`; to undo a registry mistake, revert it on `main` and apply again.
`registry.yaml.previous` is the last registry that was replaced.

### Cutting a release

Releases are commits on the release branch; a signed release is an annotated tag `release-<version>` signed with an SSH key whose public half is in `allowed_signers`
(`git tag -s` with `gpg.format = ssh`). The project's release process is described in `CONTRIBUTING.md`.
