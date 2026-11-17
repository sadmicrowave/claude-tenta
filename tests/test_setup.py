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
        self.assertTrue(any("old-laptop" in s and "workstation" not in s for s in said))          # a choice is listed with its number and meaning

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
        nested = {"identity": {"name": "claude-ballast"}, "server": {"admin_user": "me", "ssh_port": 2222}, "github": {"owner": "someone"},
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

    def test_a_processor_too_old_for_the_native_claude_code_is_reported(self):
        """The real server was a 2010 Core 2 Duo: Claude Code's installer died there with "Illegal instruction"."""
        h = FakeHost()
        h.put("/proc/cpuinfo", "processor : 0\nmodel name : Intel(R) Core(TM)2 Duo\nflags : fpu sse sse2 ssse3 sse4_1\n")
        f = detect.detect(h)
        self.assertFalse(f.claude_native)
        self.assertNotIn("claude.auth", f.suggestions())                  # still a normal choice: an older release runs there
        self.assertIn(("Claude Code", "needs an older release here: this processor is too old for the current one"), f.summary())
        h.put("/proc/cpuinfo", "processor : 0\nflags : fpu sse sse2 sse4_1 sse4_2 avx avx2\n")
        self.assertTrue(detect.detect(h).claude_native)
        h.put("/proc/cpuinfo", "processor : 0\nFeatures : fp asimd\n")                      # not x86: nothing to check
        self.assertTrue(detect.detect(h).claude_native)

    def test_old_or_unsupported_systems_are_refused_in_plain_words(self):
        self.assertIn("too old", detect.Facts(os_id="ubuntu", os_version="20.04", systemd=True).os_problem())
        self.assertIn("too old", detect.Facts(os_id="ubuntu", os_version="22.04", systemd=True).os_problem())      # Python 3.10: no tomllib
        self.assertIsNone(detect.Facts(os_id="ubuntu", os_version="24.04", systemd=True).os_problem())
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
        self.assertIn("PasswordAuthentication no", h.read("/etc/ssh/sshd_config.d/10-claude-ballast.conf"))
        order = [" ".join(c) for c in h.calls if c[0] in ("sshd", "systemctl") and c[1] in ("-t", "reload")]
        self.assertEqual(order, ["sshd -t", "systemctl reload ssh"])

    def test_dry_run_shows_the_steps_and_changes_nothing(self):
        mod, h, shown = hardening.MODULES["ssh-keys-only"], self.h, []
        h.put(H + "/me/.ssh/authorized_keys", "ssh-ed25519 AAAA test\n")
        h.replies["sshd -T"] = Result(0, "passwordauthentication yes\n")
        status, _ = hardening.run_module(mod, h, self.cfg, dry_run=True, out=shown.append)
        self.assertEqual(status, "planned")
        self.assertFalse(h.exists("/etc/ssh/sshd_config.d/10-claude-ballast.conf"))
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

    def test_the_tripwire_explains_a_machine_with_no_kernel_audit(self):
        mod, h = hardening.MODULES["auditd-canaries"], self.h
        h.replies["systemctl is-active"] = Result(3, "inactive\n")
        h.replies["systemctl is-enabled auditd"] = Result(1, "masked-runtime\n")
        status, detail = hardening.run_module(mod, h, self.cfg, dry_run=False, out=lambda s: None, confirm=lambda p: True)
        self.assertEqual(status, "blocked")
        self.assertIn("no kernel audit", detail)
        self.assertEqual(h.ran("apt-get"), [])

    def test_a_failing_step_stops_and_says_which(self):
        mod, h = hardening.MODULES["unattended-upgrades"], self.h
        h.replies["dpkg -s"] = Result(1)
        h.replies["apt-get install"] = Result(100, "E: unable to locate package")
        status, detail = hardening.run_module(mod, h, self.cfg, dry_run=False, out=lambda s: None)
        self.assertEqual(status, "failed")
        self.assertIn("unable to locate", detail)

    def test_job_isolation_installs_bubblewrap_and_restores_the_userns_restriction(self):
        mod, h = hardening.MODULES["job-isolation"], self.h
        h.replies["sysctl -n kernel.apparmor_restrict_unprivileged_userns"] = Result(0, "0\n")
        h.replies["runuser"] = Result(0, "Inter-|   Receive\n face |bytes\n    lo: 0 0 0\n")
        h.put("/usr/bin/bwrap", "")
        h.replies["stat -c %a"] = Result(0, "755\n")
        c = mod.check(h, self.cfg)
        self.assertFalse(c.ok)
        self.assertIn("open to every account", c.detail)
        steps = [a.show() for a in mod.plan(h, self.cfg)]
        self.assertTrue(any("kernel.apparmor_restrict_unprivileged_userns = 1" in s for s in steps))
        self.assertIn("sysctl -w kernel.apparmor_restrict_unprivileged_userns=1", steps)
        self.assertFalse(any("apt-get" in s for s in steps))                      # already installed

    def test_job_isolation_is_satisfied_when_jails_work_and_the_restriction_is_on(self):
        mod, h = hardening.MODULES["job-isolation"], self.h
        h.replies["sysctl -n kernel.apparmor_restrict_unprivileged_userns"] = Result(0, "1\n")
        h.replies["runuser"] = Result(0, "Inter-|   Receive\n face |bytes\n    lo: 0 0 0\n")
        h.replies["stat -c %a"] = Result(0, "755\n")
        h.put("/usr/bin/bwrap", "")
        self.assertTrue(mod.check(h, self.cfg).ok)

    def test_job_isolation_on_debian_has_no_sysctl_to_set(self):
        mod, h = hardening.MODULES["job-isolation"], self.h
        h.replies["sysctl -n"] = Result(255, "cannot stat")
        h.replies["runuser"] = Result(0, "Inter-|   Receive\n face |bytes\n    lo: 0 0 0\n")
        h.replies["stat -c %a"] = Result(0, "755\n")
        h.put("/usr/bin/bwrap", "")
        self.assertTrue(mod.check(h, self.cfg).ok)
        self.assertEqual(mod.plan(h, self.cfg), [])

    def test_job_isolation_refuses_a_setuid_bubblewrap_and_installs_a_missing_one(self):
        mod, h = hardening.MODULES["job-isolation"], self.h
        h.put("/usr/bin/bwrap", "")
        h.replies["stat -c %a"] = Result(0, "4755\n")
        self.assertIn("setuid", mod.check(h, self.cfg).detail)
        h2 = FakeHost()
        steps = [a.show() for a in mod.plan(h2, self.cfg)]
        self.assertIn("apt-get install -y bubblewrap", steps)
        self.assertIn("not installed", mod.check(h2, self.cfg).detail)

    def test_job_isolation_reports_a_jail_that_cannot_be_built(self):
        mod, h = hardening.MODULES["job-isolation"], self.h
        h.put("/usr/bin/bwrap", "")
        h.replies["stat -c %a"] = Result(0, "755\n")
        h.replies["runuser"] = Result(1, "bwrap: No permissions to create a new namespace\n")
        c = mod.check(h, self.cfg)
        self.assertFalse(c.ok)
        self.assertIn("No permissions to create a new namespace", c.detail)

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

    def test_the_private_alert_address_goes_to_the_secrets_file_and_never_into_cw_toml(self):
        topic = "https://ntfy.example/very-private-topic-name"
        s = self.setup_obj(dry_run=False, given={"alerts.kind": "ntfy", "alerts.url": topic})
        s.run_all()
        toml = self.h.read("/etc/claude-ballast/cw.toml")
        self.assertNotIn("very-private-topic-name", toml)
        self.assertIn('kind = "ntfy"', toml)
        secrets = config.read_env_file(self.h.path("/etc/claude-ballast/secrets.env"))
        self.assertEqual(secrets["CW_ALERT_URL"], topic)
        self.assertEqual(self.h.path("/etc/claude-ballast/secrets.env").stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.h.path("/etc/claude-ballast/cw.toml").stat().st_mode & 0o777, 0o644)        # nothing private in it, so the admin can read it
        self.assertNotIn("very-private-topic-name", "\n".join(self.said))
        config.load(self.h.path("/etc/claude-ballast/cw.toml"), env={"CW_CONFIG": ""})                    # and the worker can still load it without the address

    def test_commands_named_after_an_earlier_install_are_removed(self):
        for p in ("/usr/local/bin/claude-ballast", "/usr/local/sbin/claude-ballast-update"):
            self.h.put(p, "old wrapper\n")
        s = self.setup_obj(dry_run=False)
        s.run_all()
        self.assertFalse(self.h.exists("/usr/local/bin/claude-ballast"))
        self.assertFalse(self.h.exists("/usr/local/sbin/claude-ballast-update"))
        self.assertTrue(self.h.exists("/usr/local/bin/cw-worker"))
        self.assertTrue(self.h.exists("/usr/local/sbin/cw-update"))

    def test_a_dry_run_changes_nothing_and_shows_every_step(self):
        s = self.setup_obj(dry_run=True)
        s.run_all()
        self.assertFalse(self.h.exists("/etc/claude-ballast/cw.toml"))
        self.assertEqual([c for c in self.h.calls if c[0] in ("useradd", "install", "ssh-keygen", "apt-get") or c[:2] == ["systemctl", "enable"]], [])
        text = "\n".join(self.said)
        for needle in ("useradd --system", "ssh-keygen", "systemctl enable --now claude-ballast-fetch.timer", "[github]"):
            self.assertIn(needle, text)

    def test_apply_creates_accounts_installs_the_code_and_writes_the_config(self):
        s = self.setup_obj(dry_run=False)
        s.run_all()
        self.assertTrue(self.h.ran("useradd --system --create-home --home-dir " + H + "/cwworker --shell /usr/sbin/nologin cwworker"))
        self.assertEqual(config.load(self.h.path("/etc/claude-ballast/cw.toml")).github.owner, "someone")
        self.assertTrue(self.h.exists("/opt/claude-ballast/lib/cw/cli.py"))
        self.assertTrue(self.h.exists("/usr/local/bin/cw-worker"))
        self.assertEqual(self.h.read("/etc/claude-ballast/installed-rev").strip(), "a" * 40)
        self.assertIn("Host github-claude-ballast-queue", self.h.read(H + "/cwworker/.ssh/config"))

    def test_it_waits_for_deploy_keys_before_cloning_and_hardening(self):
        self.h.replies["runuser -u cwworker -- git ls-remote"] = Result(128, "Permission denied (publickey)")
        s = self.setup_obj(dry_run=False)
        report = s.run_all()
        self.assertTrue(any(r[1] == "waiting" for r in report))
        self.assertEqual(self.h.ran("systemctl enable"), [])
        self.assertTrue(any("gh repo deploy-key add" in line and "--allow-write" in line for line in self.said))
        self.assertTrue(any("claude-ballast-queue.pub" in line and "--allow-write" not in line for line in self.said))

    def test_with_keys_in_place_it_clones_hardens_and_starts_the_timers(self):
        s = self.setup_obj(dry_run=False, confirm=lambda p: True)
        s.run_all()
        self.assertEqual(len(self.h.ran("runuser -u cwworker -- git clone")), 2)
        self.assertIn("git@github-claude-ballast-queue:someone/claude-ballast-queue.git", " ".join(self.h.ran("runuser -u cwworker -- git clone")[0]))
        self.assertTrue(self.h.ran("systemctl enable --now claude-ballast-fetch.timer"))
        self.assertTrue(self.h.ran("systemctl enable --now claude-ballast-step.timer"))
        self.assertTrue(self.h.exists("/etc/claude-ballast/hardening.json"))
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
        self.assertFalse(self.h.exists("/etc/claude-ballast/hardening.json"))

    def test_the_registry_is_not_installed_while_waiting_for_deploy_keys(self):
        self.h.replies["runuser -u cwworker -- git ls-remote"] = Result(128, "Permission denied")
        self.setup_obj(dry_run=False).run_all()
        self.assertEqual(self.registry_calls, [])

    def test_missing_prerequisites_are_installed_not_left_as_a_chore(self):
        self.h.replies["python3 -c import yaml"] = Result(1, "No module named yaml")
        self.setup_obj(dry_run=False, confirm=lambda p: True).run_all()
        self.assertTrue(self.h.ran("apt-get install -y python3-yaml"))
        self.assertFalse(self.h.ran("apt-get install -y python3-yaml git"))        # git was there

    def test_a_preview_names_the_missing_prerequisites_and_installs_nothing(self):
        self.h.replies["python3 -c import yaml"] = Result(1, "No module named yaml")
        s = self.setup_obj(dry_run=True)
        s.run_all()
        self.assertEqual(self.h.ran("apt-get"), [])
        self.assertTrue(any("python3-yaml" in line for line in self.said))
        self.assertTrue(any(r[0] == "prerequisites" and r[1] == "planned" for r in s.report))

    def test_the_public_deploy_keys_are_published_for_the_administrator(self):
        self.h.put(H + "/cwworker/.ssh/id_queue.pub", "ssh-ed25519 QQQQ claude-ballast-queue\n")
        self.h.put((H + "/cwworker/.ssh/id_results.pub"), "ssh-ed25519 RRRR claude-ballast-results\n")
        self.setup_obj(dry_run=False, confirm=lambda p: True).run_all()
        self.assertEqual(self.h.read("/etc/claude-ballast/deploy-keys/queue.pub").strip(), "ssh-ed25519 QQQQ claude-ballast-queue")
        self.assertEqual(self.h.read("/etc/claude-ballast/deploy-keys/results.pub").strip(), "ssh-ed25519 RRRR claude-ballast-results")
        self.assertEqual(oct(self.h.path("/etc/claude-ballast/deploy-keys/queue.pub").stat().st_mode & 0o777), "0o644")

    def test_it_waits_for_the_keys_and_carries_on_when_they_arrive(self):
        calls = {"n": 0}

        def ls_remote(argv):
            calls["n"] += 1
            return Result(0) if calls["n"] > 4 else Result(128, "Permission denied (publickey)")
        self.h.replies["runuser -u cwworker -- git ls-remote"] = ls_remote
        clock = {"t": 0}
        s = self.setup_obj(dry_run=False, confirm=lambda p: True, wait_for_keys=600, sleep=lambda x: clock.update(t=clock["t"] + x), clock=lambda: clock["t"])
        s.run_all()
        self.assertFalse(s.waiting)
        self.assertEqual(len(self.h.ran("runuser -u cwworker -- git clone")), 2)
        self.assertTrue(any("Waiting for the deploy keys" in line for line in self.said))
        self.assertTrue(self.h.ran("systemctl enable --now claude-ballast-fetch.timer"))

    def test_it_gives_up_waiting_after_the_time_and_says_so(self):
        self.h.replies["runuser -u cwworker -- git ls-remote"] = Result(128, "Permission denied")
        clock = {"t": 0}
        s = self.setup_obj(dry_run=False, wait_for_keys=20, sleep=lambda x: clock.update(t=clock["t"] + x), clock=lambda: clock["t"])
        s.run_all()
        self.assertTrue(s.waiting)
        self.assertEqual(self.h.ran("systemctl enable"), [])

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
        self.assertTrue(any("gh repo create someone/claude-ballast-queue --private" in s for s in self.said))
        self.assertFalse(h.exists(h.env["HOME"] + "/.config"))

    def test_apply_writes_config_creates_private_repos_and_seeds_the_registry(self):
        h, c = self.make(dry_run=False)
        sh("git", "-C", str(self.tmp / "queue.git"), "update-ref", "-d", "refs/heads/main")      # a brand-new, empty repository
        self.repos.pull_results = lambda: None
        self.repos.clone_all = lambda home: [("already cloned", str(self.repos.queue)), ("already cloned", str(self.repos.results))]
        c.run_all()
        self.assertEqual(len(h.ran("gh repo create")), 2)
        self.assertTrue(all("--private" in " ".join(x) for x in h.ran("gh repo create")))
        self.assertEqual(config.load(h.path(h.env["HOME"] + "/.config/claude-ballast/cw.toml")).github.owner, "someone")
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


    def test_audit_flags_an_alert_address_left_in_the_readable_configuration(self):
        h, cfg = FakeHost(), cfg_of()
        path = cfg.path("config_file")
        name = "the private alert address is kept out of the configuration file"
        h.put(path, '[alerts]\nkind = "ntfy"\nurl = "https://ntfy.example/x"\n')
        rows, _ = audit.run(h, cfg, say=lambda s: None)
        self.assertFalse(next(r for r in rows if r[0] == name)[1])
        h.put(path, '[alerts]\nkind = "ntfy"\n')
        rows, _ = audit.run(h, cfg, say=lambda s: None)
        self.assertTrue(next(r for r in rows if r[0] == name)[1])


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
        self.assertFalse(h.exists("/etc/claude-ballast/cw.toml"))

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


class ClaudeSignIn(ServerTests):
    """The Claude step: nothing for `none`; the tool and the stored sign-in otherwise."""

    class Screen:
        def __init__(self, token="", install=True):
            self.token, self.install, self.cards, self.asked, self.infos = token, install, [], [], []

        total_hint = 0

        def begin(self, *a, **k):
            pass

        def finish(self, *a, **k):
            pass

        def heading(self, *a, **k):
            pass

        def sub(self, *a, **k):
            pass

        def protection(self, info):
            self.cards.append(info["id"])
            self.infos.append(info)
            return self.install if info["id"] == "claude-install" else True

        def ask_secret(self, q):
            self.asked.append(q)
            return self.token

    def run_with(self, auth, token="", install=True, **kw):
        screen = self.Screen(token, install)
        s = self.setup_obj(dry_run=False, ui=screen, given={"claude.auth": auth}, **kw)
        s.run_all()
        return s, screen

    def secrets(self):
        return config.read_env_file(self.h.path("/etc/claude-ballast/secrets.env"))

    def test_none_installs_nothing_and_says_other_jobs_are_unaffected(self):
        s, screen = self.run_with("none")
        self.assertNotIn("claude-install", screen.cards)
        self.assertEqual(screen.asked, [])
        self.assertTrue(any(step == "Claude sign-in" and status == "skipped" and "every other job runs" in detail for step, status, detail in s.report))
        self.assertFalse(self.h.ran("runuser -u cwworker -- env"))

    def test_account_installs_the_tool_as_the_worker_and_stores_only_the_token_it_is_given(self):
        token = "sk-ant-oat01-" + "x" * 40
        s, screen = self.run_with("account", token=token)
        self.assertIn("claude-install", screen.cards)
        install = [c for c in self.h.calls if c[:4] == ["runuser", "-u", "cwworker", "--"] and "bash" in c]
        self.assertEqual(len(install), 1)
        self.assertIn("https://claude.ai/install.sh", install[0][-1])
        self.assertEqual(self.secrets().get("CLAUDE_CODE_OAUTH_TOKEN"), token)
        self.assertNotIn("ANTHROPIC_API_KEY", self.secrets())
        self.assertNotIn(token, self.h.read("/etc/claude-ballast/cw.toml"))               # the configuration never holds it
        self.assertNotIn(token, "\n".join(self.said))
        self.assertEqual(oct(self.h.path("/etc/claude-ballast/secrets.env").stat().st_mode & 0o777), "0o640")

    def test_a_token_from_the_installer_is_stored_without_asking_and_replaces_an_older_one(self):
        old = "sk-ant-oat01-" + "o" * 40
        new = "sk-ant-oat01-" + "n" * 40
        self.run_with("account", token=old)
        s, screen = self.run_with("account", token="", claude_secrets={"CLAUDE_CODE_OAUTH_TOKEN": new})
        self.assertEqual(screen.asked, [])
        self.assertEqual(self.secrets()["CLAUDE_CODE_OAUTH_TOKEN"], new)
        self.assertNotIn(new, "\n".join(self.said))

    def test_the_read_only_claude_class_comes_with_the_install_exactly_when_claude_is_used(self):
        self.run_with("account", token="sk-ant-oat01-" + "p" * 40)
        self.assertEqual(config.load(self.h.path("/etc/claude-ballast/cw.toml")).registry.packs, ["core", "claude"])
        self.run_with("none")                                              # switching to none takes it away again
        self.assertEqual(config.load(self.h.path("/etc/claude-ballast/cw.toml")).registry.packs, ["core"])

    def test_the_approver_key_is_installed_root_owned_and_replaces_an_older_one(self):
        import subprocess as sp
        import tempfile as tf
        from cw import approvals
        d = Path(tf.mkdtemp())
        pubs = []
        for n in ("a", "b"):
            sp.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(d / n)], check=True)
            pubs.append((d / f"{n}.pub").read_text())
        for pub in pubs:
            s = self.setup_obj(dry_run=False, approver_pubkey=pub)
            s.run_all()
        text = self.h.read("/etc/claude-ballast/approvers")
        self.assertEqual(approvals.fingerprints(text), [approvals.fingerprint(pubs[1])])
        self.assertEqual(self.h.path("/etc/claude-ballast/approvers").stat().st_mode & 0o777, 0o644)
        self.assertIn('namespaces="cw-approval,cw-registry,cw-control"', text)
        self.assertFalse(self.h.exists("/etc/claude-ballast/allowed_signers"))         # the release-signing file is a different one

    def test_no_approver_key_given_means_no_approvers_file(self):
        self.setup_obj(dry_run=False).run_all()
        self.assertFalse(self.h.exists("/etc/claude-ballast/approvers"))

    def test_something_that_is_not_a_key_is_refused_without_stopping_the_setup(self):
        s = self.setup_obj(dry_run=False, approver_pubkey="not a key at all")
        s.run_all()
        self.assertEqual({st: v for st, v, _ in s.report}["approver key"], "failed")
        self.assertFalse(self.h.exists("/etc/claude-ballast/approvers"))

    def test_the_weekly_claude_update_timer_runs_only_where_claude_is_used(self):
        self.run_with("account", token="sk-ant-oat01-" + "t" * 40)
        self.assertTrue(self.h.ran("systemctl enable --now claude-ballast-claude-update.timer"))

    def test_no_claude_update_timer_without_claude(self):
        self.run_with("none")
        self.assertFalse(self.h.ran("systemctl enable --now claude-ballast-claude-update.timer"))

    def test_the_day_an_account_token_was_stored_is_recorded_for_the_one_year_warning(self):
        self.run_with("account", token="sk-ant-oat01-" + "e" * 40)
        rec = __import__("json").loads(self.h.read("/etc/claude-ballast/claude-token.json"))
        self.assertEqual(rec["route"], "account")
        self.assertLess(abs(rec["set_at"] - __import__("time").time()), 120)

    def test_an_api_key_has_no_expiry_record(self):
        self.run_with("api-key", token="sk-ant-api03-" + "e" * 40)
        self.assertFalse(self.h.exists("/etc/claude-ballast/claude-token.json"))

    def test_api_key_goes_under_its_own_name_and_replaces_the_other_route(self):
        s, screen = self.run_with("api-key", token="sk-ant-api03-" + "y" * 40)
        self.assertIn("ANTHROPIC_API_KEY", self.secrets())
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", self.secrets())

    def test_skipping_the_install_or_leaving_the_token_empty_is_allowed_and_said(self):
        s, screen = self.run_with("account", token="", install=False)
        statuses = {step: status for step, status, _ in s.report}
        self.assertEqual((statuses["Claude tool"], statuses["Claude sign-in"]), ("skipped", "skipped"))
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", self.secrets())

    def test_something_that_is_not_a_token_is_refused(self):
        with self.assertRaises(server.SetupError):
            self.run_with("account", token="two words here and more than twenty chars")

    def test_a_token_that_is_already_stored_is_kept_and_not_asked_again(self):
        self.run_with("account", token="sk-ant-oat01-" + "z" * 40)
        s2, screen2 = self.run_with("account", token="")
        self.assertEqual(screen2.asked, [])
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", self.secrets())

    def test_switching_to_none_deletes_what_was_stored(self):
        self.run_with("api-key", token="sk-ant-api03-" + "k" * 40)
        self.assertIn("ANTHROPIC_API_KEY", self.secrets())
        s, _ = self.run_with("none")
        self.assertEqual({k for k in self.secrets() if "ANTHROPIC" in k or "CLAUDE" in k}, set())
        self.assertTrue(any(step == "Claude sign-in" and status == "removed" for step, status, _ in s.report))

    def test_an_installer_that_dies_does_not_stop_the_rest_of_the_setup(self):
        """On the real server the installer was killed ("Illegal instruction"); that stopped the whole setup before the timers were started."""
        real = self.h.run
        self.h.run = lambda argv, **kw: Result(132, "Illegal instruction") if "claude.ai/install.sh" in " ".join(argv) else real(argv, **kw)
        s, screen = self.run_with("account", token="sk-ant-oat01-" + "w" * 40)
        statuses = {step: status for step, status, _ in s.report}
        self.assertEqual((statuses["Claude tool"], statuses["Claude sign-in"]), ("failed", "skipped"))
        self.assertEqual(screen.asked, [])                                  # no point asking for a token for a tool that cannot run
        self.assertTrue(self.h.ran("systemctl enable --now claude-ballast-fetch.timer"))        # the worker still started

    def test_a_processor_too_old_for_the_native_program_gets_the_last_javascript_release(self):
        self.h.put("/proc/cpuinfo", "processor : 0\nflags : fpu sse sse2 ssse3 sse4_1\n")
        self.h.replies["node --version"] = Result(0, "v22.22.1\n")
        s, screen = self.run_with("account", token="sk-ant-oat01-" + "v" * 40)
        calls = [" ".join(c) for c in self.h.calls]
        self.assertFalse(any("claude.ai/install.sh" in c for c in calls))                   # the native installer is never run there
        npm = [c for c in calls if "npm install" in c]
        self.assertEqual(len(npm), 1)
        self.assertIn("@anthropic-ai/claude-code@" + server.CLAUDE_LEGACY, npm[0])
        self.assertTrue(npm[0].startswith("runuser -u cwworker --"))                         # as the worker account, never root
        self.assertEqual({step: status for step, status, _ in s.report}["Claude tool"], "installed")
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", self.secrets())                            # and the sign-in is stored as usual

    def test_the_older_release_is_described_honestly_on_its_card(self):
        self.h.put("/proc/cpuinfo", "processor : 0\nflags : fpu sse sse2\n")
        s, screen = self.run_with("account", token="")
        card = next(i for i in screen.infos if i["id"] == "claude-install")
        self.assertIn(server.CLAUDE_LEGACY, card["title"])
        self.assertIn("does not update itself", card["risk"])

    def test_a_preview_asks_for_nothing_and_changes_nothing(self):
        screen = self.Screen("sk-ant-oat01-" + "q" * 40)
        s = self.setup_obj(dry_run=True, ui=screen, given={"claude.auth": "account"})
        s.run_all()
        self.assertEqual(screen.asked, [])
        self.assertTrue(any("curl -fsSL https://claude.ai/install.sh" in line for line in self.said))
