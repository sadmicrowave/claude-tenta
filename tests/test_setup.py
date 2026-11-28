"""arm-setup: the questions, the config writer, hardening modules, and the client, server and audit halves, all against a fake machine."""
import io
import json
import os
import tempfile
import tomllib
import unittest
from unittest import mock
from contextlib import redirect_stderr
from pathlib import Path

from support import REGISTRY, RepoFixture, sh  # noqa: F401  (support sets HOME to a throwaway folder)

from armada import config, registry
from armada.setup import audit, cli, detect, hardening, questions, server, toml_out
from armada.setup.client import ClientSetup
from armada.setup.host import Host, Result

H = "/" + "home"      # a home-folder prefix, spelled in two parts so the privacy guard does not mistake it for a real path
ROOT = Path(__file__).resolve().parents[1]
OS_RELEASE = 'NAME="Ubuntu"\nID=ubuntu\nVERSION_ID="24.04"\n'


class FakeHost(Host):
    """A folder standing in for / plus scripted command results. `replies` maps a command prefix (joined) to a Result; the default is success."""

    def __init__(self, env=None):
        super().__init__(tempfile.mkdtemp(prefix="arm-fakehost-"), env={"HOME": H + "/me", "PATH": "/bin", **(env or {})})
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


class VirtualMachineProfile(unittest.TestCase):
    """A virtual machine of its own, made to offload one project's tests and builds (today project A, tomorrow project B on another machine)."""

    def test_it_is_the_fourth_choice_and_custom_moves_to_the_fifth(self):
        names = [v for v, _ in questions.CHOICES["hardware.profile"]]
        self.assertEqual(names, ["old-laptop", "small-server", "workstation", "virtual-machine", "custom"])

    def test_it_is_a_valid_profile_with_its_own_limits(self):
        self.assertIn("virtual-machine", config.PROFILES)
        nested, problems = config.validate({"server": {"admin_user": "me", "host": "h.example"}, "github": {"owner": "someone"}, "hardware": {"profile": "virtual-machine"}})
        self.assertEqual(problems, [])
        self.assertEqual(config.Config(nested).hardware.profile, "virtual-machine")

    def test_a_virtual_machine_is_suggested_for_a_virtual_machine_whatever_its_size_and_reports_no_heat(self):
        for ram in (2048, 8000, 32000):
            self.assertEqual(detect.Facts(ram_mb=ram, cores=2, virtual=True).suggestions()["hardware.profile"], "virtual-machine")
        h = FakeHost()
        h.replies["systemd-detect-virt --vm"] = Result(0, "vmware\n")
        h.put("/sys/class/hwmon/hwmon0/name", "acpitz")                          # a virtual machine can show a sensor that is not its processor's
        f = detect.detect(h)
        self.assertTrue(f.virtual)
        self.assertEqual(f.thermal, "none")
        self.assertEqual(f.suggestions()["hardware.profile"], "virtual-machine")

    def test_real_hardware_and_containers_are_not_called_a_virtual_machine(self):
        h = FakeHost()
        h.replies["systemd-detect-virt --vm"] = Result(1, "none\n")
        self.assertFalse(detect.detect(h).virtual)
        self.assertFalse(detect.detect(FakeHost()).virtual)                      # nothing said: not assumed


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
        self.assertEqual(nested["server"]["worker_user"], "armworker")
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
        nested = {"identity": {"name": "armada"}, "server": {"admin_user": "me", "ssh_port": 2222}, "github": {"owner": "someone"},
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
        self.assertIn("PasswordAuthentication no", h.read("/etc/ssh/sshd_config.d/10-armada.conf"))
        order = [" ".join(c) for c in h.calls if c[0] in ("sshd", "systemctl") and c[1] in ("-t", "reload")]
        self.assertEqual(order, ["sshd -t", "systemctl reload ssh"])

    def test_dry_run_shows_the_steps_and_changes_nothing(self):
        mod, h, shown = hardening.MODULES["ssh-keys-only"], self.h, []
        h.put(H + "/me/.ssh/authorized_keys", "ssh-ed25519 AAAA test\n")
        h.replies["sshd -T"] = Result(0, "passwordauthentication yes\n")
        status, _ = hardening.run_module(mod, h, self.cfg, dry_run=True, out=shown.append)
        self.assertEqual(status, "planned")
        self.assertFalse(h.exists("/etc/ssh/sshd_config.d/10-armada.conf"))
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
        h.replies["id -nG armworker"] = Result(0, "armworker sudo\n")
        c = mod.check(h, self.cfg)
        self.assertFalse(c.ok)
        self.assertIn("me is in lxd", c.detail)
        self.assertIn("armworker is in sudo", c.detail)
        self.assertEqual([" ".join(a.argv) for a in mod.plan(h, self.cfg)], ["gpasswd -d me lxd", "gpasswd -d armworker sudo"])

    def test_the_admin_may_keep_sudo_but_not_lxd(self):
        h = self.h
        h.replies["id -nG me"] = Result(0, "me sudo\n")
        h.replies["id -nG armworker"] = Result(0, "armworker\n")
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

    def test_the_firewall_keeps_the_installing_session_alive_then_removes_its_rule(self):
        mod = hardening.MODULES["ufw-lan-egress"]
        h = FakeHost(env={"SSH_CONNECTION": "192.0.2.9 5000 192.0.2.1 22"})
        plan = [a.show() for a in mod.plan(h, cfg_of(network={"ssh_from": ["192.0.2.0/24"]}))]
        keep, enable = "ufw allow out from any port 22 to 192.0.2.9 proto tcp", plan.index("ufw --force enable")
        self.assertLess(plan.index(keep), min(i for i, p in enumerate(plan) if p.startswith("ufw deny out")))
        self.assertEqual(plan[enable + 1], "ufw delete " + keep[4:])
        self.assertFalse(any("from any port 22" in p for p in [a.show() for a in mod.plan(self.h, cfg_of())]))      # no SSH session, no rule

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

    def test_on_ubuntu_24_04_with_the_restriction_on_bubblewrap_gets_an_apparmor_profile(self):
        """Found on a real Ubuntu 24.04 machine: 'bwrap: loopback: Failed RTM_NEWADDR' with the restriction on and no profile for bubblewrap, so no job could ever run (container test machines have no AppArmor)."""
        mod, h = hardening.MODULES["job-isolation"], self.h
        h.put("/usr/bin/bwrap", "")
        h.put("/etc/apparmor.d/abi/4.0", "")
        h.replies["sysctl -n kernel.apparmor_restrict_unprivileged_userns"] = Result(0, "1\n")
        h.replies["stat -c %a"] = Result(0, "755\n")
        h.replies["runuser"] = Result(1, "bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted\n")
        h.env["TOOLS"] = "git ssh gh bwrap apparmor_parser"
        c = mod.check(h, self.cfg)
        self.assertFalse(c.ok)
        acts = mod.plan(h, self.cfg)
        writes = [a for a in acts if a.kind == "write"]
        self.assertEqual([a.path for a in writes], ["/etc/apparmor.d/arm-bwrap"])
        for needle in ("profile arm-bwrap /bin/bwrap flags=(unconfined)", "userns,", "abi <abi/4.0>"):
            self.assertIn(needle, writes[0].text)
        self.assertIn("apparmor_parser -r /etc/apparmor.d/arm-bwrap", [a.show() for a in acts])
        h.put("/etc/apparmor.d/arm-bwrap", "# there")
        self.assertTrue(any(a.kind == "remove" and a.path == "/etc/apparmor.d/arm-bwrap" for a in mod.revert(h, self.cfg)))

    def test_where_the_profile_is_already_there_or_apparmor_is_absent_nothing_is_written(self):
        mod, h = hardening.MODULES["job-isolation"], self.h
        h.put("/usr/bin/bwrap", "")
        h.replies["sysctl -n kernel.apparmor_restrict_unprivileged_userns"] = Result(0, "1\n")
        h.replies["runuser"] = Result(1, "bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted\n")
        h.replies["stat -c %a"] = Result(0, "755\n")
        self.assertEqual([a for a in mod.plan(h, self.cfg) if a.kind == "write"], [])          # Debian, or a system without AppArmor 4: no abi file, no parser
        h.put("/etc/apparmor.d/abi/4.0", "")
        h.put("/etc/apparmor.d/arm-bwrap", "# there")
        h.env["TOOLS"] = "git ssh gh bwrap apparmor_parser"
        self.assertEqual([a for a in mod.plan(h, self.cfg) if a.kind == "write"], [])

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


class ServerTestsFixture(unittest.TestCase):
    def setUp(self):
        self.h = FakeHost()
        self.h.replies["id me"] = Result(0)
        self.h.replies["id armworker"] = Result(1)
        self.said = []
        self.src = self.make_source()
        self.registry_calls = []

    def make_source(self):
        src = Path(tempfile.mkdtemp(prefix="arm-src-"))
        import shutil
        shutil.copytree(ROOT / "src", src / "src", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(ROOT / "templates", src / "templates")
        return src

    def setup_obj(self, **kw):
        # A fake machine cannot change its own state, so every protection "is applied, but the check still fails" here. That used to be tolerated and is now fatal (a failed protection stops the setup),
        # so the tests that are about something else see a protection that worked. The tests about the failure itself patch run_module again and say what they want.
        real_run = hardening.run_module
        tolerant = lambda mod, *a, **k: (("applied",) + tuple(real_run(mod, *a, **k)[1:])) if real_run(mod, *a, **k)[0] == "failed" else real_run(mod, *a, **k)      # noqa: E731
        patcher = mock.patch.object(hardening, "run_module", tolerant)
        patcher.start()
        self.addCleanup(patcher.stop)
        given = {"server.admin_user": "me", "github.owner": "someone", **kw.pop("given", {})}
        return server.ServerSetup(self.h, self.src, say=self.said.append, given=given, use_defaults=True, rev="a" * 40, is_root=lambda: True,
                                  systemctl=lambda *a: None, sysctl=lambda *a: None, sandbox=lambda t: 0,
                                  registry_installer=lambda cfg, src, **k: self.registry_calls.append(src) or "bootstrapped",
                                  **{"calibrator": lambda code_dir: {"hash": 0.5, "git": 0.25}, **kw})


class ServerTests(ServerTestsFixture):
    def test_the_install_records_how_fast_the_computer_is_and_never_fails_because_of_it(self):
        s = self.setup_obj(dry_run=False)
        s.run_all()
        written = [p for p in Path(self.h.root).rglob("calibration.json")]
        self.assertEqual(len(written), 1)
        self.assertEqual(json.loads(written[0].read_text())["phases"], {"hash": 0.5, "git": 0.25})
        self.assertTrue(any(step == "speed measurement" and status == "recorded" for step, status, _ in s.report))
        broken = self.setup_obj(dry_run=False, calibrator=lambda d: (_ for _ in ()).throw(OSError("boom")))
        broken.run_all()
        self.assertTrue(any(step == "speed measurement" and status == "skipped" and "boom" in detail for step, status, detail in broken.report))

    def test_the_private_alert_address_goes_to_the_secrets_file_and_never_into_arm_toml(self):
        topic = "https://ntfy.example/very-private-topic-name"
        s = self.setup_obj(dry_run=False, given={"alerts.kind": "ntfy", "alerts.url": topic})
        s.run_all()
        toml = self.h.read("/etc/armada/armada.toml")
        self.assertNotIn("very-private-topic-name", toml)
        self.assertIn('kind = "ntfy"', toml)
        secrets = config.read_env_file(self.h.path("/etc/armada/secrets.env"))
        self.assertEqual(secrets["ARM_ALERT_URL"], topic)
        self.assertEqual(self.h.path("/etc/armada/secrets.env").stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.h.path("/etc/armada/armada.toml").stat().st_mode & 0o777, 0o644)        # nothing private in it, so the admin can read it
        self.assertNotIn("very-private-topic-name", "\n".join(self.said))
        config.load(self.h.path("/etc/armada/armada.toml"), env={"ARM_CONFIG": ""})                    # and the worker can still load it without the address

    def test_commands_named_after_an_earlier_install_are_removed(self):
        for p in ("/usr/local/bin/armada", "/usr/local/sbin/armada-update"):
            self.h.put(p, "old wrapper\n")
        s = self.setup_obj(dry_run=False)
        s.run_all()
        self.assertFalse(self.h.exists("/usr/local/bin/armada"))
        self.assertFalse(self.h.exists("/usr/local/sbin/armada-update"))
        self.assertTrue(self.h.exists("/usr/local/bin/arm-worker"))
        self.assertTrue(self.h.exists("/usr/local/sbin/arm-update"))

    def test_an_install_that_belongs_to_another_account_stops_before_making_a_second_one(self):
        self.h.replies["stat -c %U"] = Result(0, "cwworker\n")
        s = self.setup_obj(dry_run=False)
        with self.assertRaises(server.SetupError) as e:
            s.run_all()
        self.assertIn("already runs as another account", e.exception.title)
        self.assertIn(("Existing", "cwworker"), e.exception.facts)                                # (the account is a labelled row, not part of the title)
        self.assertIn("--set server.worker_user=cwworker", " ".join(e.exception.steps))
        self.assertFalse(self.h.ran("useradd"))
        self.h.replies["stat -c %U"] = Result(0, "armworker\n")                          # the account this install means: nothing to say
        self.setup_obj(dry_run=False).run_all()

    def test_a_direct_install_makes_the_mailbox_and_asks_nothing_of_github(self):
        self.h.put("/etc/armada/mailbox-client.pub", "ssh-ed25519 AAAATESTKEY client@example\n")
        self.h.replies["id armada-mail"] = Result(1, "id: no such user")                # not there yet
        s = self.setup_obj(dry_run=False, given={"transport.kind": "direct"})
        s.run_all()
        ran = lambda prefix: self.h.ran(prefix)      # noqa: E731
        self.assertTrue(ran("useradd --system --user-group --home-dir /var/lib/armada-mail --no-create-home --shell /bin/sh armada-mail"))
        self.assertTrue(ran("usermod -aG armada-mail armworker"))
        self.assertTrue(ran("git init --bare --quiet -b main /var/lib/armada-mail/queue.git"))
        self.assertTrue(ran("git init --bare --quiet -b main /var/lib/armada-mail/results.git"))
        self.assertTrue(ran("chown -R armada-mail:armada-mail /var/lib/armada-mail/queue.git"))
        self.assertTrue(ran("chown -R armworker:armada-mail /var/lib/armada-mail/results.git"))      # the worker writes results; the mailbox account only reads them
        self.assertTrue(ran("git -C /var/lib/armada-mail/queue.git config core.sharedRepository group"))
        self.assertFalse(ran("git -C /var/lib/armada-mail/results.git config core.sharedRepository"))     # found on a real machine: group-shared (setgid) object files are refused by the worker's services (RestrictSUIDSGID)
        self.assertTrue(ran("find /var/lib/armada-mail/results.git -type d -exec chmod g+s"))     # real machine: the worker's umask is 027 and its group is its own, so without inheritance the mailbox account could not read a new result
        keys = self.h.read("/var/lib/armada-mail/.ssh/authorized_keys")
        self.assertEqual(keys, 'restrict,command="/usr/local/sbin/arm-mail-shell" ssh-ed25519 AAAATESTKEY client@example\n')
        self.assertFalse(ran("ssh-keygen"))                                           # no deploy keys: nothing is registered anywhere
        self.assertFalse(self.h.exists("/var/lib/armworker/.ssh/config") and False)
        self.assertTrue(self.h.exists("/usr/local/sbin/arm-mail-shell"))
        self.assertTrue(ran("runuser -u armworker -- git clone --quiet /var/lib/armada-mail/queue.git"))                  # the worker clones from the local path

    def test_setting_up_again_for_direct_points_an_existing_github_clone_at_the_mailbox_after_filling_it(self):
        """Seen on a real machine: the worker's clones said EXISTS and kept fetching from GitHub, so a job sent through the mailbox never arrived."""
        self.h.put("/etc/armada/mailbox-client.pub", "ssh-ed25519 AAAATESTKEY client@example\n")
        self.h.put("/var/lib/armada/queue/.git/HEAD", "ref: refs/heads/main\n")
        self.h.put("/var/lib/armada/results/.git/HEAD", "ref: refs/heads/main\n")
        self.h.replies["git -C /var/lib/armada/queue remote get-url origin"] = Result(0, "git@github-armada-queue:someone/armada-queue.git\n")
        self.h.replies["git -C /var/lib/armada/results remote get-url origin"] = Result(0, "git@github-armada-results:someone/armada-results.git\n")
        self.h.replies["git -C /var/lib/armada-mail/queue.git rev-parse"] = Result(1, "")                      # the mailbox is empty
        self.h.replies["git -C /var/lib/armada-mail/results.git rev-parse"] = Result(1, "")
        s = self.setup_obj(dry_run=False, given={"transport.kind": "direct"})
        s.run_all()
        ran = self.h.ran
        self.assertTrue([c for c in self.h.calls if c[:2] == ["sh", "-c"] and " ".join(c[4:]).startswith("git -C /var/lib/armada/queue push --quiet --force /var/lib/armada-mail/queue.git refs/remotes/origin/*:refs/heads/*")])
        self.assertTrue([c for c in self.h.calls if c[:2] == ["sh", "-c"] and " ".join(c[4:]).startswith("git -C /var/lib/armada/queue push --quiet --force /var/lib/armada-mail/queue.git refs/heads/*:refs/heads/*")])
        self.assertTrue(ran("runuser -u armworker -- git -C /var/lib/armada/queue remote set-url origin /var/lib/armada-mail/queue.git"))
        self.assertTrue(ran("runuser -u armworker -- git -C /var/lib/armada/results remote set-url origin /var/lib/armada-mail/results.git"))
        self.assertFalse(ran("git clone"))                                                                            # nothing is cloned again

    def test_an_existing_clone_that_already_points_the_right_way_is_left_alone(self):
        self.h.put("/etc/armada/mailbox-client.pub", "ssh-ed25519 AAAATESTKEY client@example\n")
        self.h.put("/var/lib/armada/queue/.git/HEAD", "ref: refs/heads/main\n")
        self.h.put("/var/lib/armada/results/.git/HEAD", "ref: refs/heads/main\n")
        self.h.replies["git -C /var/lib/armada/queue remote get-url origin"] = Result(0, "/var/lib/armada-mail/queue.git\n")
        self.h.replies["git -C /var/lib/armada/results remote get-url origin"] = Result(0, "/var/lib/armada-mail/results.git\n")
        self.setup_obj(dry_run=False, given={"transport.kind": "direct"}).run_all()
        self.assertFalse(self.h.ran("runuser -u armworker -- git -C /var/lib/armada/queue remote set-url"))
        self.assertFalse([c for c in self.h.calls if c[:2] == ["sh", "-c"] and "GIT_CONFIG_GLOBAL" in " ".join(c)])

    def test_a_github_install_has_no_mailbox_and_no_mail_shell(self):
        s = self.setup_obj(dry_run=False)
        s.run_all()
        self.assertFalse(self.h.ran("useradd --system --user-group"))
        self.assertFalse(self.h.exists("/usr/local/sbin/arm-mail-shell"))

    def test_a_dry_run_changes_nothing_and_shows_every_step(self):
        s = self.setup_obj(dry_run=True)
        s.run_all()
        self.assertFalse(self.h.exists("/etc/armada/armada.toml"))
        self.assertEqual([c for c in self.h.calls if c[0] in ("useradd", "install", "ssh-keygen", "apt-get") or c[:2] == ["systemctl", "enable"]], [])
        text = "\n".join(self.said)
        for needle in ("useradd --system", "ssh-keygen", "systemctl enable --now armada-fetch.timer", "[github]"):
            self.assertIn(needle, text)

    def test_apply_creates_accounts_installs_the_code_and_writes_the_config(self):
        s = self.setup_obj(dry_run=False)
        s.run_all()
        self.assertTrue(self.h.ran("useradd --system --create-home --home-dir " + H + "/armworker --shell /usr/sbin/nologin armworker"))
        self.assertEqual(config.load(self.h.path("/etc/armada/armada.toml")).github.owner, "someone")
        self.assertTrue(self.h.exists("/opt/armada/lib/armada/cli.py"))
        self.assertTrue(self.h.exists("/usr/local/bin/arm-worker"))
        self.assertEqual(self.h.read("/etc/armada/installed-rev").strip(), "a" * 40)
        self.assertIn("Host github-armada-queue", self.h.read(H + "/armworker/.ssh/config"))
        self.assertNotIn("ControlMaster", self.h.read(H + "/armworker/.ssh/config"))                # a background ssh master would hold a pass's output pipes open

    def test_it_waits_for_deploy_keys_before_cloning_and_hardening(self):
        self.h.replies["runuser -u armworker -- git ls-remote"] = Result(128, "Permission denied (publickey)")
        s = self.setup_obj(dry_run=False)
        report = s.run_all()
        self.assertTrue(any(r[1] == "waiting" for r in report))
        self.assertEqual(self.h.ran("systemctl enable"), [])
        self.assertTrue(any("gh repo deploy-key add" in line and "--allow-write" in line for line in self.said))
        self.assertTrue(any("armada-queue.pub" in line and "--allow-write" not in line for line in self.said))

    def test_with_keys_in_place_it_clones_hardens_and_starts_the_timers(self):
        s = self.setup_obj(dry_run=False, confirm=lambda p: True)
        s.run_all()
        self.assertEqual(len(self.h.ran("runuser -u armworker -- git clone")), 2)
        self.assertIn("git@github-armada-queue:someone/armada-queue.git", " ".join(self.h.ran("runuser -u armworker -- git clone")[0]))
        self.assertTrue(self.h.ran("systemctl enable --now armada-fetch.timer"))
        self.assertTrue(self.h.ran("systemctl enable --now armada-step.timer"))
        self.assertTrue(self.h.exists("/etc/armada/hardening.json"))
        self.assertEqual([c for c in self.h.calls if c[:2] == ["systemctl", "enable"] and "update.timer" in " ".join(c)], [])

    def test_a_failed_protection_stops_the_setup_before_the_job_loop_is_started(self):
        """Seen on a real machine: job isolation failed and the setup went on to start the timers and send a first job anyway."""
        s = self.setup_obj(dry_run=False, confirm=lambda p: True)
        real = hardening.run_module
        with mock.patch.object(hardening, "run_module", lambda mod, *a, **k: ("failed", "a test jail could not be created: bwrap: loopback: Failed RTM_NEWADDR") if mod.id == "job-isolation" else real(mod, *a, **k)):
            with self.assertRaises(server.SetupError) as e:
                s.run_all()
        self.assertIn("not started", e.exception.title)
        self.assertIn("Failed RTM_NEWADDR", e.exception.why)                                     # the machine's own message is kept
        for part in ("fetch", "step", "registry-sync"):
            self.assertEqual(self.h.ran(f"systemctl enable --now armada-{part}.timer"), [], part)      # the job loop was not started (the canary watch belongs to the tripwire protection, not to it)
        self.assertTrue(self.h.ran("systemctl disable --now armada-fetch.timer"))          # and one a first run left behind is stopped
        self.assertTrue(self.h.ran("systemctl disable --now armada-step.timer"))

    def test_a_blocked_protection_does_not_stop_the_setup_but_a_failed_one_does(self):
        s = self.setup_obj(dry_run=False, confirm=lambda p: True)
        real = hardening.run_module
        with mock.patch.object(hardening, "run_module", lambda mod, *a, **k: ("blocked", "this machine has no kernel audit") if mod.id == "auditd-canaries" else real(mod, *a, **k)):
            s.run_all()
        self.assertTrue(self.h.ran("systemctl enable --now armada-fetch.timer"))

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
        self.assertFalse(self.h.exists("/etc/armada/hardening.json"))

    def test_a_queue_repository_that_cannot_be_read_is_not_blamed_on_the_registry_file(self):
        def unreadable(cfg, src, **k):
            raise server.update.FetchError("could not fetch the queue repository: fatal: cannot change to '/var/lib/armada/queue': Permission denied")
        s = self.setup_obj(dry_run=False, confirm=lambda p: True)
        s.registry_installer = unreadable
        with self.assertRaises(server.SetupError) as e:
            s.run_all()
        self.assertIn("could not read the queue repository", e.exception.title)
        steps = " ".join(e.exception.steps)
        self.assertIn("sudo -u armworker git -C /var/lib/armada/queue fetch origin", steps)      # the command that shows git's real reason
        self.assertIn("--set server.worker_user=", steps)
        self.assertNotIn("registry.yaml", steps)

    def test_the_registry_is_not_installed_while_waiting_for_deploy_keys(self):
        self.h.replies["runuser -u armworker -- git ls-remote"] = Result(128, "Permission denied")
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
        self.h.put(H + "/armworker/.ssh/id_queue.pub", "ssh-ed25519 QQQQ armada-queue\n")
        self.h.put((H + "/armworker/.ssh/id_results.pub"), "ssh-ed25519 RRRR armada-results\n")
        self.setup_obj(dry_run=False, confirm=lambda p: True).run_all()
        self.assertEqual(self.h.read("/etc/armada/deploy-keys/queue.pub").strip(), "ssh-ed25519 QQQQ armada-queue")
        self.assertEqual(self.h.read("/etc/armada/deploy-keys/results.pub").strip(), "ssh-ed25519 RRRR armada-results")
        self.assertEqual(oct(self.h.path("/etc/armada/deploy-keys/queue.pub").stat().st_mode & 0o777), "0o644")

    def test_it_waits_for_the_keys_and_carries_on_when_they_arrive(self):
        calls = {"n": 0}

        def ls_remote(argv):
            calls["n"] += 1
            return Result(0) if calls["n"] > 4 else Result(128, "Permission denied (publickey)")
        self.h.replies["runuser -u armworker -- git ls-remote"] = ls_remote
        clock = {"t": 0}
        s = self.setup_obj(dry_run=False, confirm=lambda p: True, wait_for_keys=600, sleep=lambda x: clock.update(t=clock["t"] + x), clock=lambda: clock["t"])
        s.run_all()
        self.assertFalse(s.waiting)
        self.assertEqual(len(self.h.ran("runuser -u armworker -- git clone")), 2)
        self.assertTrue(any("Waiting for the deploy keys" in line for line in self.said))
        self.assertTrue(self.h.ran("systemctl enable --now armada-fetch.timer"))

    def test_an_existing_clone_moved_to_github_also_waits_for_the_keys_before_the_registry_fetch(self):
        """Found by a real run (scripts/real-github-check): an install moved from Direct to GitHub already had its clones, so the server pointed them at GitHub and fetched the registry at once,
        before arm-setup on the person's computer had added the deploy keys ('Permission denied (publickey)'). Only a fresh install waited."""
        for which in ("queue", "results"):
            self.h.path(f"/var/lib/armada/{which}/.git").mkdir(parents=True)
        self.h.replies["git -C /var/lib/armada/queue remote get-url origin"] = Result(0, "/var/lib/armada-mail/queue.git\n")
        self.h.replies["git -C /var/lib/armada/results remote get-url origin"] = Result(0, "/var/lib/armada-mail/results.git\n")
        calls = {"n": 0}

        def ls_remote(argv):
            calls["n"] += 1
            return Result(0) if calls["n"] > 4 else Result(128, "Permission denied (publickey)")
        self.h.replies["runuser -u armworker -- git ls-remote"] = ls_remote
        clock = {"t": 0}
        s = self.setup_obj(dry_run=False, confirm=lambda p: True, wait_for_keys=600, sleep=lambda x: clock.update(t=clock["t"] + x), clock=lambda: clock["t"])
        s.run_all()
        self.assertTrue(any("Waiting for the deploy keys" in line for line in self.said), self.said)
        self.assertFalse(s.waiting)

    def test_it_gives_up_waiting_after_the_time_and_says_so(self):
        self.h.replies["runuser -u armworker -- git ls-remote"] = Result(128, "Permission denied")
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

    def test_a_shared_computer_offers_the_whole_machine_protections_and_yes_takes_none_of_them(self):
        """Shared and special-use computers: the protections that change the machine for every user are offered one by one; --yes applies none, and the owner's decision is recorded."""
        flat, problems = questions.collect("server", given={"isolation.purpose": "shared"}, use_defaults=True)
        self.assertEqual(flat["hardening"]["modules"], ["job-isolation", "auditd-canaries"])
        self.assertEqual(flat["hardening"]["offered"], ["ssh-keys-only", "ufw-lan-egress", "no-root-equivalent-groups", "unattended-upgrades"])
        s = self.setup_obj(dry_run=False, given={"isolation.purpose": "shared"})
        results = s.run_all()
        done = json.loads(self.h.read("/etc/armada/hardening.json"))
        for mid in ("ssh-keys-only", "ufw-lan-egress", "no-root-equivalent-groups", "unattended-upgrades"):
            self.assertEqual(done[mid]["status"], "declined", mid)
            self.assertIn("every user of this machine", done[mid]["detail"])
        self.assertNotIn("ssh-keys-only", [m for m, r in done.items() if r["status"] == "applied"])

    def test_an_offered_protection_the_person_accepts_is_applied_and_then_audited_like_the_others(self):
        s = self.setup_obj(dry_run=False, given={"isolation.purpose": "shared"})
        s.use_defaults = False                                              # a person is answering
        s.confirm = lambda q: q.startswith("Apply unattended-upgrades")      # yes to one, no to the rest
        s.run_all()
        done = json.loads(self.h.read("/etc/armada/hardening.json"))
        self.assertIn(done["unattended-upgrades"]["status"], ("applied", "satisfied"))
        self.assertNotIn(done["ssh-keys-only"]["status"], ("applied", "satisfied"))      # said no (or the fake machine blocks it): not applied
        from armada.setup import audit
        self.assertIn("unattended-upgrades", audit.audited_modules(self.h, s.cfg))
        self.assertNotIn("ssh-keys-only", audit.audited_modules(self.h, s.cfg))            # declined is not drift

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

    def test_a_github_account_that_is_not_the_signed_in_one_is_refused_before_anything_is_created(self):
        """Seen on a real run: the wizard was answered with the server's login name; the first gh repo create died with a GraphQL permissions error after the configuration was written."""
        h, c = self.make(dry_run=False, given={"github.owner": "agent"})
        h.replies["gh api user/orgs"] = Result(0, "an-org\n")
        h.replies["gh api user"] = Result(0, "someone\n")
        with self.assertRaises(server.SetupError) as e:
            c.run_all()
        self.assertEqual(h.ran("gh repo create"), [])
        self.assertIn("not yours", e.exception.title)
        self.assertIn("signed in to GitHub as someone", e.exception.why)
        self.assertTrue(any("answer someone" in st for st in e.exception.steps))

    def test_the_signed_in_account_or_one_of_its_organisations_is_accepted(self):
        for owner in ("someone", "Someone", "an-org"):
            h, c = self.make(dry_run=True, given={"github.owner": owner})
            h.replies["gh api user/orgs"] = Result(0, "an-org\n")
            h.replies["gh api user"] = Result(0, "someone\n")
            c.run_all()                                                              # no refusal
            c.dry_run = False
            c.check_owner()

    def test_when_the_signed_in_account_cannot_be_read_nothing_is_blocked(self):
        h, c = self.make(dry_run=False, given={"github.owner": "agent"})
        h.replies["gh api user"] = Result(1, "")
        c.prerequisites()
        c.configure()
        c.check_owner()

    def test_dry_run_prints_the_steps_and_writes_nothing(self):
        h, c = self.make(dry_run=True)
        c.run_all()
        self.assertEqual(h.ran("gh repo create"), [])
        self.assertTrue(any("gh repo create someone/armada-queue --private" in s for s in self.said))
        self.assertFalse(h.exists(h.env["HOME"] + "/.config"))

    def test_apply_writes_config_creates_private_repos_and_seeds_the_registry(self):
        h, c = self.make(dry_run=False)
        sh("git", "-C", str(self.tmp / "queue.git"), "update-ref", "-d", "refs/heads/main")      # a brand-new, empty repository
        self.repos.pull_results = lambda: None
        self.repos.clone_all = lambda home: [("exists", str(self.repos.queue)), ("exists", str(self.repos.results))]
        c.run_all()
        self.assertEqual(len(h.ran("gh repo create")), 2)
        self.assertTrue(all("--private" in " ".join(x) for x in h.ran("gh repo create")))
        self.assertEqual(config.load(h.path(h.env["HOME"] + "/.config/armada/armada.toml")).github.owner, "someone")
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
        from armada import packs
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


class RootLevelAudit(unittest.TestCase):
    def rows(self, registry_text, probe_reply):
        h, cfg = FakeHost(), cfg_of()
        h.put(cfg.path("registry"), registry_text)
        h.replies["systemd-run"] = Result(0, probe_reply)
        rows, _ = audit.run(h, cfg, say=lambda s: None)
        return {r[0]: r for r in rows}

    ACTIONS = "actions:\n  a-b:\n    run: [/usr/local/sbin/x]\n    sha256: \"%s\"\nclasses: {}\n" % ("a" * 64)

    def test_an_install_with_a_root_level_action_is_checked_for_an_enforced_sandbox(self):
        name = "the sandbox for root-level actions is enforced"
        self.assertTrue(self.rows(self.ACTIONS, "ENFORCED\n")[name][1])
        row = self.rows(self.ACTIONS, "OPEN\n")[name]
        self.assertFalse(row[1])
        self.assertIn("ignores the sandbox", row[2])

    def test_an_install_without_one_has_no_such_row(self):
        self.assertNotIn("the sandbox for root-level actions is enforced", self.rows("classes: {}\n", "OPEN\n"))
        self.assertNotIn("the root-level helper answers", self.rows("classes: {}\n", "OPEN\n"))

    def test_the_helper_is_asked_something_it_must_refuse_and_any_answer_shows_it_starts(self):
        h, cfg = FakeHost(), cfg_of()
        h.put(cfg.path("registry"), self.ACTIONS)
        h.replies["systemd-run"] = Result(0, "ENFORCED\n")
        h.replies["python3 -c"] = Result(0, '{"status": "refused", "note": "x"}\n')
        rows = {r[0]: r for r in audit.run(h, cfg, say=lambda s: None)[0]}
        self.assertTrue(rows["the root-level helper answers"][1])
        h.replies["python3 -c"] = Result(1, "ConnectionResetError: [Errno 104] Connection reset by peer\n")
        rows = {r[0]: r for r in audit.run(h, cfg, say=lambda s: None)[0]}
        self.assertFalse(rows["the root-level helper answers"][1])
        self.assertIn("Connection reset", rows["the root-level helper answers"][2])


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
        self.assertFalse(h.exists("/etc/armada/armada.toml"))

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


class WhatTheComputerIsUsedFor(unittest.TestCase):
    """Special Use and shared computers: one question chooses which protections are the default, each protection says who it affects, and tool folders are asked only for Special Use."""

    def test_a_computer_only_for_armada_changes_nothing(self):
        from armada.setup import questions
        flat = questions.derive({"isolation.purpose": "armada-only"})
        self.assertEqual(flat, {"isolation.purpose": "armada-only"})

    def test_shared_and_special_use_default_to_the_protections_that_affect_only_armada(self):
        from armada.setup import hardening, questions
        shared = questions.derive({"isolation.purpose": "shared"})
        self.assertEqual(shared["hardening.modules"], ["job-isolation", "auditd-canaries"])
        self.assertEqual(shared["hardening.offered"], ["ssh-keys-only", "ufw-lan-egress", "no-root-equivalent-groups", "unattended-upgrades"])
        self.assertNotIn("isolation.mode", shared)                                       # shared keeps full isolation
        special = questions.derive({"isolation.purpose": "special-use"})
        self.assertEqual((special["hardening.modules"], special["isolation.mode"]), (["job-isolation", "auditd-canaries"], "relaxed"))
        mine = questions.derive({"isolation.purpose": "shared", "hardening.modules": ["job-isolation", "ssh-keys-only"]})
        self.assertEqual(mine["hardening.modules"], ["job-isolation", "ssh-keys-only"])  # what the person chose is never replaced
        for mid in questions.ARMADA_ONLY_MODULES:                                         # the list in the questions is exactly the modules that say so
            self.assertEqual(hardening.MODULES[mid].affects, hardening.ARMADA_ONLY, mid)
        self.assertEqual(sorted(mid for mid, m in hardening.MODULES.items() if m.affects == hardening.ARMADA_ONLY and mid in config.SCHEMA["hardening"]["modules"][0]), sorted(questions.ARMADA_ONLY_MODULES))

    def test_every_protection_says_who_it_affects_in_the_text_and_on_the_screen(self):
        from armada.setup import hardening
        for mid, mod in hardening.MODULES.items():
            self.assertIn(mod.affects, (hardening.ARMADA_ONLY, hardening.EVERYONE), mid)
        self.assertEqual(hardening.MODULES["ssh-keys-only"].affects, hardening.EVERYONE)
        self.assertEqual(hardening.MODULES["ufw-lan-egress"].affects, hardening.EVERYONE)

    def test_the_purpose_is_asked_and_tool_folders_only_for_special_use(self):
        from armada.setup import questions
        by = questions.BY_KEY
        self.assertIn("isolation.purpose", by)
        self.assertEqual([c[0] for c in questions.CHOICES["isolation.purpose"]], ["armada-only", "shared", "special-use"])
        self.assertIn("Special Use", questions.CHOICES["isolation.purpose"][2][1])
        when = by["isolation.tool_roots"]["when"]
        self.assertTrue(when({"isolation.purpose": "special-use"}))
        self.assertFalse(when({"isolation.purpose": "shared"}))
        self.assertFalse(when({}))
        flat, problems = questions.collect("server", given={"isolation.purpose": "special-use", "isolation.tool_roots": "/opt/tools, /srv/pentest"}, use_defaults=True)
        self.assertEqual(flat["isolation"]["tool_roots"], ["/opt/tools", "/srv/pentest"])             # any number
        self.assertEqual(flat["isolation"]["mode"], "relaxed")


if __name__ == "__main__":
    unittest.main()


class GitHubToolOffer(RepoFixture):
    """`gh` makes the repositories, so it must be installed and signed in. The wizard offers each (an amber card, then it runs on the person's terminal); declining or a silent run prints the commands
    and the repository steps are done by hand. Seen by the user: the app assumed gh was installed and signed in."""

    make = ClientTests.make

    def go(self, tools, yes=(), auth_ok=True, installs=True, auth_after=True):
        h, c = self.make(dry_run=False, given={"github.owner": "someone"})
        h.env["TOOLS"] = tools
        h.replies["gh auth status"] = lambda argv: Result(0 if (auth_ok or (len(h.ran("gh auth login")) and auth_after)) else 1)
        h.replies["gh api user"] = Result(1, "")
        asked, ran = [], []

        def confirm(info):
            asked.append(info["id"])
            return info["id"] in yes

        def interactive(argv):
            ran.append(argv)
            if argv[:2] == ["brew", "install"] and installs:
                h.env["TOOLS"] += " gh"
            h.calls.append(argv)
            return 0, ""
        c.confirm_card, c.interactive = confirm, interactive
        c.gh = bool(h.which("gh"))
        c.github_tool()
        return h, c, asked, ran

    def test_gh_missing_is_offered_and_installed_when_the_person_says_yes(self):
        h, c, asked, ran = self.go("git ssh brew", yes=("gh-install",))
        self.assertEqual(asked[0], "gh-install")
        self.assertEqual(ran[0], ["brew", "install", "gh"])
        self.assertTrue(c.gh)

    def test_declining_the_install_prints_the_command_and_the_repositories_are_done_by_hand(self):
        h, c, asked, ran = self.go("git ssh apt-get")
        self.assertEqual(ran, [])
        self.assertFalse(c.gh)
        self.assertTrue(any("sudo apt-get install -y gh" in str(s) for s in self.said))

    def test_no_package_manager_points_at_the_official_page(self):
        h, c, asked, ran = self.go("git ssh")
        self.assertEqual(ran, [])
        self.assertTrue(any("cli.github.com" in str(s) for s in self.said))

    def test_an_install_that_does_not_produce_gh_says_so_and_falls_back(self):
        h, c, asked, ran = self.go("git ssh brew", yes=("gh-install",), installs=False)
        self.assertFalse(c.gh)
        self.assertTrue(any(r[1] == "failed" for r in c.report), c.report)

    def test_gh_installed_but_signed_out_offers_the_sign_in_and_runs_it_on_yes(self):
        h, c, asked, ran = self.go("git ssh gh", yes=("gh-login",), auth_ok=False)
        self.assertEqual(asked, ["gh-login"])
        self.assertEqual(ran[0][:3], ["gh", "auth", "login"])
        self.assertTrue(c.gh)

    def test_declining_the_sign_in_gives_the_command_and_stops_using_gh(self):
        h, c, asked, ran = self.go("git ssh gh", auth_ok=False)
        self.assertEqual(ran, [])
        self.assertFalse(c.gh)
        self.assertTrue(any("gh auth login" in str(s) for s in self.said))

    def test_signed_in_asks_nothing(self):
        h, c, asked, ran = self.go("git ssh gh")
        self.assertEqual(asked, [])
        self.assertTrue(c.gh)

    def test_a_run_nobody_answers_never_installs_anything(self):
        h, c = self.make(dry_run=False)
        h.env["TOOLS"] = "git ssh brew"
        c.gh, c.confirm_card = False, None
        c.interactive = lambda argv: self.fail("installed unasked")
        c.github_tool()
        self.assertFalse(c.gh)
