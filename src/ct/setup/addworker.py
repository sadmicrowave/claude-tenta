"""ct-setup add-worker: find a computer on your network, check that it is good enough, and set it up as ANOTHER worker, without touching anything you already have.

The flow, each step in the wizard's own style:
  1. SCAN     look at your own networks for computers that answer on the SSH port; say how many and what the network alone can tell (see `discover`)
  2. PICK     choose one (or give an address yourself)
  3. CHECK    say exactly what the deeper look will do, ask Y/N, then read the machine over SSH (read-only, see `assess`) and say whether it meets the requirements and why not if not
  4. SET UP   only if it meets them and you say yes: the ordinary install wizard for THIS one machine, its address already filled in

What makes it targeted: the new worker gets its OWN configuration file (`<name>.toml` beside your main one) and its OWN folder for clones and keys (`~/.ct/workers/<name>`). The approval key
is shared (a link to the one you already have, so one signature works everywhere); nothing else is. Your main configuration, your other workers' configurations and clones, and the commands on your
PATH are not written. After the install the files that must not change are compared with how they were before, and any difference is shown. The only thing added elsewhere is the worker's
line in `workers.toml`, which is how `ct-admin workers` and `ct-admin watch` find it.
"""
import hashlib
import os
import re
import socket
import sys
import tomllib
from pathlib import Path

from .. import config, ui as _ui
from ..admin import workers as workers_mod
from ..ui import GREEN, PRIMARY, RED, YELLOW
from . import assess as assess_mod, discover, toml_out
from .server import SetupError

NAME_MAX = 24


def view(ui):
    return {"items": [], "pos": 0, "total": 1, "can_back": False, "prefill": "", "bare": True}


def ask_one(ui, ask, say, q, default="", options=(), validate=None):
    """One question in the wizard's own style: its screen on a terminal, a plain prompt anywhere else. An empty answer takes the default; a choice may be given by its number."""
    problem = None
    for _ in range(5):
        if ui is not None and q.get("select") and q.get("choices"):
            _kind, text = ui.select(q, default)                                      # a list: arrows move, Enter takes it (down is NOT "accept" here, as it is in a text question)
        elif ui is not None:
            _kind, text = ui.ask(q, default, list(options), problem=problem, shown=default, view=view(ui))
            ui.erase()
        else:
            say(f"\n{q['title']}\n  {q['why']}")
            for n, (value, desc) in enumerate(q.get("choices") or [], 1):
                say(f"    {n}  {value:<18} {desc}")
            text = ask(f"  [{default}] " if default else "  ")
        text = (text or "").strip() or default
        if q.get("choices") and text.isdigit() and 1 <= int(text) <= len(q["choices"]):
            text = q["choices"][int(text) - 1][0]
        problem = validate(text) if validate else None
        if not problem and not (q.get("required") and not text):
            return text
        problem = problem or "this one needs an answer"
    raise SetupError("That question was not answered", problem)


def yes_no(ui, ask, say, title, why, default="yes"):
    q = {"key": "worker.confirm", "title": title, "why": why, "choices": [("yes", "go on"), ("no", "stop here; nothing has been changed")], "select": True}
    return ask_one(ui, ask, say, q, default, ["yes", "no"]).lower().startswith("y")


def worker_name(label, taken):
    """A valid, unused install name from a machine's name: worker2.example -> tenta2 (3 to 24 lowercase letters, digits or -, starting with a letter)."""
    base = re.sub(r"[^a-z0-9-]+", "-", (label or "worker").lower().removesuffix(".local").split(".")[0]).strip("-")
    base = re.sub(r"^claude-", "", base) or "worker"
    if not base[0].isalpha():
        base = "w" + base
    base = (base + "-worker")[:NAME_MAX] if len(base) < 3 else base[:NAME_MAX]
    name, n = base, 2
    while name in taken:
        name = f"{base[:NAME_MAX - len(str(n)) - 1]}-{n}"
        n += 1
    return name


def paths_for(main_cfg, name):
    """(configuration file, client folder) for a new worker: beside the main configuration, and under the main client folder's workers/."""
    conf = Path(main_cfg.source).parent / f"{name}.toml" if main_cfg.source else Path("~/.config/claude-tenta").expanduser() / f"{name}.toml"
    return conf, Path(main_cfg.paths.client_home).expanduser() / "workers" / name


def known_workers(main_cfg, wfile):
    """(names, hosts) of the workers you already have: the main one and those in workers.toml."""
    names, hosts = {main_cfg.identity.name}, {main_cfg.server.host or ""}
    for name, path in workers_mod.load_list(wfile).items():
        names.add(name)
        try:
            raw = tomllib.loads(Path(path).expanduser().read_text())
            names.add((raw.get("identity") or {}).get("name", name))
            hosts.add((raw.get("server") or {}).get("host", ""))
        except (OSError, ValueError):
            pass
    return names, {h for h in hosts if h}


def share_approval_key(main_cfg, worker_home):
    """The new worker's folder links to your existing approval key, so what you sign for one worker is accepted by every worker (and no second key exists to confuse them)."""
    main_dir = Path(main_cfg.paths.client_home).expanduser() / "approval"
    link = Path(worker_home) / "approval"
    if main_dir.exists() and not link.exists():
        link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(main_dir, link)
        return True
    return False


def fingerprint(paths):
    """{path: sha256 or None} of the files that must not change while another worker is added."""
    out = {}
    for p in paths:
        try:
            out[str(p)] = hashlib.sha256(Path(p).read_bytes()).hexdigest()
        except OSError:
            out[str(p)] = None
    return out


def protected_files(main_cfg, wfile, leave=None):
    """Everything of the existing installs that adding a worker must leave exactly as it is."""
    home = Path(main_cfg.paths.client_home).expanduser()
    files = [main_cfg.source, home / "queue" / ".git" / "config", home / "results" / ".git" / "config", home / "approval" / "key.pub", home / "install-answers.toml", home / "install-state.json"]
    for wname, path in workers_mod.load_list(wfile).items():
        if leave and wname == leave:                                       # the worker being set up again: its own files are the ones that change
            continue
        files.append(Path(path).expanduser())
        try:
            wh = Path((tomllib.loads(Path(path).expanduser().read_text()).get("paths") or {}).get("client_home", "")).expanduser()
            files += [wh / "queue" / ".git" / "config", wh / "results" / ".git" / "config"] if str(wh) not in ("", ".") else []
        except (OSError, ValueError):
            pass
    return [f for f in files if f]


def changed(before, after):
    return sorted(p for p in before if before[p] != after.get(p))


# ---- the cards ----------------------------------------------------------------------------------------------------------------------------------
def found_card(devices, notes, nets, card=None):
    card = card or _ui.say
    rows = [("Networks", ", ".join(f"{n.network} ({i})" for i, n in nets))] if nets else []
    mine = [d for d in devices if not d.known]
    servers = [d for d in mine if d.kind == "linux-server"]
    real, soft = [d for d in devices if not d.virtual], [d for d in devices if d.virtual]
    where = (f"{len(real)} on your real network, {len(soft)} on virtual networks inside this computer (VMs and containers; other computers on your network cannot reach those)"
             if soft else "all on your real network")
    rows.append(("Found", f"{len(devices)} device(s) answering on the SSH port: {where}. {len(servers)} look like Linux servers"
                 + (f", {len(devices) - len(mine)} already yours" if len(devices) != len(mine) else "")))
    for d in devices:
        what = d.os_hint + (f" · {d.vendor}" if d.vendor else "") + ("".join(f" · {n}" for n in d.notes)) + f" · {d.latency_ms:g} ms" + (f" · on {d.iface}, virtual" if d.virtual else f" · on {d.iface}" if d.iface else "")
        rows.append((d.ip + (f"  {d.name}" if d.name else "") + ("  (already a worker)" if d.known else ""), what))
    rows += [f"Note: {n}" for n in notes]
    rows.append("This is all the network alone can tell. Nothing was logged in to; the next steps ask before they look any deeper.")
    card("Network scan", *rows, tag="FOUND" if devices else "NONE", color=PRIMARY if devices else YELLOW, stream=sys.stderr)


def summary_card(host, user, card=None):
    card = card or _ui.say
    rows = [("Machine", host), ("Logs in as", f"{user}, with your SSH key (no password is asked or sent)")]
    rows += [("Will check", c) for c in assess_mod.CHECKS]
    rows += ["It only reads. Nothing is installed, written or changed on that computer, and nothing on this one."]
    card("Candidacy check", *rows, tag="BEFORE", color=PRIMARY, stream=sys.stderr)


def verdict_card(host, v, card=None):
    card = card or _ui.say
    icons = {"OK": "OK", "WARN": "WARN", "FAIL": "FAIL"}
    rows = [(f"{icons[s]:<4} {w}", d) for s, w, d in v.rows]
    if v.meets:
        rows.append("It meets the requirements" + (f", with {len(v.warnings)} thing(s) to know about." if v.warnings else "."))
        card(f"{host} can be a worker", *rows, tag="MEETS", color=GREEN, stream=sys.stderr)
    else:
        rows += [f"Why not: {why}" for s, w, why in v.failures]
        card(f"{host} cannot be a worker yet", *rows, tag="NOT YET", color=RED, steps=v.fixes or None, steps_label="How to fix it", stream=sys.stderr)


# ---- the flow ------------------------------------------------------------------------------------------------------------------------------------
def reachable_name(facts, ip, resolve=None):
    """The name to install with: the machine's own hostname + .local when that resolves to the same address (it survives a new DHCP lease), else the address."""
    resolve = resolve or (lambda n: socket.gethostbyname(n))
    hn = (facts or {}).get("HOSTNAME", "").strip()
    if hn:
        for cand in (f"{hn}.local", hn):
            try:
                if resolve(cand) == ip:
                    return cand
            except OSError:
                continue
    return ip


def existing_worker(target, wfile):
    """(name, configuration file, its settings) of the worker in workers.toml whose address is `target`, or None (the main worker, or nothing)."""
    for entry, path in workers_mod.load_list(wfile).items():
        try:
            raw = tomllib.loads(Path(path).expanduser().read_text())
        except (OSError, ValueError):
            continue
        if (raw.get("server") or {}).get("host") == target:
            return (raw.get("identity") or {}).get("name") or entry, Path(path).expanduser(), raw
    return None


def rerun(a, cfg, wfile, ex, target, ui, ask, say, notice, installer_factory, interactive):
    """A worker you already added is set up AGAIN: the current release is sent to it, its protections and connection are checked and repaired, and a first job proves it works, using ITS OWN
    configuration and folder (the plain installer would read the first install's). Everything else stays as it is."""
    name, conf, raw = ex
    user = a.user or (raw.get("server") or {}).get("admin_user") or cfg.server.admin_user or os.environ.get("USER", "")
    home = Path((raw.get("paths") or {}).get("client_home") or paths_for(cfg, name)[1]).expanduser()
    notice(f"{name} is already a worker: set it up again", ("Machine", f"{user}@{target}"), ("Its configuration", str(conf)),
           "This sends it the current release, checks and repairs its protections and the connection, and sends a first job. Your other workers, your first install and the commands on your PATH are not touched.",
           tag="AGAIN", color=PRIMARY, stream=sys.stderr)
    if interactive and not yes_no(ui, ask, say, f"Set {name} up again?", "The install wizard runs for this one worker with its own answers filled in. It asks for the sudo password on that computer."):
        notice("Stopped", "Nothing was changed.", tag="STOPPED", color=YELLOW, stream=sys.stderr)
        return 1
    kind = (raw.get("transport") or {}).get("kind")
    return install_worker(cfg, wfile, installer_factory, ui, notice, conf, home, name, target, user, again=True, extra={"transport.kind": kind} if kind else None)      # its own connection, not the first install's


def choose_device(ui, ask, say, devices, user_given=None):
    """Pick one of the found devices (or give an address). Returns the address."""
    fresh = [d for d in devices if not d.known]
    choices = [(d.ip, (d.name + " · " if d.name else "") + d.os_hint + (f" · {d.vendor}" if d.vendor else "") + ("".join(f" · {n}" for n in d.notes)) + (" · virtual network inside this computer" if d.virtual else "")) for d in fresh]
    choices.append(("other", "none of these: type an address or name yourself"))
    q = {"key": "worker.device", "title": "Which computer should become a worker?", "why": "The list is what answered on your network, likely Linux servers first. Picking one does not change it: a short check comes next, and asks first.",
         "choices": choices, "required": True, "select": True}
    pick = ask_one(ui, ask, say, q, choices[0][0], [v for v, _ in choices])
    if pick == "other":
        pick = ask_one(ui, ask, say, {"key": "worker.address", "title": "What is its name or address?", "why": "A name such as box.example or an address such as 198.51.100.50.", "examples": ["worker2.example", "198.51.100.50"], "required": True},
                       "", ())
    return pick


def run(a, host, ask, ssh_factory, say, *, ui=None, installer_factory=None, scan=None, assess=None, resolve=None, notice=None, interactive=None, networks=None):
    """ct-setup add-worker [ADDRESS] [--user U] [--worker-name N] [--yes]. Returns the exit code."""
    notice = notice or (lambda *x, **k: _ui.say(*x, **k))
    cfg = config.load()
    if not cfg.source:
        notice("Set up your first worker first", "add-worker adds ANOTHER worker, and there is no configuration on this computer yet.", tag="ERROR", color=RED, run="ct-setup install")
        return 2
    wfile = Path(cfg.source).parent / "workers.toml"
    names, hosts = known_workers(cfg, wfile)
    if interactive is None:
        interactive = not a.yes and (ui is not None or sys.stdin.isatty())
    target = a.name or None                                                           # the positional word: an address or name to skip the scan
    if not target:
        nets = (networks or discover.local_networks)()
        notice("Scanning your network", f"Looking at {', '.join(str(n.network) for _, n in nets) or 'nothing'} for computers that answer on the SSH port. This takes a few seconds.", tag="SCAN", color=PRIMARY, stream=sys.stderr)
        lookup, known_addrs = resolve or socket.gethostbyname, set(hosts)
        for h in hosts:                                                                    # a worker you have by name is recognised by its address too
            try:
                known_addrs.add(lookup(h))
            except OSError:
                pass
        devices, notes = (scan or discover.scan)(nets, known=known_addrs)
        found_card(devices, notes, nets, notice)
        if not [d for d in devices if not d.known] and not interactive:
            return 1
        if not devices:
            notice("Nothing to pick from", "No computer on your networks answers on the SSH port.", steps=["Check that the machine is on, on the same network, and that its SSH server runs (sudo systemctl status ssh).",
                   "Or give its address yourself: ct-setup add-worker box.example"], steps_label="What to try", tag="STOPPED", color=YELLOW, stream=sys.stderr)
            return 1
        if not interactive:
            notice("Say which computer", "Pass its address to add one without questions.", tag="NEXT", color=YELLOW, run="ct-setup add-worker <address> --yes", stream=sys.stderr)
            return 1
        target = choose_device(ui, ask, say, devices)
    if target in hosts:
        ex = existing_worker(target, wfile)
        if ex is not None:
            return rerun(a, cfg, wfile, ex, target, ui, ask, say, notice, installer_factory, interactive)
        notice("That computer is already one of your workers", f"{target} is in your configuration. To change it use ct-setup install; to see it: ct-admin workers.", tag="NOTE", color=YELLOW, run="ct-admin workers", stream=sys.stderr)
        return 0
    default_user = a.user or cfg.server.admin_user or os.environ.get("USER", "")
    user = a.user or (ask_one(ui, ask, say, {"key": "worker.user", "title": "Which account should it log in as on that computer?", "why": "The account with SSH key login and sudo rights that you would use to administer it.",
                                            "examples": [default_user], "required": True}, default_user, ()) if interactive else default_user)
    summary_card(target, user, notice)
    if interactive and not yes_no(ui, ask, say, "Run this check now?", "It only reads, as described above. Say no and nothing happens."):
        notice("Stopped", "Nothing was checked and nothing was changed.", tag="STOPPED", color=YELLOW, stream=sys.stderr)
        return 1
    v = (assess or assess_mod.assess)(target, user, int(getattr(a, "port", 22) or 22))
    verdict_card(target, v, notice)
    if not v.meets:
        return 1
    if getattr(a, "dry_run", False):
        notice("Check only", f"{target} can be a worker. Nothing was installed or changed (this was a preview).", tag="PREVIEW", color=PRIMARY, run=f"ct-setup add-worker {target}", run_label="To set it up", stream=sys.stderr)
        return 0
    if interactive and not yes_no(ui, ask, say, "Set it up as a worker?", "The install wizard runs for this one computer, with its address filled in. Your other installs are not touched."):
        notice("Stopped", f"{target} was checked and can be a worker. Run ct-setup add-worker {target} when you want it.", tag="STOPPED", color=YELLOW, stream=sys.stderr)
        return 0
    is_ip = bool(re.match(r"^\d+\.\d+\.\d+\.\d+$", target))
    server_host = reachable_name(v.facts, target, resolve) if is_ip else target                 # an address found by the scan is installed by the machine's own .local name when that resolves to it
    name_default = a.worker_name or worker_name(v.facts.get("HOSTNAME") or target, names)
    taken = lambda n: None if n not in names and config.NAME_RE.match(n) else ("that name is already a worker" if n in names else "3 to 24 lowercase letters, digits or -, starting with a letter")      # noqa: E731
    name = a.worker_name or (ask_one(ui, ask, say, {"key": "worker.name", "title": "What should this worker be called?", "why": "Its install name, its configuration file and its folder on this computer are named after it. It must differ from your other workers.",
                                            "examples": [name_default], "required": True}, name_default, (), validate=taken) if interactive else name_default)
    if taken(name):
        raise SetupError("That worker name cannot be used", taken(name), [f"Choose another with --worker-name, for example --worker-name {worker_name(name, names)}"])
    conf, home = paths_for(cfg, name)
    return install_worker(cfg, wfile, installer_factory, ui, notice, conf, home, name, server_host, user)


def install_worker(cfg, wfile, installer_factory, ui, notice, conf, home, name, server_host, user, again=False, extra=None):
    """Run the installer for ONE worker with its own configuration and folder, prove nothing of the others changed, and register it. `again`: the worker already exists and is being set up again
    (its own files are of course allowed to change; everything else is not)."""
    before = fingerprint(protected_files(cfg, wfile, leave=name if again else None))
    share_approval_key(cfg, home)
    suggest = {"identity.name": name, "server.host": server_host, "server.admin_user": user, "paths.client_home": str(home), **(extra or {})}
    old_env = os.environ.get("CT_CONFIG")
    os.environ["CT_CONFIG"] = str(conf)                                                         # the first job and every command the install runs read THIS worker's configuration
    try:
        inst = installer_factory(conf, home, name, server_host, user, suggest)
        screen = ui
        if screen is not None:
            screen.banner(f"add {name}")
        with (screen.session() if screen is not None else _nullcontext()):
            report = inst.run_all()
    finally:
        if old_env is None:
            os.environ.pop("CT_CONFIG", None)
        else:
            os.environ["CT_CONFIG"] = old_env
    after = fingerprint(protected_files(cfg, wfile, leave=name if again else None))
    touched = changed(before, after)
    if touched:
        notice("Something of your existing installs changed", *[("Changed", p) for p in touched], "Adding a worker must not do that. Nothing was undone; check these files.", tag="WARNING", color=RED, stream=sys.stderr)
    if any(r[1] == "failed" for r in report):
        notice(f"{name} was set up but its first job did not come back", "Run ct-admin check and ct-admin status for it.", tag="WARNING", color=YELLOW, stream=sys.stderr)
    workers_mod.add(wfile, name, str(conf))
    notice(f"{name} is set up again" if again else f"{name} is now a worker", ("Machine", f"{user}@{server_host}"), ("Configuration", str(conf)), ("Your other installs", "unchanged" if not touched else "see the warning above"),
           tag="DONE", color=GREEN, run_label="See all your workers", run=["ct-admin workers", "ct-admin watch"], stream=sys.stderr)
    return 1 if touched else 0


class _nullcontext:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False
