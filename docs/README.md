# Documentation

**Using it**

- [Getting started](getting-started.md): install, set up, first job, day to day.

**How it works** (each page has a plain-words summary, then the technical specification)

- [Overview](components/overview.md): the parts, the data flow, the trust boundaries.
- [Jobs](components/jobs.md): the life of one job, statuses, limits, approval.
- [Security model](components/security.md): what stops what, the guard, lockdown, alerts, known limits.
- [Configuration](components/config.md) and the generated [configuration reference](reference/config.md).
- [Packs](components/packs.md): job classes and off-limits rules, and how they combine.
- [Colours and the look of the output](components/theme.md): one place that decides every colour; change it with one file.
- [Root-level actions](components/privileged.md): how a job may cause something to happen as root, narrowly, and only with your signature.
- [Connections](components/transports.md): Direct or GitHub, what each gains and gives up, moving an install, `ct-admin watch`.
- [Hardening](components/hardening.md): the protections the wizard applies, and how to add one.
- [The setup wizard](components/setup.md): `ct-setup client`, `server`, `audit`.
- [Sending work: ct-run](components/client.md) · [Running it: ct-admin](components/admin.md) · the generated [command reference](reference/commands.md).
- [Releases and updates](components/updates.md).
- [Testing](components/testing.md).

**Project papers**

- [Security review of the original install](REVIEW.md) · [Design](DESIGN.md) · [Command-set decisions](design/COMMANDS.md) · [Several workers](design/SEVERAL-WORKERS.md) · [Transports (design)](design/TRANSPORTS.md).
