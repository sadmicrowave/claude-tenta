"""Software a class needs: what is missing, how it is fetched for a profile run, and how it is installed for real.

A profile run fetches packages into a THROWAWAY folder as the unprivileged user (apt: download and unpack files only, so no install
script runs; npm: --ignore-scripts; pip: wheels only), runs the command with no network, and deletes the folder unless the command fits.
A registry class installs them for real, as root, only after the administrator approves that exact registry.
Everything that runs a command or looks at the machine is passed in, so the planning can be tested without root or a network.
"""
import json
import os
import re
import shlex
import shutil
import time
from pathlib import Path

from . import envs, refusals
from .pkgspec import npm_split, pip_norm, split_pkgs
from .runner import run_group, tree_mb
from .util import SAFE_PATH
from .words import plural
from .setup.report import sink as _sink

APT_ENV = {"PATH": SAFE_PATH, "LANG": "C", "HOME": "/tmp"}
DEFAULT_ENV_NEED_MB = 200                      # free space wanted before a build starts when the caller does not know better


def card_safe(text):
    """The tool's own words, with only its punctuation changed so the cards do not mistake them for a command: a backtick quote becomes an apostrophe and 'x: npm ...' becomes 'x - npm ...'
    (a command word after a colon is drawn as a code chip below the card, which would cut the real error out of the sentence)."""
    return text.replace("`", "'").replace(": ", " - ").strip().rstrip(".")


class PrepError(Exception):
    """Packages could not be fetched. `kind` is "failed" or "timeout"; `code` is the refusal-catalog entry when the message came from it."""

    def __init__(self, message, kind="failed", code=None):
        super().__init__(message)
        self.kind, self.code = kind, code


def os_release_id():
    """The worker's OS release as one word (part of an environment's hash: the same packages on another release are another environment)."""
    try:
        f = dict(l.split("=", 1) for l in Path("/etc/os-release").read_text().splitlines() if "=" in l)
        return f"{f.get('ID', '').strip(chr(34))}-{f.get('VERSION_ID', '').strip(chr(34))}"
    except OSError:
        return ""


class Acquired:
    """A hold on a shared environment for the life of one job: `rt` is runtime_env(...), `note` is the one line for the result when it had to be rebuilt, `seconds` what the build cost."""

    def __init__(self, key, path, rt, lease, note=None, seconds=0):
        self.key, self.path, self.rt, self.note, self.seconds, self._lease = key, path, rt, note, seconds, lease

    def release(self):
        if self._lease is not None:
            self._lease.close()
            self._lease = None


def version_in(mgr, root, base):
    """The version of an npm or pip package installed under `root` (a throwaway folder or the tools folder), or None."""
    root = Path(root)
    if mgr == "npm":
        try:
            return json.loads((root / "npm" / "node_modules" / base / "package.json").read_text()).get("version")
        except (OSError, ValueError):
            return None
    for x in (root / "pip").glob("*.dist-info") if (root / "pip").is_dir() else []:
        name, _, ver = x.name[:-len(".dist-info")].partition("-")
        if pip_norm(name) == pip_norm(base):
            return ver
    return None


def runtime_env(env):
    """PATH entries and variables that make a throwaway folder's programs and libraries visible to the command."""
    env = Path(env)

    def dirs(*pats):
        out = []
        for pat in pats:
            out += sorted(str(p) for p in env.glob(pat) if p.is_dir())
        return out
    libs = dirs("apt/usr/lib", "apt/lib", "apt/usr/lib/*-linux-gnu", "apt/lib/*-linux-gnu")
    py = dirs("pip", "apt/usr/lib/python3/dist-packages")
    nd = dirs("npm/node_modules", "apt/usr/lib/node_modules", "apt/usr/lib/nodejs", "apt/usr/share/nodejs")
    v = {}
    if libs:
        v["LD_LIBRARY_PATH"] = ":".join(libs)
    if py:
        v["PYTHONPATH"] = ":".join(py)
    if nd:
        v["NODE_PATH"] = ":".join(nd)
    return {"path": dirs("apt/usr/local/bin", "apt/usr/bin", "apt/bin", "npm/node_modules/.bin", "pip/bin"), "vars": v}


def missing_hint(path_dirs, limit=12):
    """When the command is not found after fetching packages, say what the packages DO provide: a package name is often not the name of
    its program (openssh-client gives `ssh`)."""
    names = []
    for d in path_dirs:
        try:
            names += [e.name for e in sorted(Path(d).iterdir()) if e.is_file() and os.access(e, os.X_OK)]
        except OSError:
            pass
    names = sorted(set(names))
    if not names:
        return " The packages fetched no programs at all, so check the package names."
    more = f" (and {len(names) - limit} more)" if len(names) > limit else ""
    return f" Command names are not always package names. The packages provide: {', '.join(names[:limit])}{more}. Use one of those as the command."


class Packages:
    def __init__(self, tools_root, env_root, limits=None, run=run_group, which=None, product="the worker", apply_command="ct-admin apply",
                 step_timeout=300, total_timeout=900, clock=time.time, free_mb=None, os_release=None, manifest_path=None):
        self.tools_root, self.env_root = Path(tools_root), Path(env_root)
        self.clock, self.free_mb = clock, free_mb                       # free_mb(): free disk in MB where the environments live (None: not checked)
        self.os_release = os_release_id() if os_release is None else os_release
        self.envs, self.manifest = envs.EnvStore(self.env_root, clock), envs.AptManifest(manifest_path)
        self.limits = {"max_download_mb": 1000, "max_env_mb": 2000, **(limits or {})}
        self._run = run
        self._which = which or (lambda name: shutil.which(name, path=SAFE_PATH))
        self.product, self.apply_command = product, apply_command
        self.step_timeout, self.total_timeout = step_timeout, total_timeout

    # ---- what is already installed for real ----
    def have(self, mgr, name):
        """Is this npm or pip entry installed in the tools folder? A pinned entry is only satisfied by exactly that version."""
        base = name.split("==")[0] if mgr == "pip" else npm_split(name)[0] if mgr == "npm" else name
        want = name.partition("==")[2] if mgr == "pip" else npm_split(name)[1] if mgr == "npm" else ""
        have = version_in(mgr, self.tools_root, base)
        if want:
            return have == want
        if have is not None:
            return True
        if mgr == "npm":
            return (self.tools_root / "npm" / "node_modules" / base).is_dir()
        p = self.tools_root / "pip"                        # a package folder without dist-info metadata
        return any(pip_norm(x.name) == pip_norm(base) for x in p.glob("*")) if p.is_dir() else False

    def resolve(self, env, pkgs):
        """The requested packages with the versions actually fetched, so a registry entry drafted from a profile run is pinned (npm and pip;
        apt entries stay as named). Anything that cannot be resolved is left as requested."""
        out = set()
        for p in pkgs:
            mgr, _, name = p.partition(":")
            if mgr not in ("npm", "pip"):
                out.add(p)
                continue
            base = npm_split(name)[0] if mgr == "npm" else name.partition("==")[0]
            ver = version_in(mgr, env, base) or version_in(mgr, self.tools_root, base)
            out.add(f"npm:{base}@{ver}" if mgr == "npm" and ver else f"pip:{base}=={ver}" if ver else p)
        return sorted(out)

    # ---- planning (nothing is changed) ----
    def apt_plan(self, names):
        """From a SIMULATED install (no root): (to_install, already_installed, download_mb, installed_mb, problems)."""
        try:
            r = self._run(["apt-get", "-s", "install", "--no-install-recommends", *names], 60, env=APT_ENV)
        except OSError:
            return [], [], 0, 0, ["apt is not available on this machine"]
        if r.returncode != 0:
            errs = [l[2:].strip() for l in (r.stdout + r.stderr).splitlines() if l.startswith("E:")]
            return [], [], 0, 0, ["apt: " + ("; ".join(errs) or "could not plan the install")[:300]]
        inst = [m.group(1) for m in re.finditer(r"(?m)^Inst (\S+)", r.stdout)]
        already = []
        for n in names:
            if n not in inst:
                s = self._run(["dpkg", "-s", n], 10, env=APT_ENV)
                if s.returncode == 0 and "install ok installed" in s.stdout:
                    already.append(n)
                else:
                    return inst, already, 0, 0, [f"apt: {n!r} is neither installed nor installable"]
        dl = ins = 0.0
        if inst:
            s = self._run(["apt-cache", "show", "--no-all-versions", *inst], 30, env=APT_ENV)
            for stanza in s.stdout.split("\n\n"):
                m1, m2 = re.search(r"(?m)^Size: (\d+)", stanza), re.search(r"(?m)^Installed-Size: (\d+)", stanza)
                dl += int(m1.group(1)) / 1e6 if m1 else 0
                ins += int(m2.group(1)) / 1024 if m2 else 0
        return inst, already, round(dl, 1), round(ins, 1), []

    def permanent_commands(self, pkgs):
        """The exact commands that install these packages for real, as root (run after the administrator approves the registry)."""
        by, out = split_pkgs(pkgs), []
        if by.get("apt"):
            out.append(["apt-get", "install", "--no-install-recommends", "-y", *by["apt"]])
        if by.get("npm"):
            out.append(["npm", "install", "--prefix", str(self.tools_root / "npm"), "--ignore-scripts", "--no-audit", "--no-fund", *by["npm"]])
        if by.get("pip"):
            out.append(["python3", "-m", "pip", "install", "--target", str(self.tools_root / "pip"), "--only-binary=:all:", "--no-input",
                        "--disable-pip-version-check", "--upgrade", *by["pip"]])
        return out

    def plan(self, pkgs, jid="<job>"):
        """What a profile run's packages need: what is missing, how big, any problems, and the exact commands."""
        by = split_pkgs(pkgs)
        env = self.env_root / jid
        plan = {"requested": pkgs, "problems": [], "install": [], "already": [], "experiment": [], "permanent": [], "download_mb": 0, "installed_mb": 0}
        if by.get("apt"):
            inst, already, dl, ins, problems = self.apt_plan(by["apt"])
            plan["problems"] += problems
            plan["already"] += [f"apt:{n}" for n in already]
            plan["install"] += [f"apt:{n}" for n in inst]
            plan["download_mb"], plan["installed_mb"] = dl, ins
            if dl > self.limits["max_download_mb"]:
                plan["problems"].append(f"the apt packages would download {dl:.0f} MB; the limit is {self.limits['max_download_mb']} MB")
            if inst:
                plan["experiment"] += [f"apt-get download {' '.join(inst)}",
                                       f"dpkg-deb -x <each downloaded file> {env}/apt   (unpacks files only; no install script runs)"]
        for mgr in ("npm", "pip"):
            names = by.get(mgr) or []
            need = [n for n in names if not self.have(mgr, n)]
            plan["already"] += [f"{mgr}:{n}" for n in names if n not in need]
            if not need:
                continue
            have = self._which("npm" if mgr == "npm" else "python3")
            if mgr == "npm" and not have and "npm" not in by.get("apt", []):
                plan["problems"].append(f"npm is not installed on {self.product}; add apt:nodejs and apt:npm to this run's packages")
            if mgr == "pip":
                r = self._run(["python3", "-m", "pip", "--version"], 15, env=APT_ENV) if have else None
                if (not r or r.returncode != 0) and "python3-pip" not in by.get("apt", []):
                    plan["problems"].append(f"pip is not available on {self.product}; add apt:python3-pip to this run's packages")
            plan["install"] += [f"{mgr}:{n}" for n in need]
            plan["experiment"].append(" ".join(shlex.quote(x) for x in (
                ["npm", "install", "--prefix", f"{env}/npm", "--ignore-scripts", "--no-audit", "--no-fund", "--no-save"] if mgr == "npm" else
                ["python3", "-m", "pip", "install", "--target", f"{env}/pip", "--only-binary=:all:", "--no-input",
                 "--disable-pip-version-check"]) + need))
        plan["permanent"] = [shlex.join(c) for c in self.permanent_commands(plan["install"] or pkgs)]
        return plan

    def plan_text(self, plan):
        """The approval-screen block: what is missing, that it is required, the commands, and what gets undone."""
        if not plan["requested"]:
            return ""
        L, p = [], self.product
        if plan["install"]:
            L += [f"INSTALL REQUIRED. This command needs software {p} does not have, and cannot run without it:", ""]
            for mgr in ("apt", "npm", "pip"):
                names = [x.split(":", 1)[1] for x in plan["install"] if x.startswith(mgr + ":")]
                if names:
                    extra = f"   (download about {plan['download_mb']} MB, {plan['installed_mb']} MB on disk)" if mgr == "apt" else ""
                    L.append(f"    {mgr}: {', '.join(names)}{extra}")
            L += ["", f"If you approve, {p} first fetches them into a THROWAWAY folder, with no root and nothing installed",
                  "on the system, using exactly these commands:", ""] + [f"    {c}" for c in plan["experiment"]]
            L += ["", "The command then runs with NO network. If the run does not clearly fit (or fails), the throwaway folder is",
                  f"deleted and nothing is left behind. If it fits, `{self.apply_command}` will later show you this permanent install",
                  "(run as root, only after you approve it):", ""] + [f"    {c}" for c in plan["permanent"]]
        if plan["already"]:
            L += ["", f"Already installed on {p}: " + ", ".join(plan["already"])]
        if plan["problems"]:
            L += ["", "PROBLEMS: " + "; ".join(plan["problems"])]
        return "\n".join(L).strip("\n")

    # ---- fetching into a throwaway folder (unprivileged) ----
    def prepare(self, jid, pkgs, on_step=None, dest=None):
        """Fetch the packages into <env_root>/<jid> (or `dest`) as the unprivileged user. Returns runtime_env(...). Raises PrepError.
        `on_step(text)` is told what is happening, so the worker can show progress."""
        env = Path(dest) if dest else self.env_root / jid
        shutil.rmtree(env, ignore_errors=True)
        env.mkdir(parents=True, mode=0o700)
        (env / "home").mkdir()
        by = split_pkgs(pkgs)
        base = {"PATH": SAFE_PATH, "HOME": str(env / "home"), "LANG": "C.UTF-8"}
        deadline = self.clock() + self.total_timeout

        def step(argv, cwd, e=None):
            if on_step:
                on_step(" ".join(argv)[:80])
            limit = max(10, min(self.step_timeout, int(deadline - self.clock())))
            r = self._run(argv, limit, cwd=str(cwd), env=e or base)
            if r.returncode == 124:
                raise PrepError(f"`{' '.join(argv)[:100]}` took longer than {limit}s, so {self.product} gave up. "
                                "Check that it can reach the package servers.", kind="timeout")
            if r.returncode != 0:
                raise PrepError(f"`{' '.join(argv)[:100]}` failed: {(r.stderr or r.stdout).strip()[-300:]}")
            if tree_mb(env) > self.limits["max_env_mb"]:
                raise PrepError(f"the packages need more than {self.limits['max_env_mb']} MB")
        try:
            if by.get("apt"):
                inst, _, _, _, problems = self.apt_plan(by["apt"])
                if problems:
                    raise PrepError("; ".join(problems))
                if inst:
                    debs, root = env / "apt-debs", env / "apt"
                    debs.mkdir()
                    root.mkdir()
                    step(["apt-get", "download", *inst], debs)
                    for deb in sorted(debs.glob("*.deb")):
                        step(["dpkg-deb", "-x", str(deb), str(root)], env)       # unpack files only; no maintainer script runs
                    shutil.rmtree(debs, ignore_errors=True)
            rt = runtime_env(env)
            pe = {**base, "PATH": ":".join(rt["path"] + [SAFE_PATH]), **rt["vars"]}
            for mgr in ("npm", "pip"):
                names = [n for n in by.get(mgr, []) if not self.have(mgr, n)]
                if not names:
                    continue
                cmd = (["npm", "install", "--prefix", str(env / "npm"), "--ignore-scripts", "--no-audit", "--no-fund", "--no-save"]
                       if mgr == "npm" else
                       ["python3", "-m", "pip", "install", "--target", str(env / "pip"), "--only-binary=:all:", "--no-input",
                        "--disable-pip-version-check"])
                step(cmd + names, env, pe)
            return runtime_env(env)
        except (OSError, PrepError) as e:
            shutil.rmtree(env, ignore_errors=True)               # a half-built environment is never kept: it could be reused with missing packages
            raise PrepError(str(e), kind=getattr(e, "kind", "failed"))

    # ---- shared environments: one per package-set hash, built on first use, reused, removed after keep_days unused ----
    def needs_env(self, pkgs):
        """Does running with these packages need a shared environment? Only when an npm or pip entry is not already in the tools folder (an older install put them there)."""
        by = split_pkgs(pkgs)
        return any(not self.have(m, n) for m in ("npm", "pip") for n in by.get(m, []))

    def _room_for(self, need_mb, label):
        """Before a build: enough free disk, freeing the oldest unused environments first. Raises PrepError (env.rebuild.no_space) when it is still too little."""
        if self.free_mb is None:
            return
        have = self.free_mb()
        if have >= need_mb:
            return
        freed = sum(mb for _, mb in self.envs.evict_lru(need_mb - have))
        have = self.free_mb()
        if have < need_mb:
            r = refusals.refuse("env.rebuild.no_space", label=label, need=int(need_mb), have=int(have), freed=int(freed))
            raise PrepError(str(r), code=r.code)

    def acquire(self, pkgs, on_step=None, label="this class", need_mb=None):
        """The shared environment for these packages, built now if it is missing. Returns an Acquired holding a shared lock until `release()`, so the prune pass skips it while a job
        uses it. A build has its own limit (total_timeout) and is not charged to any job; if it fails nothing is kept and the job is not run (PrepError with a catalog code)."""
        key = envs.env_key(pkgs, self.os_release)
        lease, note, seconds = self.envs.lease(key), None, 0
        try:
            lease.share()                       # held from now until release(): the prune pass skips the environment, even while it is being built
            if not self.envs.complete(key):
                build = self.envs.build_lease(key)
                try:
                    build.exclusive()           # another job may be building the same environment: wait for it, then find it complete
                    if not self.envs.complete(key):
                        tomb = self.envs.tombstone(key)
                        self._room_for(DEFAULT_ENV_NEED_MB if need_mb is None else need_mb, label)
                        began = self.clock()
                        try:
                            self.prepare(None, pkgs, on_step=on_step, dest=self.envs.path(key))
                        except PrepError as e:
                            raise self._build_failed(e, label, tomb)
                        self.envs.mark_complete(key)
                        self.envs.clear_tombstone(key)
                        seconds = max(0, int(self.clock() - began))
                        if tomb:
                            idle = int((self.clock() - (tomb.get("last_used") or self.clock())) // envs.DAY)
                            note = f"the packages for this class were rebuilt (unused for {plural(idle, 'day')}); this run took {seconds} s longer"
                finally:
                    build.close()
            self.envs.touch(key)
            return Acquired(key, self.envs.path(key), runtime_env(self.envs.path(key)), lease, note, seconds)
        except BaseException:
            lease.close()
            raise

    def _build_failed(self, e, label, tomb):
        if e.kind == "timeout":
            r = refusals.refuse("env.rebuild.timeout", label=label, minutes=max(1, self.total_timeout // 60))
        else:
            days = int(((tomb.get("pruned") or 0) - (tomb.get("last_used") or 0)) // envs.DAY) if tomb else 0
            state = f"were removed after {plural(days, 'unused day')} and cannot be rebuilt now" if tomb else "could not be fetched"
            r = refusals.refuse("env.rebuild.failed", label=label, state=state, detail=card_safe(str(e)))
        return PrepError(str(r), kind=e.kind, code=r.code)

    def promote_path(self, path, env):
        """Where a program found in a throwaway folder will live once the packages are installed for real."""
        p, e = str(path), str(env)
        if p.startswith(f"{e}/apt/"):
            return str(Path("/") / p[len(f"{e}/apt/"):])
        if p.startswith((f"{e}/npm/", f"{e}/pip/")):
            return Path(p).name                       # npm and pip programs live in a shared environment now: found by name on the path it provides at run time
        return p

    def drop_env(self, jid):
        shutil.rmtree(self.env_root / jid, ignore_errors=True)

    def prune_old_envs(self, keep_days=envs.DEFAULT_KEEP_DAYS, now=None):
        """Remove shared environments unused for more than `keep_days` (never one in use) and old per-job throwaway folders. Returns an envs.PruneReport."""
        rep = self.envs.prune(keep_days)
        limit = (now or self.clock()) - keep_days * envs.DAY
        for d in self.env_root.iterdir() if self.env_root.exists() else []:
            try:
                if d.name != "shared" and d.is_dir() and d.stat().st_mtime < limit:
                    shutil.rmtree(d, ignore_errors=True)
                    rep.folders += 1
            except OSError:
                pass
        return rep

    # ---- installing for real (root; run by the update tool before it installs a registry) ----
    def install_registry_packages(self, registry, out=None, run_root=None):
        """Make every apt `packages:` entry of the registry exist. Returns 0 on success, 1 on failure (and says which command failed).
        npm and pip entries are NOT installed here: they live in shared environments, built unprivileged on first use and removed after packages.keep_days unused.
        What tenta installs with apt is written to the manifest (a package that was already there is never listed, so it is never a removal candidate)."""
        out = out or _sink()
        import subprocess
        run_root = run_root or (lambda cmd: subprocess.run(cmd, env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"}, timeout=self.total_timeout))
        classes = registry.get("classes") or {}
        wanted_by = {}
        for cname, c in classes.items():
            for pk in c.get("packages") or []:
                if pk.startswith("apt:"):
                    wanted_by.setdefault(pk[4:], []).append(cname)
        miss = [n for n in sorted(wanted_by) if self._run(["dpkg", "-s", n], 10).returncode != 0]
        if miss:
            cmd = ["apt-get", "install", "--no-install-recommends", "-y", *miss]
            out("installing: " + shlex.join(cmd))
            if run_root(cmd).returncode != 0:
                out(f"package install failed: {shlex.join(cmd)}")
                return 1
        if miss or self.manifest.load():
            self.manifest.record(miss, wanted_by, {n: self._installed_mb(n) for n in miss}, self.clock())
        return 0

    def _installed_mb(self, name):
        try:
            r = self._run(["dpkg-query", "-W", "-f=${Installed-Size}", name], 10, env=APT_ENV)
            return round(int(r.stdout.strip() or 0) / 1024, 1) if r.returncode == 0 else 0.0
        except (OSError, ValueError):
            return 0.0

    def apt_leftovers(self, registry):
        """[(name, MB)] of apt packages tenta installed for classes that are no longer in the registry. Reported, never removed here."""
        wanted = {pk[4:] for c in (registry.get("classes") or {}).values() for pk in c.get("packages") or [] if pk.startswith("apt:")}
        return self.manifest.leftovers(wanted)

    def apt_leftover_text(self, left):
        if not left:
            return ""
        names = " ".join(n for n, _ in left)
        return (f"{plural(len(left), 'apt package')} from classes that no longer run {'remains' if len(left) == 1 else 'remain'} on this worker "
                f"({'; '.join(f'{n}, {mb:g} MB' for n, mb in left)}). This release only reports them; if nothing else needs them, remove with: sudo apt-get remove {names}")
