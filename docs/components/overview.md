# How it fits together

## In plain words

You have a laptop and a spare computer on your home network, the **worker**. Some jobs are slow or heavy: a long test run, a big lint, a build.
You send those to the worker instead of running them on your laptop, and read the answer when it is done.

The two computers never talk to each other directly for this. They leave notes in two **private GitHub repositories** that belong to you, like
a mailbox: one holds the jobs you send (the *queue*), the other holds the answers (the *results*). The worker only ever reaches out to GitHub;
nothing on your network has to accept a connection from the internet.

The worker does not run whatever you send. It has a short list of **job classes** it is willing to run (the *registry*), each with fixed
commands, typed parameters and limits. A job that names something not on the list is refused. Jobs run with **no network access**, under
time, memory and temperature limits, and everything a job prints is **scanned before it is published**. If the scan finds something that
looks like a private address, a key or a token, the result is withheld, the worker **locks itself down**, and your phone gets a short alert.
Anything risky (a command that needs the network, a command you are testing for the first time) waits for **your approval**, and the
approval is tied to the exact job file you looked at.

## Technical specification

### Parts

| Part | Runs on | What it is |
|---|---|---|
| `cw-run` | your computer | queue jobs, read results, test commands (`offload`) |
| `cw-admin` | your computer | review and approve jobs, manage the registry, install releases; reaches the worker over SSH |
| `cw-setup` | both | the wizard: `client`, `server`, `audit` |
| `<name>` (default `claude-ballast`) | the worker | the worker's own engine: `fetch`, `step`, `exec` (run by systemd timers) and the admin commands (`status`, `show`, `review`, `quarantine`, `output` need no password; `approve`, `cancel`, `kill`, `unlock` do) |
| `cw-update` | the worker, as root | installs a release (code, units, wrappers, packs, the composed registry) |
| `canary-watch` | the worker, as root | raises the alarm when auditd sees a decoy credential file being read |

### Data flow

```
 your computer                          GitHub (private)                          the worker
 cw-run submit ──push jobs/<id>.json──▶ queue repo, branch `jobs` ◀──fetch (read-only deploy key)── fetch timer
                                                                      validate against the INSTALLED registry
                                                                      reject | hold for approval | queue
 cw-admin approve ──ssh──▶ `<name> approve <id> --sha256 <digest>` ──▶ approval recorded for that digest
                                                                      step timer starts jobs (capacity, load, heat)
                                                                      exec: no-network namespace, limits, shape + scan output
 cw-run result ◀──pull── results repo ◀──push (write deploy key)───── publish result
```

### Trust boundaries

1. **The registry the worker trusts is the root-owned installed copy**, never the copy in a repository. A repository can only *propose*.
2. **The queue repository is untrusted input.** Every field of a job file is validated; the file is snapshotted once, so later edits to the
   repository copy change nothing.
3. **The results repository is untrusted output.** A deploy key that can write can also read and rewrite history, so the client treats everything
   it reads from there as data.
4. **The worker account has no sudo, no login shell and no root-equivalent groups.** Anything privileged goes through a root-owned entry point that
   the admin runs with `sudo`, which asks for the admin's password.
5. **Approval is bound to content**: it stores the SHA-256 of the exact job file, and the worker refuses if its copy differs.

### Where to read next

- The life of one job: [jobs](jobs.md). What stops bad things: [security](security.md).
- Settings: [config](config.md). Adding job classes and rules: [packs](packs.md).
- Setting it up: [setup](setup.md) and [hardening](hardening.md). Day to day: [client](client.md) and [admin](admin.md). Updating: [updates](updates.md).
