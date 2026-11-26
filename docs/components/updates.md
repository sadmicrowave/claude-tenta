# Releases and updates

## In plain words

The worker's software is installed from the project's public repository, **only when you say so** (or, if you choose, only from releases signed by a key you trust). `ct-admin diff`
shows what would change, flagging anything that runs as root. `ct-admin apply` installs it, checks the worker still works by running a real test job, and offers to go back to the previous
version if it does not. A new version never replaces working files in a half-finished state, and a change to the worker's services is kept only if a test proves jobs can still run under it.

Your own job classes live in your private repository, separate from the project, so updating the software never touches them.

## Technical specification

### The update tool

`sudo ct-update --rev <commit>` · `--verified` · `--local <release checkout> <commit>`. The wrapper (`/usr/local/sbin/<name>-update`) runs from `/` with a cleared environment and a fixed
`PATH` and `PYTHONPATH`, then `python3 -m ct.update`.

| Mode | Source | Allowed when |
|---|---|---|
| `--rev <commit>` | a bare clone of `updates.source` kept on the server | the commit is on `updates.branch` (an unmerged or abandoned commit cannot be installed) |
| `--verified` | the newest tag named `release-*` whose signature verifies | the tag is signed by a key in `<config>/allowed_signers`; unsigned or untrusted tags are skipped and said so |
| `--local` | a checkout you give it | used by the wizard and tests |

The release is exported with `git archive`, so no `.git` folder, hook or config comes with it.

### What an install does, in order

1. Render every generated file from the configuration and **fail closed** before writing anything if any value does not match its pattern.
2. Write each file atomically (a temporary file in the same folder, then a rename) with its mode: the `ct` package into `<code>/lib/ct`, the wrappers, the systemd units, the packs the configuration names into `<config>/packs/`, the sudoers rule (checked with `visudo` first and never installed unchecked), and
   the IPv6 sysctl file when `network.ipv6 = "disabled"` (removed, and the setting restored, when enabled).
3. Remove files the new release no longer has, in `lib/ct` and in the packs it ships, so a renamed module cannot leave stale code behind. Packs it does not ship are left alone.
4. If a unit file changed, run the **sandbox test** against the new step unit: transient units carrying exactly those settings start the dispatcher, build a no-network namespace, write the state and home folders,
   reach the repositories over SSH, read the sensors, log and read the package database. If anything fails, the previous unit files are put back, nothing is reloaded, and the revision is not recorded.
5. `systemctl daemon-reload`, then record the installed revision in `<config>/installed-rev`.
6. Install the registry: compose packs and your registry, validate, install packages first, keep `registry.yaml.previous`, write ([packs](packs.md)).

A second install of the same release changes nothing and does not re-test or reload.

### Update modes

`updates.mode = "manual"` (default): you run `ct-admin apply`. `"signed-tags"`: the `update` timer (every `schedule.update_min` minutes) runs `--verified`.
Pushing to the repository alone can therefore never change what runs on the worker in that mode.

### Rolling back

`ct-admin apply <previous commit> --no-verify` installs an older revision. The registry is recomposed from the packs and the queue repository's current `main`; to undo a registry mistake, revert it on `main` and apply again.
`registry.yaml.previous` is the last registry that was replaced.

### Cutting a release

Releases are commits on the release branch; a signed release is an annotated tag `release-<version>` signed with an SSH key whose public half is in `allowed_signers`
(`git tag -s` with `gpg.format = ssh`). The project's release process is described in `CONTRIBUTING.md`.

## Knowing a newer release exists

A release is a tag named `release-X.Y.Z` in the project repository (`updates.source`), made with `scripts/release X.Y.Z` (it bumps the version, commits, and tags with the notes, signing the tag if you ask; it never pushes). Two things look for them:

- **A quiet banner.** After a `ct-run` or `ct-admin` command, on a terminal, one dim line says that a newer release exists (`A newer release is available: release-0.2.0 (this computer has 0.1.0; the worker is on a different release). See what changed and update: ct-admin update`). It is read from a cache, so a command never waits for the network; the cache (`~/.ct/update-check.json`) is refreshed at most once a day by a detached background process (`git ls-remote --tags`: nothing is cloned or downloaded). The banner appears at most twice a day, never when output is piped or with `--json`, and is switched off by `updates.check = false` or the environment variable `CT_NO_UPDATE_CHECK`. Nothing is installed by it.
- **`ct-admin update`.** Looks now, shows the release's notes, and asks. Saying yes to "update this computer" fast-forwards your checkout to exactly the release tag's commit (a checkout with uncommitted changes, or with commits the release does not contain, is refused and left alone; a pip-installed copy is reinstalled from the tag). Installing it on a worker is a separate question for each worker, because it logs in and needs that worker's sudo password: it runs the same `apply`. With several workers the release is looked up once, what changed is shown once, a card first lists the devices it would touch (this computer, then each worker and whether it will be updated, is already current or is left alone), this computer is updated once, then each worker in turn, and one card says how each went (a worker that fails or is declined does not stop the others; the exit code is the worst). `--worker NAME` narrows it to one. A Windows worker is left alone: updating an installed Windows worker is not built yet. `--check` only reports. A worker set to `updates.mode = "signed-tags"` installs a new release by itself, but only if the tag's signature checks against the keys you trust.

