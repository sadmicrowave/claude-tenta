"""ct-setup rename: give an install a new name, on every computer that carries it.

The install's name is in a lot of places: the folders, the services, the sudo rule, the files the protections wrote, the worker's GitHub aliases, the two
GitHub repositories, the deploy key titles, and your own computer's configuration. One command changes all of them, with a preview first.

The server half (this module's `Rename`, run as root on the worker) moves the folders, writes the new name into the configuration and then lets the
ordinary release installer write every file that is named after the install (units, sudo rule, wrappers), exactly as an update would; it renames the files
the protections wrote, points the worker's GitHub access at the new aliases, removes what is named after the old name, and starts the timers again.
The client half (`rename_command`, on your computer) runs that over SSH, renames the GitHub repositories when they carry the install's name first (the worker's clones are pointed at the new names), then, after the server half, replaces the
deploy keys' titles and rewrites your own configuration and clones. Job history, secrets, keys and the approver key are all kept: nothing is reinstalled.
"""
import json
import re
import shlex
import sys
import tomllib
from pathlib import Path

from .. import config, render
from . import hardening, report as report_mod, toml_out
from .server import SetupError, alias_for, remote_url, ssh_config_text
from .report import sink as _sink


STYLER = report_mod.Styler(styled=sys.stderr.isatty(), stream=sys.stderr)          # the status rows every other setup step prints (plain when nobody is watching)


def render_plan(steps, old, new, *, where):
    """The server's plan as ONE card: a heading per step and its actions beneath, each cut to a line (the full commands are in `--verbose`, never secrets). `steps` is
    [(title, [line])]."""
    from .. import cards, ui
    rows = []
    for title, lines in steps:
        rows += [cards.Heading(title), cards.Block([ln if len(ln) <= 78 else ln[:77] + "…" for ln in lines] or ["(nothing to do)"])]
    ui.say(f"Rename {old} to {new} {where}", *rows, ("Kept", "your job history, secrets, deploy keys, the approver key and every setting. Only the name changes."),
           tag="PREVIEW", color=ui.PRIMARY, stream=sys.stderr)


def with_name(text, new, old=None, legacy=False):
    """The configuration text with identity.name set to `new` (everything else as it was). The installer writes the repositories' names into the file too, as
    `<name>-queue` and `<name>-results`; those that carry the old name follow it (a custom repository name is left alone), or the file would keep pointing at repositories
    that no longer have that name. `old` is the name the file meant, when it does not say (an old settings file that never wrote its name)."""
    raw = tomllib.loads(text or "")
    old = old or (raw.get("identity") or {}).get("name")
    raw.setdefault("identity", {})["name"] = new
    if legacy:                                              # an old file never wrote the worker account's name; the account on the machine keeps its old one
        raw.setdefault("server", {}).setdefault("worker_user", config.LEGACY_WORKER)
    gh = raw.get("github")
    if old and isinstance(gh, dict):
        for key, which in (("queue_repo", "queue"), ("results_repo", "results")):
            if gh.get(key) == f"{old}-{which}":
                gh[key] = f"{new}-{which}"
    return toml_out.dump(raw)


def load_new_config(text, new, old=None, legacy=False):
    clean, problems = config.validate(tomllib.loads(with_name(text, new, old, legacy)), require_alert_url=False)
    if problems:
        raise SetupError("The new name does not make a valid configuration", "\n".join(problems[:5]))
    return config.Config(clean)


class Rename:
    def __init__(self, host, cfg, new, *, source, rev, say=None, dry_run=True, confirm=None, skip_sandbox_test=False):
        say = say or _sink()
        self.host, self.cfg, self.new, self.source, self.rev = host, cfg, new, source, rev
        self.say, self.dry_run, self.confirm = say, dry_run, confirm
        self.old = cfg.identity.name
        self.report = []
        # The settings come from the file this was started with, wherever it is now: an old install keeps them in cw.toml, and a rename that stopped partway has already moved
        # the folder, so the same command run again finds the file under the new folder name and carries on from there.
        source = getattr(cfg, "source", None) or cfg.path("config_file")
        self.legacy = Path(source).name == config.LEGACY_CONFIG
        self.text = host.read(source) or host.read(cfg.path("config_file")) or ""
        self.cfg2 = None
        self.skip_sandbox_test = skip_sandbox_test

    # ---- checks, before anything is touched ----
    def check(self):
        if not config.NAME_RE.match(self.new or ""):
            raise SetupError("That is not a valid install name", "3 to 24 lowercase letters, digits or -, starting with a letter.")
        if self.new == self.old:
            raise SetupError("The install already has that name", f"{self.old} is what its configuration says, so there is nothing to rename.")
        if not self.host.run(["id", self.cfg.server.worker_user]).ok:                  # before anything is touched: a plan written for an account that does not exist stops halfway
            raise SetupError(f"The worker account {self.cfg.server.worker_user} does not exist on this machine", "Nothing was changed.",
                             ["Check which account the worker runs as: ls /home", "Then set it in the configuration: [server] worker_user = \"...\""])
        self.cfg2 = load_new_config(self.text, self.new, self.old, self.legacy)
        for label, before, after in self.moves():
            if self.host.exists(after):
                raise SetupError(f"{after} already exists", "Something else is using the new name's folder, so nothing was changed.")
        for part in render.UNITS:
            if self.host.exists(f"/etc/systemd/system/{self.cfg2.unit(part)}"):
                raise SetupError(f"A service named {self.cfg2.unit(part)} already exists", "Something else is using the new name, so nothing was changed.")
        running = self.host.run(["sh", "-c", f"ls {self.cfg.path('state')}/running 2>/dev/null | head -1"]).out.strip()
        if running:
            raise SetupError("A job is running", f"{running} is still running. Wait for it, or stop it (ct-admin kill), then try again; nothing was changed.")
        if not self.host.exists(f"{self.source}/src/ct/__init__.py"):
            raise SetupError("The release to install under the new name is missing", f"{self.source} is not a release checkout.")

    def moves(self):
        """[(what, old path, new path)] for each folder whose path follows the name (a folder with a custom path is left where it is)."""
        pairs = (("code", self.cfg.paths.code, self.cfg2.paths.code), ("configuration", self.cfg.paths.config, self.cfg2.paths.config),
                 ("state", self.cfg.paths.state_root, self.cfg2.paths.state_root), ("watch state", self.cfg.path("watch_state"), self.cfg2.path("watch_state")),
                 ("release clone", self.cfg.path("source_clone"), self.cfg2.path("source_clone")),
                 ("root-level helper state", self.cfg.path("helper_state"), self.cfg2.path("helper_state")))          # its record of what already ran: losing it would let an old approval run again
        return [(w, a, b) for w, a, b in pairs if a != b and self.host.exists(a)]

    # ---- the plan ----
    def steps(self):
        c, c2, old, new, h = self.cfg, self.cfg2, self.old, self.new, self.host
        A = hardening.Action
        timers = [p for p in render.UNITS if p.endswith((".timer", ".socket")) and h.exists(f"/etc/systemd/system/{c.unit(p)}")]
        enabled = [p for p in timers if h.run(["systemctl", "is-enabled", c.unit(p)]).out.strip() == "enabled"]
        out = [("Stop the services", [A("run", argv=["systemctl", "disable", "--now", c.unit(p)]) for p in timers]
                + [A("run", argv=["systemctl", "stop", c.unit(p)]) for p in render.UNITS if p.endswith(".service") and "@" not in p and h.exists(f"/etc/systemd/system/{c.unit(p)}")])]
        out.append(("Move the folders", [A("run", argv=["mv", a, b]) for _, a, b in self.moves()]))
        new_conf = f"{c2.paths.config}/ct.toml"
        out.append(("Write the new name into the configuration", [A("write", path=new_conf, text=with_name(self.text, new, old, self.legacy), mode=0o644)]))
        w, home = c.server.worker_user, c.paths.worker_home
        ssh = [A("write", path=f"{home}/.ssh/config", text=ssh_config_text(c2), mode=0o600), A("run", argv=["chown", f"{w}:{w}", f"{home}/.ssh/config"])]
        for which in ("queue", "results"):
            clone = c2.path("queue_clone" if which == "queue" else "results_clone")
            ssh.append(A("run", argv=["runuser", "-u", w, "--", "git", "-C", clone, "remote", "set-url", "origin", remote_url(c2, which)]))
        out.append(("Point the worker's GitHub access at the new name", ssh))
        out.append(("Write every file that is named after the install (the same installer an update uses)", [
            A("run", argv=["env", "PYTHONDONTWRITEBYTECODE=1", f"PYTHONPATH={self.source}/src", f"CT_CONFIG={new_conf}", "python3", "-m", "ct.update"] + (["--no-sandbox-test"] if self.skip_sandbox_test else []) + ["--local", self.source, self.rev],
              why="re-renders the services, the sudo rule and the commands for the new name")]))
        files = []
        for before, after in ((f"/etc/sysctl.d/60-{old}-ipv6.conf", f"/etc/sysctl.d/60-{new}-ipv6.conf"), (f"/etc/sysctl.d/61-{old}-userns.conf", f"/etc/sysctl.d/61-{new}-userns.conf"),
                              (f"/etc/audit/rules.d/60-{old}.rules", f"/etc/audit/rules.d/60-{new}.rules"), (f"/etc/ssh/sshd_config.d/10-{old}.conf", f"/etc/ssh/sshd_config.d/10-{new}.conf")):
            body = h.read(before)
            if body is not None:
                files += [A("write", path=after, text=body.replace(old, new), mode=0o644), A("remove", path=before)]
        if any("audit" in a.path for a in files if a.kind == "write"):
            files.append(A("run", argv=["augenrules", "--load"]))
        if files:
            out.append(("Rename the files the protections wrote", files))
        gone = [A("remove", path=f"/etc/systemd/system/{c.unit(p)}") for p in render.UNITS if h.exists(f"/etc/systemd/system/{c.unit(p)}")]
        for stale in (f"/etc/sudoers.d/{old}", f"{c2.paths.code}/bin/{old}"):
            if h.exists(stale):
                gone.append(A("remove", path=stale))
        if self.legacy:                                         # what the old version wrote under names the new one does not use
            gone.append(A("remove", path=f"{c2.paths.config}/{config.LEGACY_CONFIG}"))
            for old_cmd in ("/usr/local/bin/cw-worker", "/usr/local/sbin/cw-update"):
                if h.exists(old_cmd):
                    gone.append(A("remove", path=old_cmd))
            if h.exists(f"{c.paths.code}/lib/cw"):
                gone.append(A("run", argv=["rm", "-rf", f"{c2.paths.code}/lib/cw"], why="the old package folder; the new release installed its own as lib/ct"))
            secrets = c2.path("secrets")
            if h.exists(c.path("secrets")):
                gone.append(A("run", argv=["sed", "-i", "s/^CW_/CT_/", secrets], why="the secrets file's keys use the new prefix (the values are not shown or changed)"))
        out.append(("Remove what is named after the old name", gone + [A("run", argv=["systemctl", "daemon-reload"])]))
        out.append(("Start the services again", [A("run", argv=["systemctl", "enable", "--now", c2.unit(p)]) for p in enabled]))
        return out

    def plan_lines(self):
        """[(step title, [one short line per action])]: what the plan does, in lines that fit a card (a write shows its path, never its text)."""
        out = []
        for title, actions in self.steps():
            lines = []
            for a in actions:
                if a.kind == "run":
                    lines.append(shlex.join(a.argv))
                elif a.kind == "write":
                    lines.append(f"write {a.path}")
                else:
                    lines.append(f"remove {a.path}")
            out.append((title, lines))
        return out

    def run_all(self):
        self.check()
        steps = self.steps()
        render_plan(self.plan_lines(), self.old, self.new, where="on this machine")
        if self.dry_run:
            self.report.append(("rename", "planned", f"{sum(len(a) for _, a in steps)} steps"))
            return self.report
        if self.confirm is not None and not self.confirm(f"Rename {self.old} to {self.new} on this machine?"):
            self.report.append(("rename", "declined", ""))
            return self.report
        for title, actions in steps:
            for a in actions:
                r = a.do(self.host)
                if not r.ok and not (a.argv and a.argv[0] == "systemctl" and a.argv[1] in ("stop", "disable")):
                    raise SetupError(f"{title} failed", f"`{a.show().splitlines()[0][:160]}` said: {r.out.strip()[:240]}",
                                     [f"The folders may already be under the new name. Read what is on this machine, then either fix the cause and run the rename again, or put the old name back with: ct-setup rename {self.old}"])
            self.say(STYLER.note("done", title))
        self.report.append(("rename", "done", f"{self.old} -> {self.new}"))
        return self.report


# ---- the client half: on your own computer ----------------------------------------------------------------------------------------------------------
def github_renames(cfg, new):
    """[(old slug, new repository name)] for the two repositories, only those that carry the install's name (a custom name is kept)."""
    old = cfg.identity.name
    out = []
    for which, repo in (("queue", cfg.github.queue_repo), ("results", cfg.github.results_repo)):
        if repo == f"{old}-{which}":
            out.append((f"{cfg.github.owner}/{repo}", f"{new}-{which}"))
    return out


def rename_repositories(host, cfg, new):
    """Rename the two repositories when they carry the install's name (GitHub keeps redirecting the old names). Done BEFORE the server half, because the
    worker's clones are pointed at the new names there and must be able to fetch from them. Returns [(step, status, detail)]."""
    notes = []
    if not github_renames(cfg, new):
        return notes
    if not host.which("gh"):
        slugs = ", ".join(f"{a} -> {b}" for a, b in github_renames(cfg, new))
        return [("GitHub repositories", "manual", f"`gh` is not available: rename {slugs} on GitHub yourself first, then run this again")]
    for slug, new_repo in github_renames(cfg, new):
        new_slug = f"{cfg.github.owner}/{new_repo}"
        if host.run(["gh", "repo", "view", new_slug]).ok:
            notes.append((f"repository {new_slug}", "exists", "already renamed"))
            continue
        r = host.run(["gh", "repo", "rename", new_repo, "--repo", slug, "--yes"])
        notes.append((f"repository {slug}", "renamed" if r.ok else "failed", new_repo if r.ok else r.out.strip()[-160:]))
    return notes


def retitle_keys(host, cfg, new):
    """Give the worker's deploy keys the new name in their titles (a title cannot be edited: the key is removed and added again, same key, same access)."""
    import json
    import os
    import tempfile
    notes, old = [], cfg.identity.name
    if not host.which("gh"):
        return [("deploy keys", "manual", f"retitle them from {old}-server to {new}-server on GitHub")]
    for _, new_repo in github_renames(cfg, new):
        slug = f"{cfg.github.owner}/{new_repo}"
        keys = host.run(["gh", "repo", "deploy-key", "list", "-R", slug, "--json", "id,title,key,readOnly"])
        try:
            listed = json.loads(keys.out) if keys.ok else []
        except ValueError:
            listed = []
        for k in listed:
            if not str(k.get("title", "")).startswith(f"{old}-"):
                continue
            title = k["title"].replace(old, new, 1)
            with tempfile.NamedTemporaryFile("w", suffix=".pub", delete=False) as f:
                f.write(k["key"] + "\n")
            try:
                host.run(["gh", "repo", "deploy-key", "delete", str(k["id"]), "-R", slug])
                r = host.run(["gh", "repo", "deploy-key", "add", f.name, "-R", slug, "-t", title] + ([] if k.get("readOnly", True) else ["--allow-write"]))
                notes.append((f"deploy key on {new_repo}", "retitled" if r.ok else "failed", title))
            finally:
                os.unlink(f.name)
    return notes


def rewrite_local(host, cfg, cfg2, new):
    """Everything on this computer that says the old name: the configuration, the answers saved for the next install (a reinstall would otherwise go back to the
    old name, taking it from there), and the two clones' remote URLs. Returns the files changed."""
    from pathlib import Path
    changed = []
    text = with_name(host.read(cfg.source) or "", new, cfg.identity.name, Path(cfg.source).name == config.LEGACY_CONFIG)
    old_home, new_home = Path(cfg.paths.client_home).expanduser(), Path(cfg2.paths.client_home).expanduser()
    if old_home != new_home and host.exists(str(old_home)) and not host.exists(str(new_home)):
        host.run(["mv", str(old_home), str(new_home)])                       # a computer set up before the project was renamed keeps its clones, keys and notes in the old folder
        changed.append(str(new_home))
    target = cfg.source
    if Path(cfg.source) == config.legacy_config_paths()[0]:                   # the old default place and name: move to the new ones
        target = str(Path.home() / ".config" / "claude-tenta" / "ct.toml")
    host.write(target, text, 0o600)
    changed.append(target)
    if target != cfg.source:
        host.run(["rm", "-f", cfg.source])
    host.run(["sh", "-c", "for c in cw-run cw-admin cw-setup cw-help; do l=\"$HOME/.local/bin/$c\"; [ -L \"$l\" ] && rm -f \"$l\"; done; true"])      # the old command links; `ct-setup client` makes the new ones
    answers = str(Path(cfg2.paths.client_home).expanduser() / "install-answers.toml")
    if host.exists(answers):
        host.write(answers, with_name(host.read(answers) or "", new, cfg.identity.name, Path(cfg.source).name == config.LEGACY_CONFIG), 0o600)
        changed.append(answers)
    for clone, url in ((cfg2.path("client_queue"), cfg2.queue_url), (cfg2.path("client_results"), cfg2.results_url)):
        if host.exists(clone + "/.git") or host.exists(clone + "/HEAD"):
            host.run(["git", "-C", clone, "remote", "set-url", "origin", url])
    return changed


def rename_command(a, host, ask, ssh_factory, say, verify=None):
    """ct-setup rename NEW: from your computer. Preview, ask once, run the server half over ssh -t, then the GitHub and local halves."""
    from .. import ui as _ui
    from ..admin.ssh import Ssh
    from .install import deliver_source, remove_remote_dir
    from ..ui import BOLD, GREEN, RED, YELLOW, paint

    def emit(*x, **k):
        _ui.say(*x, **k)
    cfg = config.load()
    cfg.require("server.host", "server.admin_user")
    old, new = cfg.identity.name, a.name
    if not new or not config.NAME_RE.match(new):
        emit("Say the new name", "3 to 24 lowercase letters, digits or -, starting with a letter.", tag="ERROR", color=RED, run="ct-setup rename <new-name>")
        return 2
    if new == old:
        emit("The install already has that name", ("Name", old), ("Configuration", cfg.source or "built-in defaults"), ("Server", cfg.server.host),
             ("GitHub repositories", f"{cfg.github.queue_repo}, {cfg.github.results_repo}"),
             "There is nothing to rename. If you meant to rename an install made under the old name (claude-ballast), its configuration file should be the old cw.toml; check that it is the one shown above.",
             tag="NOTE", color=YELLOW)
        return 0
    cfg2 = load_new_config(host.read(cfg.source) or "", new, old, Path(cfg.source).name == config.LEGACY_CONFIG)
    ssh = (ssh_factory or (lambda h, u, p: Ssh(h, u, p)))(cfg.server.host, cfg.server.admin_user, cfg.server.ssh_port)
    d, rev, _ = deliver_source(ssh, a.source, say=say)
    try:
        # Which settings file the server starts from: an old install's cw.toml, or a current ct.toml, in the old folder or (when an earlier run stopped partway and has
        # already moved it) the new one. The server picks the first that exists, so the same command can simply be run again.
        files = " ".join(f"{d_}/{n_}" for d_ in (cfg.paths.config, cfg2.paths.config) for n_ in (config.LEGACY_CONFIG, "ct.toml"))
        pick = f'$(for f in {files}; do [ -f "$f" ] && echo "$f" && break; done)'
        base = (f"env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH={d}/src CT_CONFIG={pick} python3 -m ct.setup rename --here --to {new} --rev {rev} --source {d}" + (" --no-sandbox-test" if getattr(a, "no_sandbox_test", False) else ""))
        r = ssh.capture(base + " --json")
        try:
            plan = json.loads((r.stdout or "").strip().splitlines()[-1])
        except (ValueError, IndexError):
            plan = None
        if r.returncode or plan is None:
            emit("The server could not plan the rename", (r.stderr or r.stdout or "no answer").strip()[-600:], "Nothing was changed.", tag="ERROR", color=RED)
            return 1
        render_plan([(s["title"], s["lines"]) for s in plan["steps"]], old, new, where=f"on {cfg.server.host}")
        gh = github_renames(cfg, new)
        emit("On GitHub", *[("Repository", f"{slug} becomes {new_repo}") for slug, new_repo in gh], *([("Deploy keys", f"retitled {old}-server to {new}-server")] if gh else []),
             *([] if gh else ["The repositories do not carry the install's name, so they keep theirs."]), tag="PREVIEW", color=_ui.PRIMARY)
        emit("On this computer", ("Configuration", f"{cfg.source}: the name {old} becomes {new}" + ("; it moves to the new default place" if Path(cfg.source) == config.legacy_config_paths()[0] else "")),
             ("The two clones", "point at the new repository names"), *([("Your folder", f"{cfg.paths.client_home} moves to {cfg2.paths.client_home}")] if cfg.paths.client_home != cfg2.paths.client_home else []),
             tag="PREVIEW", color=_ui.PRIMARY)
        if not a.apply:
            emit("Nothing has been changed yet", tag="NEXT", color=GREEN, run_label="To do it", run=f"ct-setup rename {new} --apply")
            return 0
        if not a.yes:
            if not sys.stdin.isatty() or ask(_ui.confirm_prompt(f"Rename {old} to {new} on {cfg.server.host}, on GitHub and here? The worker is briefly stopped.")).strip().lower() != "y":
                emit("Not renamed", "You answered no, or there was nobody to ask.", tag="STOPPED", color=YELLOW)
                return 1
        st = report_mod.Styler(styled=_ui._color_on(sys.stderr), stream=sys.stderr)
        for step, status, detail in rename_repositories(host, cfg, new):
            say(st.note(status, step, detail))
            if status in ("failed", "manual"):
                emit("The repositories were not renamed", "Nothing on the server or on this computer was changed.", tag="STOPPED", color=YELLOW)
                return 1
        rc = ssh.interactive(f"sudo {base} --apply --yes")
    finally:
        remove_remote_dir(ssh, d)
    if rc:
        emit("The rename stopped on the server", f"Exit {rc}. Read the messages above. The repositories are already renamed on GitHub (it keeps redirecting the old names); "
             "your own configuration was not changed.", tag="ERROR", color=RED, run_label="Try again", run=f"ct-setup rename {new} --apply")
        return 1
    for step, status, detail in retitle_keys(host, cfg, new):
        say(st.note(status, step, detail))
    rewrite_local(host, cfg, cfg2, new)
    emit("Renamed", f"{old} is now {new} on {cfg.server.host}, on GitHub and in {cfg.source}.", tag="DONE", color=GREEN,
         run_label="Check that it works", run="ct-admin status")
    return 0
