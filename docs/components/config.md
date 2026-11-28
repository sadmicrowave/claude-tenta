# Configuration

## In plain words

Everything that differs between one install and another (the server's name, your GitHub account, how busy the worker may get, whether to turn
IPv6 off, where alerts go) lives in one file, `armada.toml`. You rarely edit it by hand: the setup wizard asks plain questions and writes it. It only
records the answers you chose; everything else follows the built-in defaults, so a new release can improve a default for everyone who did not choose.

Passwords, API keys and alert tokens never go in this file. They go in a separate `secrets.env` that only the right accounts can read.

## Technical specification

### Where it is read from, in order of precedence

1. Built-in defaults.
2. `armada.toml`: `ARM_CONFIG` if set, otherwise `~/.config/armada/armada.toml`, otherwise `/etc/armada/armada.toml`.
3. Environment variables `ARM_<SECTION>_<KEY>`, for example `ARM_SERVER_HOST=worker.example.lan`. Lists are comma-separated.

### Rules

- **Strict.** An unknown section or key is an error, so a typo cannot silently fall back to a default. A key that looks like a secret gets a pointer to `secrets.env`.
- **Every value has a pattern or a range.** Host names, user names, paths, ports, CIDRs, URLs and branch names are all checked. Nothing that reaches a shell
  command, a systemd unit, a sudoers line or a firewall rule is free text. Paths must be absolute with no shell characters.
- **Cross-checks.** `admin_user` and `worker_user` must differ; the code, state and config folders must be three separate folders, none inside another;
  `alerts.url` is required when alerts are on; `isolation.require_netns = false` is refused unless `ARM_ALLOW_UNSAFE=1` is set (for throwaway tests only).
- **Hardware profile.** `hardware.profile` (`old-laptop`, `small-server`, `workstation`, `custom`) fills in `capacity`, `max_load` and `max_temp_c`
  unless you set them.
- **Standard library only** (Python 3.11 or newer for `tomllib`), so it loads on a fresh server before anything is installed.
- **Secrets**: `secrets.env` is `KEY=value` lines. Loading it warns if it is readable by every account.

### Checking and showing

```
python3 -m armada config check [path]
python3 -m armada config show [path]
```

The full list of settings, defaults and allowed values is the generated [configuration reference](../reference/config.md).

### Derived names and paths

Nothing else in the code builds a name by hand: `Config.path(...)` gives every location (installed registry, queue and results clones, state, the
installed revision, the allowed-signers file, the client's folders), and `Config.unit(...)` gives every unit name. Defaults use `{name}`
(`/opt/{name}`, `/var/lib/{name}`, `/etc/{name}`), so two installs with different names never collide.
