"""Building and queueing a job: parameters, the text files it carries, splitting a large set into independent jobs, and its id.

The size limits are not decided here: the worker publishes the limits it actually enforces in registry.json, and these functions use
exactly those, so a job is never sent that the worker would refuse. (Until a worker publishes them, the conservative old defaults apply.)
"""
import base64
import fnmatch
import hashlib
import json
import os
import re
import secrets
import subprocess
import tempfile
import zlib
from datetime import datetime, timezone
from pathlib import Path

from .. import guard, refusals
from .. import jobs
from ..jobs import file_name_problem
from . import refuse
from .repo import ClientError
from ..words import plural

SKIP_DIRS = {"node_modules", "__pycache__", "venv", "dist", "build", "target"}
ID_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"            # no 0/o or 1/l: easy to read out and to type
OLD_FILE_LIMITS = {"max_count": 50, "max_file_kb": 100, "max_total_kb": 300, "max_batches": 6}


def file_limits(info):
    """{'max_count', 'max_file_kb', 'max_total_kb', 'max_batches'} exactly as the worker's installed registry says."""
    given = ((info or {}).get("limits") or {}).get("files") or {}
    return {**OLD_FILE_LIMITS, **{k: v for k, v in given.items() if k in OLD_FILE_LIMITS}}


def glob_match(rel, pat):
    """'src/**/*.js' style globs: ** spans folders (or none), * stays inside one folder; a bare '*.js' matches at any depth."""
    if "/" not in pat:
        rel = rel.rsplit("/", 1)[-1]
    rx, i = "", 0
    while i < len(pat):
        if pat.startswith("**/", i):
            rx, i = rx + "(?:.*/)?", i + 3
        elif pat.startswith("**", i):
            rx, i = rx + ".*", i + 2
        elif pat[i] == "*":
            rx, i = rx + "[^/]*", i + 1
        elif pat[i] == "?":
            rx, i = rx + "[^/]", i + 1
        else:
            rx, i = rx + re.escape(pat[i]), i + 1
    return re.fullmatch(rx, rel) is not None


SECRET_NAMES = (".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore", "*.jks", "id_rsa", "id_rsa.*", "id_dsa", "id_ecdsa", "id_ed25519", "id_ed25519.*", ".netrc", ".npmrc", ".pypirc",
                ".git-credentials", ".htpasswd", "credentials", "credentials.json", "*.tfstate", "*.tfstate.*", "kubeconfig", "secrets.*", "*.secret", "*.secrets")
NOT_SECRET_NAMES = (".env.example", ".env.sample", ".env.template", "*.pub", "*.example", "*.sample", "*.template")      # the same names with nothing in them, or the public half
SECRET_TEXT = ("private_key", "token")                                                                                  # the output scanner's own rules, reused for what is about to be sent


def secret_name(rel):
    base = rel.rsplit("/", 1)[-1].lower()
    return any(fnmatch.fnmatchcase(base, p) for p in SECRET_NAMES) and not any(fnmatch.fnmatchcase(base, p) for p in NOT_SECRET_NAMES)


def named_exactly(rel, include):
    """True when an --include pattern with no wildcard names this file: its whole path or its file name."""
    return any(not set("*?[") & set(pat) and pat in (rel, rel.rsplit("/", 1)[-1]) for pat in include or ())


def safe_git(*args, timeout=120, **kw):
    """Run git for the file collection without trusting the folder it looks at. A repository's own config can name a program git runs (core.fsmonitor and friends), so attaching a checkout you do not trust would run code
    as you: those options are switched off on the command line (which wins over every config file), and GIT_* variables from the environment (a GIT_DIR, a GIT_CONFIG) are dropped."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"})
    return subprocess.run(["git", "-c", "core.fsmonitor=false", "-c", f"core.hooksPath={os.devnull}", "-c", "core.untrackedCache=false", *args], capture_output=True, timeout=timeout, env=env, **kw)


def outermost(names):
    """Sorted, with a name left out when a folder that holds it is already in the list (a folder git reports whole, and a file inside it, is one thing to say)."""
    return sorted(n for n in names if not any(n.startswith(m + "/") for m in names))


TEMPLATES = Path(__file__).parent / "ignore_templates"          # GitHub's language ignore templates (github/gitignore, CC0), vendored: nothing is fetched when a job is sent (scripts/refresh-gitignore renews them)
LANGUAGES = {"Python": ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "Pipfile"), "Node": ("package.json",), "Go": ("go.mod",), "Rust": ("Cargo.toml",), "Ruby": ("Gemfile",),
             "Composer": ("composer.json",), "Maven": ("pom.xml",), "Gradle": ("build.gradle", "build.gradle.kts", "settings.gradle"), "Java": ("pom.xml", "build.gradle"),
             "Kotlin": ("build.gradle.kts",), "Swift": ("Package.swift",), "Dart": ("pubspec.yaml",), "Elixir": ("mix.exs",), "Terraform": ("*.tf",), "Dotnet": ("*.csproj", "*.fsproj"),
             "VisualStudio": ("*.sln", "*.csproj"), "C": ("CMakeLists.txt",), "C++": ("CMakeLists.txt",)}
GLOBALS = ("macOS", "Linux", "Windows", "JetBrains", "VisualStudioCode", "Vim")                 # editor and operating-system junk, always applied with a language


def detect_languages(base):
    """The language templates that apply to a folder, from the files at its top level."""
    return [lang for lang, markers in LANGUAGES.items() if any(next(Path(base).glob(m), None) for m in markers)]


def templates_problems(folder=None):
    """What is wrong with the vendored templates, as a list (empty when they are exactly what scripts/refresh-gitignore wrote): every file is pinned by hash in SHA256SUMS, none may be missing, added or changed. They are
    third-party data the privacy check does not scan, and they decide what is sent, so a template edited to hide files (or to show them) is refused before it is used."""
    folder = Path(folder or TEMPLATES)
    try:
        pinned = {}
        for line in (folder / "SHA256SUMS").read_text().splitlines():
            digest, _, name = line.partition("  ")
            if digest and name:
                pinned[name] = digest
    except OSError:
        return ["SHA256SUMS is missing"]
    problems = []
    present = {p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file() and p.name != "SHA256SUMS"}
    for name in sorted(present - set(pinned)):
        problems.append(f"{name} is not in SHA256SUMS")
    for name, digest in sorted(pinned.items()):
        path = folder / name
        if not path.is_file():
            problems.append(f"{name} is missing")
        elif hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            problems.append(f"{name} changed")
    return problems


def template_candidates(base):
    """(files, ignored, languages) for a folder that is not a git work tree but is recognisably a project (a language marker at its top): git's own matcher is run over the vendored templates, so the rules mean exactly
    what they mean in a repository. None when no language is recognised (the interim rule applies)."""
    langs = detect_languages(base)
    if not langs or templates_problems():
        return None                                                              # no language recognised, or the templates are not what was vendored: the interim rule decides
    text = "\n".join((TEMPLATES / "lang" / f"{lang}.gitignore").read_text() for lang in langs) + "\n" + "\n".join((TEMPLATES / "global" / f"{g}.gitignore").read_text() for g in GLOBALS) + "\n.git/\n"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            exclude, gitdir = Path(tmp) / "exclude", Path(tmp) / "g"
            exclude.write_text(text)
            safe_git("init", "-q", "--bare", str(gitdir), check=True, timeout=30)
            run = lambda *more: safe_git("-c", f"core.excludesFile={os.devnull}", "--git-dir", str(gitdir), "--work-tree", str(base), "ls-files", "-z", "--others", "--exclude-from", str(exclude),    # noqa: E731
                                                "--exclude-standard", *more)
            listed, ignored = run(), run("--ignored", "--directory")
    except (OSError, subprocess.SubprocessError):
        return None
    if listed.returncode != 0:
        return None
    names = lambda raw: outermost({n.rstrip("/") for n in raw.decode("utf-8", "replace").split("\0") if n})      # noqa: E731
    return names(listed.stdout), (names(ignored.stdout) if ignored.returncode == 0 else []), langs


def gitignore_candidates(base):
    """(files, ignored) for a folder that is not a git work tree but has its own `.gitignore`: git's own matcher is run over the folder's `.gitignore` files (nested ones too), so the rules mean exactly what they mean in a
    repository, the default rules still apply on top. Git is only the matcher: the folder need not be a repository. None when there is no `.gitignore` at the top or the matcher cannot run (the default rules then decide)."""
    if not (Path(base) / ".gitignore").is_file() or (Path(base) / ".gitignore").is_symlink():
        return None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            exclude, gitdir = Path(tmp) / "exclude", Path(tmp) / "g"
            exclude.write_text("")
            safe_git("init", "-q", "--bare", str(gitdir), check=True, timeout=30)
            run = lambda *more: safe_git("-c", f"core.excludesFile={os.devnull}", "--git-dir", str(gitdir), "--work-tree", str(base), "ls-files", "-z", "--others", "--exclude-from", str(exclude),    # noqa: E731
                                                "--exclude-standard", *more)
            listed, ignored = run(), run("--ignored", "--directory")
    except (OSError, subprocess.SubprocessError):
        return None
    if listed.returncode != 0:
        return None
    names = lambda raw: outermost({n.rstrip("/") for n in raw.decode("utf-8", "replace").split("\0") if n})      # noqa: E731
    return names(listed.stdout), (names(ignored.stdout) if ignored.returncode == 0 else [])


def git_candidates(base):
    """(files, ignored) when `base` is inside a git work tree: the files git treats as the project (tracked, and untracked but not ignored) and the names of what the project's own ignore rules leave out. None when it is not one."""
    try:
        inside = safe_git("-C", str(base), "rev-parse", "--is-inside-work-tree", text=True, timeout=30)
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return None
        listed = safe_git("-C", str(base), "ls-files", "-z", "--cached", "--others", "--exclude-standard")
        ignored = safe_git("-C", str(base), "ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory")
    except (OSError, subprocess.SubprocessError):
        return None
    if listed.returncode != 0:
        return None
    names = lambda raw: outermost({n.rstrip("/") for n in raw.decode("utf-8", "replace").split("\0") if n})      # noqa: E731
    return names(listed.stdout), (names(ignored.stdout) if ignored.returncode == 0 else [])


class LeftOut:
    """What was NOT sent, and why, so nothing is dropped silently. `excluded`: left out by rule (hidden files and folders, build and dependency folders) as {path: files counted}; the person is told and can name
    one with --include. `unsent`: files that could not be sent (not text, too big, an unusable name, unreadable) as (name, catalog code, text): they make the set incomplete, so the job does not start unless the person
    chooses (--send-incomplete)."""

    def __init__(self):
        self.excluded, self.unsent, self.held, self.warn, self.links = {}, [], [], [], []
        self.source = ""                                                          # which rule decided what was left out, in words

    def exclude(self, path, count=0):
        self.excluded[path] = self.excluded.get(path, 0) + count

    def unsend(self, name, code, **facts):
        self.unsent.append((name, code, str(refusals.refuse(code, name=name, **facts))))

    def link(self, name):
        self.links.append((name, "files.link.skipped", refusals.explain("files.link.skipped", name=name)))

    def hold(self, name):
        self.held.append((name, "files.held.secret_name", refusals.explain("files.held.secret_name", name=name)))

    def hold_text(self, name):
        self.held.append((name, "files.held.secret_content", refusals.explain("files.held.secret_content", name=name)))

    def warn_about(self, name):
        self.warn.append((name, "files.warn.secret_content", refusals.explain("files.warn.secret_content", name=name)))


def by_rule(part):
    return part.startswith(".") or part in SKIP_DIRS


def named_by_include(rel, include):
    """True when an --include pattern NAMES the hidden or skipped part of `rel` literally ('.gitignore', '.github/**'): a wildcard never reaches into a skipped folder."""
    parts = rel.split("/")
    for i, part in enumerate(parts):
        if by_rule(part):
            return any(len(segs := pat.split("/")) >= i + 1 and all(s == p and not set("*?") & set(s) for s, p in zip(segs, parts[:i + 1])) for pat in include or ())
    return False


def walked(base, include, left):
    """The files under `base` when it is not a git folder: hidden files and folders, build output and dependency folders are left out (recorded in `left`) unless an include names them."""
    for root, dirs, names in os.walk(base):
        rel_root = Path(root).relative_to(base).as_posix()
        prefix = "" if rel_root == "." else rel_root + "/"
        keep = []
        for dname in sorted(dirs):
            if by_rule(dname) and not named_by_include(prefix + dname, include):
                left.exclude(prefix + dname)
            else:
                keep.append(dname)
        dirs[:] = keep
        for fname in sorted(names):
            rel = prefix + fname
            if by_rule(fname) and not named_by_include(rel, include):
                left.exclude(rel, 1)
            else:
                yield rel


def collect_dir(d, limits, include=None, left=None, hold_secret_text=False, exclude=None):
    """Text files under `d`. In a git folder the project's own rules decide: tracked files and untracked ones that are not ignored (what it ignores is named in `left`, and `.git` is never part of it). Elsewhere hidden files and
    folders, build output and dependency folders are left out unless an include names them. `include` (glob patterns like 'src/**/*.js') keeps only what the command needs. A file whose name usually holds a secret is held back
    unless an include names it exactly. With `hold_secret_text` (the GitHub transport: the job leaves the person's network) a file whose text looks like a secret is held the same way; otherwise it is sent with a warning. Every file that is left out, held back or cannot be sent is recorded in `left` (a LeftOut), never dropped silently."""
    base = Path(d).expanduser().resolve()
    left = left if left is not None else LeftOut()
    found = git_candidates(base)
    left.source = "this project's own ignore rules" if found is not None else ""
    own = None
    if found is None:
        own = gitignore_candidates(base)                                      # the folder's own .gitignore only ever ADDS to the default rules: it can leave more out, never let in what they keep out
        found = template_candidates(base)
        if found is not None:
            left.source = f"the ignore templates for {', '.join(found[2][:3])}" + (" and others" if len(found[2]) > 3 else "") + " (this folder is not a git repository)"
    if found is not None:
        for name in found[1]:
            left.exclude(name)
    candidates = found[0] if found is not None else walked(base, include, left)
    if own is not None:
        allowed = set(own[0])
        candidates = [c for c in candidates if c in allowed or c.startswith(".git/")]          # git never lists .git itself; the defaults already keep it out unless an include names it
        for name in own[1]:
            left.exclude(name)
        left.source = (left.source + " and " if left.source else "the default rules (not a git repository) and ") + "this folder's own .gitignore"
    files = {}
    for rel in candidates:
        if include and not any(glob_match(rel, pat) for pat in include):
            continue                                                          # the person's own filter: not a file that failed
        if exclude and any(glob_match(rel, pat) for pat in exclude):
            left.exclude(rel, 1)                                          # the person's own removal: it only ever takes files away, and each is told
            continue
        path = base / rel
        if path.is_symlink() or os.path.islink(base / Path(rel).parts[0]):
            left.link(rel)                                                    # a link is never followed: it may point at a file outside the project, whose contents would be sent under the link's name
            continue
        if not path.is_file():
            continue                                                          # tracked but deleted, or not a plain file
        if secret_name(rel) and not named_exactly(rel, include):
            left.hold(rel)
            continue
        problem = file_name_problem(rel)
        if problem:
            left.unsend(rel, problem[0], reason=problem[1])
            continue
        try:
            text = path.read_bytes().decode("utf-8")                       # the file's exact bytes (no newline translation), so what is hashed and sent is what is on disk
        except UnicodeDecodeError:
            left.unsend(rel, "files.not_text")
            continue
        except OSError as e:
            left.unsend(rel, "files.unreadable", reason=e.strerror or "it cannot be read")
            continue
        if len(text) > limits["max_file_kb"] * 1000:
            left.unsend(rel, "files.too_big", limit=limits["max_file_kb"])
            continue
        if any(guard.GUARD_RULES[k].search(text) for k in SECRET_TEXT):
            if hold_secret_text and not named_exactly(rel, include):
                left.hold_text(rel)
                continue
            left.warn_about(rel)
        files[rel] = text
    return files


BIG_SET_BYTES = 1_000_000_000                                                  # Direct has no size limit; above this the set is compressed (unless --no-compress), and the card says so
CODEC = jobs.CODEC


def compress_files(files):
    """{'codec', 'size' (bytes once unpacked), 'data'}: the whole set as one text value that the worker can unpack (jobs.unpack_files). Done AFTER the secret scan, on what is going to be sent."""
    z, size = zlib.compressobj(6), 0
    out = []

    def feed(text):
        nonlocal size
        raw = text.encode()
        size += len(raw)
        out.append(z.compress(raw))

    # the same bytes as json.dumps(files), written one piece at a time so the whole set is never held a second and third time
    feed("{")
    for i, (name, text) in enumerate(files.items()):
        feed(("" if i == 0 else ", ") + json.dumps(name) + ": " + json.dumps(text))
    feed("}")
    out.append(z.flush())
    return {"codec": CODEC, "size": size, "data": base64.b64encode(b"".join(out)).decode()}


class Pack:
    """What happens to the files of a submit: `state` is "plain" (sent as they are), "compressed" (over BIG_SET_BYTES on Direct: `z` is the value to send, `before` and `after` the sizes) or "declined" (the person said --no-compress)."""

    def __init__(self, state="plain", z=None, before=0, after=0):
        self.state, self.z, self.before, self.after = state, z, before, after


def plan_pack(kind, files, info, no_compress, example, worker="the worker"):
    """Decide how a set is sent. Over BIG_SET_BYTES on Direct it is compressed unless the person asked not to; a worker that did not say it can unpack, or that would refuse the unpacked size, is refused HERE,
    before anything is sent, with the command that sends the job again without compression."""
    count, size = totals(files)
    if kind != "direct" or size <= BIG_SET_BYTES:
        return Pack()
    if no_compress:
        return Pack("declined", before=size)
    can = (info or {}).get("compress")
    if not isinstance(can, dict) or can.get("codec") != CODEC:
        raise refuse.error("files.compress.worker_cannot", ("Files", plural(count, "file")), ("Size", size_text(size)), count=plural(count, "file"), size=size_text(size),
                           threshold=size_text(BIG_SET_BYTES), worker=worker, example=example)
    if size > float(can.get("max_unpacked_mb") or 0) * 1_000_000:
        raise refuse.error("files.compress.over_worker_cap", ("Files", plural(count, "file")), ("Size", size_text(size)), count=plural(count, "file"), size=size_text(size),
                           cap=can.get("max_unpacked_mb"), example=example)
    return Pack("compressed", before=size)                    # the caller compresses (it shows progress) and fills in `z` and `after`


def totals(files):
    """(file count, bytes) of what is about to be sent."""
    return len(files), sum(len(v.encode()) for v in files.values())


def size_text(n):
    for unit, step in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if n >= step:
            return f"{n / step:.1f} {unit}"
    return f"{n} bytes"


def check_not_empty(files, left, folder):
    """A folder whose rules leave nothing to send is a mistake to say out loud (a `.gitignore` of `*`, an `--include` that matches nothing, an `--exclude` that removes everything), not a job that runs on no files."""
    if not Path(folder).expanduser().is_dir():
        raise refuse.error("files.dir.missing", ("Folder", str(folder)))
    if files or left.unsent or left.held:
        return
    decided = left.source or "the default rules (hidden files and folders, build output and dependency folders are not sent)"
    raise refuse.error("files.dir.empty", ("Folder", folder), decided=decided, left=plural(len(left.excluded), "item"))


def check_complete(left, allow=False):
    """A set with files that could not be sent is an incomplete set: refuse, unless the person chose to send the rest (--send-incomplete). Left out by rule is not incomplete."""
    if left.unsent and not allow:
        entry = refusals.CATALOG["files.incomplete"]
        raise ClientError(entry.title, str(refusals.refuse("files.incomplete", count=plural(len(left.unsent), "file"))), list(entry.steps), facts=[("Code", entry.code)])


def plan_batches(files, batchable, limits):
    """Split `files` into jobs of at most max_count files each. Returns a list of dicts, or raises ClientError with the reason. The WHOLE set
    must fit the size limit first (nobody sends a huge tree as lots of jobs); splitting by count is allowed only when the class says its
    files are independent (`batchable`) and it needs at most max_batches jobs."""
    max_files, max_total = limits["max_count"], limits["max_total_kb"] * 1000
    total = sum(len(v) for v in files.values())
    if total > max_total:
        raise refuse.error("files.set.too_big", count=plural(len(files), "file"), kb=total // 1000, limit=max_total // 1000)
    if len(files) <= max_files:
        return [files]
    n = -(-len(files) // max_files)
    if not batchable:
        raise refuse.error("files.set.not_batchable", count=plural(len(files), "file"), limit=max_files)
    if n > limits["max_batches"]:
        raise refuse.error("files.set.batches", count=plural(len(files), "file"), jobs=plural(n, "job"), limit=limits["max_batches"])
    names = sorted(files)
    size = -(-len(names) // n)
    return [{k: files[k] for k in names[i:i + size]} for i in range(0, len(names), size)]


def new_job_id(prefix, known=()):
    """`profile-k7m2xq`: the kind of job plus six random characters. Short enough to type; the time is in `status`."""
    known = set(known)
    while True:
        jid = f"{prefix[:40]}-{''.join(secrets.choice(ID_ALPHABET) for _ in range(6))}"
        if jid not in known and not any(k.startswith(jid + "-b") for k in known):
            return jid


def parse_params(pairs):
    """['n=3', 'dir=src'] -> {'n': 3, 'dir': 'src'}; whole numbers become numbers, everything else stays text."""
    params = {}
    for kv in pairs or []:
        if "=" not in kv:
            raise refuse.error("param.unreadable", ("Value", kv), example="-p dir=src")
        k, v = kv.split("=", 1)
        params[k] = int(v) if re.fullmatch(r"-?\d+", v) else v
    return params


def build_job(cls, params=None, prompt=None, files=None):
    job = {"class": cls, "params": params or {}}
    if prompt:
        job["prompt"] = prompt
    if files:
        job["files"] = files
    return job


def job_ids(base, count):
    """One id for a single job; base-b1, base-b2, ... when a set of files was split into independent jobs."""
    return [base] if count == 1 else [f"{base[:60]}-b{i}" for i in range(1, count + 1)]


def stamp(jid, job, clock=lambda: datetime.now(timezone.utc)):
    """The job file as it is pushed: the id, the job, and a UTC creation time (the worker refuses jobs that are too old or from the future)."""
    return {"id": jid, **job, "created": clock().strftime("%Y-%m-%dT%H:%M:%SZ")}


def push_job(repos, jid, job, clock=lambda: datetime.now(timezone.utc)):
    """Jobs live on their own branch so main stays a clean log of code and registry changes. Refuses if the worker has locked itself."""
    locked = repos.pull_results()
    if locked:
        raise refuse.error("placement.locked.submit", example="ct-admin quarantine\nct-admin unlock")
    repos.push_files(repos.jobs_branch, {f"jobs/{jid}.json": json.dumps(stamp(jid, job, clock), indent=2) + "\n"}, f"job: {jid}")
