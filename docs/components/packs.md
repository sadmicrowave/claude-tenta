# Packs: job classes and off-limits rules

## In plain words

A **pack** is a folder that adds things the worker may do, and things it must never do. The **core** pack that comes with the project holds a few
safe starter classes and the basic off-limits rules (no privilege tools, no touching storage or the kernel, no paths that climb out of the job's
folder). You add your own classes in your private queue repository, or share a set of classes as a pack.

Two promises keep packs safe to mix:

1. **Rules can only get stricter.** Every pack's rules are added together. There is no way for a pack to say "allow", to override, or to remove a rule.
2. **Names belong to one place.** If two packs, or a pack and your own classes, define the same class name, the install stops and tells you. Nothing is
   silently replaced.

## Technical specification

### Layout

```
packs/<name>/pack.toml          name, version, description, kinds = ["classes", "policy", "actions"], [requires] os = ["ubuntu", "debian"]
packs/<name>/classes/*.yaml     a `classes:` mapping, nothing else
packs/<name>/policy/*.toml      off-limits rules
```

`pack.toml` is strict: unknown keys, a name that differs from the folder, `kinds` that do not match the folders present, a missing version or description, or a
class defined twice in one pack are all errors. Hidden files are never loaded. Class files may contain only `classes:`; limits and profiling rules belong to the
install, not to a pack.

### Class schema

Same as the registry: `tier` (`auto`/`approve`), `cost` (`light`/`medium`/`heavy`), `run` (argument list with `{param}` placeholders), `params` (`enum`, `int`,
`regex`, optional), `timeout_s`, `max_rss_mb`, `max_temp_c`, `max_disk_mb`, `network`, `accepts_files`, `batchable`, `output_keys`, `output_allow`, `packages`
(pinned), `env` (only `PYTHONPATH` or `NODE_PATH`, only under the tools folder), `kind` (`command`, `profile`). A class that hands its work back as data sets `returns: patch` (needs `accepts_files: true` and `reviewed: true`, optional `patch_max_kb`, default 200): the worker compares the job's folder with the files it was sent and publishes one unified diff inside the result; you read and apply it yourself with `ct-run patch <job>`. See `docs/design/SINGLE-JOB-DEVICES.md`.

### Policy file

```toml
[[deny.command]]            # a program name (no path) that no class may run, and no profile run may use
names  = ["nsenter", "chroot"]
reason = "a job must never gain privileges"
locked = true

[[deny.param]]              # a pattern no parameter value (or profile argument) may match
regex  = '(^|/)\.\.(/|$)'
reason = "paths that climb out of the job's folder are not allowed"

[[require.approval]]        # a class matching `when` always asks first
when   = { network = true }      # keys allowed: network (bool), kind (string), accepts_files (bool)
reason = "the job can reach the network"
```

A rule needs a `reason`. Unknown keys (including any `allow` or `override`) are errors. A built-in baseline (`sudo`, `su`, `doas`, `pkexec`) is always in force.

### Composition

`ct-update` installs the packs the configuration names (`registry.packs`, `policy.packs`) root-owned under `<config>/packs/<name>/`, then composes the
registry the worker will trust:

1. packs' classes, in the order named; a name defined twice is an error naming both packs;
2. your layer: `registry.yaml` on the main branch of your queue repository (your classes, plus optional `limits:` and `profiling:`); redefining a pack's class is an error;
3. policy applied: a denied program in any class is an error; a rule that needs approval sets that class's tier to `approve` and the update prints a note;
4. the result is validated and written as `<config>/registry.yaml` with a header saying it is generated; the previous one is kept as `registry.yaml.previous`.

Nothing is written if any step fails. A pack for another operating system is refused. The queue repository is read through `git` run **as the worker**,
never as root.

### Enforcement at job time

The composed registry carries a `policy:` section. The worker refuses a job whose parameter value or profile argument matches a `deny.param` pattern, and a
profile run of a denied program, and reports the policy's own reason.

### Checking a pack

```
python3 -m ct.packs check <pack folder>...
```

prints each pack's version, counts and SHA-256 digest, or every problem found. The digest covers every file name and content, so what an administrator approves is exactly this.

### Not loadable from packs (yet)

Hardening modules and anything that installs a root-owned unit or script. They run as root, so they need a reviewed, digest-pinned installation step first.

### Actions in a pack

A pack may also carry `actions/*.yaml` (root-level actions, see [Root-level actions](privileged.md)). A pack cannot know the hash of a program on your machine, so the
install refuses its actions until your own registry pins each one (`action_pins`, from `ct-admin action-hash <path>`). `packs/fan-control` is the worked example.
