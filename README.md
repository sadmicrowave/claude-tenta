# claude-worker

Offload slow or heavy jobs from a laptop to a spare Linux machine on your home network, using two private GitHub repos as the mailbox.
Jobs are allow-listed, run with no network and hard limits, and every result is scanned before it leaves the machine.

**Status: design stage.** Nothing here installs yet. See [docs/DESIGN.md](docs/DESIGN.md) for the plan and
[docs/REVIEW.md](docs/REVIEW.md) for the security review that drives it.

Licensed under the Apache License 2.0.
