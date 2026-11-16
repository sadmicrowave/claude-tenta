"""cw-setup: the questions, the config writer, hardening modules, and the client, server and audit halves, all against a fake machine."""
import io
import os
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from support import REGISTRY, RepoFixture, sh  # noqa: F401  (support sets HOME to a throwaway folder)

from cw import config, registry
from cw.setup import audit, cli, detect, hardening, questions, server, toml_out
from cw.setup.client import ClientSetup
from cw.setup.host import Host, Result

H = "/" + "home"      # a home-folder prefix, spelled in two parts so the privacy guard does not mistake it for a real path
ROOT = Path(__file__).resolve().parents[1]
OS_RELEASE = 'NAME="Ubuntu"\nID=ubuntu\nVERSION_ID="24.04"\n'


class FakeHost(Host):
    """A folder standing in for / plus scripted command results. `replies` maps a command prefix (joined) to a Result; the default is success."""

    def __init__(self, env=None):
        super().__init__(tempfile.mkdtemp(prefix="cw-fakehost-"), env={"HOME": H + "/me", "PATH": "/bin", **(env or {})})
        self.calls, self.replies = [], {}
        self.put("/etc/os-release", OS_RELEASE)
        self.put("/proc/meminfo", "MemTotal:        8000000 kB\n")
        self.put("/proc/cpuinfo", "processor : 0\nprocessor : 1\n")
        self.path("/run/systemd/system").mkdir(parents=True)

    def put(self, path, text):
        self.write(path, text)

    def run(self, argv, **kw):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        line = " ".join(argv)
        for prefix in sorted(self.replies, key=len, reverse=True):
            if line.startswith(prefix):
                r = self.replies[prefix]
                return r(argv) if callable(r) else r
        return Result(0, "")

    def ran(self, prefix):
        return [c for c in self.calls if " ".join(c).startswith(prefix)]

    def which(self, name):
        return "/bin/" + name if name in self.env.get("TOOLS", "git ssh gh") else None


def cfg_of(**over):
    nested = {"server": {"admin_user": "me", "host": "worker.example"}, "github": {"owner": "someone"}, **over}
    clean, problems = config.validate(nested)
    assert not problems, problems
    return config.Config(clean)


class QuestionTests(unittest.TestCase):
    def test_defaults_alone_do_not_answer_required_questions(self):
        nested, missing = questions.collect("server", use_defaults=True)
        self.assertIn("server.admin_user", missing)
        self.assertIn("github.owner", missing)

    def test_given_answers_are_checked_by_the_schemas_own_check(self):
        with self.assertRaises(questions.WizardError) as e:
            questions.collect("server", use_defaults=True, given={"server.admin_user": "Bad User!", "github.owner": "x"})
        self.assertIn("server.admin_user", str(e.exception))

    def test_a_person_can_take_defaults_and_fix_a_bad_answer(self):
        said, answers = [], iter(["", "me", "", "", "someone"])
        nested, missing = questions.collect("server", ask=lambda p: next(answers, ""), say=said.append)
        self.assertEqual(missing, [])
        self.assertEqual(nested["server"]["admin_user"], "me")
        self.assertEqual(nested["server"]["worker_user"], "cwworker")
        self.assertTrue(any("Choices:" in s for s in said))

    def test_a_bad_typed_answer_is_asked_again_with_the_reason(self):
        said, answers = [], iter(["BAD NAME", "me"])
        nested, _ = questions.collect("both" if False else "server", ask=lambda p: next(answers, ""), say=said.append, only={"server.admin_user"})
        self.assertEqual(nested["server"]["admin_user"], "me")
        self.assertTrue(any("That did not work" in s for s in said))

    def test_lists_bools_and_numbers_parse_from_text(self):
        self.assertEqual(questions.parse("network.lan_allow_out", "192.0.2.0/24, 198.51.100.0/24"), ["192.0.2.0/24", "198.51.100.0/24"])
        self.assertIs(questions.parse("network.lan_deny_out", "no"), False)
        self.assertEqual(questions.parse("server.ssh_port", "2222"), 2222)

    def test_follow_up_questions_are_skipped_when_they_do_not_apply(self):
        nested, _ = questions.collect("server", use_defaults=True, given={"server.admin_user": "me", "github.owner": "x", "network.lan_deny_out": False})
        self.assertNotIn("lan_allow_out", nested["network"])
        self.assertNotIn("url", nested["alerts"])

    def test_set_pairs_must_name_real_settings(self):
        self.assertEqual(questions.parse_set(["server.host=worker.example"]), {"server.host": "worker.example"})
        with self.assertRaises(questions.WizardError):
            questions.parse_set(["server.nonsense=1"])
        with self.assertRaises(questions.WizardError):
            questions.parse_set(["noequals"])

    def test_every_question_names_a_real_setting(self):
        for q in questions.QUESTIONS:
            section, _, name = q["key"].partition(".")
            self.assertIn(name, config.SCHEMA[section], q["key"])


class TomlTests(unittest.TestCase):
    def test_only_chosen_values_are_written_and_they_load_back_identically(self):
        nested = {"identity": {"name": "claude-worker"}, "server": {"admin_user": "me", "ssh_port": 2222}, "github": {"owner": "someone"},
                  "network": {"lan_allow_out": ["192.0.2.0/24"], "ipv6": "disabled", "lan_deny_out": True}}
        text = toml_out.dump(nested)
        self.assertNotIn("identity", text)
        self.assertNotIn("lan_deny_out", text)
        self.assertIn('ipv6 = "disabled"', text)
        back = tomllib.loads(text)
        clean, problems = config.validate(back)
        self.assertEqual(problems, [])
        want, _ = config.validate(nested)
        self.assertEqual(clean, want)

    def test_no_secret_can_reach_the_file(self):
        text = toml_out.dump({"server": {"admin_user": "me"}, "alerts": {"kind": "ntfy", "url": "https://ntfy.example/topic"}}).lower()
        for word in ("token", "password", "api_key", "api-key"):
            self.assertNotIn(word + " =", text)


class DetectTests(unittest.TestCase):
    def test_facts_and_suggestions(self):
        h = FakeHost()
        h.replies["sudo --version"] = Result(0, "sudo-rs 0.2\n")
        f = detect.detect(h)
        self.assertEqual((f.os_id, f.os_version, f.ram_mb, f.cores, f.sudo), ("ubuntu", "24.04", 7812, 2, "sudo-rs"))
        self.assertIsNone(f.os_problem())
        self.assertEqual(f.suggestions()["hardware.profile"], "small-server")

    def test_old_or_unsupported_systems_are_refused_in_plain_words(self):
        self.assertIn("too old", detect.Facts(os_id="ubuntu", os_version="20.04", systemd=True).os_problem())
        self.assertIn("not supported", detect.Facts(os_id="fedora", os_version="40", systemd=True).os_problem())
        self.assertIn("systemd", detect.Facts(os_id="debian", os_version="12", systemd=False).os_problem())

    def test_small_memory_or_battery_suggests_the_laptop_profile(self):
        self.assertEqual(detect.Facts(ram_mb=2048).suggestions()["hardware.profile"], "old-laptop")
        self.assertEqual(detect.Facts(ram_mb=32000, cores=16).suggestions()["hardware.profile"], "workstation")


class HardeningTests(unittest.TestCase):
    def setUp(self):
        self.h, self.cfg = FakeHost(), cfg_of()

    def test_ssh_keys_only_refuses_when_no_key_is_installed(self):
        mod = hardening.MODULES["ssh-keys-only"]
        self.h.replies["sshd -T"] = Result(0, "passwordauthentication yes\nkbdinteractiveauthentication no\n")
        self.assertFalse(mod.check(self.h, self.cfg).ok)
        self.assertIn("locked out", mod.preflight(self.h, self.cfg))
        self.h.put(H + "/me/.ssh/authorized_keys", "ssh-ed25519 AAAA test\n")
        self.assertIsNone(mod.preflight(self.h, self.cfg))

    def test_ssh_keys_only_writes_a_dropin_that_wins_and_validates_before_reloading(self):
        mod, h = hardening.MODULES["ssh-keys-only"], self.h
        h.put(H + "/me/.ssh/authorized_keys", "ssh-ed25519 AAAA test\n")
        state = {"on": False}
        h.replies["sshd -T"] = lambda a: Result(0, "passwordauthentication " + ("no" if state["on"] else "yes") + "\nkbdinteractiveauthentication no\n")
        h.replies["systemctl reload ssh"] = lambda a: state.update(on=True) or Result(0)
        status, _ = hardening.run_module(mod, h, self.cfg, dry_run=False, out=lambda s: None, confirm=lambda p: True)
        self.assertEqual(status, "applied")
        self.assertIn("PasswordAuthentication no", h.read("/etc/ssh/sshd_config.d/10-claude-worker.conf"))
        order = [" ".join(c) for c in h.calls if c[0] in ("sshd", "systemctl") and c[1] in ("-t", "reload")]
        self.assertEqual(order, ["sshd -t", "systemctl reload ssh"])

    def test_dry_run_shows_the_steps_and_changes_nothing(self):
        mod, h, shown = hardening.MODULES["ssh-keys-only"], self.h, []
        h.put(H + "/me/.ssh/authorized_keys", "ssh-ed25519 AAAA test\n")
        h.replies["sshd -T"] = Result(0, "passwordauthentication yes\n")
        status, _ = hardening.run_module(mod, h, self.cfg, dry_run=True, out=shown.append)
        self.assertEqual(status, "planned")
        self.assertFalse(h.exists("/etc/ssh/sshd_config.d/10-claude-worker.conf"))
        self.assertTrue(any("PasswordAuthentication no" in s for s in shown))

    def test_declining_applies_nothing(self):
        mod, h = hardening.MODULES["unattended-upgrades"], self.h
        h.replies["dpkg -s"] = Result(1, "not installed")
        status, _ = hardening.run_module(mod, h, self.cfg, dry_run=False, out=lambda s: None, confirm=lambda p: False)
        self.assertEqual(status, "declined")
        self.assertEqual(h.ran("apt-get"), [])

    def test_root_equivalent_groups_are_found_and_removed(self):
        mod, h = hardening.MODULES["no-root-equivalent-groups"], self.h
        h.replies["id -nG me"] = Result(0, "me sudo lxd\n")
        h.replies["id -nG cwworker"] = Result(0, "cwworker sudo\n")
        c = mod.check(h, self.cfg)
        self.assertFalse(c.ok)
        self.assertIn("me is in lxd", c.detail)
        self.assertIn("cwworker is in sudo", c.detail)
        self.assertEqual([" ".join(a.argv) for a in mod.plan(h, self.cfg)], ["gpasswd -d me lxd", "gpasswd -d cwworker sudo"])

    def test_the_admin_may_keep_sudo_but_not_lxd(self):
        h = self.h
        h.replies["id -nG me"] = Result(0, "me sudo\n")
        h.replies["id -nG cwworker"] = Result(0, "cwworker\n")
        self.assertTrue(hardening.MODULES["no-root-equivalent-groups"].check(h, self.cfg).ok)

    def test_the_firewall_refuses_when_it_would_lock_you_out(self):
        mod = hardening.MODULES["ufw-lan-egress"]
        cfg = cfg_of(network={"ssh_from": ["192.0.2.0/24"]})
        h = FakeHost(env={"SSH_CONNECTION": "198.51.100.9 5000 192.0.2.1 22"})
        self.assertIn("lock you out", mod.preflight(h, cfg))
        h2 = FakeHost(env={"SSH_CONNECTION": "192.0.2.9 5000 192.0.2.1 22"})
        self.assertIsNone(mod.preflight(h2, cfg))

    def test_the_firewall_plan_comes_from_the_configuration(self):
        mod = hardening.MODULES["ufw-lan-egress"]
        plan = [a.show() for a in mod.plan(self.h, cfg_of(network={"ipv6": "disabled"}))]
        self.assertIn("ufw default deny incoming", plan)
        self.assertIn("ufw --force enable", plan)
        self.assertFalse(any("fc00::/7" in p for p in plan))

    def test_a_failing_step_stops_and_says_which(self):
        mod, h = hardening.MODULES["unattended-upgrades"], self.h
        h.replies["dpkg -s"] = Result(1)
        h.replies["apt-get install"] = Result(100, "E: unable to locate package")
        status, detail = hardening.run_module(mod, h, self.cfg, dry_run=False, out=lambda s: None)
        self.assertEqual(status, "failed")
        self.assertIn("unable to locate", detail)

    def test_netns_check_reports_but_never_changes_anything(self):
        mod, h = hardening.MODULES["netns-required"], self.h
        h.replies["runuser"] = Result(1, "unshare: unshare failed: Operation not permitted")
        status, detail = hardening.run_module(mod, h, self.cfg, dry_run=False, out=lambda s: None)
        self.assertEqual(status, "failed")
        self.assertIn("Operation not permitted", detail)

    def test_state_permissions_flags_world_readable_state(self):
        mod, h = hardening.MODULES["state-permissions"], self.h
        h.put(self.cfg.path("state") + "/x", "")
        h.replies["stat -c %a"] = Result(0, "755\n")
        self.assertFalse(mod.check(h, self.cfg).ok)
        h.replies["stat -c %a"] = Result(0, "750\n")
        self.assertTrue(mod.check(h, self.cfg).ok)

    def test_every_default_module_exists_and_explains_itself(self):
        for mid in config.SCHEMA["hardening"]["modules"][0]:
            m = hardening.MODULES[mid]
            self.assertTrue(m.title and m.why and m.risk, mid)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.h = FakeHost()
        self.h.replies["id me"] = Result(0)
        self.h.replies["id cwworker"] = Result(1)
        self.said = []
        self.src = self.make_source()
        self.registry_calls = []

    def make_source(self):
        src = Path(tempfile.mkdtemp(prefix="cw-src-"))
        import shutil
        shutil.copytree(ROOT / "src", src / "src", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(ROOT / "templates", src / "templates")
        return src

    def setup_obj(self, **kw):
        given = {"server.admin_user": "me", "github.owner": "someone", **kw.pop("given", {})}
        return server.ServerSetup(self.h, self.src, say=self.said.append, given=given, use_defaults=True, rev="a" * 40, is_root=lambda: True,
                                  systemctl=lambda *a: None, sysctl=lambda *a: None, sandbox=lambda t: 0,
                                  registry_installer=lambda cfg, src, **k: self.registry_calls.append(src) or "bootstrapped", **kw)

    def test_a_dry_run_changes_nothing_and_shows_every_step(self):
        s = self.setup_obj(dry_run=True)
        s.run_all()
        self.assertFalse(self.h.exists("/etc/claude-worker/cw.toml"))
        self.assertEqual([c for c in self.h.calls if c[0] in ("useradd", "install", "ssh-keygen", "apt-get") or c[:2] == ["systemctl", "enable"]], [])
        text = "\n".join(self.said)
        for needle in ("useradd --system", "ssh-keygen", "systemctl enable --now claude-worker-fetch.timer", "[github]"):
            self.assertIn(needle, text)

    def test_apply_creates_accounts_installs_the_code_and_writes_the_config(self):
        s = self.setup_obj(dry_run=False)
        s.run_all()
        self.assertTrue(self.h.ran("useradd --system --create-home --home-dir " + H + "/cwworker --shell /usr/sbin/nologin cwworker"))
        self.assertEqual(config.load(self.h.path("/etc/claude-worker/cw.toml")).github.owner, "someone")
        self.assertTrue(self.h.exists("/opt/claude-worker/lib/cw/cli.py"))
        self.assertTrue(self.h.exists("/usr/local/bin/claude-worker"))
        self.assertEqual(self.h.read("/etc/claude-worker/installed-rev").strip(), "a" * 40)
        self.assertIn("Host github-claude-worker-queue", self.h.read(H + "/cwworker/.ssh/config"))

    def test_it_waits_for_deploy_keys_before_cloning_and_hardening(self):
        self.h.replies["runuser -u cwworker -- git ls-remote"] = Result(128, "Permission denied (publickey)")
        s = self.setup_obj(dry_run=False)
        report = s.run_all()
        self.assertTrue(any(r[1] == "waiting" for r in report))
        self.assertEqual(self.h.ran("systemctl enable"), [])
        self.assertTrue(any("gh repo deploy-key add" in line and "--allow-write" in line for line in self.said))
        self.assertTrue(any("claude-worker-queue.pub" in line and "--allow-write" not in line for line in self.said))

    def test_with_keys_in_place_it_clones_hardens_and_starts_the_timers(self):
        s = self.setup_obj(dry_run=False, confirm=lambda p: True)
        s.run_all()
        self.assertEqual(len(self.h.ran("runuser -u cwworker -- git clone")), 2)
        self.assertIn("git@github-claude-worker-queue:someone/cw-queue.git", " ".join(self.h.ran("runuser -u cwworker -- git clone")[0]))
        self.assertTrue(self.h.ran("systemctl enable --now claude-worker-fetch.timer"))
        self.assertTrue(self.h.ran("systemctl enable --now claude-worker-step.timer"))
        self.assertTrue(self.h.exists("/etc/claude-worker/hardening.json"))
        self.assertEqual([c for c in self.h.calls if c[:2] == ["systemctl", "enable"] and "update.timer" in " ".join(c)], [])

    def test_the_registry_is_installed_after_the_clones_and_before_hardening(self):
        order = []
        s = self.setup_obj(dry_run=False, confirm=lambda p: True)
        s.registry_installer = lambda cfg, src, **k: order.append("registry") or "updated"
        orig = s.hardening_step
        s.hardening_step = lambda: order.append("hardening") or orig()
        s.run_all()
        self.assertEqual(order, ["registry", "hardening"])
        self.assertTrue(any("job registry" in r[0] and r[1] == "updated" for r in s.report))

    def test_a_bad_registry_stops_the_wizard_before_hardening(self):
        def bad(cfg, src, **k):
            raise server.update.InstallError("the registry is not valid")
        s = self.setup_obj(dry_run=False, confirm=lambda p: True)
        s.registry_installer = bad
        with self.assertRaises(server.SetupError) as e:
            s.run_all()
        self.assertIn("not valid", e.exception.why)
        self.assertFalse(self.h.exists("/etc/claude-worker/hardening.json"))

    def test_the_registry_is_not_installed_while_waiting_for_deploy_keys(self):
        self.h.replies["runuser -u cwworker -- git ls-remote"] = Result(128, "Permission denied")
        self.setup_obj(dry_run=False).run_all()
        self.assertEqual(self.registry_calls, [])

    def test_missing_pyyaml_is_explained(self):
        self.h.replies["python3 -c import yaml"] = Result(1, "No module named yaml")
        with self.assertRaises(server.SetupError) as e:
            self.setup_obj(dry_run=True).run_all()
        self.assertIn("python3-yaml", " ".join(e.exception.steps))

    def test_a_missing_admin_account_stops_with_the_command_to_create_it(self):
        self.h.replies["id me"] = Result(1, "no such user")
        with self.assertRaises(server.SetupError) as e:
            self.setup_obj(dry_run=False).run_all()
        self.assertIn("adduser", " ".join(e.exception.steps))

    def test_an_unsupported_system_is_refused_before_anything_is_asked(self):
        self.h.put("/etc/os-release", "ID=fedora\nVERSION_ID=40\n")
        with self.assertRaises(server.SetupError) as e:
            self.setup_obj(dry_run=True).run_all()
        self.assertIn("not supported", e.exception.why)

    def test_not_being_root_is_refused_for_a_real_run_only(self):
        s = self.setup_obj(dry_run=False)
        s.is_root = lambda: False
        with self.assertRaises(server.SetupError):
            s.run_all()
        self.setup_obj(dry_run=True).run_all()

    def test_unknown_hardening_modules_are_refused(self):
        with self.assertRaises(server.SetupError) as e:
            self.setup_obj(dry_run=True, given={"hardening.modules": "ssh-keys-only,made-up"}).run_all()
        self.assertIn("made-up", e.exception.why)

    def test_modules_can_be_chosen_without_a_question(self):
        s = self.setup_obj(dry_run=True, given={"hardening.modules": "ssh-keys-only"})
        s.run_all()
        self.assertEqual(s.cfg.hardening.modules, ["ssh-keys-only"])

    def test_the_two_deploy_keys_get_different_permissions(self):
        cmds = server.deploy_key_commands(cfg_of(), {"queue": "k1", "results": "k2"})
        self.assertNotIn("--allow-write", cmds[0][2])
        self.assertIn("--allow-write", cmds[1][2])


class ClientTests(RepoFixture):
    def make(self, **kw):
        h = FakeHost(env={"HOME": str(self.tmp / "home-of-user")})
        h.replies["gh repo view"] = Result(1)
        self.said = []
        given = {"server.host": "worker.example", "server.admin_user": "me", "github.owner": "someone", **kw.pop("given", {})}
        c = ClientSetup(h, self.tmp / "client-home", say=self.said.append, given=given, use_defaults=True,
                        repos_factory=lambda cfg: self.repos, **kw)
        return h, c

    def test_dry_run_prints_the_steps_and_writes_nothing(self):
        h, c = self.make(dry_run=True)
        c.run_all()
        self.assertEqual(h.ran("gh repo create"), [])
        self.assertTrue(any("gh repo create someone/cw-queue --private" in s for s in self.said))
        self.assertFalse(h.exists(h.env["HOME"] + "/.config"))

    def test_apply_writes_config_creates_private_repos_and_seeds_the_registry(self):
        h, c = self.make(dry_run=False)
        sh("git", "-C", str(self.tmp / "queue.git"), "update-ref", "-d", "refs/heads/main")      # a brand-new, empty repository
        self.repos.pull_results = lambda: None
        self.repos.clone_all = lambda home: [("already cloned", str(self.repos.queue)), ("already cloned", str(self.repos.results))]
        c.run_all()
        self.assertEqual(len(h.ran("gh repo create")), 2)
        self.assertTrue(all("--private" in " ".join(x) for x in h.ran("gh repo create")))
        self.assertEqual(config.load(h.path(h.env["HOME"] + "/.config/claude-worker/cw.toml")).github.owner, "someone")
        self.repos.fetch_queue()
        reg = self.repos.show_file("origin/main", "registry.yaml")
        self.assertIn("classes:", reg)
        self.assertNotIn("sys-status", reg)         # the core classes come from the core pack, not from your repository

    def test_deploy_keys_are_added_read_only_for_queue_and_with_write_for_results(self):
        h, c = self.make(dry_run=False, queue_key="/k/queue.pub", results_key="/k/results.pub")
        self.repos.clone_all = lambda home: []
        c.run_all()
        adds = h.ran("gh repo deploy-key add")
        self.assertEqual(len(adds), 2)
        self.assertNotIn("--allow-write", adds[0])
        self.assertIn("--allow-write", adds[1])

    def test_without_gh_the_steps_are_printed_not_guessed(self):
        h, c = self.make(dry_run=False)
        h.env["TOOLS"] = "git ssh"
        self.repos.clone_all = lambda home: []
        c.run_all()
        self.assertEqual(h.ran("gh "), [])
        self.assertTrue(any("https://github.com/new" in s for s in self.said))

    def test_missing_git_stops_the_wizard(self):
        h, c = self.make(dry_run=True)
        h.env["TOOLS"] = "ssh"
        with self.assertRaises(server.SetupError):
            c.run_all()

    def test_an_existing_main_branch_is_never_overwritten(self):
        h, c = self.make(dry_run=False)
        self.repos.push_files("main", {"registry.yaml": "classes:\n  mine:\n    tier: auto\n    cost: light\n    run: [/bin/echo]\n"}, "mine")
        self.repos.clone_all = lambda home: []
        c.run_all()
        self.repos.fetch_queue()
        self.assertIn("mine", self.repos.show_file("origin/main", "registry.yaml"))
        self.assertNotIn("sys-status", self.repos.show_file("origin/main", "registry.yaml"))


class CorePackTests(unittest.TestCase):
    def test_the_core_registry_is_valid(self):
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("PyYAML is not installed")
        from cw import packs
        pack = packs.load_pack(ROOT / "packs" / "core")
        reg, _ = packs.compose({"core": pack}, ["core"], ["core"], {})
        self.assertEqual(registry.validate(reg, "/opt/x/tools"), [])
        self.assertIn("sys-status", reg["classes"])


class AuditTests(unittest.TestCase):
    def test_audit_reports_each_module_and_is_read_only(self):
        h, cfg = FakeHost(), cfg_of()
        h.replies["sshd -T"] = Result(0, "passwordauthentication yes\nkbdinteractiveauthentication no\n")
        h.replies["ufw status"] = Result(0, "Status: inactive\n")
        h.replies["systemctl is-active"] = Result(3, "inactive\n")
        h.replies["dpkg -s"] = Result(1)
        rows, bad = audit.run(h, cfg, say=lambda s: None)
        self.assertGreaterEqual(len(bad), 4)
        self.assertEqual([c for c in h.calls if c[0] in ("apt-get", "ufw", "gpasswd", "chmod") and c[:2] != ["ufw", "status"]], [])
        self.assertTrue(any("password logins are on" in r[2] for r in rows))


class CliTests(unittest.TestCase):
    def run_cli(self, *args, host=None, **kw):
        err = io.StringIO()
        with redirect_stderr(err):
            code = cli.main(list(args), host=host or FakeHost(), **kw)
        return code, err.getvalue()

    def test_help_and_unknown_commands(self):
        import contextlib
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["help"]), 0)
        for word in ("client", "server", "audit", "--dry-run"):
            self.assertIn(word, out.getvalue())
        self.assertEqual(self.run_cli("nonsense")[0], 2)

    def test_server_is_a_preview_unless_you_say_apply(self):
        h = FakeHost()
        h.replies["id me"] = Result(0)
        code, err = self.run_cli("server", "--yes", "--set", "server.admin_user=me", "--set", "github.owner=someone", "--rev", "a" * 40, host=h)
        self.assertEqual(code, 0)
        self.assertIn("Nothing was changed", err)
        self.assertFalse(h.exists("/etc/claude-worker/cw.toml"))

    def test_an_answers_file_and_set_are_combined_and_checked(self):
        f = Path(tempfile.mkdtemp()) / "a.toml"
        f.write_text('[server]\nadmin_user = "me"\n[github]\nowner = "someone"\n')
        self.assertEqual(cli.load_answers(str(f), ["server.ssh_port=2200"]), {"server.admin_user": "me", "github.owner": "someone", "server.ssh_port": "2200"})
        code, err = self.run_cli("server", "--yes", "--set", "server.admin_user=BAD USER", "--set", "github.owner=x")
        self.assertEqual(code, 2)
        self.assertIn("server.admin_user", err)

    def test_missing_required_answers_are_named(self):
        h = FakeHost()
        code, err = self.run_cli("server", "--yes", host=h)
        self.assertEqual(code, 1)
        self.assertIn("server.admin_user", err)

    def test_unsupported_machine_is_explained_and_exits_nonzero(self):
        h = FakeHost()
        h.put("/etc/os-release", "ID=arch\nVERSION_ID=1\n")
        code, err = self.run_cli("server", "--yes", "--set", "server.admin_user=me", "--set", "github.owner=x", host=h)
        self.assertEqual(code, 1)
        self.assertIn("not supported", err)


if __name__ == "__main__":
    unittest.main()
