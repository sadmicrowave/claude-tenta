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

from .pkgspec import npm_split, pip_norm, split_pkgs
from .runner import run_group, tree_mb
from .util import SAFE_PATH

PKG_KEEP_DAYS = 7
APT_ENV = {"PATH": SAFE_PATH, "LANG": "C", "HOME": "/tmp"}


class PrepError(Exception):
    pass


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
                 step_timeout=300, total_timeout=900):
        self.tools_root, self.env_root = Path(tools_root), Path(env_root)
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
    def prepare(self, jid, pkgs, on_step=None):
        """Fetch the packages into <env_root>/<jid> as the unprivileged user. Returns runtime_env(...). Raises PrepError.
        `on_step(text)` is told what is happening, so the worker can show progress."""
        env = self.env_root / jid
        shutil.rmtree(env, ignore_errors=True)
        env.mkdir(parents=True, mode=0o700)
        (env / "home").mkdir()
        by = split_pkgs(pkgs)
        base = {"PATH": SAFE_PATH, "HOME": str(env / "home"), "LANG": "C.UTF-8"}
        deadline = time.time() + self.total_timeout

        def step(argv, cwd, e=None):
            if on_step:
                on_step(" ".join(argv)[:80])
            limit = max(10, min(self.step_timeout, int(deadline - time.time())))
            r = self._run(argv, limit, cwd=str(cwd), env=e or base)
            if r.returncode == 124:
                raise PrepError(f"`{' '.join(argv)[:100]}` took longer than {limit}s, so {self.product} gave up. "
                                "Check that it can reach the package servers.")
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
            shutil.rmtree(env, ignore_errors=True)
            raise PrepError(str(e))

    def promote_path(self, path, env):
        """Where a program found in a throwaway folder will live once the packages are installed for real."""
        p, e = str(path), str(env)
        for sub, dest in (("apt", Path("/")), ("npm", self.tools_root / "npm"), ("pip", self.tools_root / "pip")):
            if p.startswith(f"{e}/{sub}/"):
                return str(dest / p[len(f"{e}/{sub}/"):])
        return p

    def drop_env(self, jid):
        shutil.rmtree(self.env_root / jid, ignore_errors=True)

    def prune_old_envs(self, now=None):
        """Delete throwaway folders older than PKG_KEEP_DAYS. Returns how many."""
        now, n = now or time.time(), 0
        for d in self.env_root.iterdir() if self.env_root.exists() else []:
            try:
                if d.stat().st_mtime < now - PKG_KEEP_DAYS * 86400:
                    shutil.rmtree(d, ignore_errors=True)
                    n += 1
            except OSError:
                pass
        return n

    # ---- installing for real (root; run by the update tool before it installs a registry) ----
    def install_registry_packages(self, registry, out=print, run_root=None):
        """Make every `packages:` entry of the registry exist. Returns 0 on success, 1 on failure (and says which command failed)."""
        import subprocess
        run_root = run_root or (lambda cmd: subprocess.run(cmd, env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"}, timeout=self.total_timeout))
        want = sorted({p for c in (registry.get("classes") or {}).values() for p in c.get("packages") or []})
        by, todo = split_pkgs(want), []
        if by.get("apt"):
            miss = [n for n in by["apt"] if self._run(["dpkg", "-s", n], 10).returncode != 0]
            if miss:
                todo.append(["apt-get", "install", "--no-install-recommends", "-y", *miss])
        for mgr in ("npm", "pip"):
            miss = [n for n in by.get(mgr, []) if not self.have(mgr, n)]
            if miss:
                todo += self.permanent_commands([f"{mgr}:{n}" for n in miss])
        for cmd in todo:
            out("installing: " + shlex.join(cmd))
            if cmd[0] in ("npm", "pip") or cmd[:3] == ["python3", "-m", "pip"]:
                self.tools_root.mkdir(parents=True, exist_ok=True)
            if run_root(cmd).returncode != 0:
                out(f"package install failed: {shlex.join(cmd)}")
                return 1
        if todo:
            for old in self.env_root.glob("*") if self.env_root.exists() else []:
                shutil.rmtree(old, ignore_errors=True)       # the throwaway folders are no longer needed
        return 0
