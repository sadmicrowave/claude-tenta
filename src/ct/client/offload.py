"""Deciding what is worth running on the worker: is a command already registered or excluded, test-running a new one, and turning the
result into a registry proposal or an exclusion.

  fit        registered / excluded / retry-after-upgrade / unknown, for one exact command (exit 0 / 3 / 5 / 4). Every check is logged locally.
  profile    queue a profiling run: ONE exact command, described by its requester, that the administrator must approve.
  propose    from a profile result: a command that fits becomes a registry PROPOSAL (a branch the administrator reviews and installs);
             one that does not fit becomes an EXCLUSION on the data branch, so it is never offloaded again.

Nothing here has authority: a proposal changes nothing on the worker until the administrator installs it.
"""
import calendar
import hashlib
import json
import re
import shlex
import time
from pathlib import Path

from .. import registry
from ..jobs import PROFILE_DENY
from . import refuse
from .repo import ClientError

CLASS_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
FIT_KEEP_DAYS, FIT_KEEP_MAX = 60, 500                      # the history trims itself: nothing older than this, never more than this
SECRET = re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key|bearer\s|authorization:|-----BEGIN|AKIA[0-9A-Z]{12,}|"
                    r"ghp_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}|[A-Z_]*(KEY|TOKEN|SECRET|PASS)[A-Z_]*=)")
EST_KEYS = ("est_time_s", "est_mem_mb", "est_cpu_cores", "est_disk_mb")
SPEC_KEYS = {"command", "name", "what", "expect", "why", "side_effects", "watch_for", "timeout_s", "package", *EST_KEYS}
PACKAGE_RE = re.compile(r"(apt|npm|pip):[A-Za-z0-9@/._+=!-]+")


def iso_now(clock=time.time):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock()))


def parse_ts(ts):
    try:
        return calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None


# ---- the fit decision ----
def match_run(template, argv):
    """A registry run line matches a command if every token matches (a {param} matches anything). A command given by bare name (python3)
    matches the absolute path the worker resolved it to (/usr/bin/python3)."""
    if len(template) != len(argv):
        return False
    head = template[0] == argv[0] or ("/" not in argv[0] and Path(template[0]).name == argv[0])
    return head and all(t == a or re.fullmatch(r"\{\w+\}", t) for t, a in zip(template[1:], argv[1:]))


def fit_lookup(argv, info, excluded):
    """Pure decision: {'verdict': registered|excluded|retry|unknown, 'class', 'cost', 'tier', 'reasons', 'rc'}."""
    now_mb = (info or {}).get("mem_total_mb") or 0
    for e in excluded:
        if e["argv"] == argv:
            pr, why = e.get("profiled", {}), "; ".join(e.get("reasons", []))
            if "memory" in e.get("limiting", []) and now_mb and pr.get("mem_total_mb") and now_mb > pr["mem_total_mb"]:
                return {"verdict": "retry", "reasons": why, "then_mb": pr["mem_total_mb"], "now_mb": now_mb, "rc": 5}
            return {"verdict": "excluded", "reasons": why, "rc": 3}
    for name, c in ((info or {}).get("classes") or {}).items():
        if c.get("run") and match_run(c["run"], argv):
            return {"verdict": "registered", "class": name, "cost": c["cost"], "tier": c["tier"], "rc": 0}
    return {"verdict": "unknown", "rc": 4}


def fit_message(res, argv, product="the worker", submit="ct-run submit", profile="ct-run offload profile"):
    """The one-line answer, worded so a person or Claude can act on it."""
    v = res["verdict"]
    if v == "retry":
        return f"EXCLUDED earlier ({res['reasons']}) when {product} had {res['then_mb']} MB; it has {res['now_mb']} MB now. Worth profiling again: ask the user."
    if v == "excluded":
        return f"EXCLUDED: {res['reasons']}. Do not offload this; run it yourself."
    if v == "registered":
        return f"REGISTERED as class {res['class']} ({res['cost']}, tier {res['tier']}): {submit} {res['class']} --wait"
    return (f"UNKNOWN: not registered and not excluded. If it is slow, repeatable and needs no secrets, propose a profiling run and ask the user to "
            f"approve it: {profile} -- " + " ".join(argv))


# ---- what the worker published and the shared lists ----
def read_exclusions(repos):
    """The advisory exclusion list lives on the data branch (its history is worth keeping); main is the legacy spot."""
    for ref in ("origin/data", "origin/main"):
        txt = repos.show_file(ref, "exclusions.json")
        if txt:
            try:
                return json.loads(txt)
            except ValueError:
                pass
    return {"excluded": []}


def proposed_names(repos):
    """Class names in the registry proposal on main (and so installable), read without a YAML library."""
    text = repos.show_file("origin/main", "registry.yaml")
    names, in_classes = [], False
    for line in (text or "").splitlines():
        if re.match(r"^classes:\s*$", line):
            in_classes = True
        elif in_classes and re.match(r"^\S", line) and not line.startswith("#"):
            in_classes = False
        elif in_classes:
            m = re.match(r"^  ([a-z0-9][a-z0-9-]*):\s*$", line)
            if m:
                names.append(m.group(1))
    return names


def taken_names(repos):
    return set((repos.registry_info() or {}).get("classes", {})) | set(proposed_names(repos))


def check_fit(repos, argv, log=None, source="fit", clock=time.time, by="captured"):
    """Refresh what we know and decide. Logs the check locally when a FitLog is given. Returns the decision dict."""
    t0 = clock()
    repos.pull_results()
    repos.fetch_queue()
    info = repos.registry_info() or {}
    res = fit_lookup(argv, info, read_exclusions(repos).get("excluded", []))
    if log:
        log.record(argv, res, source, int((clock() - t0) * 1000), info.get("mem_total_mb"), by)
    return res


# ---- the fit history ----
class FitLog:
    """Every fit check, kept on this computer only (mode 0600) and trimmed to FIT_KEEP_DAYS / FIT_KEEP_MAX. A command that looks like it
    carries a secret is hidden before it is written."""

    def __init__(self, home, clock=time.time):
        self.path, self.home, self.clock = Path(home) / "fit-log.jsonl", Path(home), clock

    def read(self):
        out = []
        try:
            for line in self.path.read_text().splitlines():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
        except OSError:
            pass
        return out

    def record(self, argv, res, source, ms, mem_mb, by="captured"):
        """Append one line and trim. Never raises: a history problem must not break a fit check."""
        try:
            text = " ".join(argv)
            entry = {"ts": iso_now(self.clock), "command": "(hidden: it looked like it carried a secret)" if SECRET.search(text) else text[:300],
                     "verdict": res["verdict"], "class": res.get("class"), "cost": res.get("cost"), "tier": res.get("tier"),
                     "reasons": (res.get("reasons") or "")[:200] or None, "worker_mb": mem_mb or None, "source": source, "by": by, "ms": ms}
            self.home.mkdir(parents=True, exist_ok=True)
            new = not self.path.exists()
            with self.path.open("a") as f:
                f.write(json.dumps({k: v for k, v in entry.items() if v is not None}) + "\n")
            if new:
                self.path.chmod(0o600)
            self.prune()
        except OSError:
            pass

    def prune(self):
        """Drop entries older than FIT_KEEP_DAYS, then keep only the newest FIT_KEEP_MAX. Returns how many were dropped."""
        rows = self.read()
        cutoff = self.clock() - FIT_KEEP_DAYS * 86400
        keep = [r for r in rows if (parse_ts(r.get("ts")) or 0) >= cutoff][-FIT_KEEP_MAX:]
        if len(keep) != len(rows):
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text("".join(json.dumps(r) + "\n" for r in keep))
            tmp.chmod(0o600)
            tmp.replace(self.path)
        return len(rows) - len(keep)

    def clear(self):
        n = len(self.read())
        self.path.unlink(missing_ok=True)
        return n

    def filtered(self, verdict=None, command=None):
        rows = self.read()
        if verdict:
            rows = [r for r in rows if r.get("verdict") == verdict]
        if command:
            rows = [r for r in rows if command.lower() in r.get("command", "").lower()]
        return rows


# ---- names for proposed classes ----
class NameBook:
    """The name chosen when a profiling run was queued, kept on this computer so `propose` and the job list can use it."""

    def __init__(self, home):
        self.path = Path(home) / "profile-names.json"

    def _read(self):
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def remember(self, jid, name):
        data = self._read()
        data[jid] = name
        for old in list(data)[:-200]:
            del data[old]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=1) + "\n")

    def get(self, jid):
        return self._read().get(jid)


def derive_name(argv):
    """A readable slug for a registry class: the program, then its flags and plain words, in order. `ssh -V` -> ssh-v, `npm run build` ->
    npm-run-build. Values that are paths, numbers or anything with odd characters are left out: they vary between runs, they do not name the command."""
    slug = lambda x: re.sub(r"[^a-z0-9]+", "-", x.lower()).strip("-")           # noqa: E731
    parts = [slug(Path(argv[0]).name)]
    for tok in argv[1:]:
        if tok.startswith("-") and tok.strip("-"):
            word = slug(tok.split("=", 1)[0])
        elif re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", tok):
            word = slug(tok)
        else:
            continue
        if word and word not in parts:
            parts.append(word)
        if len(parts) == 5:
            break
    name = "-".join(p for p in parts if p)
    while len(name) > 38 and "-" in name:                                        # drop whole trailing parts, never cut a word
        name = name.rsplit("-", 1)[0]
    return name[:38].strip("-") or "adopted"


def class_name_problem(name, taken):
    if not CLASS_RE.match(name):
        return "Use lowercase letters, digits and dashes, 2 to 41 characters, starting with a letter or digit."
    if name in taken:
        return "A class with that name is already installed or proposed. Pick a different name."
    return None


def suggest_name(argv, taken, preferred=None):
    """A class name from the command itself, made unique against installed and proposed classes. The same command always gets the same suffix."""
    if preferred and not class_name_problem(preferred, taken):
        return preferred
    base = derive_name(argv)
    if base not in taken:
        return base
    tag = hashlib.sha1(json.dumps(argv).encode()).hexdigest()[:4]
    name, n = f"{base[:33]}-{tag}", 2
    while name in taken:
        name = f"{base[:30]}-{tag}-{n}"
        n += 1
    return name


# ---- profile: build the request ----
def load_spec(text, where="the spec"):
    """A profile spec: plain `key: value` lines (and `flag -x: meaning`). Returns a dict, or raises ClientError."""
    spec = {"flags": {}, "packages": []}
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, val = line.partition(":")
        is_flag = key.lower().startswith("flag ")
        key, val = (key.strip() if is_flag else key.strip().lower().replace("-", "_").replace(" ", "_")), val.strip()
        if not sep:
            raise refuse.error("profile.spec.line", ("Where", f"{where} line {n}"))
        if is_flag:
            spec["flags"][key[5:].strip()] = val
        elif key == "package":
            spec["packages"] += [x for x in re.split(r"[\s,]+", val) if x]
        elif key in SPEC_KEYS:
            spec[key] = val
        else:
            raise refuse.error("profile.spec.field", ("Where", f"{where} line {n}"), ("Field", repr(key)), fields=", ".join(sorted(SPEC_KEYS)))
    return spec


def build_profile_job(argv, what, expect, est, *, flags=None, packages=None, why=None, side_effects=None, watch_for=None, timeout_s=None, files=None,
                      retry_example=None):
    """The profile-run job. Whoever approves it sees these details next to the worker's hard limits, so what, expect and the two honest estimates
    are required. Raises ClientError naming exactly what is missing."""
    if not argv:
        raise refuse.error("profile.command.missing", example="ct-run offload profile --what '...' --expect '...' --est-time-s 10 --est-mem-mb 100 -- eslint .")
    if argv[0].rsplit("/", 1)[-1] in PROFILE_DENY:
        raise refuse.error("profile.command.sudo", ("Command", argv[0]))
    needs, add = [], []
    for ok, need, flag in ((what, "--what  \"what the command does\", in words someone who has never used it would understand", "--what 'what the command does'"),
                           (expect, "--expect  \"how long it should take, how much memory, and what it prints\"", "--expect 'a few seconds, tiny, prints one line'"),
                           (est.get("est_time_s") is not None, "--est-time-s  your honest guess, in seconds", "--est-time-s 10"),
                           (est.get("est_mem_mb") is not None, "--est-mem-mb  your honest guess at peak memory, in MB", "--est-mem-mb 100")):
        if not ok:
            needs.append(need)
            add.append(flag)
    if needs:
        raise refuse.error("profile.details.missing", steps=["Missing: " + n for n in needs] + ["Optional extras: --flag '-x=what it means' (one per flag), --why, --side-effects, --watch-for"],
                           example=(retry_example or (lambda extra: "ct-run offload profile " + extra))(" ".join(add)))
    for p in packages or []:
        if not PACKAGE_RE.fullmatch(p):
            raise refuse.error("profile.package.unreadable", ("Value", p))
    job = {"class": "profile-run", "argv": argv, "what": what, "expect": expect}
    if packages:
        job["packages"] = sorted(set(packages))
    for key, val in (("why", why), ("side_effects", side_effects), ("watch_for", watch_for)):
        if val:
            job[key] = val
    job.update({k: v for k, v in est.items() if v is not None})
    for k, v in (("flags", flags), ("timeout_s", timeout_s), ("files", files)):
        if v:
            job[k] = v
    return job


def parse_flags(pairs):
    flags = {}
    for kv in pairs or []:
        if "=" not in kv:
            raise refuse.error("profile.flag.unreadable", ("Value", kv), example="--flag '-n=use null as the input'")
        k, v = kv.split("=", 1)
        flags[k.strip()] = v.strip()
    return flags


# ---- propose: from a profile result to a proposal or an exclusion ----
def exclusion_entry(res, jid):
    pr = res["profile"]
    return {"argv": pr["argv"], "why": pr.get("why", ""), "verdict": "does-not-fit", "reasons": pr["reasons"], "limiting": pr["limiting"], "metrics": res.get("metrics", {}),
            "profiled": {"job": jid, "date": (res.get("finished") or "")[:10], "mem_total_mb": (res.get("metrics") or {}).get("mem_total_mb")}}


class PackBook(NameBook):
    """How a profiling run's files were packaged (folder, include patterns, files), kept on this computer so `propose` can save it in the class."""

    def __init__(self, home):
        self.path = Path(home) / "profile-packs.json"


class AdoptBook(NameBook):
    """Profile results already offered for automatic adoption on this computer (once each), with the tier used."""

    def __init__(self, home):
        self.path = Path(home) / "profile-adopted.json"


class CommandBook(NameBook):
    """The command line a profiling run was started with, kept on this computer so a result that needs a longer limit can show the exact command to send again."""

    def __init__(self, home):
        self.path = Path(home) / "profile-commands.json"


def make_recipe(dir_arg, includes, file_args, cwd, exclude=None):
    """(recipe, note): the packaging of a profile run, relative to `cwd` so it still means something when someone else runs `ct-run submit`. A folder
    outside `cwd` is not saved (the note says why); no files means no recipe."""
    recipe, note, cwd = {}, None, Path(cwd).resolve()
    if dir_arg:
        try:
            recipe["dir"] = ((cwd / Path(dir_arg).expanduser()).resolve().relative_to(cwd).as_posix() or ".")
        except ValueError:
            return None, f"The folder {dir_arg} is outside the one you are in, so how it was packaged was not saved with the class."
    if includes:
        recipe["include"] = list(includes)
    if exclude:
        recipe["exclude"] = list(exclude)
    rel = []
    for f in file_args or []:
        try:
            rel.append((cwd / Path(f).expanduser()).resolve().relative_to(cwd).as_posix())
        except ValueError:
            return None, f"The file {f} is outside the folder you are in, so how it was packaged was not saved with the class."
    if rel:
        recipe["files"] = rel
    return (recipe or None), note


def with_recipe(entry, jid, packs):
    """`entry` plus the saved packaging of profile run `jid`, only for a class that takes files."""
    recipe = packs.get(jid) if packs else None
    return {**entry, "submit": recipe} if recipe and entry.get("accepts_files") else entry


def use_recipe(recipe, dir_arg, file_args, include, cwd, exclude=None):
    """(dir, files, include, exclude, used): what `ct-run submit` packages. Anything typed wins; the class's saved recipe fills in only when nothing was typed."""
    if dir_arg or file_args or include or exclude or not recipe:
        return dir_arg, file_args, include, exclude, False
    problem = registry.pack_problem(recipe)
    if problem:
        raise refuse.error("pack.recipe.unsafe", reason=problem)
    cwd = Path(cwd)
    return (str(cwd / recipe["dir"]) if "dir" in recipe else None), [str(cwd / f) for f in recipe.get("files", [])] or None, recipe.get("include"), recipe.get("exclude"), True


def read_recipe_files(paths):
    """{file name: text} for the single files a saved recipe names. One that is gone, or not text, is a refusal that names it, never a stack trace."""
    out = {}
    for f in paths or []:
        p = Path(f)
        try:
            out[p.name] = p.read_text()
        except (OSError, UnicodeDecodeError) as e:
            raise refuse.error("pack.recipe.file_missing", ("File", f), reason=f"{getattr(e, 'strerror', None) or 'it is not text'}")
    return out


def class_yaml_lines(name, entry, pr, jid):
    """The registry lines for a proposed class: a comment saying where it came from, then the class itself."""
    lines = [f"  # {pr['what']}" if pr.get("what") else f"  # adopted from profile run {jid}", f"  # profile run {jid}: " + "; ".join(pr["reasons"]), f"  {name}:"]
    for k in ("tier", "cost", "run", "timeout_s", "max_rss_mb", "max_temp_c", "max_disk_mb", "accepts_files", "batchable", "submit", "packages", "env"):
        if k in entry:
            v = entry[k]
            lines.append(f"    {k}: " + (json.dumps(v) if k in ("run", "packages", "env", "submit") else str(v).lower() if isinstance(v, bool) else str(v)))
    if pr.get("files"):
        lines.insert(2, f"  # profiled with input files: {', '.join(pr['files'])} (the command expects files with these names)")
    return lines


def add_class_to_registry(text, lines):
    """Insert the class lines at the end of the `classes:` section of a registry file. Returns the new text."""
    text = text.splitlines()
    try:
        ci = next(i for i, l in enumerate(text) if re.match(r"^classes:\s*$", l))
    except StopIteration:
        raise refuse.error("registry.shape")
    end = next((i for i in range(ci + 1, len(text)) if re.match(r"^\S", text[i]) and not text[i].startswith("#")), len(text))
    while end > ci + 1 and (not text[end - 1].strip() or text[end - 1].startswith("#")):      # a comment above the next section belongs to that section
        end -= 1
    text[end:end] = [""] + lines
    return "\n".join(text) + "\n"


def propose(repos, jid, res, *, name=None, tier="auto", batchable=False, dry_run=False, names=None, packs=None):
    """Act on a finished profile run. Returns a dict describing what happened: {'kind': 'excluded'|'proposed'|'preview', ...}.
    A command that does not fit is recorded as an exclusion; one that fits becomes the branch registry/<name> (never main)."""
    pr = res.get("profile")
    if not pr:
        raise refuse.error("profile.result.not_ready", ("Job", jid), example=f"ct-run result {jid} --wait")
    if pr["verdict"] == "needs-longer":
        raise refuse.error("profile.needs_longer", ("Job", jid), steps=pr["reasons"] + ["Profile it again with a longer limit: the result shows the command."])
    argv, ex = pr["argv"], read_exclusions(repos)
    ex.setdefault("excluded", [])
    if pr["verdict"] == "inconclusive":
        raise refuse.error("profile.inconclusive", steps=pr["reasons"] + ["Run the command by hand to see why it fails, correct it, then profile it again."])
    if pr["verdict"] == "does-not-fit":
        ex["excluded"] = [e for e in ex["excluded"] if e["argv"] != argv] + [exclusion_entry(res, jid)]
        repos.push_files("data", {"exclusions.json": json.dumps(ex, indent=2) + "\n"}, f"exclude: {' '.join(argv)[:60]} (profile {jid})")
        return {"kind": "excluded", "argv": argv, "reasons": pr["reasons"]}
    taken = taken_names(repos)
    name = name or suggest_name(argv, taken, preferred=(names.get(jid) if names else None))
    if not CLASS_RE.match(name):
        raise refuse.error("class.name.invalid", example="--name npm-lint")
    if name in taken:
        raise refuse.error("class.name.taken", ("Class", name))
    entry = with_recipe({**pr["entry"], "tier": tier}, jid, packs)
    if batchable:
        if not entry.get("accepts_files"):
            raise refuse.error("class.batchable.needs_files")
        entry["batchable"] = True
    branch = f"registry/{name}"
    info = {"name": name, "branch": branch, "entry": entry, "tier": tier, "packages": entry.get("packages")}
    if dry_run:
        return {"kind": "preview", "already": repos.remote_branch_exists(branch), **info}
    if repos.remote_branch_exists(branch):
        raise refuse.error("class.proposal.waiting", ("Class", name), ("Branch", branch), example=f"ct-admin apply {branch}", branch=branch)
    text = add_class_to_registry(repos.show_file("origin/main", "registry.yaml") or "", class_yaml_lines(name, entry, pr, jid))
    before = len(ex["excluded"])
    ex["excluded"] = [e for e in ex["excluded"] if e["argv"] != argv]
    if len(ex["excluded"]) != before:                      # a re-profile that now fits lifts the old exclusion
        repos.push_files("data", {"exclusions.json": json.dumps(ex, indent=2) + "\n"}, f"exclude: lift exclusion for {' '.join(argv)[:50]}")
    repos.push_files(branch, {"registry.yaml": text}, f"feat(registry): add {name} class\n\nAdopted from profile run {jid}: " + "; ".join(pr["reasons"]), base="main")
    return {"kind": "proposed", **info}


def rerun_with_limit(raw, seconds):
    """The profile command as it was typed, with its time limit replaced by `seconds`."""
    cut = raw.index("--") if "--" in raw else len(raw)
    head, skip = [], False
    for x in raw[:cut]:
        if skip:
            skip = False
        elif x == "--timeout-s":
            skip = True
        elif not x.startswith("--timeout-s="):
            head.append(x)
    return own_command_with(head + raw[cut:], f"--timeout-s {int(seconds)}")


def own_command_with(argv, extra):
    """The command the person just typed, with `extra` flags added before the `--` (so it can be copied and run as it is)."""
    cut = argv.index("--") if "--" in argv else len(argv)
    return "ct-run " + " ".join(shlex.quote(x) for x in argv[:cut]) + (" " + extra if extra else "") + \
           (" " + " ".join(["--"] + [shlex.quote(x) for x in argv[cut + 1:]]) if cut < len(argv) else "")
