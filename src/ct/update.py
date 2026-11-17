"""Installing a release of the software on the server, safely.

A release is a checkout of the public project (src/ct, templates/). Installing it means copying the `ct` package into <code>/lib, writing the
two small command wrappers, rendering the systemd units and the sudoers rule from the configuration, and recording which revision is
installed. Nothing is built, nothing is downloaded here, and root runs no code from a user's repository: the source tree is given.

Safety rules kept here:
  * Rendering happens first and fails closed (a value that does not match its pattern stops the install before anything is written).
  * Every file is written atomically (a temp file in the same folder, then a rename).
  * A changed unit file is only KEPT if the sandbox test passes; otherwise the previous unit files are put back, nothing is reloaded and the
    revision is not recorded. The code that was installed stays (it does not depend on the unit).
  * The sudoers file is checked with visudo before it is installed, and is never installed unchecked.
  * Files that the new release no longer has are removed from <code>/lib/ct, so a renamed module cannot leave stale code behind.
"""
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import render
from .config import ConfigError
from . import ui as _ui
from .setup.report import sink as _sink

REV_RE = re.compile(r"^[0-9a-f]{7,40}$")


class InstallError(Exception):
    pass


class FetchError(InstallError):
    """The queue repository could not be fetched (network, key or folder permission), as opposed to its registry being wrong."""


@dataclass
class Report:
    rev: str
    updated: list = field(default_factory=list)
    removed: list = field(default_factory=list)
    unchanged: int = 0
    units_changed: bool = False
    rolled_back: bool = False
    errors: list = field(default_factory=list)

    @property
    def ok(self):
        return not self.errors and not self.rolled_back


def _dest(prefix, path):
    return Path(prefix) / str(path).lstrip("/")


def _write_atomic(path, data, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.new")
    tmp.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def desired_files(src, cfg):
    """{absolute install path: (content, mode)} for a release, or raise InstallError / render.RenderError."""
    src = Path(src)
    pkg, templates = src / "src" / "ct", src / "templates"
    if not (pkg / "__init__.py").is_file() or not (templates / "systemd").is_dir():
        raise InstallError(f"{src} is not a release checkout (expected src/ct and templates/)")
    code, files = cfg.paths.code, {}
    for f in sorted(pkg.rglob("*.py")):
        if "__pycache__" not in f.parts:
            files[f"{code}/lib/ct/{f.relative_to(pkg).as_posix()}"] = (f.read_bytes(), 0o644)
    files[render.wrapper_path(cfg)] = (render.cli_wrapper(cfg), 0o755)
    files[f"{render.BIN_DIR}/{render.WORKER_CMD}"] = (render.sudo_wrapper(cfg), 0o755)
    files[render.update_wrapper_path(cfg)] = (render.update_wrapper(cfg), 0o755)
    for name, text in render.unit_files(cfg, templates).items():
        files[f"/etc/systemd/system/{name}"] = (text, 0o644)
    for pack in sorted(set(cfg.registry.packs) | set(cfg.policy.packs)):            # packs the configuration names and this release ships
        root = src / "packs" / pack
        if root.is_dir():
            for f in sorted(p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
                files[f"{cfg.paths.config}/packs/{pack}/{f.relative_to(root).as_posix()}"] = (f.read_bytes(), 0o644)
    return files


def check_sudoers(text):
    """(ok, why): ask visudo whether this sudoers text is valid. A broken sudoers file can lock everyone out of sudo."""
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".sudoers", delete=False) as f:
        f.write(text)
        name = f.name
    try:
        os.chmod(name, 0o440)
        r = subprocess.run(["visudo", "-cf", name], capture_output=True, text=True)
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    except OSError:
        return False, "visudo is not available, so the sudoers file cannot be checked"
    finally:
        os.unlink(name)


def install_release(src, cfg, rev, prefix="/", *, sandbox=None, systemctl=None, visudo=check_sudoers, chown=None, sysctl=None, out=None):
    """Install the release in `src`. `sandbox(unit_text) -> int` (0 = fine) gates changed unit files; `systemctl(*args)` reloads systemd.
    Returns a Report; raises InstallError / render.RenderError before writing anything if the release cannot be rendered."""
    out = out or _sink()
    if not REV_RE.match(str(rev)):
        raise InstallError("the revision must be 7 to 40 hex characters")
    files = desired_files(src, cfg)
    unit_prefix = "/etc/systemd/system/"
    sudoers_path = f"/etc/sudoers.d/{cfg.identity.name}"
    sudoers = render.sudoers_text(cfg, Path(src) / "templates")
    report = Report(rev=rev)
    if sudoers is not None:
        ok, why = visudo(sudoers)
        if ok:
            files[sudoers_path] = (sudoers, 0o440)
        else:
            report.errors.append(f"the sudoers rule is not valid, so it was NOT installed: {why}")
    chown = chown if chown is not None else (lambda p: os.chown(p, 0, 0) if os.geteuid() == 0 else None)
    old_units = {}
    for path, (data, mode) in files.items():
        dest = _dest(prefix, path)
        data_b = data if isinstance(data, bytes) else data.encode("utf-8")
        if dest.exists() and dest.read_bytes() == data_b and (dest.stat().st_mode & 0o7777) == mode:
            report.unchanged += 1
            continue
        if path.startswith(unit_prefix):
            old_units[path] = dest.read_bytes() if dest.exists() else None
            report.units_changed = True
        _write_atomic(dest, data_b, mode)
        chown(dest)
        report.updated.append(path)
    # IPv6 follows the configuration: a sysctl file when it is disabled, none (and the setting put back) when it is enabled
    sysctl_path, ipv6_text = f"/etc/sysctl.d/60-{cfg.identity.name}-ipv6.conf", render.sysctl_text(cfg)
    ipv6_dest = _dest(prefix, sysctl_path)
    if ipv6_text is not None:
        data = ipv6_text.encode()
        if not (ipv6_dest.exists() and ipv6_dest.read_bytes() == data):
            _write_atomic(ipv6_dest, data, 0o644)
            chown(ipv6_dest)
            report.updated.append(sysctl_path)
            if sysctl is not None:
                sysctl("--system")
        else:
            report.unchanged += 1
    elif ipv6_dest.exists():
        ipv6_dest.unlink()
        report.removed.append(sysctl_path)
        if sysctl is not None:
            sysctl("-w", "net.ipv6.conf.all.disable_ipv6=0", "net.ipv6.conf.default.disable_ipv6=0")
    # a sudoers file from an earlier release stays only while the configuration still asks for it
    if sudoers is None:
        stale = _dest(prefix, sudoers_path)
        if stale.exists():
            stale.unlink()
            report.removed.append(sudoers_path)
    libdir, libroot = _dest(prefix, f"{cfg.paths.code}/lib/ct"), f"{cfg.paths.code}/lib/ct/"
    wanted = {p for p in files if p.startswith(libroot)}
    for old in sorted(libdir.rglob("*.py")) if libdir.is_dir() else []:
        rel = libroot + old.relative_to(libdir).as_posix()
        if "__pycache__" not in old.parts and rel not in wanted:
            old.unlink()
            report.removed.append(rel)
    for pack in sorted({p[len(cfg.paths.config + "/packs/"):].split("/")[0] for p in files if p.startswith(cfg.paths.config + "/packs/")}):
        pdir, proot = _dest(prefix, f"{cfg.paths.config}/packs/{pack}"), f"{cfg.paths.config}/packs/{pack}/"
        for old in sorted(pdir.rglob("*")) if pdir.is_dir() else []:
            rel = proot + old.relative_to(pdir).as_posix()
            if old.is_file() and rel not in files:
                old.unlink()
                report.removed.append(rel)
    if report.units_changed and sandbox is not None:
        step_unit = files[f"{unit_prefix}{cfg.identity.name}-step.service"][0]
        out("Testing the unit sandbox before keeping it")
        if sandbox(step_unit) != 0:
            for path, previous in old_units.items():
                dest = _dest(prefix, path)
                if previous is None:
                    dest.unlink(missing_ok=True)
                else:
                    _write_atomic(dest, previous, 0o644)
            report.rolled_back = True
            report.errors.append("a sandbox check failed, so the new unit files were NOT kept (the previous ones were put back and nothing was reloaded)")
            return report
    if report.units_changed and systemctl is not None:
        systemctl("daemon-reload")
    _write_atomic(_dest(prefix, f"{cfg.paths.config}/installed-rev"), rev + "\n", 0o644)
    return report


def install_registry(cfg, src, *, run, prefix="/", install_packages=None, out=None, user_text=None, check_exe=None):
    """Compose and install the job registry the worker will trust: the packs the configuration names (installed by root under
    <config>/packs) plus YOUR classes, limits and profiling rules from registry.yaml on the main branch of the queue repository.

    The queue clone belongs to the worker account and its contents are untrusted, so git runs AS the worker (never as root, whose hooks and
    config it could otherwise influence) and root only receives the file's text. Everything is checked before anything is written: the pack
    manifests, your layer, name collisions, the policy, and the composed result. Packages are installed first; the previous registry is kept as
    registry.yaml.previous. Returns 'unchanged', 'updated' or 'bootstrapped' (nothing was installed before); raises InstallError and writes
    nothing when anything is wrong. `run(argv) -> (returncode, output)`."""
    out = out or _sink()
    from . import packs, registry
    worker, clone, main_branch = cfg.server.worker_user, cfg.path("queue_clone"), cfg.github.main_branch
    dest, text = _dest(prefix, cfg.path("registry")), user_text
    if user_text is None and _dest(prefix, clone + "/.git").exists():             # user_text: already fetched and signature-checked by registry_sync; nothing is fetched again
        as_worker = ["runuser", "-u", worker, "--", "git", "-C", clone]
        rc, msg = run(as_worker + ["fetch", "--quiet", "origin", main_branch])
        empty = rc != 0 and "couldn't find remote ref" in msg           # a brand-new repository with nothing pushed yet is not an error
        if rc and not empty:
            raise FetchError(f"could not fetch the queue repository, so the registry was not updated: {msg.strip()[:200]}")
        rc, body = (1, "") if empty else run(as_worker + ["show", f"origin/{main_branch}:registry.yaml"])
        text = body if rc == 0 else None
    try:
        user = registry.parse(text) if text is not None else {}
    except Exception as e:                                   # not valid YAML
        raise InstallError(f"your registry.yaml is not valid, so nothing was installed: {e}")
    mine = registry.validate(user, cfg.path("tools"), allow_empty=True)
    if mine:
        raise InstallError("your registry.yaml is not valid, so nothing was installed: " + "; ".join(mine[:5]))
    osr = (_dest(prefix, "/etc/os-release").read_text() if _dest(prefix, "/etc/os-release").exists() else "")
    m = re.search(r'^ID="?([^"\n]+)"?$', osr, re.M)
    names = list(dict.fromkeys(list(cfg.registry.packs) + list(cfg.policy.packs)))
    try:
        loaded = packs.load_named(names, [_dest(prefix, f"{cfg.paths.config}/packs")], os_id=m.group(1).lower() if m else None)
        reg, notes = packs.compose(loaded, cfg.registry.packs, cfg.policy.packs, user)
    except packs.PackError as e:
        raise InstallError("the registry was NOT installed: " + "; ".join(e.problems[:6]))
    problems = registry.validate(reg, cfg.path("tools"))
    if problems:
        raise InstallError("the composed registry is not valid, so it was NOT installed: " + "; ".join(problems[:5]))
    if check_exe is None and str(prefix) == "/":
        from . import actions as actions_mod
        check_exe = actions_mod.check_executable
    for aname, spec in (reg.get("actions") or {}).items():           # a root-level action is only installed if its program is exactly the one pinned, and only root could have changed it
        if check_exe is not None:
            ok, why = check_exe(spec["run"][0], spec["sha256"])
            if not ok:
                raise InstallError(f"the root-level action {aname!r} was NOT installed, so the registry was not either: {why}")
    import yaml
    header = (f"# GENERATED by the update tool from packs ({', '.join(f'{n} {loaded[n].version}' for n in names)}) and your registry.yaml.\n"
              "# Do not edit this file: change your registry.yaml in the queue repository, then run the update again.\n")
    composed = header + yaml.safe_dump(reg, sort_keys=False, default_flow_style=False, width=110)
    for n in notes:
        out("NOTE     " + n)
    if dest.exists() and dest.read_text() == composed:
        return "unchanged"
    if install_packages is not None:
        ok, why = install_packages(composed)
        if not ok:
            raise InstallError(f"the packages the registry needs could not be installed, so the registry was NOT installed: {why}")
    existed = dest.exists()
    if existed:
        _write_atomic(dest.with_name(dest.name + ".previous"), dest.read_bytes(), 0o644)
    _write_atomic(dest, composed, 0o644)
    return "updated" if existed else "bootstrapped"


REGISTRY_SYNC_STATUS = "registry-sync.json"      # <config>/registry-sync.json: what the last registry sync did (root writes it; `status` reads it)
REGISTRY_MARK = "registry.source"           # <config>/registry.source: the digest and time of the signed registry.yaml that is installed


def registry_sync(cfg, *, run=None, install_packages=None, now=time.time, out=None, prefix="/", systemctl=None):
    """Install the registry the administrator approved from their own computer, with no login to this worker. Run as root by a timer.

    Nothing from the queue repository is trusted by itself: the registry.yaml on its main branch is installed only when a signature over exactly that text, made
    with the approver key this worker was set up with, sits on the approvals branch, and is no older than the registry already installed. The text that was
    checked is the text that is installed (it is not fetched twice). Returns (status text, ok); ok is False only for something worth a failed unit."""
    out = out or _sink()
    from . import approvals
    run = run or real_run
    worker, clone, main_branch = cfg.server.worker_user, cfg.path("queue_clone"), cfg.github.main_branch
    signers = _dest(prefix, cfg.path("approvers"))
    if not signers.is_file():
        return "no approver key is installed, so signed registry updates are off", True
    if not _dest(prefix, clone + "/.git").exists():
        return "the queue repository has not been cloned yet", True
    as_worker = ["runuser", "-u", worker, "--", "git", "-C", clone]
    rc, msg = run(as_worker + ["fetch", "--quiet", "origin", "+refs/heads/*:refs/remotes/origin/*"])
    if rc:
        return f"could not fetch the queue repository: {msg.strip()[:160]}", True
    rc, text = run(as_worker + ["show", f"origin/{main_branch}:registry.yaml"])
    if rc:
        return "there is no registry.yaml on the queue repository", True
    rc, signed = run(as_worker + ["show", f"origin/{approvals.BRANCH}:{approvals.REGISTRY_PATH}"])
    if rc:
        return "no signed registry has been published yet", True
    mark_path = _dest(prefix, f"{cfg.paths.config}/{REGISTRY_MARK}")
    try:
        mark = json.loads(mark_path.read_text())
    except (OSError, ValueError):
        mark = {}
    ok, why, rec = approvals.check_registry(signed, text, signers, now=now(), not_before=int(mark.get("issued", 0)))
    if not ok:
        again = " On your computer, sign the registry again: ct-admin proposals publish" if why.startswith(("signature does not match", "the signed approval is for a different")) else ""
        return f"refused: {why}.{again}", False
    if mark.get("sha256") == rec["sha256"] and _dest(prefix, cfg.path("registry")).exists():
        sync_helper_socket(cfg, systemctl=systemctl, prefix=prefix)           # idempotent: an install that predates the helper's folder gets it on the next pass
        return "up to date", True
    result = install_registry(cfg, None, run=run, prefix=prefix, install_packages=install_packages, out=out, user_text=text)
    _write_atomic(mark_path, json.dumps({"sha256": rec["sha256"], "issued": rec["issued"]}), 0o644)
    sync_helper_socket(cfg, systemctl=systemctl, prefix=prefix)
    return f"installed the signed registry ({result})", True


def sync_helper_socket(cfg, *, systemctl=None, prefix="/"):
    """Run the root helper's socket only while the installed registry has a root-level action: no action, no root service listening. Returns what it did."""
    systemctl = systemctl or real_systemctl
    try:
        from . import registry
        wanted = bool((registry.parse(_dest(prefix, cfg.path("registry")).read_text()) or {}).get("actions"))
    except Exception:
        wanted = False
    unit = cfg.unit("helper.socket")
    if wanted:                                           # the helper's unit makes this folder writable; systemd refuses to start it at all when the folder is missing
        try:
            folder = _dest(prefix, cfg.path("helper_state"))
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError:
            return "could not create the helper's state folder"
    try:
        systemctl("enable", "--now", unit) if wanted else systemctl("disable", "--now", unit)
    except (subprocess.CalledProcessError, OSError):
        return "could not " + ("start" if wanted else "stop") + f" {unit}"
    return "started" if wanted else "off"


def real_run(argv):
    r = subprocess.run(argv, capture_output=True, text=True, timeout=300)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def real_install_packages(cfg):
    """Install the packages a registry names, using the worker's own tool (it pins and verifies them). Returns (ok, why)."""
    def install(text):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(text)
            name = f.name
        try:
            env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "PYTHONPATH": f"{cfg.paths.code}/lib", "CT_CONFIG": f"{cfg.paths.config}/ct.toml"}
            r = subprocess.run(["/usr/bin/python3", "-m", "ct.cli", "install-packages", name], capture_output=True, text=True, env=env, cwd="/", timeout=900)
            return r.returncode == 0, ((r.stdout or "") + (r.stderr or "")).strip()[-300:]
        finally:
            os.unlink(name)
    return install


def run_git(*args, cwd=None):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=300)
    return r.returncode, (r.stdout + r.stderr).strip()


def fetch_release(cfg, *, verified=False, rev=None, git=run_git, workdir=None, out=None):
    """Get a clean copy of a release from the project repository (cfg.updates.source) and return (source folder, full commit id, label).

      verified   the NEWEST tag named release-* whose signature checks against <config>/allowed_signers (tags with no or an untrusted
                 signature are skipped, and said so). Pushing to the repository alone can never change what runs here.
      rev        one exact commit, which must be on cfg.updates.branch (so an unmerged or abandoned commit cannot be installed).
    The source is exported with `git archive`, so no .git folder, hook or config comes with it."""
    out = out or _sink()
    import tempfile
    cfg.require("updates.source")
    src = Path(cfg.paths.state_root + "-src")
    work = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="ct-release-"))
    if not (src / "HEAD").exists():
        src.parent.mkdir(parents=True, exist_ok=True)
        rc, msg = git("clone", "--bare", "--quiet", cfg.updates.source, str(src))
        if rc:
            raise InstallError(f"could not fetch {cfg.updates.source}: {msg[:200]}")
    rc, msg = git("-C", str(src), "fetch", "--quiet", "--prune", "--force", "--tags", cfg.updates.source, "+refs/heads/*:refs/heads/*")
    if rc:
        raise InstallError(f"could not update from {cfg.updates.source}: {msg[:200]}")
    if verified:
        signers = Path(cfg.path("allowed_signers"))
        if not signers.is_file():
            raise InstallError(f"{signers} is missing, so automatic updates are off (it lists the keys whose signed release tags are trusted)")
        rc, tags = git("-C", str(src), "tag", "-l", "release-*", "--sort=-creatordate")
        target = label = None
        for tag in tags.split():
            rc, _ = git("-C", str(src), "-c", "gpg.format=ssh", "-c", f"gpg.ssh.allowedSignersFile={signers}", "verify-tag", tag)
            if rc == 0:
                rc, sha = git("-C", str(src), "rev-parse", f"{tag}^{{commit}}")
                target, label = sha, tag
                break
            out(f"SKIP  ignoring {tag} (not signed by a trusted key)")
        if not target:
            raise InstallError("no release tag signed by a trusted key; nothing to do")
    else:
        if not rev or not REV_RE.match(rev):
            raise InstallError("an explicit revision must be 7 to 40 hex characters")
        rc, sha = git("-C", str(src), "rev-parse", "--verify", "-q", f"{rev}^{{commit}}")
        if rc:
            raise InstallError(f"commit {rev} was not found in {cfg.updates.source}")
        rc, _ = git("-C", str(src), "merge-base", "--is-ancestor", sha, f"refs/heads/{cfg.updates.branch}")
        if rc:
            raise InstallError(f"commit {rev} is not on {cfg.updates.branch}; only commits on that branch can be installed")
        target, label = sha, rev
    work.mkdir(parents=True, exist_ok=True)
    arc = subprocess.run(["git", "-C", str(src), "archive", target], capture_output=True, timeout=300)
    if arc.returncode:
        raise InstallError("could not export the release")
    subprocess.run(["tar", "-x", "-C", str(work)], input=arc.stdout, check=True, timeout=300)
    return work, target, label


def installed_rev(cfg, prefix="/"):
    try:
        return _dest(prefix, f"{cfg.paths.config}/installed-rev").read_text().split()[0]
    except (OSError, IndexError):
        return None


def real_systemctl(*args):
    return subprocess.run(["systemctl", *args], check=True, timeout=60)


def real_sysctl(*args):
    return subprocess.run(["sysctl", *args], check=True, capture_output=True, timeout=60)


def main(argv=None):
    """python3 -m ct.update --verified | --rev <commit> | --local <release checkout> <commit> | --registry-sync | --audit      (run as root)"""
    from . import config, sandboxtest
    argv = list(sys.argv[1:] if argv is None else argv)
    usage = "usage: python3 -m ct.update --verified | --rev <commit> | --local <release checkout> <commit> | --registry-sync | --audit"
    try:
        cfg = config.load()
        skip_sandbox = "--no-sandbox-test" in argv                  # for a test machine whose systemd cannot enforce a sandbox (a container): never on a real worker
        argv = [x for x in argv if x != "--no-sandbox-test"]
        if argv == ["--audit"]:                                      # what ct-admin audit runs on the worker: a read-only check against its own configuration
            from .setup import audit
            from .setup.host import Host
            rows, bad = audit.run(Host(), cfg)
            return 1 if bad else 0
        if argv == ["--registry-sync"]:
            status, ok = registry_sync(cfg, install_packages=real_install_packages(cfg))
            try:
                _write_atomic(Path(cfg.paths.config) / REGISTRY_SYNC_STATUS, json.dumps({"ts": int(time.time()), "ok": ok, "status": status}), 0o644)
            except OSError:
                pass                                                      # reporting must never be the reason a sync fails
            _ui.say("Registry sync", status, tag="OK" if ok else "FAIL", color=_ui.GREEN if ok else _ui.RED, stream=sys.stdout if ok else sys.stderr)
            return 0 if ok else 1
        if argv == ["--verified"] or (len(argv) == 2 and argv[0] == "--rev"):
            src, rev, label = fetch_release(cfg, verified=argv[0] == "--verified", rev=argv[1] if len(argv) == 2 else None)
            _ui.say(f"Installing {label}", ("Release", rev[:9]), tag="INSTALL", color=_ui.PRIMARY, stream=sys.stdout)
        elif len(argv) == 3 and argv[0] == "--local":
            src, rev = argv[1], argv[2]
        else:
            _ui.say("Say what to install", usage, tag="ERROR", color=_ui.RED)
            return 2
        report = install_release(src, cfg, rev, sandbox=None if skip_sandbox else (lambda text: sandboxtest.test_unit(text, cfg)), systemctl=real_systemctl, sysctl=real_sysctl)
        if report.ok:
            result = install_registry(cfg, src, run=real_run, install_packages=real_install_packages(cfg))
            if result != "unchanged":
                report.updated.append(f"{cfg.path('registry')} ({result})")
            helper = sync_helper_socket(cfg)
            if helper != "off":
                report.updated.append(f"{cfg.unit('helper.socket')} ({helper})")
    except (InstallError, render.RenderError, ConfigError) as e:
        _ui.say("The install was refused", str(e), tag="REFUSED", color=_ui.RED)
        return 1
    _ui.say("Installed" if report.ok else "Installed with errors", *[("Updated", p) for p in report.updated], *[("Removed", p) for p in report.removed], *[("Error", e) for e in report.errors],
            tag="DONE" if report.ok else "ERROR", color=_ui.GREEN if report.ok else _ui.RED, stream=sys.stdout if report.ok else sys.stderr)
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
