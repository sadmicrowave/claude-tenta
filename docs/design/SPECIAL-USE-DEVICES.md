# Special-use and shared devices (design, 2026-11-19; slices 1 to 5 built)

## The need

A worker is not always a clean machine used only for armada. Two real cases:

- **A tools box.** A penetration tester already has a machine with its tools set up the way they need. Jobs there need the network (internal and external), and they must run the tools that already exist, not copies reinstalled into a locked-down armada account.
- **A shared machine.** Other people and services use the computer. Whole-machine protections the install applies today (blocking the LAN for outgoing traffic, SSH keys only, removing root-equivalent groups, automatic updates) change life for every user, so they cannot be forced; they are offered.

Today the install bundles three different things: a job jail with no network (the default for every class), a jail that shows only system folders and a fixed PATH, and the whole-machine protections. A class can already say `network = true`, but a worker whose isolation is off is left out of scheduling, and existing tools are invisible to jobs.

## What changes: three separate settings

1. **Isolation level, chosen on purpose.** `isolation.mode = "full"` (today's default: system folders only) or `"relaxed"` (the machine owner accepts that tool folders are shown to jobs). Network stays what it already is: per class (`network: true` in the class, which is why such a class is `approve` by default); the mode does not change it. A relaxed worker is a normal worker: it publishes `isolation: relaxed (by choice)` and the orchestrator takes it. `arm-admin status` and `arm-admin workers` show the state in plain words, never as "OFF": `ISOLATION RELAXED  classes that declare network may use it; tool folders: /opt/tools`.
2. **Tool folders.** The machine owner lists the folders classes may use in the worker's config: `isolation.tool_roots = ["/opt/tools", "/usr/local/pentest"]` (any number). A class names the folders it needs: `tools: ["/opt/tools/nmap", "/opt/tools/bin"]` (any number, each inside a root; checked when the registry is validated, so a class cannot expose a folder the owner did not allow). Each folder is shown read-only inside the jail and put in front of the job's PATH, in the order given. Packages are not reinstalled: a class that lists `tools` needs no `packages`. A folder that does not exist is a clear error when the class is approved and when it runs, with the fix.
3. **Whole-machine protections are individually opt-in on a shared machine.** Each hardening module says in one line whether it changes only armada's jobs or every user of the machine (`affects: armada only | every user`). The setup question below picks the defaults; every one stays editable in `armada.toml` and in the review screen.

## The setup question (after the hardware profile)

**What is this computer used for?**

| Choice | Isolation | Whole-machine protections | Tool folders |
| --- | --- | --- | --- |
| Only for armada (default) | full | all on, as today | none |
| Shared with other uses | full | only those that affect armada's jobs alone; the rest offered one by one with their effect | none |
| Special Use | relaxed | offered one by one (LAN egress blocking off by default: it would stop the tools) | asked: one or more folders |

Each answer is a starting point with its consequences shown in the card before it is accepted; nothing is hidden behind the label.

## Commands for what a class may do (no hand-editing `registry.yaml`)

There is no command today that changes a class after it was created: `arm-admin adopt` sets the tier once, `retire` removes. New, in the registry family:

- `arm-admin class set CLASS [--tier approve|auto] [--network on|off] [--tools FOLDER ...] [--worker NAME]` (`--tools` repeats, or takes several folders; `--tools none` clears). It edits the registry text the same way adopt and retire do, shows the exact change first, asks, and installs it through the usual signed path.
- `arm-admin class show CLASS` prints what the class may do: tier, network, tool folders, and each folder's state on the worker.
- Making a network class `auto` (no approval each time) is allowed but asked about in words: "anyone who can send this job will run it with the network, without you approving it". Setting `--network on` on a class that is `auto` offers to make it `approve`.

## Safety, unchanged and added

- Approvals, signatures, the output guard, the ledger and the canaries are not touched by any of this. Relaxing isolation is an owner decision, written in the worker's own configuration and published, never something a job or a sender can ask for.
- A class cannot name a folder outside `tool_roots`; `..` and symlinks that leave a root are refused.
- A relaxed worker raises the security notice level of `arm-admin status` to "NOTICE", not "problem", so it is visible without crying wolf.
- Out of scope for the first build: running a tool **as another local user** (the tools box's own account, to use its home and databases). That needs a privileged-action style rule with a pinned program, like root-level actions; it is a separate design.

## Slices

1. **Built.** `isolation.mode`, `tool_roots` in the configuration, `tools` in the registry (validated), the jail shows the folders read-only and sets PATH; unit tests plus a probe on the four supported releases (a jail run that can read a tool folder and cannot read another).
2. **Built.** `arm-admin class set` and `class show`.
3. **Built.** The setup question and the per-protection labels (see "Slice 3 as built").
4. **Built.** Status, workers cards and the orchestrator know the relaxed state.
5. **Built.** A check before approval that shows which tool a class's program resolves to.
6. Documentation and the test matrix rows (fresh install per purpose; an upgrade from a full-isolation worker that is switched to relaxed; a class naming a missing folder).

## Slice 1 as built

- `isolation.mode` (`full` default, `relaxed`) and `isolation.tool_roots` (a list, absolute plain folders, never a system folder, never under `~`; they need `relaxed`) in `armada.toml` and `config/armada.example.toml`.
- A command class may carry `tools: [folder, ...]`. `registry.validate` checks the shape everywhere (absolute, plain, no repeat, command classes only) and, on the worker at install time, that each folder is inside a root.
- The worker enforces it again at **every run** (`Worker.tool_folders`): full isolation refuses with how the owner changes it; a missing folder refuses; a folder that leads outside the roots once symbolic links are followed refuses. Otherwise the real folders go first on the job's PATH in the order given, and the jail (`extra_ro_paths`) shows them read-only.
- The worker publishes `tools` per class in `registry.json` and `isolation.mode` / `tool_roots` in `worker-state.json`.
- Proved on real kernels with `scripts/probe --on USER@HOST tooldirs`: on all three workers (Debian 13, Ubuntu 24.04, Ubuntu 26.04) a tool in a named folder runs inside the jail, the order of folders decides which tool wins, an unnamed folder is invisible and a named one is read-only. A container cannot build the jail, so there the probe reports a skip.

## Slice 3 as built

- **The question.** `isolation.purpose` (`armada-only` default, `shared`, `special-use`) is asked after the machine question, with each choice's consequence in the card. `special-use` also asks `isolation.tool_roots` (any number of folders, separated by commas).
- **What a purpose decides** (`questions.derive`, only when the person did not say otherwise): `shared` and `special-use` default `hardening.modules` to the protections that affect only armada's own jobs (`job-isolation`, `auditd-canaries`); `special-use` also sets `isolation.mode = "relaxed"`. The whole-machine protections (`ssh-keys-only`, `ufw-lan-egress`, `no-root-equivalent-groups`, `unattended-upgrades`) are not in the default list there; they are added with `--set hardening.modules=...` or by editing `armada.toml`.
- **Who a protection affects.** Every hardening module carries `affects`: "only armada's own jobs" or "every user of this machine". It is printed with the protection and shown as an `AFFECTS` row on the protection screen, whatever the purpose.
- **Offered one by one (built after slice 5).** For `shared` and `special-use`, `hardening.modules` holds only the armada-only protections and the new `hardening.offered` lists the whole-machine ones (`ssh-keys-only`, `ufw-lan-egress`, `no-root-equivalent-groups`, `unattended-upgrades`). The server half offers each in turn on the protection screen, starting on **Skip** with the `AFFECTS` row and a `Default: skip` row; `--yes` applies none of them and records them as declined. The ones the person accepts are checked by `arm-setup audit` like the others; a declined one is not drift (`audit.audited_modules` reads `hardening.json`).

## Slices 4 and 5 as built

- **Status.** `arm-worker status` says `ISOLATION RELAXED` (a warning-coloured notice, never a problem) with the tool folders, and a `USED FOR` row. `arm-admin workers` shows `PURPOSE` and `jail ON, relaxed`; `arm-run classes` lists a class's `TOOLS`. A worker from before these settings publishes nothing and shows no row.
- **Orchestrator.** A class that names tool folders goes only to a worker in `relaxed` mode that allows each folder; the others are left out with the reason (`arm-run where CLASS` says it).
- **Which tool will run.** The worker looks at its own folders (`workerstate.tool_check`) and publishes, per class with tool folders: the program (the first word of its command), where it is found (the first folder, in the order the class lists them, that holds an executable of that name; else the system's folders; else not found) and each folder's state (exists here, allowed). `arm-admin class show CLASS` and the review card that `arm-admin review` and `approve` show for a job of that class print it: `FOUND /opt/tools/a/scan (in a tool folder)`, `NOT FOUND ...` in red words, `/opt/tools/b missing on the worker`. When the worker has not published it (an older release, or not looked yet) the card says "not known yet" and claims nothing.
