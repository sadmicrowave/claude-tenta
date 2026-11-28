# Root-level actions

## In plain words

Some jobs need to do something only root can: change a fan's speed, restart a service, write a system setting. A job on this worker must never get `sudo`, because a job is
exactly the thing that might be taken over. So there is a different, narrow way: **a root-level action**. You write down one program and the shapes of its arguments
("run `/usr/local/sbin/fan-profile`, with a mode that is one of idle, load or fixed"), pin that program by its fingerprint, and sign it into your registry. A job of a
*privileged class* can then ask for that action, and only that. Each run still needs **your signature on that exact job**, made on your own computer, and nothing the
worker itself says can stand in for it. The action runs in its own locked-down sandbox: the system is read-only to it, it can write only the places you named, and it has
no network unless you said it needs one.

What this is not: it is not a way to run any command as root. A shell, an interpreter or a generic launcher cannot be an action, and the program must be one that only
root can change.

## Technical specification

### The pieces

```
job (class kind: privileged)  ->  worker  ->  unix socket  ->  armada.helper (root)  ->  systemd-run (sandboxed unit)  ->  your program
                                    |                              ^
                          the exact job file + your signed approval, and nothing else is believed
```

- **Action** (registry `actions:` or a pack's `actions/*.yaml`): `run` (a program in a system folder and arguments; only whole-argument placeholders like `"{mode}"`),
  `params` (typed: `enum`, `int [lo, hi]` or `regex`, optionally `optional: true`), `sha256` (the program's pin, required), `writable` (the only paths it may write),
  `network` (default false), `timeout_s` (1 to 600), `description`. Programs that run whatever they are given (shells, interpreters, `env`, `find`, `curl`, `systemd-run` and
  the like) and the privilege tools are refused.
- **Class** `kind: privileged`, `action: <name>`: it takes its program, parameters and limits from the action and cannot set its own `run`, `params`, `network`,
  `packages` or `tier`. It always needs approval.
- **A pack's action** cannot know the hash of a program on your machine. `arm-admin action add` pins it for you; a pack enabled on the server instead needs `action_pins: {name: "<sha256>"}`
  in your registry (`arm-admin action-hash <path>` prints the value). Without the pin it does not install.
- **Registry install** (`arm-update`, `registry-sync`): the root-owned registry is only written if every action's program passes `actions.check_executable` (a regular file
  owned by root and writable by nobody else, in folders that are the same, with the pinned SHA-256), and the policy's refused programs apply to actions too. The helper's socket
  (`<name>-helper.socket`) runs only while the installed registry has an action.

### What the helper checks, every time

`armada.helper` runs as root, one process per connection, and the worker is a stranger to it:

1. The caller must be the worker account (`SO_PEERCRED`), and the socket (`/run/<name>-helper.sock`) is mode 0660 root:worker.
2. The request is the exact job file and the signed approval. The approval must verify against the root-owned `approvers` file (the same key that approves jobs from your
   computer), name this job id and this file's SHA-256, and not be expired. **The worker's own approval markers are never looked at.**
3. The class and action come from the root-owned installed registry, never from the request; the class must be privileged; the parameters must satisfy the action's own spec;
   the argument list is built from the template (no shell).
4. The program on disk is checked again against the pin and its ownership and mode.
5. This machine's systemd must really enforce the sandbox: the helper probes it, and refuses when it does not (a container's systemd often accepts the settings and ignores
   them). `arm-setup audit` shows the same check.
6. A job runs **at most once**: its id is recorded before it runs.
7. It runs in a transient unit: `ProtectSystem=strict`, `ProtectHome=yes`, `PrivateTmp=yes`, `NoNewPrivileges=yes`, `PrivateNetwork=yes` unless `network: true`, a memory cap, a
   runtime limit, and `ReadWritePaths` only for `writable`.

Its output goes back to the worker and through the same output guard as any job (a private address in it quarantines the result and locks the worker down). Every decision is
in the journal.

### Approving

`arm-admin review` and `arm-admin approve` show a banner for such a job: **RUNS AS ROOT**, the exact program and arguments with the job's values substituted, what it may write,
its network and its time limit. Approval must be signed (`arm-admin approve` from your computer); `arm-worker approve` on the worker refuses it, and a job waiting for approval says so.

### Registering one: the same shape as adopting a class

```
arm-admin action add packs/fan-control
```

One command, one question, like `adopt`:

1. It reads the definition (a YAML file, or a pack folder such as `packs/fan-control`).
2. It asks the worker, read-only, for the program's **fingerprint** (its SHA-256 and whether only root could have changed it). You do not look the hash up or type it anywhere.
3. It shows a **RUNS AS ROOT** card: the exact program, its pin, the arguments it takes, the only paths it may write, whether it has a network, its time limit, and
   a line telling you how to read the program before you answer.
4. You say yes once. It writes the registry change on a branch, merges it, signs it with your approval key and the worker installs it. No login to the worker.

Why the program is pinned at all: the worker will run a root program only if its fingerprint is the one you saw in step 3. If the program is edited later, it refuses to run
it until you look again, which is what `arm-admin action add` does when you run it a second time (it shows the new fingerprint and only moves the pin).
`arm-admin action-hash <path>` still prints the fingerprint by itself, for the curious.

Running it again is safe. If the action is already registered and the program has not changed it says so and changes nothing; if the program has changed it offers to pin the new
fingerprint; and a program that is already registered under another name is refused (two names for one root program would be two ways to run it).

To see or take one away:

```
arm-admin action list
arm-admin action remove fan-profile
```

`remove` takes the action's name, or the pack folder it came from, shows what goes (the action and the class that runs it; the program itself is never touched), asks once, then merges, signs
and installs the change. Queued jobs of that class are rejected. A misspelt name lists what is registered and suggests the nearest.

After that a fan job is an ordinary job with a louder warning:

```
arm-run submit fan-profile -p mode=fixed -p rpm=3000
arm-admin approve <job>
```

The approval screen starts with **RUNS AS ROOT** in its own colour (the `root` role in the theme: no other warning looks like it).

`packs/fan-control` wraps a program of your own, `/usr/local/sbin/fan-profile idle | load | fixed <rpm>` (a bounded thermal profiling script for a Mac mini that samples the
temperatures, runs a CPU load, or holds the fan at a fixed speed, and puts the fan daemon back afterwards). Its action writes only `/sys/devices/platform/applesmc.768`, has no
network, and may run for six minutes.

Packs that the server enables itself (`registry.packs` in armada.toml) can carry actions too; those need the admin's `action_pins` (see Packs). `action add` is the simpler road and
does not need the pack to be enabled on the server.

### Limits and honest notes

- The helper is only as safe as the root-owned program you point it at; read it before you pin it. The pin makes sure it is the program you read.
- An action needing more than the sandbox gives (the network, other paths) has to say so, in the definition you signed.
- Only the worker account can open the socket, so this protects against a taken-over *job*; an attacker who is already root on the worker has no need of it.
- A machine whose systemd cannot enforce the sandbox (some containers) will refuse every action; on a real machine this is not an issue. Test machines can set
  `ARM_HELPER_ALLOW_UNENFORCED_SANDBOX=1` for the helper, never a real worker.
