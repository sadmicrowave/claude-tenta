import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import support  # noqa: F401
from ct import config
from ct.admin.ssh import family_for
from ct.setup import addworker, assess, discover, install
from ct.setup.host import Host

MAIN = '[identity]\nname = "claude-tenta"\n[server]\nhost = "mini.example"\nadmin_user = "me"\n[github]\nowner = "someone"\n[transport]\nkind = "direct"\n'
GOOD = assess.verdict({"OS_ID": "ubuntu", "OS_LIKE": "debian", "OS_VERSION": "24.04", "OS_NAME": "Ubuntu 24.04", "ARCH": "x86_64", "CORES": "2", "MEM_MB": "4000", "DISK_MB": "20000", "PYTHON": "3.12",
                      "SYSTEMD": "yes", "SUDO": "yes", "SUDO_GROUP": "yes", "VIRT": "vmware", "INSTALLS": "", "WHO": "agent", "HOSTNAME": "claude-tenta2"}, "agent")
BAD = assess.verdict({"OS_ID": "ubuntu", "OS_VERSION": "22.04", "OS_NAME": "Ubuntu 22.04", "ARCH": "x86_64", "MEM_MB": "4000", "DISK_MB": "20000", "PYTHON": "3.10", "SYSTEMD": "yes", "SUDO": "yes", "SUDO_GROUP": "yes", "WHO": "agent"}, "agent")


def args(**kw):
    return SimpleNamespace(**{"name": None, "worker_name": None, "user": None, "port": 22, "yes": False, **kw})


class Pieces(unittest.TestCase):
    def test_a_worker_name_is_valid_unused_and_made_from_the_hostname(self):
        self.assertEqual(addworker.worker_name("claude-tenta2" + ".lo" + "cal", set()), "tenta2")
        self.assertEqual(addworker.worker_name("claude-tenta2" + ".lo" + "cal", {"tenta2"}), "tenta2-2")
        self.assertEqual(addworker.worker_name("Pi 5!", set()), "pi-5")
        self.assertEqual(addworker.worker_name("9lives", set()), "w9lives")
        self.assertEqual(addworker.worker_name("", set()), "worker")
        for host in ("a", "x" * 60, "UPPER.example.com", "9"):
            self.assertTrue(config.NAME_RE.match(addworker.worker_name(host, set())), host)

    def test_a_scanned_address_is_installed_by_the_machines_own_local_name_when_that_resolves_to_it(self):
        self.assertEqual(addworker.reachable_name({"HOSTNAME": "box"}, "192.0.2.5", lambda n: "192.0.2.5" if n == "box" + ".lo" + "cal" else (_ for _ in ()).throw(OSError())), "box" + ".lo" + "cal")
        self.assertEqual(addworker.reachable_name({"HOSTNAME": "box"}, "192.0.2.5", lambda n: "192.0.2.9"), "192.0.2.5")            # resolves to another machine: keep the address
        self.assertEqual(addworker.reachable_name({}, "192.0.2.5"), "192.0.2.5")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.conf_dir = self.tmp / ".config" / "claude-tenta"
        self.conf_dir.mkdir(parents=True)
        self.main = self.conf_dir / "ct.toml"
        self.main.write_text(MAIN + f'[paths]\nclient_home = "{self.tmp}/.ct"\n')
        for d in ("queue", "results"):
            (self.tmp / ".ct" / d / ".git").mkdir(parents=True)
            (self.tmp / ".ct" / d / ".git" / "config").write_text('[remote "origin"]\n\turl = ssh://main-mailbox\n')
        (self.tmp / ".ct" / "approval").mkdir()
        (self.tmp / ".ct" / "approval" / "key.pub").write_text("ssh-ed25519 AAAAMAIN approver\n")
        self.env = mock.patch.dict(os.environ, {"CT_CONFIG": str(self.main), "HOME": str(self.tmp)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.calls, self.cards = [], []

    def notice(self, title, *rows, **kw):
        self.cards.append((title, rows, kw))

    def text(self):
        return "\n".join(str(x) for c in self.cards for x in (c[0],) + tuple(c[1]) + tuple(c[2].get("steps") or ()))

    def factory(self, mutate=None, fail=False):
        def make(conf, home, name, host, user, suggest):
            self.calls.append({"conf": conf, "home": home, "name": name, "host": host, "user": user, "suggest": suggest, "ct_config": os.environ.get("CT_CONFIG")})

            class Inst:
                def run_all(inner):
                    conf.write_text(f'[identity]\nname = "{name}"\n[server]\nhost = "{host}"\nadmin_user = "{user}"\n[paths]\nclient_home = "{home}"\n')
                    if mutate:
                        mutate()
                    return [("a job went through the whole loop", "failed" if fail else "ok", "")]
            return Inst()
        return make

    def run_flow(self, a, ask=None, scan=None, assess_fn=None, interactive=False, mutate=None):
        asks = iter(ask or [])
        return addworker.run(a, Host(str(self.tmp / "root")), lambda p: next(asks), None, lambda m: None, ui=None, installer_factory=self.factory(mutate), interactive=interactive, networks=lambda: [],
                             scan=scan or (lambda nets, known=(): ([], [])), assess=assess_fn or (lambda h, u, p=22: GOOD), resolve=lambda n: (_ for _ in ()).throw(OSError()), notice=self.notice)


class Targeted(Base):
    def test_a_named_machine_that_meets_the_requirements_is_set_up_with_its_own_files_and_the_host_is_untouched(self):
        before = {p: Path(p).read_text() for p in (self.main, self.tmp / ".ct" / "queue" / ".git" / "config", self.tmp / ".ct" / "approval" / "key.pub")}
        rc = self.run_flow(args(name="worker2.example", yes=True))
        self.assertEqual(rc, 0, self.text())
        (c,) = self.calls
        self.assertEqual((c["name"], c["host"], c["user"]), ("tenta2", "worker2.example", "me"))
        self.assertEqual(c["conf"], self.conf_dir / "tenta2.toml")                                     # its own file beside the main one
        self.assertEqual(c["home"], self.tmp / ".ct" / "workers" / "tenta2")                           # its own folder
        self.assertEqual(c["ct_config"], str(self.conf_dir / "tenta2.toml"))                           # the install's own commands read the new worker's configuration
        self.assertEqual(os.environ["CT_CONFIG"], str(self.main))                                      # and the environment is put back
        for p, text in before.items():
            self.assertEqual(Path(p).read_text(), text, p)                                             # nothing of the host changed
        self.assertTrue((c["home"] / "approval").is_symlink())                                         # the one approval key is shared
        self.assertEqual(config.load(self.main).identity.name, "claude-tenta")

    def test_the_worker_is_listed_where_admin_workers_and_watch_look_for_it(self):
        from ct.admin import workers
        self.run_flow(args(name="192.0.2.9", yes=True))
        (name, path), = workers.load_list(self.conf_dir / "workers.toml").items()
        self.assertEqual(Path(path), self.calls[0]["conf"])

    def test_a_machine_that_does_not_meet_the_requirements_is_not_installed_and_says_why(self):
        rc = self.run_flow(args(name="old.example", yes=True), assess_fn=lambda h, u, p=22: BAD)
        self.assertEqual(rc, 1)
        self.assertEqual(self.calls, [])
        self.assertFalse((self.conf_dir / "workers.toml").exists())
        t = self.text()
        for needle in ("cannot be a worker yet", "Ubuntu 24.04 or newer is needed", "3.10", "sudo apt-get install python3"):
            self.assertIn(needle, t)
        self.assertEqual([c[2].get("steps_label") for c in self.cards if c[2].get("steps")], ["How to fix it"])

    def test_something_of_your_existing_install_changing_is_reported_and_not_called_success(self):
        rc = self.run_flow(args(name="box.example", yes=True), mutate=lambda: self.main.write_text(MAIN + "# edited\n"))
        self.assertEqual(rc, 1)
        self.assertIn(str(self.main), self.text())
        self.assertIn("changed", self.text())

    def test_a_preview_checks_the_machine_and_installs_nothing(self):
        rc = self.run_flow(args(name="box.example", yes=True, dry_run=True))
        self.assertEqual((rc, self.calls), (0, []))
        self.assertFalse((self.conf_dir / "workers.toml").exists())
        self.assertIn("this was a preview", self.text())

    def test_a_computer_that_is_already_one_of_your_workers_is_left_alone(self):
        rc = self.run_flow(args(name="mini.example", yes=True))
        self.assertEqual((rc, self.calls), (0, []))

    def test_a_name_that_is_taken_is_refused_with_a_suggestion(self):
        with self.assertRaises(addworker.SetupError) as e:
            self.run_flow(args(name="box.example", worker_name="claude-tenta", yes=True))
        self.assertIn("already a worker", e.exception.why)
        self.assertIn("--worker-name", " ".join(e.exception.steps))
        self.assertEqual(self.calls, [])


class TheRealCommand(unittest.TestCase):
    """`ct-setup add-worker ADDRESS` through the real command line and the real installer (only the network and the machine are fakes). A fake factory hid a crash: the command read its
    configuration AFTER pointing CT_CONFIG at the new worker's file, which does not exist yet ('cannot read .../tenta2.toml')."""

    def setUp(self):
        from test_install import InstallBase
        self.base = InstallBase("setUp")
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.addCleanup(self.base.tearDown)
        b = self.base
        self.conf_dir = b.tmp / ".config" / "claude-tenta"
        self.conf_dir.mkdir(parents=True)
        self.main = self.conf_dir / "ct.toml"
        self.main.write_text(MAIN + f'[paths]\nclient_home = "{b.tmp}/.ct"\n')
        self.env = mock.patch.dict(os.environ, {"CT_CONFIG": str(self.main)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_command(self, *extra):
        from ct.setup import cli as setup_cli
        b = self.base
        registered = []
        err = io.StringIO()
        quick = mock.patch.dict(install.Installer.__init__.__kwdefaults__, {"sleep": lambda s: None, "poll_s": 0})              # the fake server half takes a few polls; do not really wait between them
        with mock.patch("ct.setup.assess.assess", lambda h, u, p=22, run=None: GOOD), mock.patch("ct.admin.workers.add", lambda w, n, c: registered.append((n, c))), quick, \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = setup_cli.main(["add-worker", "worker2.example", "--user", "agent", "--yes", "--worker-name", "tenta2", "--source", str(b.tmp), *extra], host=b.machine,
                                ask=lambda p: "", repos_factory=lambda cfg: b.repos, ssh_factory=lambda h, u, p: b.ssh, verify=lambda cfg: True)
        return rc, registered, err.getvalue()

    def test_the_whole_command_runs_without_reading_a_configuration_that_does_not_exist_yet(self):
        rc, registered, text = self.run_command()
        self.assertNotIn("cannot read", text)
        self.assertNotIn("configuration has problems", text)
        self.assertEqual(rc, 0, text)
        self.assertEqual([n for n, _ in registered], ["tenta2"])
        self.assertTrue(self.base.machine.exists(str(self.conf_dir / "tenta2.toml")), "the worker's own configuration was written")
        self.assertIn("tenta2", self.base.machine.read(str(self.conf_dir / "tenta2.toml")))
        self.assertEqual(config.load(self.main).identity.name, "claude-tenta")                       # and the first install's file is as it was
        self.assertEqual(os.environ["CT_CONFIG"], str(self.main))                                     # the environment is put back

    def test_the_same_command_again_sets_the_registered_worker_up_again_through_the_real_installer(self):
        """The route after the first run: through the real command line, with the worker's own configuration (a file that exists now, unlike the first time)."""
        b = self.base
        w_conf, w_home = self.conf_dir / "tenta2.toml", b.tmp / ".ct" / "workers" / "tenta2"
        w_conf.write_text(f'[identity]\nname = "tenta2"\n[server]\nhost = "worker2.example"\nadmin_user = "agent"\n[transport]\nkind = "direct"\n[paths]\nclient_home = "{w_home}"\n')
        (self.conf_dir / "workers.toml").write_text(f'[workers.tenta2]\nconfig = "{w_conf}"\n')
        main_before = self.main.read_text()
        rc, registered, text = self.run_command()
        self.assertEqual(rc, 0, text)
        self.assertNotIn("cannot read", text)
        self.assertIn("set up again", text)
        self.assertEqual([n for n, _ in registered], ["tenta2"])
        self.assertEqual(self.main.read_text(), main_before)
        self.assertTrue(b.machine.exists(str(w_conf)))                                               # the worker's own file was rewritten by the install, in its own place

    def test_the_new_worker_is_asked_about_nothing_it_was_already_told_and_uses_the_first_installs_connection(self):
        rc, _, text = self.run_command()
        cfg_text = self.base.machine.read(str(self.conf_dir / "tenta2.toml"))
        self.assertIn('host = "worker2.example"', cfg_text)
        self.assertIn('admin_user = "agent"', cfg_text)
        self.assertIn('kind = "direct"', cfg_text)                                                    # the connection of the first install is the default for the next

    def test_the_workers_ipv6_setting_is_in_this_computers_file_so_ssh_does_not_wait_for_addresses_it_no_longer_has(self):
        """Seen on a real worker: 30 s per connection. The setting was only in the worker's file, so this computer never knew to connect over IPv4."""
        rc, _, err = self.run_command("--set", "network.ipv6=disabled")
        self.assertEqual(rc, 0, err)
        text = self.base.machine.read(str(self.conf_dir / "tenta2.toml"))
        self.assertIn('ipv6 = "disabled"', text)
        import tempfile
        written = tempfile.mkdtemp() + "/t.toml"
        Path(written).write_text(text)
        self.assertEqual(family_for(config.load(written)), "inet")           # so this computer's ssh and git to that worker use IPv4 only


class TheScreens(unittest.TestCase):
    """The lists are real selectors: arrows move a highlight, Enter takes it, and nothing else leaks onto the screen (seen: down accepted the first device and pulled the next wizard question over the scan)."""

    def screen(self, events, rows=40):
        import io
        from ct.setup import tui
        out = io.StringIO()
        return tui.TuiPrompter(out=out, width=90, rows=rows, keys=iter(events), color=False), out

    def devices(self, n=3):
        return [discover.Device(f"198.51.100.{10 + i}", name=f"box{i}.local", os_hint="Ubuntu, OpenSSH 9.6", kind="linux-server", latency_ms=1.0) for i in range(n)]

    def choose(self, events, n=3, rows=40):
        ui, out = self.screen(events, rows)
        return addworker.choose_device(ui, None, lambda m: None, self.devices(n)), out.getvalue()

    def test_the_list_shows_the_section_title_choices_and_the_keys(self):
        pick, text = self.choose(["enter"])
        self.assertEqual(pick, "198.51.100.10")
        for needle in ("NEW WORKER", "Which computer should become a worker?", "198.51.100.11", "box1", "none of these", "choose", "Enter select"):
            self.assertIn(needle, text)

    def test_down_moves_the_highlight_it_does_not_accept_and_enter_takes_the_one_highlighted(self):
        self.assertEqual(self.choose(["down", "enter"])[0], "198.51.100.11")
        self.assertEqual(self.choose(["down", "down", "up", "enter"])[0], "198.51.100.11")

    def test_the_ends_of_the_list_stop_the_highlight_and_a_stray_key_does_nothing(self):
        self.assertEqual(self.choose(["up", "up", "enter"])[0], "198.51.100.10")
        ui, _ = self.screen(["x", "left", "tab", "backspace", "down", "enter"])
        self.assertEqual(ui.select({"key": "worker.device", "title": "t", "why": "w", "choices": [("a", "A"), ("b", "B")]}, "a"), ("answer", "b"))
        ui, _ = self.screen(["down"] * 9 + ["enter"])
        self.assertEqual(ui.select({"key": "worker.device", "title": "t", "why": "w", "choices": [("a", "A"), ("b", "B")]}, "a"), ("answer", "b"))

    def test_a_number_jumps_to_that_row_and_two_digits_reach_a_long_list(self):
        self.assertEqual(self.choose(["2", "enter"])[0], "198.51.100.11")
        pick, _ = self.choose(["1", "2", "enter"], n=14)
        self.assertEqual(pick, "198.51.100.21")                                                     # row 12 of 15 (the 15th is "none of these")

    def test_the_last_row_is_the_way_to_type_an_address_yourself(self):
        ui2, _ = self.screen(["down"] * 3 + ["enter"] + list("box.example") + ["enter"])
        self.assertEqual(addworker.choose_device(ui2, None, lambda m: None, self.devices(3)), "box.example")

    def test_a_long_list_never_draws_a_block_taller_than_the_screen_and_says_what_is_hidden(self):
        ui, out = self.screen(["down"] * 20 + ["enter"], rows=20)
        addworker.choose_device(ui, None, lambda m: None, self.devices(30))
        frames = out.getvalue().split("\n")
        text = out.getvalue()
        self.assertIn("more above", text)
        self.assertIn("more below", text)
        import re
        ups = [int(n) for n in re.findall(r"\x1b\[(\d+)A", text)]
        self.assertTrue(all(n <= 20 for n in ups), ups)                                             # it never moves the cursor up past the top of the screen, so the scan above is never overwritten

    def test_yes_and_no_are_a_list_too(self):
        ui, _ = self.screen(["down", "enter"])
        self.assertFalse(addworker.yes_no(ui, None, lambda m: None, "Run this check now?", "It only reads."))
        ui, _ = self.screen(["enter"])
        self.assertTrue(addworker.yes_no(ui, None, lambda m: None, "Run this check now?", "It only reads."))

    def test_ctrl_c_stops_cleanly(self):
        ui, _ = self.screen(["ctrl-c"])
        with self.assertRaises(KeyboardInterrupt):
            addworker.choose_device(ui, None, lambda m: None, self.devices(2))


class SettingAWorkerUpAgain(Base):
    """The route a user takes after the first add-worker: the same command, for a worker that is already registered. Uses ITS configuration and folder, never the first install's."""

    def setUp(self):
        super().setUp()
        self.w_home = self.tmp / ".ct" / "workers" / "tenta2"
        self.w_conf = self.conf_dir / "tenta2.toml"
        self.w_conf.write_text(f'[identity]\nname = "tenta2"\n[server]\nhost = "worker2.example"\nadmin_user = "agent"\n[transport]\nkind = "github"\n[paths]\nclient_home = "{self.w_home}"\n')
        (self.w_home / "queue" / ".git").mkdir(parents=True)
        (self.w_home / "queue" / ".git" / "config").write_text("[remote]\n")
        (self.conf_dir / "workers.toml").write_text(f'[workers.tenta2]\nconfig = "{self.w_conf}"\n')

    def test_the_command_for_a_registered_worker_sets_that_worker_up_again_with_its_own_files(self):
        rc = self.run_flow(args(name="worker2.example", yes=True))
        self.assertEqual(rc, 0, self.text())
        (c,) = self.calls
        self.assertEqual((c["name"], c["host"], c["user"], c["conf"], c["home"]), ("tenta2", "worker2.example", "agent", self.w_conf, self.w_home))
        self.assertEqual(c["ct_config"], str(self.w_conf))
        self.assertEqual(c["suggest"]["transport.kind"], "github")                                     # ITS connection, not the first install's (direct)
        self.assertIn("set up again", self.text())
        self.assertEqual(config.load(self.main).identity.name, "claude-tenta")                          # the first install is as it was

    def test_the_workers_own_files_may_change_and_any_other_file_may_not(self):
        other = self.conf_dir / "other.toml"
        other.write_text('[identity]\nname = "other"\n[server]\nhost = "other.example"\n')
        (self.conf_dir / "workers.toml").write_text(f'[workers.tenta2]\nconfig = "{self.w_conf}"\n[workers.other]\nconfig = "{other}"\n')
        rc = self.run_flow(args(name="worker2.example", yes=True), mutate=lambda: self.w_conf.write_text(self.w_conf.read_text() + "# rewritten by the install\n"))
        self.assertEqual(rc, 0, self.text())                                                           # its own configuration changing is the point of setting it up again
        rc = self.run_flow(args(name="worker2.example", yes=True), mutate=lambda: other.write_text("# touched\n"))
        self.assertEqual(rc, 1)                                                                        # another worker's file changing is reported
        self.assertIn(str(other), self.text())

    def test_the_first_worker_is_not_set_up_again_by_this_command(self):
        self.calls.clear()
        rc = self.run_flow(args(name="mini.example", yes=True))
        self.assertEqual((rc, self.calls), (0, []))
        self.assertIn("already one of your workers", self.text())

    def test_saying_no_stops_before_anything_is_changed(self):
        rc = self.run_flow(args(name="worker2.example"), ask=["n"], interactive=True)
        self.assertEqual((rc, self.calls), (1, []))


class Interactive(Base):
    DEV = [discover.Device("198.51.100.194", name="worker2.example", os_hint="Ubuntu, OpenSSH 9.6", kind="linux-server", vendor="VMware", notes=["a virtual machine"], latency_ms=14.7),
           discover.Device("198.51.100.2", os_hint="an appliance or router (dropbear)", kind="other", latency_ms=2.0)]

    def test_scan_pick_check_confirm_then_install_with_the_address_filled_in(self):
        asked = []
        scan = lambda nets, known=(): (self.DEV, [])                                              # noqa: E731
        rc = self.run_flow(args(), ask=["1", "agent", "y", "y", ""], scan=scan, interactive=True, assess_fn=lambda h, u, p=22: (asked.append((h, u)), GOOD)[1])
        self.assertEqual(rc, 0, self.text())
        self.assertEqual(asked, [("198.51.100.194", "agent")])
        (c,) = self.calls
        self.assertEqual((c["host"], c["user"], c["name"]), ("198.51.100.194", "agent", "tenta2"))
        t = self.text()
        self.assertIn("2 device(s)", t)
        self.assertIn("1 look like Linux servers", t)

    def test_a_worker_you_have_by_name_is_recognised_by_its_address_in_the_scan(self):
        seen = []
        addrs = addworker.run(args(), Host(str(self.tmp / "root")), lambda p: "", None, lambda m: None, ui=None, installer_factory=self.factory(), interactive=False, networks=lambda: [],
                              scan=lambda nets, known=(): (seen.append(set(known)) or ([], [])), resolve=lambda n: "198.51.100.193" if n == "mini.example" else (_ for _ in ()).throw(OSError()), notice=self.notice)
        self.assertIn("198.51.100.193", seen[0])
        self.assertIn("mini.example", seen[0])

    def test_saying_no_to_the_check_stops_before_anything_is_read(self):
        asked = []
        rc = self.run_flow(args(), ask=["1", "agent", "n"], scan=lambda nets, known=(): (self.DEV, []), interactive=True, assess_fn=lambda h, u, p=22: asked.append(h) or GOOD)
        self.assertEqual((rc, asked, self.calls), (1, [], []))

    def test_saying_no_after_a_good_result_stops_before_the_install(self):
        rc = self.run_flow(args(), ask=["1", "agent", "y", "n"], scan=lambda nets, known=(): (self.DEV, []), interactive=True)
        self.assertEqual((rc, self.calls), (0, []))

    def test_nothing_found_says_what_to_try_and_does_not_install(self):
        rc = self.run_flow(args(), scan=lambda nets, known=(): ([], []), interactive=True)
        self.assertEqual((rc, self.calls), (1, []))
        self.assertIn("ct-setup add-worker box.example", self.text())

    def test_without_a_terminal_and_without_an_address_nothing_is_guessed(self):
        rc = self.run_flow(args(yes=True), scan=lambda nets, known=(): (self.DEV, []))
        self.assertEqual((rc, self.calls), (1, []))


class TheRealInstaller(unittest.TestCase):
    """The guard that matters: the ordinary installer, run for one more worker, writes only that worker's own files."""

    def setUp(self):
        from test_install import InstallBase
        self.base = InstallBase("setUp")
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)                       # the base's patches (home folder, colour) must end with this test, or they leak into every test after it
        self.addCleanup(self.base.tearDown)

    def test_adding_a_worker_leaves_the_first_installs_configuration_clones_and_commands_alone(self):
        b = self.base
        m = b.machine
        home = m.env["HOME"]
        main, first_home = f"{home}/.config/claude-tenta/ct.toml", str(b.tmp / "client-home")
        for path, text in ((main, MAIN), (f"{first_home}/install-answers.toml", '[server]\nhost = "mini.example"\n'), (f"{first_home}/approval/key.pub", "ssh-ed25519 AAAAMAIN approver\n"),
                           (f"{first_home}/approval/key", "PRIVATE-PLACEHOLDER")):
            m.write(path, text)
        watched = [m.path(p) for p in (main, f"{first_home}/install-answers.toml", f"{first_home}/approval/key.pub", f"{first_home}/approval/key")]
        before = addworker.fingerprint(watched)
        worker_home, worker_conf = f"{home}/.ct/workers/tenta2", f"{home}/.config/claude-tenta/tenta2.toml"
        inst = install.Installer(b.tmp, ssh_factory=lambda h, u, p: b.ssh, local=m, say=b.said.append, use_defaults=True, repos_factory=lambda cfg: b.repos, poll_s=1, sleep=lambda s: None,
                                 archive=lambda src: (b"TARDATA", "a" * 40, False), interactive=lambda argv: (0, ""), verify=lambda cfg: True, home=m.path(worker_home), config_path=worker_conf, targeted=True,
                                 given={**__import__("test_install").GIVEN, "identity.name": "tenta2", "server.host": "worker2.example", "server.admin_user": "agent", "transport.kind": "direct"})
        inst.run_all()
        self.assertTrue(m.exists(worker_conf), "the new worker has its own configuration file")
        self.assertIn("tenta2", m.read(worker_conf))
        self.assertEqual(addworker.fingerprint(watched), before, "nothing of the first install changed")
        self.assertFalse(m.exists(f"{home}/.local/bin/ct-run"), "no commands were linked again")
        self.assertTrue(any("left as they are" in str(r) for r in inst.report), inst.report)
        for rc in (".zshrc", ".bashrc", ".profile"):
            self.assertFalse(m.exists(f"{home}/{rc}"), rc)                                              # no shell startup file was touched
        self.assertIn("tenta2", str(inst.state_file), "its progress is kept in its own folder")


if __name__ == "__main__":
    unittest.main()
