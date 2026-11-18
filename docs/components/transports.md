# Connections: how jobs and results travel

## In plain words

Your computer and the worker need a place to leave jobs and pick up results. There are two choices, and the wizard asks which one when you install.

| | **Direct** | **GitHub** |
| --- | --- | --- |
| How it works | The worker keeps a mailbox; your computer reaches it over SSH with one key made for that. | Two private repositories on GitHub that only you and the worker can read; both sides connect out to them. |
| Speed | Fastest: a job is picked up within seconds. | A pass every 5 s when busy, 90 s idle, 5 min asleep. |
| What it gives up | The worker's SSH port is open to your computer's address, and the worker must be reachable when you send a job (otherwise sending fails with a clear message; nothing is queued behind your back). | Your job names and results pass through a third party; the worker makes outbound connections. |
| Needs | Your computer can reach the worker (same network, VPN). | A GitHub account; `gh` makes the repositories for you. |

Neither is marked "recommended": Direct is faster and keeps everything on your network, GitHub works from anywhere. Direct has been run end to end on a real machine (see below), GitHub is the one with the longest track record.

## Technical specification

### Direct

- The worker has a mailbox account (`<name>-mail`) with no password. Its one SSH key (yours, made on your computer, no passphrase) is restricted to a forced command: git reading the queue and results, git writing the queue. It cannot get a shell, forward a port or write the results, and a hostile client cannot change what the worker publishes (`src/ct/mailshell.py`, `tests/test_mailshell.py`).
- The worker never connects out. It reads its queue and writes its results as local repositories (`<state>-mail/queue.git`, `results.git`).
- Alerts: a worker with no outbound access cannot post to a phone. `ct-admin watch` runs on your computer, reads what each worker published (`security.json`, `worker-state.json`) and alerts on changes: locked, reminders while locked, unlocked, gone quiet, unreachable, recovered. Each worker has its own schedule; one that does not answer is asked less often each time (up to every 15 minutes) so many workers cost little. Run it from a scheduler of your own, or `ct-admin watch --loop`.

### Moving an installed worker

```bash
ct-setup transport direct            # preview: nothing changes
ct-setup transport direct --apply    # do it
```

It makes the mailbox, copies the history into it (`git push --mirror`), points the worker's clones and yours at it and pauses the job loop only while it does so. Your GitHub repositories and the worker's deploy keys are not touched: they are your way back. Approvals stay valid (they are signed over the job file, not over where the repositories live). Going back to GitHub is `ct-setup install`, answering GitHub; `ct-setup transport github` says so and changes nothing.

### What running it for real found

The first end-to-end run of Direct on a fresh machine found four faults that no unit test had: the mailbox wrapper's clean environment dropped the client's command, the worker's sandbox did not allow writing its mailbox, git's group-shared files are refused by the sandbox, and the worker's umask hid new results from the mailbox account. Each now has a test. Run `scripts/e2e-routes direct` for the Direct route; the `move` route ends on Direct.

### Later

mDNS discovery (a laptop that moves between networks), a LAN git server, the packaged Gitea and the several-workers scheduler: see [Transports (design)](../design/TRANSPORTS.md).
