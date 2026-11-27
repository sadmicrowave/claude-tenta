"""ct-setup uninstall --worker NAME, for a Windows worker (scope: docs/design/WINDOWS-UNINSTALL.md).

From this computer, over SSH with PowerShell: the start task, then the mailbox account, then the Python, rights, worker account and folders (winremove). Then this computer's own side of that worker: its entry in
workers.toml, its configuration file and its client folder. A preview (no --apply) lists every step and changes nothing; --keep-data leaves the state folders on the Windows computer, as on Linux. Your GitHub
repositories, Windows itself, OpenSSH and the administrator account are never touched."""
from . import refuse
import shutil
from pathlib import Path

from ..admin import workers as workers_mod
from . import winmailbox, winremove, wintask, wintools
from .server import SetupError
from .winsetup import WindowsInstaller


def windows_root(cfg):
    """The worker's folder on the Windows computer as a Windows path, from its own configuration (C:/ProgramData/armada/release -> C:\\ProgramData\\armada)."""
    return str(cfg.paths.code).rsplit("/", 1)[0].replace("/", "\\")


class WindowsUninstall:
    def __init__(self, cfg, wfile, *, run=None, say=None, dry_run=True, keep_data=False, confirm=None):
        self.cfg, self.wfile = cfg, Path(wfile)
        self.name, self.host, self.admin = cfg.identity.name, cfg.server.host, cfg.server.admin_user
        self.worker_user, self.port = cfg.server.worker_user, int(cfg.server.ssh_port or 22)
        self.run = run or wintools.ssh_runner(self.admin, self.host, self.port)
        self.say = say or (lambda m: None)
        self.dry_run, self.keep_data, self.confirm = dry_run, keep_data, confirm
        self.root = windows_root(cfg)
        self.report = []

    def mail_account(self):
        return f"{self.name}-mail"

    def rows(self):
        """[(label, value)] for the preview: fixed labels, each name or path on its own line. Static: a preview must never depend on the computer being reachable."""
        out = [("Remove task", "armada-worker"), ("Remove account", self.mail_account()), ("Remove account", self.worker_user), ("Uninstall Python", self.root + "\\tools\\python")]
        out += [("Keep folder", self.root + "\\" + k) for k in winremove.KEEP] if self.keep_data else []
        out += [("Remove folder", self.root)] if not self.keep_data else [("Remove folders", "every other folder under " + self.root)]
        return out + [("Remove entry", str(self.wfile)), ("Remove here", str(self.cfg.source)), ("Keep here" if self.keep_data else "Remove here", str(self.cfg.paths.client_home))]

    def step(self, what, fn):
        try:
            return fn()
        except wintools.WinToolsError as e:
            self.report.append((what, "failed", f"{e.what}: {e.detail}"))
            raise refuse.error("win.removal.step_failed", ("Step", what), ("Computer", self.host), what=e.what, detail=e.detail)

    def run_all(self):
        if self.dry_run:
            self.report.append(("removal", "planned", ""))
            return self.report
        if self.confirm and not self.confirm(f"Remove the {self.name} worker from {self.host}?"):
            self.report.append(("removal", "declined", ""))
            return self.report
        WindowsInstaller(self.cfg.source, self.cfg.paths.client_home, self.name, self.host, self.admin, suggest={"server.ssh_port": self.port}, run=self.run).reach()
        t = self.step("start task", lambda: wintask.remove(self.run, root=self.root, user=self.worker_user))
        self.report.append(("start task", "removed" if t.get("removed") else "already gone", ""))
        m = self.step("mailbox account", lambda: winmailbox.remove(self.run, root=self.root, name=self.name, worker=self.worker_user))
        self.report.append(("mailbox account", "removed" if m.get("removed") else "already gone", ""))
        w = self.step("worker", lambda: winremove.remove(self.run, root=self.root, user=self.worker_user, keep_data=self.keep_data))
        self.report.append(("worker", "removed", f"Python {w.get('python')}; {len(w.get('folders') or [])} folders; account {'removed' if w.get('account') else 'already gone'}"
                            + (f"; kept {', '.join(w['kept'])}" if w.get("kept") else "") + (f"; its profile folder {w['profile_left']} is still held by Windows" if w.get("profile_left") else "")))
        if w.get("profile_left"):                           # Windows still holds the worker's profile (it lets go at the next restart): keep this computer's side so the same command can finish the job
            self.report.append(("removal", "partial", w["profile_left"]))
            return self.report
        self.tidy()
        self.report.append(("removal", "removed", ""))
        return self.report

    def tidy(self):
        """This computer's side. The client folder holds this worker's mailbox key and clones: gone with the worker, unless --keep-data."""
        gone = workers_mod.remove(self.wfile, self.name)
        conf = Path(self.cfg.source) if self.cfg.source else None
        if conf and conf.is_file():
            conf.unlink()
        home = Path(str(self.cfg.paths.client_home or ""))
        if not self.keep_data and home.is_dir() and home.name == self.name and home.parent.name == "workers":          # only this worker's own folder (workers/<name>), never a folder shared with other installs
            shutil.rmtree(home, ignore_errors=True)
        self.report.append(("this computer", "tidied", f"{'removed from ' + self.wfile.name if gone else 'not listed in ' + self.wfile.name}"))
