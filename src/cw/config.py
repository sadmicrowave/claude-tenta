"""cw.toml: the one place every name, path and limit of an install is written down.

Layers, later wins:  built-in defaults  <  cw.toml  <  CW_<SECTION>_<KEY> environment variables.

Rules that keep this safe to build commands and unit files from:
  * Strict. An unknown section or key is an error (a typo must not silently fall back to a default).
  * Every value has a pattern or range. Nothing that reaches a shell, a systemd unit, a sudoers line or a firewall rule is free text.
  * No secrets. API keys and tokens live in secrets.env (see `read_env_file`), never in cw.toml.
  * Standard library only (Python 3.11+ for tomllib), so it runs on a fresh server before anything is installed.
"""
import ipaddress
import os
import re
import stat
import sys
import tomllib
from pathlib import Path

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{2,23}$")
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")
GH_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
GH_REPO_RE = re.compile(r"^(?!\.{1,2}$)[A-Za-z0-9._-]{1,100}$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,60}$")
IDENT_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
PATH_RE = re.compile(r"^/[A-Za-z0-9._/-]+$")                     # absolute, no spaces, no quotes, no shell characters
RESERVED_USERS = {"root", "daemon", "bin", "sys", "nobody", "www-data", "ubuntu", "debian"}
PROTECTED_PATHS = {"/", "/etc", "/var", "/var/lib", "/usr", "/usr/local", "/usr/bin", "/usr/sbin", "/opt", "/home", "/root", "/boot", "/bin", "/sbin", "/lib", "/tmp"}
RFC1918 = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]

# Hardware profiles fill in capacity, load and temperature limits the user did not set explicitly.
PROFILES = {
    "old-laptop": {"capacity": 2, "max_load": 1.5, "max_temp_c": 70},
    "small-server": {"capacity": 4, "max_load": 3.0, "max_temp_c": 75},
    "workstation": {"capacity": 8, "max_load": 8.0, "max_temp_c": 80},
    "custom": {"capacity": 4, "max_load": 3.0, "max_temp_c": 75},
}


class ConfigError(Exception):
    """One or more problems with the configuration; `.problems` is a list of plain-words lines."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


# ---- field kinds: each check returns (clean_value, None) or (None, "what is wrong") ----------------------------------------------
def _str(pattern, what, allow_empty=False):
    def check(v):
        if not isinstance(v, str):
            return None, "must be text"
        if v == "" and allow_empty:
            return v, None
        return (v, None) if pattern.fullmatch(v) else (None, f"must be {what}")
    return check


def _kind(kind):
    def tag(fn):
        fn.kind = kind
        return fn
    return tag


def _int(lo, hi):
    @_kind("int")
    def check(v):
        if isinstance(v, bool) or not isinstance(v, int):
            return None, "must be a whole number"
        return (v, None) if lo <= v <= hi else (None, f"must be between {lo} and {hi}")
    return check


def _num(lo, hi):
    @_kind("float")
    def check(v):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None, "must be a number"
        return (float(v), None) if lo <= v <= hi else (None, f"must be between {lo} and {hi}")
    return check


@_kind("bool")
def _bool(v):
    return (v, None) if isinstance(v, bool) else (None, "must be true or false")


def _enum(*options):
    def check(v):
        return (v, None) if v in options else (None, "must be one of: " + ", ".join(options))
    return check


def _path(v):
    if not isinstance(v, str):
        return None, "must be text"
    if v.startswith("~"):                                          # client-side folders may start with ~
        rest = v[1:]
        if rest and not re.fullmatch(r"/[A-Za-z0-9._/-]+", rest):
            return None, "must be a plain path such as ~/.cw"
        return v, None
    if not PATH_RE.fullmatch(v) or ".." in v.split("/") or "//" in v:
        return None, "must be an absolute path of letters, digits and . _ - / only (no spaces, no ..)"
    if (v.rstrip("/") or "/") in PROTECTED_PATHS:
        return None, "must not be a system folder; use a folder of its own"
    return v, None


def _url(v):
    if not isinstance(v, str) or not re.fullmatch(r"https://[A-Za-z0-9.-]+(:\d{1,5})?(/[A-Za-z0-9._~/%-]*)?", v):
        return None, "must be an https:// address with no spaces or query"
    return v, None


def _host(v):
    if not isinstance(v, str):
        return None, "must be text"
    try:
        ipaddress.ip_address(v)
        return v, None
    except ValueError:
        pass
    return (v, None) if HOST_RE.fullmatch(v) else (None, "must be a host name or an IP address")


def _user(v):
    ok, err = _str(USER_RE, "a lowercase Linux user name (letters, digits, _ and -)")(v)
    if err:
        return None, err
    return (None, "is a system account name; choose another") if v in RESERVED_USERS else (v, None)


def _list_of(item):
    @_kind("list")
    def check(v):
        if not isinstance(v, list):
            return None, "must be a list"
        if len(v) > 64:
            return None, "has too many entries (at most 64)"
        out = []
        for i, x in enumerate(v):
            ok, err = item(x)
            if err:
                return None, f"entry {i + 1} {err}"
            out.append(ok)
        return out, None
    return check


def _cidr(v):
    try:
        return str(ipaddress.ip_network(v, strict=True)), None
    except (ValueError, TypeError):
        return None, "must be a network such as 192.0.2.0/24"


def _ip(v):
    try:
        return str(ipaddress.ip_address(v)), None
    except (ValueError, TypeError):
        return None, "must be an IP address"


@_kind("floatlist")
def _hours(v):
    ok, err = _list_of(_num(0.1, 168))(v)
    if err:
        return None, err
    if any(b <= a for a, b in zip(ok, ok[1:])):
        return None, "must be increasing, for example [1, 6, 24]"
    return ok, None


# section -> key -> (default, check, plain-words description). A default of None means "not set yet": the wizard fills it in.
SCHEMA = {
    "identity": {
        "name": ("claude-worker", _str(NAME_RE, "3 to 24 lowercase letters, digits or -, starting with a letter"),
                 "prefix for services, folders and commands on this install"),
    },
    "server": {
        "host": (None, _host, "name or address the client connects to over SSH"),
        "admin_user": (None, _user, "the human account on the server; it needs a password to use sudo"),
        "worker_user": ("cwworker", _user, "the unprivileged account that runs jobs"),
        "ssh_port": (22, _int(1, 65535), "SSH port on the server"),
    },
    "github": {
        "owner": (None, _str(GH_OWNER_RE, "a GitHub user or organisation name"), "account that owns the two private repos"),
        "queue_repo": ("cw-queue", _str(GH_REPO_RE, "a repository name"), "private repo holding jobs"),
        "results_repo": ("cw-results", _str(GH_REPO_RE, "a repository name"), "private repo holding results"),
        "jobs_branch": ("jobs", _str(BRANCH_RE, "a branch name"), "branch the client pushes job files to"),
        "data_branch": ("data", _str(BRANCH_RE, "a branch name"), "branch holding exclusions and other kept data"),
        "main_branch": ("main", _str(BRANCH_RE, "a branch name"), "branch holding the registry"),
    },
    "paths": {
        "code": ("/opt/{name}", _path, "where the code is installed on the server"),
        "state_root": ("/var/lib/{name}", _path, "state, the two git clones and job working folders (server)"),
        "config": ("/etc/{name}", _path, "this config, the registry and secrets (server)"),
        "worker_home": ("/home/{worker_user}", _path, "home folder of the worker account (server)"),
        "client_home": ("~/.cw", _path, "where the client keeps its clone and notes (your computer)"),
    },
    "hardware": {
        "profile": ("small-server", _enum(*PROFILES), "picks sensible limits for the machine; the values below override it"),
        "capacity": (None, _int(1, 64), "capacity points the running jobs may add up to (a heavy job takes 4)"),
        "max_load": (None, _num(0.1, 1000), "do not start a job above this 1-minute load average"),
        "max_temp_c": (None, _int(40, 110), "do not start a job when the hottest core is above this"),
        "thermal": ("none", _enum("none", "lm-sensors", "apple-smc"), "which temperature and fan support to enable"),
    },
    "isolation": {
        "require_netns": (True, _bool, "refuse to run a no-network job if the machine cannot isolate it (leave on)"),
        "method": ("unshare", _enum("unshare", "systemd-run"), "how a job is cut off from the network"),
    },
    "alerts": {
        "kind": ("none", _enum("none", "ntfy", "webhook"), "where security alerts are sent"),
        "url": (None, _url, "https address to post alerts to; any access token goes in secrets.env"),
        "remind_hours": ([1, 6, 24], _hours, "hours after a lockdown starts at which a reminder is sent"),
        "remind_repeat_h": (24, _num(1, 168), "then repeat the reminder this often"),
    },
    "network": {
        "lan_deny_out": (True, _bool, "stop the server starting connections to other devices on your LAN"),
        "lan_allow_out": ([], _list_of(_cidr), "LAN networks the server may still reach (for example a local DNS server)"),
        "dns_servers": ([], _list_of(_ip), "DNS servers the server may use inside the LAN"),
        "ssh_from": (list(RFC1918), _list_of(_cidr), "networks allowed to SSH in"),
    },
    "updates": {
        "mode": ("manual", _enum("manual", "signed-tags"), "manual = you apply; signed-tags = install only tags signed by your key"),
    },
    "registry": {"packs": (["core"], _list_of(_str(IDENT_RE, "a pack name")), "job-class packs to install")},
    "policy": {"packs": (["core"], _list_of(_str(IDENT_RE, "a pack name")), "off-limits rule packs to apply")},
    "hardening": {
        "modules": (["ssh-keys-only", "ufw-lan-egress", "auditd-canaries", "no-root-equivalent-groups", "unattended-upgrades"],
                    _list_of(_str(IDENT_RE, "a module name")), "hardening modules the wizard applies"),
    },
}
SECRETISH = re.compile(r"(?i)(token|passw|secret|api[_-]?key|credential)")


def _expand(value, name, worker_user):
    return value.replace("{name}", name).replace("{worker_user}", worker_user) if isinstance(value, str) else value


def _peek(raw, section, key):
    given = raw.get(section) if isinstance(raw.get(section), dict) else {}
    value = given.get(key, SCHEMA[section][key][0])
    return value if isinstance(value, str) else SCHEMA[section][key][0]


def validate(raw):
    """raw (the parsed TOML or {}) -> (clean nested dict with defaults filled in, [problems]). Never raises."""
    problems, clean = [], {}
    raw = raw if isinstance(raw, dict) else {}
    for section in raw:
        if section not in SCHEMA:
            problems.append(f"[{section}] is not a known section (known: {', '.join(SCHEMA)})")
    for section, fields in SCHEMA.items():
        if section in raw and not isinstance(raw[section], dict):
            problems.append(f"[{section}] must be a table")
        for key in (raw.get(section) if isinstance(raw.get(section), dict) else {}):
            if key not in fields:
                hint = " Secrets belong in secrets.env, never in cw.toml." if SECRETISH.search(key) else ""
                problems.append(f"{section}.{key} is not a known setting (known: {', '.join(fields)}).{hint}")
    name, worker = _peek(raw, "identity", "name"), _peek(raw, "server", "worker_user")
    for section, fields in SCHEMA.items():
        given = raw.get(section) if isinstance(raw.get(section), dict) else {}
        clean[section] = {}
        for key, (default, check, _) in fields.items():
            if key in given:
                value, err = check(_expand(given[key], name, worker))
                if err:
                    problems.append(f"{section}.{key} {err} (got {given[key]!r})")
                    value = default
            else:
                value = _expand(default, name, worker)
            clean[section][key] = value
    hw = clean["hardware"]
    for k, v in PROFILES[hw["profile"]].items():
        if hw[k] is None:
            hw[k] = v
    srv = clean["server"]
    if srv["admin_user"] and srv["admin_user"] == srv["worker_user"]:
        problems.append("server.admin_user and server.worker_user must be different accounts")
    if clean["alerts"]["kind"] != "none" and not clean["alerts"]["url"]:
        problems.append(f"alerts.url is required when alerts.kind is {clean['alerts']['kind']!r}")
    if not clean["isolation"]["require_netns"] and os.environ.get("CW_ALLOW_UNSAFE") != "1":
        problems.append("isolation.require_netns = false would let a no-network job run WITH network whenever isolation fails, so it is not "
                        "accepted (for a throwaway test only, set the environment variable CW_ALLOW_UNSAFE=1)")
    paths = clean["paths"]
    distinct = [paths["code"], paths["state_root"], paths["config"]]
    if len(set(distinct)) != 3:
        problems.append("paths.code, paths.state_root and paths.config must be three different folders")
    for a in distinct:
        for b in distinct:
            if a != b and b.startswith(a.rstrip("/") + "/"):
                problems.append(f"{b} is inside {a}; keep code, state and config in separate folders")
    return clean, sorted(set(problems))


def _coerce(kind, text):
    """Turn an environment variable's text into the type the field expects; leave it as text if it does not fit (validation then reports it)."""
    if kind == "bool":
        low = text.lower()
        return True if low in ("1", "true", "yes", "on") else False if low in ("0", "false", "no", "off") else text
    if kind == "int":
        return int(text) if re.fullmatch(r"-?\d+", text) else text
    if kind == "float":
        try:
            return float(text)
        except ValueError:
            return text
    if kind in ("list", "floatlist"):
        items = [x.strip() for x in text.split(",") if x.strip()]
        if kind == "floatlist":
            try:
                return [float(x) for x in items]
            except ValueError:
                pass
        return items
    return text


def _env_overrides(raw, env):
    """CW_<SECTION>_<KEY>=value overrides, applied to the raw TOML before validation. Lists are comma-separated."""
    out = {s: dict(v) if isinstance(v, dict) else v for s, v in (raw or {}).items()}
    for section, fields in SCHEMA.items():
        for key, (_, check, _doc) in fields.items():
            text = env.get(f"CW_{section}_{key}".upper(), "")
            if text != "" and isinstance(out.get(section, {}), dict):
                out.setdefault(section, {})[key] = _coerce(getattr(check, "kind", "str"), text)
    return out


class Section:
    """Read-only attribute access to one section: cfg.server.host."""

    def __init__(self, name, values):
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_values", dict(values))

    def __getattr__(self, key):
        try:
            return self._values[key]
        except KeyError:
            raise AttributeError(f"[{self._name}] has no setting {key!r}") from None

    def __setattr__(self, key, value):
        raise AttributeError("configuration is read-only")

    def as_dict(self):
        return dict(self._values)


class Config:
    def __init__(self, clean, source=None):
        self._clean = clean
        self.source = source                       # the file it was read from, or None for defaults only
        for section, values in clean.items():
            setattr(self, section, Section(section, values))

    def get(self, dotted):
        section, _, key = dotted.partition(".")
        return self._clean[section][key]

    def require(self, *dotted):
        """Raise ConfigError naming every setting in `dotted` that has not been set yet (the wizard fills these in)."""
        missing = [d for d in dotted if self.get(d) in (None, "")]
        if missing:
            raise ConfigError([f"{d} is not set (run the setup wizard, or add it to {self.source or 'cw.toml'})" for d in missing])

    # ---- derived names and paths: nothing else in the code base builds these by hand ----
    def unit(self, part):
        """Systemd unit name for a part such as 'fetch.service' or 'step.timer'."""
        return f"{self.identity.name}-{part}"

    @property
    def queue_url(self):
        self.require("github.owner")
        return f"git@github.com:{self.github.owner}/{self.github.queue_repo}.git"

    @property
    def results_url(self):
        self.require("github.owner")
        return f"git@github.com:{self.github.owner}/{self.github.results_repo}.git"

    def path(self, what):
        p, n = self.paths, self.identity.name
        table = {
            "dispatcher": f"{p.code}/dispatcher.py", "tools": f"{p.code}/tools",
            "state": f"{p.state_root}/state", "queue_clone": f"{p.state_root}/queue", "results_clone": f"{p.state_root}/results",
            "registry": f"{p.config}/registry.yaml", "secrets": f"{p.config}/secrets.env", "installed_rev": f"{p.config}/installed-rev",
            "allowed_signers": f"{p.config}/allowed_signers", "config_file": f"{p.config}/cw.toml",
            "watch_state": f"{p.state_root}-watch", "run_dir": f"/run/{n}",
        }
        return table[what]

    def to_dict(self):
        return {s: dict(v) for s, v in self._clean.items()}


def default_config_path(env=None):
    env = os.environ if env is None else env
    if env.get("CW_CONFIG"):
        return Path(env["CW_CONFIG"])
    for p in (Path.home() / ".config" / "claude-worker" / "cw.toml", Path("/etc/claude-worker/cw.toml")):
        if p.exists():
            return p
    return None


def load(path=None, env=None):
    """Read, merge and validate. Raises ConfigError with every problem found."""
    env = os.environ if env is None else env
    path = Path(path) if path else default_config_path(env)
    raw = {}
    if path is not None:
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except OSError as e:
            raise ConfigError([f"cannot read {path}: {e.strerror}"])
        except tomllib.TOMLDecodeError as e:
            raise ConfigError([f"{path} is not valid TOML: {e}"])
    clean, problems = validate(_env_overrides(raw, env))
    if problems:
        raise ConfigError(problems)
    return Config(clean, str(path) if path else None)


# ---- secrets: KEY=value files kept apart from the config -----------------------------------------------------------------------
def read_env_file(path):
    """Parse a KEY=value file (comments and blanks ignored, optional quotes). Returns {} if it does not exist."""
    out = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip("\"'")
    return out


def secrets_file_problem(path):
    """None if the secrets file is safe to use, else a plain-words problem: it must not be readable by 'other'."""
    try:
        mode = Path(path).stat().st_mode
    except OSError:
        return None
    if mode & stat.S_IRWXO:
        return f"{path} can be read by every account on this machine; run: chmod 640 {path}  (owner root, group the worker account)"
    return None


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "check"
    path = argv[1] if len(argv) > 1 else None
    try:
        cfg = load(path)
    except ConfigError as e:
        print("Problems with the configuration:", file=sys.stderr)
        for p in e.problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    if cmd == "check":
        print(f"OK  {cfg.source or 'no cw.toml found; built-in defaults are valid'}")
        return 0
    if cmd == "show":
        for section, values in cfg.to_dict().items():
            print(f"[{section}]")
            for k, v in values.items():
                print(f"  {k} = {v!r}")
        return 0
    print("usage: python3 -m cw config check|show [path]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
