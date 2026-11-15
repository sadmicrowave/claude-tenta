"""Installing a release of the software on the server, safely.

A release is a checkout of the public project (src/cw, templates/). Installing it means copying the `cw` package into <code>/lib, writing the
two small command wrappers, rendering the systemd units and the sudoers rule from the configuration, and recording which revision is
installed. Nothing is built, nothing is downloaded here, and root runs no code from a user's repository: the source tree is given.

Safety rules kept here:
  * Rendering happens first and fails closed (a value that does not match its pattern stops the install before anything is written).
  * Every file is written atomically (a temp file in the same folder, then a rename).
  * A changed unit file is only KEPT if the sandbox test passes; otherwise the previous unit files are put back, nothing is reloaded and the
    revision is not recorded. The code that was installed stays (it does not depend on the unit).
  * The sudoers file is checked with visudo before it is installed, and is never installed unchecked.
  * Files that the new release no longer has are removed from <code>/lib/cw, so a renamed module cannot leave stale code behind.
"""
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import render
from .config import ConfigError

REV_RE = re.compile(r"^[0-9a-f]{7,40}$")


class InstallError(Exception):
    pass


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
    pkg, templates = src / "src" / "cw", src / "templates"
    if not (pkg / "__init__.py").is_file() or not (templates / "systemd").is_dir():
        raise InstallError(f"{src} is not a release checkout (expected src/cw and templates/)")
    code, files = cfg.paths.code, {}
    for f in sorted(pkg.rglob("*.py")):
        if "__pycache__" not in f.parts:
            files[f"{code}/lib/cw/{f.relative_to(pkg).as_posix()}"] = (f.read_bytes(), 0o644)
    files[render.wrapper_path(cfg)] = (render.cli_wrapper(cfg), 0o755)
    files[f"{render.BIN_DIR}/{cfg.identity.name}"] = (render.sudo_wrapper(cfg), 0o755)
    for name, text in render.unit_files(cfg, templates).items():
        files[f"/etc/systemd/system/{name}"] = (text, 0o644)
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


def install_release(src, cfg, rev, prefix="/", *, sandbox=None, systemctl=None, visudo=check_sudoers, chown=None, sysctl=None, out=print):
    """Install the release in `src`. `sandbox(unit_text) -> int` (0 = fine) gates changed unit files; `systemctl(*args)` reloads systemd.
    Returns a Report; raises InstallError / render.RenderError before writing anything if the release cannot be rendered."""
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
    libdir, libroot = _dest(prefix, f"{cfg.paths.code}/lib/cw"), f"{cfg.paths.code}/lib/cw/"
    wanted = {p for p in files if p.startswith(libroot)}
    for old in sorted(libdir.rglob("*.py")) if libdir.is_dir() else []:
        rel = libroot + old.relative_to(libdir).as_posix()
        if "__pycache__" not in old.parts and rel not in wanted:
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


def run_git(*args, cwd=None):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=300)
    return r.returncode, (r.stdout + r.stderr).strip()


def fetch_release(cfg, *, verified=False, rev=None, git=run_git, workdir=None, out=print):
    """Get a clean copy of a release from the project repository (cfg.updates.source) and return (source folder, full commit id, label).

      verified   the NEWEST tag named release-* whose signature checks against <config>/allowed_signers (tags with no or an untrusted
                 signature are skipped, and said so). Pushing to the repository alone can never change what runs here.
      rev        one exact commit, which must be on cfg.updates.branch (so an unmerged or abandoned commit cannot be installed).
    The source is exported with `git archive`, so no .git folder, hook or config comes with it."""
    import tempfile
    cfg.require("updates.source")
    src = Path(cfg.paths.state_root + "-src")
    work = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="cw-release-"))
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
    """python3 -m cw.update --verified | --rev <commit> | --local <release checkout> <commit>      (run as root)"""
    from . import config, sandboxtest
    argv = list(sys.argv[1:] if argv is None else argv)
    usage = "usage: python3 -m cw.update --verified | --rev <commit> | --local <release checkout> <commit>"
    try:
        cfg = config.load()
        if argv == ["--verified"] or (len(argv) == 2 and argv[0] == "--rev"):
            src, rev, label = fetch_release(cfg, verified=argv[0] == "--verified", rev=argv[1] if len(argv) == 2 else None)
            print(f"installing {label} ({rev[:9]})")
        elif len(argv) == 3 and argv[0] == "--local":
            src, rev = argv[1], argv[2]
        else:
            print(usage, file=sys.stderr)
            return 2
        report = install_release(src, cfg, rev, sandbox=lambda text: sandboxtest.test_unit(text, cfg), systemctl=real_systemctl, sysctl=real_sysctl)
    except (InstallError, render.RenderError, ConfigError) as e:
        print(f"install refused: {e}", file=sys.stderr)
        return 1
    for p in report.updated:
        print(f"UPDATED  {p}")
    for p in report.removed:
        print(f"REMOVED  {p}")
    for e in report.errors:
        print(f"ERROR    {e}", file=sys.stderr)
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
