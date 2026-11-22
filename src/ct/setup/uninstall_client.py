"""ct-setup uninstall --client: take the ct commands off your own computer (scope: docs/design/WINDOWS-UNINSTALL.md).

It removes exactly what `ct-setup client` put here: the links to ct-run, ct-admin, ct-setup and ct-help in ~/.local/bin (only links that point at THESE commands, never a file or a link that is something else), the two
lines added to the shell startup file (the comment and the export, and nothing else in the file), and, unless --keep-data, your configuration (ct.toml, workers.toml, each worker's file) and the folder with the clones
and keys (the approval key and the mailbox keys). It never touches the Python environment the commands live in (it prints the one command that removes that), your GitHub repositories or any computer that is a
worker: remove a worker with `ct-setup uninstall --worker NAME` first."""
import os
import shutil
import sys
from pathlib import Path

from ..admin import workers as workers_mod
from .client import COMMANDS, ClientSetup

MARKER = "# added by ct-setup: the ct-* commands"
RC_FILES = (".zshrc", ".bashrc", ".config/fish/config.fish")


class ClientUninstall:
    def __init__(self, cfg, *, home=None, scripts_dir="auto", say=None, dry_run=True, keep_data=False, confirm=None, workers_file=None):
        self.cfg, self.say, self.dry_run, self.keep_data, self.confirm = cfg, say or (lambda m: None), dry_run, keep_data, confirm
        self.home = Path(home or Path.home())
        self.scripts = ClientSetup.find_scripts_dir() if scripts_dir == "auto" else scripts_dir
        self.cfg_dir = Path(cfg.source).parent if cfg.source else None
        self.workers_file = Path(workers_file) if workers_file else (self.cfg_dir / "workers.toml" if self.cfg_dir else None)
        self.link_dir = self.home / ".local" / "bin"
        self.report = []

    # ---- what there is ----
    def links(self):
        """The ct links in ~/.local/bin that point at this install's commands."""
        out = []
        for c in COMMANDS:
            link = self.link_dir / c
            if link.is_symlink() and self.scripts and os.path.realpath(link) == os.path.realpath(self.scripts / c):
                out.append(link)
        return out

    def rc_lines(self):
        """[(file, text without our two lines)] for each startup file that has our marked lines."""
        out = []
        for rel in RC_FILES:
            rc = self.home / rel
            try:
                lines = rc.read_text().split("\n")
            except OSError:
                continue
            if MARKER not in lines:
                continue
            i = lines.index(MARKER)
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            if str(self.link_dir) in nxt and ("export PATH" in nxt or "fish_add_path" in nxt):
                out.append((rc, "\n".join(lines[:i] + lines[i + 2:])))
        return out

    def data_paths(self):
        """The configuration files and the client folders this computer holds, from its own configuration: never a folder that is not a ct client folder."""
        if self.keep_data or not self.cfg_dir:
            return []
        out = []
        listed = workers_mod.load_list(self.workers_file) if self.workers_file else {}
        for conf in [Path(self.cfg.source), self.workers_file, *map(Path, listed.values())]:
            if conf and conf.is_file():
                out.append(conf)
        home = Path(str(self.cfg.paths.client_home or "")).expanduser()
        if home.is_dir() and home.name and home != self.home and home.parent != Path(home.anchor):
            out.append(home)
        return out

    def rows(self):
        """[(label, value)] for the preview: one row per thing, fixed labels, each path on its own line."""
        out = [("Remove link", str(p)) for p in self.links()] + [("Edit startup file", str(rc)) for rc, _ in self.rc_lines()]
        out += [("Remove", str(p)) for p in self.data_paths()]
        if self.keep_data:
            out.append(("Keep", "your configuration and keys (--keep-data)"))
        return out or [("Nothing to remove", "no commands, startup lines or configuration of this install were found")]

    def environment_command(self):
        """The one command that removes the Python environment these commands live in, or None when it is not a virtual environment of its own (it is never run from here: this process is using it)."""
        prefix = Path(sys.prefix)
        return f"rm -rf {prefix}" if prefix != Path(getattr(sys, "base_prefix", sys.prefix)) else None

    def run_all(self):
        if self.dry_run:
            self.report.append(("removal", "planned", ""))
            return self.report
        if self.confirm and not self.confirm("Remove the ct commands and your configuration from this computer?"):
            self.report.append(("removal", "declined", ""))
            return self.report
        for link in self.links():
            link.unlink()
            self.report.append(("command link", "removed", str(link)))
        for rc, text in self.rc_lines():
            mode = rc.stat().st_mode & 0o777
            rc.write_text(text)
            os.chmod(rc, mode)
            self.report.append(("shell startup file", "updated", str(rc)))
        for p in self.data_paths():
            shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink()
            self.report.append(("data", "removed", str(p)))
        self.report.append(("removal", "removed", ""))
        return self.report
