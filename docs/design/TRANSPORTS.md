# Transports: how a job reaches a worker and its result comes back

Status: **decided, not built.** Planned for after the first release (0.1.0), together with the several-workers branch (`ORCHESTRATION.md`). Today there is one transport, GitHub. This note records the decision and the reasoning so the work starts from it.

## The decision

Jobs and results travel through git repositories, as they do today. What changes is **where those repositories live and who connects to whom**. Three transports, chosen with a setting, one default:

1. **Direct (default for new installs, after the release).** Each worker holds its own queue and results repositories. Your computer connects to the worker over SSH to deliver jobs and collect results. The worker makes no connection to your computer or to anything else on your LAN.
2. **LAN git server.** One machine on your network hosts the repositories over SSH. Workers connect to it. Two ways to run it: plain bare repositories behind `git-shell` (no extra software), or a packaged, hardened Gitea container (needs a container runtime on that machine: Docker Desktop, OrbStack, Podman, or a runner app).
3. **GitHub.** What 0.1.0 does. Nothing listens anywhere; the worker only connects out to github.com.

The administrator can pick any of them. GitHub stays supported because "nothing on my network listens" is the strongest isolation for some people, and because it needs no always-reachable machine.

## Decisions for 0.2 (2026-11-17)

1. **Finding a worker:** a name or address in the configuration (`server.host`), as today. mDNS discovery for a computer that moves between networks is a **later enhancement**, not part of 0.2.
2. **A worker that is off or unreachable:** `ct-run submit` fails with a clear message. No outbox in 0.2 (store and forward stays an open question).
3. **State and alerts:** the worker never needs to reach your LAN for them. Each worker writes its own state (`worker-state.json`, `security.json`) into its results repository, and the computer collects it; the computer raises the alerts it sees. This is the state-polling the several-workers scheduler needs, so it is built once and shared. Who moves the state depends on the transport, so the strain on the host does not grow with the number of workers:
   - **GitHub and LAN git server:** workers already push to a shared place, so state check-ins flow *to* the host's reading point, and the host reads them with one cheap fetch (device to host, no per-device connection from the host).
   - **Direct:** the worker makes no connection to anything, which is the point of Direct. The host reads each worker's state over the SSH it already opens for jobs, with a per-worker schedule and a back-off for quiet workers, in parallel. Letting a worker connect out to the host would give up Direct's isolation, so it is not done.

## The wizard explains the choice

When more than one transport exists, the wizard asks for the connection method with a short plain-words description of what each one gains and costs, in speed and in security. Not long: one line of gain, one of cost. The first draft of the words, to be kept true to the numbers the product really has:

| Choice | Gains | Costs |
|---|---|---|
| **Direct** (recommended once built) | Fastest: a job is picked up within seconds, and nothing leaves your network. The worker reaches nothing on your network. | The worker's SSH port is open to your computer's address, and the worker must be reachable when you send a job. |
| **LAN git server** | Fast, and the right shape for several workers: one place for all the jobs. | A compromised worker can reach that one server's SSH port, and you keep one more service patched. |
| **GitHub** | Most isolated: nothing on your network listens, and the worker only connects out. Needs no always-on machine of yours. | Slower: a job waits for the worker's next look (every 5 s while busy, up to 90 s idle and 5 min asleep), and jobs and results pass through GitHub, a third party. |

The same words are used in `ct-setup transport` and in `ct-admin workers`, and a test fails if a transport has no description for each of gain and cost.

## Why git stays the data model

A transport is only a place to keep and move repositories. Everything that makes the system safe sits above it and does not change:

- Approvals, the registry and control orders are **signed on your computer and verified by the worker** against a key it was set up with. A transport that is tampered with cannot forge them. The worst a hostile transport can do is read, delay, delete or withhold; it cannot make a worker run something you did not approve.
- Job files are pinned by SHA-256, results pass the output guard, and a result is kept on the worker until its publish succeeds.
- Git gives atomic pushes (a job is claimed once), history, and branch separation (`jobs`, `approvals`, `control`, `main`) for free. A network share has none of that and a weak story for locking, which is why a plain share was rejected (see "Alternatives we rejected").

## Threat model

The worker is the **less trusted** device: it runs jobs, and a job could escape its jail. The administrator's computer is the most trusted. A transport is judged by what a compromised worker can reach through it.

| | Direct | LAN git server | GitHub |
|---|---|---|---|
| Who listens | the worker, on the SSH it already runs for you | the server machine, one SSH port (or Gitea's) | nobody |
| Who connects | your computer, out to the worker | every worker, out to the server | every worker and your computer, out to github.com |
| Can a compromised worker reach your computer or other LAN devices through it? | **No.** It makes no connections for the mailbox, so its block on LAN egress stays on | It can reach the server's SSH port and nothing else, with a key that only runs git on its own repositories | No |
| Can it read other workers' jobs and results? | No (each worker has only its own) | Not with per-worker keys and per-repository rights | Not with per-repository deploy keys |
| Where job data lives | your computer and the worker | the server machine | GitHub (a third party) |
| Needs an always-on machine | No | The server must be reachable when a worker publishes (a worker keeps results until it is) | No |
| New software to defend | none | OpenSSH (plain) or Gitea (container) | none |

A different port number is not a defence: what matters is the protocol and the direction of the connection. A hand-written web service on a custom port would be new code to defend and is not offered; a standard git HTTP backend behind a proven web server with client certificates is the only HTTP form worth considering, and only if SSH is not possible.

## Transport 1: Direct

### Shape

Each worker owns two bare repositories, `<state>/queue.git` and `<state>/results.git`, kept by a small dedicated account (`<name>-mail`) that has no shell. The worker's own account reads the queue and writes results through the filesystem.

- **Submit.** `ct-run submit` pushes the job to `queue.git` on the worker over SSH. The worker's fetch pass is a local read of its own repository: no network, so it can run every second or be triggered by a systemd path unit on the repository's refs, giving near-instant pickup.
- **Result.** The worker commits to `results.git` locally. Your computer fetches it over SSH (a short-lived connection, only while you are waiting or running `ct-run jobs`).
- **Approvals, orders and the registry** are pushed to `queue.git` the same way and verified by the worker exactly as today.
- **The release source** (`updates.source`) stays GitHub: it is how you get new versions, a separate thing from the mailbox.

### Hardening

- The only open port on the worker is the SSH one it already has, limited by its firewall to your computer's address (`network.ssh_from`).
- Your computer connects with a dedicated key whose `authorized_keys` line is `restrict,command="git-shell ..."`: no terminal, no forwarding, and only the git commands for the two repositories. The queue accepts pushes only to the branches your computer writes; the results repository is read-only for it. Server-side hooks enforce this, not trust.
- The worker holds **no** keys to anything on the network for this path. There are no deploy keys and no GitHub account to configure.
- Alerts: a worker with no outbound LAN access cannot reach a notifier on your LAN. Today it posts alerts to an HTTPS address. In Direct, either that single HTTPS egress stays allowed, or your computer watches the worker's published security state (`security.json`) and raises the alert itself. The second is the default.

### Behaviour when something is offline

- **Your computer is off:** the worker runs what it has and keeps results. They are collected when your computer next reaches it.
- **The worker is off or unreachable:** `ct-run submit` says so plainly. A later step is an outbox on your computer (store and forward): the job is kept and delivered when the worker returns. Open question below.

### Several workers

Each worker is its own mailbox, so routing is your computer's job, which is where the scheduling logic lives anyway (`ORCHESTRATION.md`): a batch is delivered to one worker, preferences and candidate locks decide which, and `ct-admin workers` reads each worker's `worker-state.json` over the same connection.

## Transport 2: LAN git server

One machine runs the repositories; every worker pulls jobs from it and pushes results to it over SSH. It can be your own computer (it only needs to be reachable when workers connect; a worker keeps its results until it can publish) or a small always-on box.

### 2a. Plain: bare repositories and `git-shell`

An unprivileged account with no shell, one `authorized_keys` line per worker (`restrict`, forced `git-shell`), the queue read-only and results pushable to a single branch, enforced by hooks. No extra software. The administrator's computer connects the same way.

### 2b. Packaged Gitea (container)

A hardened Gitea, shipped as a compose file and a setup step, for people who want a familiar, GitHub-like service without writing the plumbing:

- **Runtime prerequisite.** The server machine needs a container runtime (Docker Desktop, OrbStack, Podman) or a runner app. The wizard checks for one and says what is missing; it never installs one.
- **Hardening shipped as defaults.** Rootless container; image pinned by digest (never `latest`); registration disabled; the web UI off or bound to the LAN address; the HTTP port bound to a LAN address, not `0.0.0.0`; SSH on its own port; per-repository **deploy keys** (Gitea supports read-only and read-write keys, which is exactly the model GitHub gives us); firewall allow-list of the workers' addresses.
- **Setup.** `ct-setup transport lan` creates the repositories and keys through Gitea's API, the same way the wizard uses `gh` for GitHub.
- **Cost, honestly.** It is a large service to keep patched. An image digest is pinned for each release and updating it is a deliberate step, never automatic. If that is more than a person wants to own, 2a or Direct is the better choice.

## Transport 3: GitHub

Unchanged. It remains the default until the other transports exist and have passed the end-to-end test.

## Choosing and changing

A setting, asked once by the wizard with its risks in plain words:

```toml
[transport]
kind = "direct"        # direct | lan | github
```

`ct-setup transport <kind>` moves an existing install: it shows a preview, copies the repositories with their history (`git push --mirror`), rewrites the remotes on the worker and your computer, and starts the timers again. Approvals stay valid because they are signed and bound to job files, not to where the repositories live. A fleet may mix transports per worker.

**As built (0.2.4):** GitHub to Direct only. The GitHub repositories and the worker's deploy keys are left untouched as the way back; going back is `ct-setup install` answering GitHub, because the worker's GitHub SSH aliases are not written for a Direct install. The key for the mailbox is made on your computer and delivered by the command, and the real-machine run in OrbStack is 0.2.5.

## What has to change in the code

- `gitio.GitIO` (worker) and `ClientRepos` (client) take their remotes from the transport instead of assuming `git@<alias>:<owner>/<repo>.git`. In Direct the worker side is a local path.
- Setup: repository creation (`gh`) becomes a transport step: create over SSH, via Gitea's API, or via `gh`. The deploy-key flow becomes the per-transport key flow.
- Alerts, `worker-state` and the registry publish: every place that assumes "push to GitHub" asks the transport.
- Audit and the sandbox test: "GitHub can be reached over SSH" becomes "the mailbox is reachable" and checks the chosen path.
- `ct-setup rename`: repository names are per transport.
- The end-to-end test already stands in a local `git` user with bare repositories for GitHub; that is the seed of the LAN and Direct scenarios.

## Alternatives we rejected

- **A network share (SMB or NFS) as the mailbox.** A large listening surface with a history of serious flaws, credentials on every worker, every worker able to read and overwrite every other's data, and weak locking and caching for "claim this job once". Faster, but not safer.
- **A custom web server on its own port.** New code to defend; the port number is obscurity, not protection.
- **Keeping only GitHub.** Strongest isolation, but data leaves your network, and its latency and limits are the ceiling (the job-size limits exist because of it).

## Open questions

1. **Store and forward:** an outbox on your computer for jobs sent while a worker is unreachable, and what "submitted" means then. (0.2 fails clearly instead; see Decisions.)
2. **Discovery:** a name or an address in your configuration, or mDNS. A laptop that moves between networks needs the second. (0.2 uses the configured name; mDNS is a later enhancement.)
3. **Notifications without egress** (above): decided: the computer collects state and raises the alerts (see Decisions).
4. **A mixed fleet:** whether one install may have workers on different transports, and how `ct-admin workers` shows it.
5. **Windows hosts** for the administrator's computer: out of scope until asked.

## Lessons carried over from building the first transport

- A shared SSH connection (`ControlMaster`) left a background process holding the output pipes of the git command, so every pass hung until it exited. Use short-lived connections for captured git commands.
- Run the end-to-end scenario for each transport before calling it done: three real bugs found by it never showed in unit tests.

## Phasing

1. **0.2.** Introduce the transport interface; GitHub implements it unchanged; add Direct; add the `ct-setup transport` command; extend the end-to-end test with a Direct scenario.
2. **0.3.** LAN git server 2a, then the packaged Gitea (2b). The several-workers scheduler lands on top of Direct.
