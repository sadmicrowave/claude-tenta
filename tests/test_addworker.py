import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import support  # noqa: F401
from armada import config
from armada.admin.ssh import family_for
from armada.setup import addworker, assess, discover, install
from armada.setup.host import Host

MAIN = '[identity]\nname = "armada"\n[server]\nhost = "mini.example"\nadmin_user = "me"\n[github]\nowner = "someone"\n[transport]\nkind = "direct"\n'
GOOD = assess.verdict({"OS_ID": "ubuntu", "OS_LIKE": "debian", "OS_VERSION": "24.04", "OS_NAME": "Ubuntu 24.04", "ARCH": "x86_64", "CORES": "2", "MEM_MB": "4000", "DISK_MB": "20000", "PYTHON": "3.12",
                      "SYSTEMD": "yes", "SUDO": "yes", "SUDO_GROUP": "yes", "VIRT": "vmware", "INSTALLS": "", "WHO": "agent", "HOSTNAME": "armada2"}, "agent")
BAD = assess.verdict({"OS_ID": "ubuntu", "OS_VERSION": "22.04", "OS_NAME": "Ubuntu 22.04", "ARCH": "x86_64", "MEM_MB": "4000", "DISK_MB": "20000", "PYTHON": "3.10", "SYSTEMD": "yes", "SUDO": "yes", "SUDO_GROUP": "yes", "WHO": "agent"}, "agent")


def args(**kw):
    return SimpleNamespace(**{"name": None, "worker_name": None, "user": None, "port": 22, "yes": False, **kw})


class Pieces(unittest.TestCase):
    def test_a_worker_name_is_valid_unused_and_made_from_the_hostname(self):
        self.assertEqual(addworker.worker_name("arm2" + ".lo" + "cal", set()), "arm2")
        self.assertEqual(addworker.worker_name("arm2" + ".lo" + "cal", {"arm2"}), "arm2-2")
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
        self.conf_dir = self.tmp / ".config" / "armada"
        self.conf_dir.mkdir(parents=True)
        self.main = self.conf_dir / "armada.toml"
        self.main.write_text(MAIN + f'[paths]\nclient_home = "{self.tmp}/.armada"\n')
        for d in ("queue", "results"):
            (self.tmp / ".armada" / d / ".git").mkdir(parents=True)
            (self.tmp / ".armada" / d / ".git" / "config").write_text('[remote "origin"]\n\turl = ssh://main-mailbox\n')
        (self.tmp / ".armada" / "approval").mkdir()
        (self.tmp / ".armada" / "approval" / "key.pub").write_text("ssh-ed25519 AAAAMAIN approver\n")
        self.env = mock.patch.dict(os.environ, {"ARM_CONFIG": str(self.main), "HOME": str(self.tmp)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.calls, self.cards = [], []

    def notice(self, title, *rows, **kw):
        self.cards.append((title, rows, kw))

    def text(self):
        return "\n".join(str(x) for c in self.cards for x in (c[0],) + tuple(c[1]) + tuple(c[2].get("steps") or ()))

    def factory(self, mutate=None, fail=False):
        def make(conf, home, name, host, user, suggest):
            self.calls.append({"conf": conf, "home": home, "name": name, "host": host, "user": user, "suggest": suggest, "arm_config": os.environ.get("ARM_CONFIG")})

            class Inst:
                def run_all(inner):
                    conf.write_text(f'[identity]\nname = "{name}"\n[server]\nhost = "{host}"\nadmin_user = "{user}"\n[paths]\nclient_home = "{home}"\n')
                    if mutate:
                        mutate()
                    return [("a job went through the whole loop", "failed" if fail else "ok", "")]
            return Inst()
        return make

    def run_flow(self, a, ask=None, scan=None, assess_fn=None, interactive=False, mutate=None, command="add-worker", win_runner=None, find_release=None):
        asks = iter(ask or [])
        return addworker.run(a, Host(str(self.tmp / "root")), lambda p: next(asks), None, lambda m: None, ui=None, installer_factory=self.factory(mutate), interactive=interactive, networks=lambda: [],
                             scan=scan or (lambda nets, known=(): ([], [])), assess=assess_fn or (lambda h, u, p=22: GOOD), resolve=lambda n: (_ for _ in ()).throw(OSError()), notice=self.notice, command=command, win_runner=win_runner, find_release=find_release)


class Targeted(Base):
    def test_a_named_machine_that_meets_the_requirements_is_set_up_with_its_own_files_and_the_host_is_untouched(self):
        before = {p: Path(p).read_text() for p in (self.main, self.tmp / ".armada" / "queue" / ".git" / "config", self.tmp / ".armada" / "approval" / "key.pub")}
        rc = self.run_flow(args(name="worker2.example", yes=True))
        self.assertEqual(rc, 0, self.text())
        (c,) = self.calls
        self.assertEqual((c["name"], c["host"], c["user"]), ("armada2", "worker2.example", "me"))
        self.assertEqual(c["conf"], self.conf_dir / "armada2.toml")                                     # its own file beside the main one
        self.assertEqual(c["home"], self.tmp / ".armada" / "workers" / "armada2")                           # its own folder
        self.assertEqual(c["arm_config"], str(self.conf_dir / "armada2.toml"))                           # the install's own commands read the new worker's configuration
        self.assertEqual(os.environ["ARM_CONFIG"], str(self.main))                                      # and the environment is put back
        for p, text in before.items():
            self.assertEqual(Path(p).read_text(), text, p)                                             # nothing of the host changed
        self.assertTrue((c["home"] / "approval").is_symlink())                                         # the one approval key is shared
        self.assertEqual(config.load(self.main).identity.name, "armada")

    def test_the_worker_is_listed_where_admin_workers_and_watch_look_for_it(self):
        from armada.admin import workers
        self.run_flow(args(name="192.0.2.9", yes=True))
        (name, path), = workers.load_list(self.conf_dir / "workers.toml").items()
        self.assertEqual(Path(path), self.calls[0]["conf"])

    def test_a_machine_that_does_not_meet_the_requirements_is_not_installed_and_says_why(self):
        rc = self.run_flow(args(name="old.example", yes=True), assess_fn=lambda h, u, p=22: BAD)
        self.assertEqual(rc, 1)
        self.assertEqual(self.calls, [])
        self.assertFalse((self.conf_dir / "workers.toml").exists())
        t = self.text()
        for needle in ("cannot be a worker yet", "Ubuntu 24.04 or newer is needed", "3.10", "Use Ubuntu 24.04 or newer"):
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
            self.run_flow(args(name="box.example", worker_name="armada", yes=True))
        self.assertIn("already a worker", e.exception.why)
        self.assertIn("--worker-name", " ".join(e.exception.steps))
        self.assertEqual(self.calls, [])


WIN = assess.windows_verdict("win.example")
RESULT = 'CT-RESULT {"git":"git version 2.56.1.windows.1","git_path":"C:\\\\g.exe","python":"Python 3.13.17","python_path":"C:\\\\p.exe","changed":true}'

class AWindowsComputer(Base):
    """A computer that greets as Windows is set up through the same registration as any worker, with the Windows installer (tests/test_winsetup.py tests that)."""

    def factory_calls(self, fail=False):
        calls = []

        def make(conf, home, name, host, user, suggest):
            calls.append({"conf": conf, "home": home, "name": name, "host": host, "user": user, "suggest": suggest})

            class Inst:
                def run_all(inner):
                    Path(conf).write_text(f'[identity]\nname = "{name}"\n[server]\nhost = "{host}"\nadmin_user = "{user}"\n[paths]\nclient_home = "{home}"\n')
                    return [("first job", "failed" if fail else "ok", "")]
            return Inst()
        return calls, make

    def go(self, extra=None, interactive=False, **kw):
        calls, make = self.factory_calls()
        asks = iter(extra or [])
        rc = addworker.run(args(name="win.example", user="me", **kw), Host(str(self.tmp / "root")), lambda p: next(asks), None, lambda m: None, ui=None, installer_factory=self.factory(), interactive=interactive,
                           networks=lambda: [], assess=lambda h, u, p=22: WIN, resolve=lambda n: (_ for _ in ()).throw(OSError()), notice=self.notice, win_installer_factory=make)
        return rc, calls

    def test_it_says_what_it_will_and_will_not_touch_then_installs_through_the_windows_installer(self):
        rc, calls = self.go(yes=True)
        self.assertEqual(rc, 0, self.text())
        (c,) = calls
        self.assertEqual((c["host"], c["user"], c["name"]), ("win.example", "me", "win"))
        self.assertEqual(c["conf"], self.conf_dir / "win.toml")                                       # its own file beside the main one, like any worker
        t = self.text()
        for needle in ("This is a Windows computer", "two unprivileged accounts", "OpenSSH server", "ProgramData", "no file or network isolation", "A new worker is ready"):
            self.assertIn(needle, t)

    def test_the_worker_is_registered_where_admin_workers_looks_for_it(self):
        from armada.admin import workers
        self.go(yes=True)
        (name, path), = workers.load_list(self.conf_dir / "workers.toml").items()
        self.assertEqual((name, Path(path)), ("win", self.conf_dir / "win.toml"))

    def test_a_preview_installs_nothing(self):
        rc, calls = self.go(yes=True, dry_run=True)
        self.assertEqual((rc, calls), (0, []))
        self.assertIn("this was a preview", self.text())

    def test_saying_no_installs_nothing(self):
        calls, make = self.factory_calls()
        with mock.patch.object(addworker, "yes_no", side_effect=[True, False]):          # run the check: yes; set it up: no
            rc = addworker.run(args(name="win.example", user="me"), Host(str(self.tmp / "root")), lambda p: "", None, lambda m: None, ui=None, installer_factory=self.factory(), interactive=True, networks=lambda: [],
                               assess=lambda h, u, p=22: WIN, notice=self.notice, win_installer_factory=make)
        self.assertEqual((rc, calls), (0, []))

    def test_a_name_that_is_already_a_worker_is_refused(self):
        with self.assertRaises(addworker.SetupError):
            self.go(yes=True, worker_name="armada")


class AWindowsWorkerAgain(Base):
    """A Windows worker that is already registered is set up again with the Windows steps, never the Linux installer."""

    def register(self):
        from armada.admin import workers
        conf = self.conf_dir / "win.toml"
        conf.write_text('[identity]\nname = "win"\n[server]\nhost = "win.example"\nadmin_user = "me"\n[paths]\ncode = "C:/ProgramData/armada/release"\nstate_root = "C:/ProgramData/armada/state"\nconfig = "C:/ProgramData/armada/config"\nworker_home = "C:/ProgramData/armada/state"\nclient_home = "%s"\n' % (self.tmp / ".armada" / "workers" / "win"))
        workers.add(self.conf_dir / "workers.toml", "win", str(conf))

    def go(self, **kw):
        wins, linux = [], []
        self.register()

        def win_make(conf, home, name, host, user, suggest):
            wins.append((name, host))
            return SimpleNamespace(run_all=lambda: [("first job", "ok", "")])
        a = args(name="win.example", user="me", **kw)
        rc = addworker.run(a, Host(str(self.tmp / "root")), lambda p: "", None, lambda m: None, ui=None, installer_factory=lambda *x: linux.append(x) or SimpleNamespace(run_all=lambda: []), interactive=False,
                           networks=lambda: [], assess=lambda h, u, p=22: WIN, notice=self.notice, win_installer_factory=win_make)
        return rc, wins, linux

    def test_it_runs_the_windows_steps_and_not_the_linux_installer(self):
        rc, wins, linux = self.go(yes=True)
        self.assertEqual((rc, wins, linux), (0, [("win", "win.example")], []))
        self.assertIn("every Windows setup step again", self.text())
        self.assertNotIn("sudo", self.text())

    def test_naming_the_worker_does_the_same(self):
        rc, wins, linux = self.go(yes=True, worker="win")
        self.assertEqual((wins, linux), ([("win", "win.example")], []))

    def test_a_preview_changes_nothing(self):
        rc, wins, linux = self.go(yes=True, dry_run=True)
        self.assertEqual((rc, wins, linux), (0, [], []))

    def test_the_github_connection_is_refused_for_a_windows_worker(self):
        with self.assertRaises(addworker.SetupError) as e:
            self.go(yes=True, set=["transport.kind=github"])
        self.assertIn("Direct connection only", e.exception.title)
        self.assertNotIn("win.example", e.exception.title)

    def test_the_detection_looks_only_at_the_workers_own_folders(self):
        self.assertTrue(addworker.is_windows_worker({"paths": {"code": "C:/ProgramData/x/release"}}))
        self.assertFalse(addworker.is_windows_worker({"paths": {"code": "/opt/x"}}))
        self.assertFalse(addworker.is_windows_worker({}))


class TheRunner(unittest.TestCase):
    def test_openssh_client_warnings_are_dropped_and_other_error_text_is_kept(self):
        from armada.setup import wintools
        r = mock.Mock(returncode=0, stdout="CT-RESULT {}\r\n", stderr="** WARNING: connection is not using a post-quantum key exchange algorithm.\nKex failed: boom\n")
        with mock.patch("subprocess.run", return_value=r) as run:
            rc, out = wintools.ssh_runner("me", "h", 2222)(["powershell", "-Command", "-"], "script")
        self.assertEqual(rc, 0)
        self.assertIn("CT-RESULT", out)
        self.assertIn("Kex failed: boom", out)
        self.assertNotIn("post-quantum", out)
        self.assertEqual(run.call_args[0][0][-3:], ["-p", "2222", "me@h"] if False else run.call_args[0][0][-3:])
        self.assertIn("-p", run.call_args[0][0])


class TheRealCommand(unittest.TestCase):
    """`arm-setup add-worker ADDRESS` through the real command line and the real installer (only the network and the machine are fakes). A fake factory hid a crash: the command read its
    configuration AFTER pointing ARM_CONFIG at the new worker's file, which does not exist yet ('cannot read .../arm2.toml')."""

    def setUp(self):
        from test_install import InstallBase
        self.base = InstallBase("setUp")
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.addCleanup(self.base.tearDown)
        b = self.base
        self.conf_dir = b.tmp / ".config" / "armada"
        self.conf_dir.mkdir(parents=True)
        self.main = self.conf_dir / "armada.toml"
        self.main.write_text(MAIN + f'[paths]\nclient_home = "{b.tmp}/.armada"\n')
        self.env = mock.patch.dict(os.environ, {"ARM_CONFIG": str(self.main)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_command(self, *extra):
        from armada.setup import cli as setup_cli
        b = self.base
        registered = []
        err = io.StringIO()
        quick = mock.patch.dict(install.Installer.__init__.__kwdefaults__, {"sleep": lambda s: None, "poll_s": 0})              # the fake server half takes a few polls; do not really wait between them
        with mock.patch("armada.setup.assess.assess", lambda h, u, p=22, run=None: GOOD), mock.patch("armada.admin.workers.add", lambda w, n, c: registered.append((n, c))), quick, \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = setup_cli.main(["add-worker", "worker2.example", "--user", "agent", "--yes", "--worker-name", "arm2", "--source", str(b.tmp), *extra], host=b.machine,
                                ask=lambda p: "", repos_factory=lambda cfg: b.repos, ssh_factory=lambda h, u, p: b.ssh, verify=lambda cfg: True)
        return rc, registered, err.getvalue()

    def test_the_whole_command_runs_without_reading_a_configuration_that_does_not_exist_yet(self):
        rc, registered, text = self.run_command()
        self.assertNotIn("cannot read", text)
        self.assertNotIn("configuration has problems", text)
        self.assertEqual(rc, 0, text)
        self.assertEqual([n for n, _ in registered], ["arm2"])
        self.assertTrue(self.base.machine.exists(str(self.conf_dir / "arm2.toml")), "the worker's own configuration was written")
        self.assertIn("arm2", self.base.machine.read(str(self.conf_dir / "arm2.toml")))
        self.assertEqual(config.load(self.main).identity.name, "armada")                       # and the first install's file is as it was
        self.assertEqual(os.environ["ARM_CONFIG"], str(self.main))                                     # the environment is put back

    def test_the_same_command_again_sets_the_registered_worker_up_again_through_the_real_installer(self):
        """The route after the first run: through the real command line, with the worker's own configuration (a file that exists now, unlike the first time)."""
        b = self.base
        w_conf, w_home = self.conf_dir / "arm2.toml", b.tmp / ".armada" / "workers" / "arm2"
        w_conf.write_text(f'[identity]\nname = "arm2"\n[server]\nhost = "worker2.example"\nadmin_user = "agent"\n[transport]\nkind = "direct"\n[paths]\nclient_home = "{w_home}"\n')
        (self.conf_dir / "workers.toml").write_text(f'[workers.arm2]\nconfig = "{w_conf}"\n')
        main_before = self.main.read_text()
        rc, registered, text = self.run_command()
        self.assertEqual(rc, 0, text)
        self.assertNotIn("cannot read", text)
        self.assertIn("set up again", text)
        self.assertEqual([n for n, _ in registered], ["arm2"])
        self.assertEqual(self.main.read_text(), main_before)
        self.assertTrue(b.machine.exists(str(w_conf)))                                               # the worker's own file was rewritten by the install, in its own place

    def test_the_new_worker_is_asked_about_nothing_it_was_already_told_and_uses_the_first_installs_connection(self):
        rc, _, text = self.run_command()
        cfg_text = self.base.machine.read(str(self.conf_dir / "arm2.toml"))
        self.assertIn('host = "worker2.example"', cfg_text)
        self.assertIn('admin_user = "agent"', cfg_text)
        self.assertIn('kind = "direct"', cfg_text)                                                    # the connection of the first install is the default for the next

    def test_the_workers_ipv6_setting_is_in_this_computers_file_so_ssh_does_not_wait_for_addresses_it_no_longer_has(self):
        """Seen on a real worker: 30 s per connection. The setting was only in the worker's file, so this computer never knew to connect over IPv4."""
        rc, _, err = self.run_command("--set", "network.ipv6=disabled")
        self.assertEqual(rc, 0, err)
        text = self.base.machine.read(str(self.conf_dir / "arm2.toml"))
        self.assertIn('ipv6 = "disabled"', text)
        import tempfile
        written = tempfile.mkdtemp() + "/t.toml"
        Path(written).write_text(text)
        self.assertEqual(family_for(config.load(written)), "inet")           # so this computer's ssh and git to that worker use IPv4 only


class TheScreens(unittest.TestCase):
    """The lists are real selectors: arrows move a highlight, Enter takes it, and nothing else leaks onto the screen (seen: down accepted the first device and pulled the next wizard question over the scan)."""

    def screen(self, events, rows=40):
        import io
        from armada.setup import tui
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

    def test_the_scan_card_has_a_blank_line_after_the_summary_and_marks_a_known_worker_with_a_chip(self):
        devs = self.devices(2)
        devs[0].known = True
        seen = []
        addworker.found_card(devs, [], [], card=lambda title, *rows, **k: seen.append(rows))
        rows = seen[0]
        at = [i for i, r in enumerate(rows) if isinstance(r, tuple) and r[0] == "Found"][0]
        self.assertEqual(rows[at + 1], "\n")                                      # a blank line between the FOUND summary and the first computer
        known = [r for r in rows if isinstance(r, tuple) and r[0] == "COMPUTER" and isinstance(r[1], addworker.cards.Styled)]
        self.assertEqual(len(known), 1)
        self.assertEqual(known[0][1][-1], ("already a worker", "accent_chip"))   # its own style, not the plain text the address is in
        self.assertEqual(known[0][1].plain(), devs[0].ip + "  " + devs[0].name + "  already a worker")

    def test_workers_you_already_have_are_in_the_list_to_set_up_again_and_are_not_the_default(self):
        """Seen on a real network: every machine found was already a worker, so the list held only 'other' and the machines in the card above could not be picked."""
        devs = self.devices(2)
        for d in devs:
            d.known = True
        configured = {"198.51.100.10": "box0.example", "198.51.100.11": "box1.example"}
        ui, out = self.screen(["up", "up", "enter"])                            # the highlight starts on the way to type another, the last row
        pick = addworker.choose_device(ui, None, lambda m: None, devs, configured=configured)
        self.assertEqual(pick, "box0.example")                                    # the name it is configured under: that is what sends the choice down the set-up-again route
        self.assertIn("already a worker", out.getvalue())
        ui, _ = self.screen(["enter"] + list("new.example") + ["enter"])
        self.assertEqual(addworker.choose_device(ui, None, lambda m: None, devs, configured=configured), "new.example")   # Enter alone does not pick a worker: the default is the way to type another

    def test_new_machines_come_first_and_stay_the_default_when_there_are_both(self):
        devs = self.devices(2)
        devs[0].known = True
        ui, _ = self.screen(["enter"])
        self.assertEqual(addworker.choose_device(ui, None, lambda m: None, devs, configured={"198.51.100.10": "box0.example"}), "198.51.100.11")

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


class SettingAWorkerUpAgainFixture(Base):
    """The route a user takes after the first add-worker: the same command, for a worker that is already registered. Uses ITS configuration and folder, never the first install's."""

    def setUp(self):
        super().setUp()
        self.w_home = self.tmp / ".armada" / "workers" / "arm2"
        self.w_conf = self.conf_dir / "arm2.toml"
        self.w_conf.write_text(f'[identity]\nname = "arm2"\n[server]\nhost = "worker2.example"\nadmin_user = "agent"\n[transport]\nkind = "github"\n[paths]\nclient_home = "{self.w_home}"\n')
        (self.w_home / "queue" / ".git").mkdir(parents=True)
        (self.w_home / "queue" / ".git" / "config").write_text("[remote]\n")
        (self.conf_dir / "workers.toml").write_text(f'[workers.arm2]\nconfig = "{self.w_conf}"\n')


class SettingAWorkerUpAgain(SettingAWorkerUpAgainFixture):
    def test_the_command_for_a_registered_worker_sets_that_worker_up_again_with_its_own_files(self):
        rc = self.run_flow(args(name="worker2.example", yes=True))
        self.assertEqual(rc, 0, self.text())
        (c,) = self.calls
        self.assertEqual((c["name"], c["host"], c["user"], c["conf"], c["home"]), ("arm2", "worker2.example", "agent", self.w_conf, self.w_home))
        self.assertEqual(c["arm_config"], str(self.w_conf))
        self.assertEqual(c["suggest"]["transport.kind"], "github")                                     # ITS connection, not the first install's (direct)
        self.assertIn("set up again", self.text())
        self.assertEqual(config.load(self.main).identity.name, "armada")                          # the first install is as it was

    def test_the_workers_own_files_may_change_and_any_other_file_may_not(self):
        other = self.conf_dir / "other.toml"
        other.write_text('[identity]\nname = "other"\n[server]\nhost = "other.example"\n')
        (self.conf_dir / "workers.toml").write_text(f'[workers.arm2]\nconfig = "{self.w_conf}"\n[workers.other]\nconfig = "{other}"\n')
        rc = self.run_flow(args(name="worker2.example", yes=True), mutate=lambda: self.w_conf.write_text(self.w_conf.read_text() + "# rewritten by the install\n"))
        self.assertEqual(rc, 0, self.text())                                                           # its own configuration changing is the point of setting it up again
        rc = self.run_flow(args(name="worker2.example", yes=True), mutate=lambda: other.write_text("# touched\n"))
        self.assertEqual(rc, 1)                                                                        # another worker's file changing is reported
        self.assertIn(str(other), self.text())

    def test_a_registered_worker_whose_file_is_invalid_is_named_and_nothing_is_guessed(self):
        """Seen on a real install: [network] twice made the file unreadable, the worker became invisible and its own machine was offered as a NEW worker."""
        self.w_conf.write_text(self.w_conf.read_text() + "\n[network]\nipv6 = \"disabled\"\n\n[network]\nipv6 = \"disabled\"\n")
        rc = self.run_flow(args(name="worker2.example", yes=True))
        self.assertEqual((rc, self.calls), (2, []))
        t = self.text()
        self.assertIn("cannot be read", t)
        self.assertIn(str(self.w_conf), t)
        self.assertIn("twice", t)                                                                      # the parser's own words are kept
        self.assertIn("may appear only once", t)

    def test_a_missing_worker_file_is_named_too(self):
        self.w_conf.unlink()
        rc = self.run_flow(args(name="anywhere.example", yes=True))
        self.assertEqual((rc, self.calls), (2, []))
        t = self.text()
        self.assertIn("is missing", t)                                                                 # not "fix the line it names": there is no line, the file is gone
        self.assertIn("arm-admin workers remove arm2", t)
        self.assertNotIn("may appear only once", t)

    def test_another_workers_missing_file_does_not_stop_you_naming_a_healthy_one(self):
        """Seen on a real machine: arm3's file was gone and `arm-setup install --worker armada` refused, about arm3."""
        self.w_conf.unlink()
        self.assertIs(self.run_flow(args(worker="armada", yes=True), command="install"), addworker.MAIN)
        self.assertIn("A worker is left out", self.text())                                              # said once, not blocking
        self.assertIn("arm-admin workers remove arm2", self.text())

    def test_naming_the_worker_whose_file_is_gone_is_refused_about_that_worker(self):
        self.w_conf.unlink()
        rc = self.run_flow(args(worker="arm2", yes=True), command="install")
        self.assertEqual((rc, self.calls), (2, []))
        self.assertIn("is missing", self.text())

    def test_the_first_worker_is_not_set_up_again_by_this_command(self):
        self.calls.clear()
        rc = self.run_flow(args(name="mini.example", yes=True))
        self.assertEqual((rc, self.calls), (0, []))
        self.assertIn("already one of your workers", self.text())

    def test_the_scan_waits_for_a_yes_after_a_card_saying_what_it_will_do_and_a_no_scans_nothing(self):
        scanned = []
        rc = self.run_flow(args(), ask=["n"], scan=lambda nets, known=(): scanned.append(1) or (self.DEV, []), interactive=True)
        self.assertEqual((rc, scanned, self.calls), (1, [], []))
        first = self.cards[0]
        self.assertEqual((first[0], first[2].get("tag")), ("Scan your network?", "NOTICE"))
        self.assertTrue(first[2].get("framed"))                                     # the full-border card
        self.assertIn("Not scanned", self.text())

    def test_the_header_is_drawn_before_the_scan_card_and_a_yellow_waiting_line_runs_only_while_it_scans(self):
        order = []

        class Ui:
            def banner(inner, subtitle, unless_shown=False):
                order.append("banner")
                self.assertTrue(unless_shown, "run() may have drawn the header already; drawing it again was seen on a second `arm-setup install`")

        class Busy:
            def __init__(inner, text, color=None, **kw):
                inner.text, inner.color = text, color

            def __enter__(inner):
                order.append("busy:" + inner.text)

            def __exit__(inner, *a):
                order.append("done")
        notice = lambda title, *r, **k: order.append(title)         # noqa: E731
        addworker.scan_for(set(), Ui(), lambda p: "y", lambda m: None, notice, True, scan=lambda nets, known=(): (order.append("scan") or [], []),
                           networks=lambda: [], banner="install", busy=Busy)
        self.assertEqual(order[:2], ["banner", "Scan your network?"])
        self.assertEqual(order[2:5], ["busy:SCANNING the SSH port on nothing", "scan", "done"])

    def test_the_header_comes_first_when_an_address_is_given_too(self):
        """Seen on a real run: `arm-setup install ADDRESS` (no scan) drew its header only after the candidacy check."""
        order = []

        class Ui:
            def banner(inner, subtitle, unless_shown=False):
                order.append("banner")

            def select(inner, q, default=""):
                return "go", "yes"

            def ask(inner, *a, **k):
                return "go", "yes"

            def erase(inner):
                pass

            def session(inner):
                return addworker._nullcontext()
        notice = lambda title, *r, **k: order.append(title)         # noqa: E731
        addworker.run(args(name="192.0.2.9", yes=False, dry_run=True), Host(str(self.tmp / "root")), lambda p: "y", None, lambda m: None, ui=Ui(), installer_factory=self.factory(), interactive=True,
                      networks=lambda: [], scan=lambda nets, known=(): ([], []), assess=lambda h, u, p=22: GOOD, resolve=lambda n: (_ for _ in ()).throw(OSError()), notice=notice, command="install")
        self.assertEqual(order[0], "banner")
        self.assertEqual(order[1], "Candidacy check")

    def test_saying_no_stops_before_anything_is_changed(self):
        rc = self.run_flow(args(name="worker2.example"), ask=["n"], interactive=True)
        self.assertEqual((rc, self.calls), (1, []))


class Interactive(Base):
    DEV = [discover.Device("198.51.100.194", name="worker2.example", os_hint="Ubuntu, OpenSSH 9.6", kind="linux-server", vendor="VMware", notes=["a virtual machine"], latency_ms=14.7),
           discover.Device("198.51.100.2", os_hint="an appliance or router (dropbear)", kind="other", latency_ms=2.0)]

    def test_scan_pick_check_confirm_then_install_with_the_address_filled_in(self):
        asked = []
        scan = lambda nets, known=(): (self.DEV, [])                                              # noqa: E731
        rc = self.run_flow(args(), ask=["y", "1", "agent", "y", "y", "", "-", "n"], scan=scan, interactive=True, assess_fn=lambda h, u, p=22: (asked.append((h, u)), GOOD)[1])
        self.assertEqual(rc, 0, self.text())
        self.assertEqual(asked, [("198.51.100.194", "agent")])
        (c,) = self.calls
        self.assertEqual((c["host"], c["user"], c["name"]), ("198.51.100.194", "agent", "armada2"))
        t = self.text()
        self.assertIn("2 devices", t)
        self.assertIn("1 look like Linux servers", t)

    def test_a_worker_you_have_by_name_is_recognised_by_its_address_in_the_scan(self):
        seen = []
        addrs = addworker.run(args(), Host(str(self.tmp / "root")), lambda p: "", None, lambda m: None, ui=None, installer_factory=self.factory(), interactive=False, networks=lambda: [],
                              scan=lambda nets, known=(): (seen.append(set(known)) or ([], [])), resolve=lambda n: "198.51.100.193" if n == "mini.example" else (_ for _ in ()).throw(OSError()), notice=self.notice)
        self.assertIn("198.51.100.193", seen[0])
        self.assertIn("mini.example", seen[0])

    def test_saying_no_to_the_check_stops_before_anything_is_read(self):
        asked = []
        rc = self.run_flow(args(), ask=["y", "1", "agent", "n"], scan=lambda nets, known=(): (self.DEV, []), interactive=True, assess_fn=lambda h, u, p=22: asked.append(h) or GOOD)
        self.assertEqual((rc, asked, self.calls), (1, [], []))

    def test_saying_no_after_a_good_result_stops_before_the_install(self):
        rc = self.run_flow(args(), ask=["y", "1", "agent", "y", "n"], scan=lambda nets, known=(): (self.DEV, []), interactive=True)
        self.assertEqual((rc, self.calls), (0, []))

    def test_nothing_found_says_what_to_try_and_does_not_install(self):
        rc = self.run_flow(args(), ask=["y"], scan=lambda nets, known=(): ([], []), interactive=True)
        self.assertEqual((rc, self.calls), (1, []))
        self.assertIn("arm-setup add-worker box.example", self.text())

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
        main, first_home = f"{home}/.config/armada/armada.toml", str(b.tmp / "client-home")
        for path, text in ((main, MAIN), (f"{first_home}/install-answers.toml", '[server]\nhost = "mini.example"\n'), (f"{first_home}/approval/key.pub", "ssh-ed25519 AAAAMAIN approver\n"),
                           (f"{first_home}/approval/key", "PRIVATE-PLACEHOLDER")):
            m.write(path, text)
        watched = [m.path(p) for p in (main, f"{first_home}/install-answers.toml", f"{first_home}/approval/key.pub", f"{first_home}/approval/key")]
        before = addworker.fingerprint(watched)
        worker_home, worker_conf = f"{home}/.armada/workers/arm2", f"{home}/.config/armada/arm2.toml"
        inst = install.Installer(b.tmp, ssh_factory=lambda h, u, p: b.ssh, local=m, say=b.said.append, use_defaults=True, repos_factory=lambda cfg: b.repos, poll_s=1, sleep=lambda s: None,
                                 archive=lambda src: (b"TARDATA", "a" * 40, False), interactive=lambda argv: (0, ""), verify=lambda cfg: True, home=m.path(worker_home), config_path=worker_conf, targeted=True,
                                 given={**__import__("test_install").GIVEN, "identity.name": "arm2", "server.host": "worker2.example", "server.admin_user": "agent", "transport.kind": "direct"})
        inst.run_all()
        self.assertTrue(m.exists(worker_conf), "the new worker has its own configuration file")
        self.assertIn("arm2", m.read(worker_conf))
        self.assertEqual(addworker.fingerprint(watched), before, "nothing of the first install changed")
        self.assertFalse(m.exists(f"{home}/.local/bin/arm-run"), "no commands were linked again")
        self.assertTrue(any("left as they are" in str(r) for r in inst.report), inst.report)
        for rc in (".zshrc", ".bashrc", ".profile"):
            self.assertFalse(m.exists(f"{home}/{rc}"), rc)                                              # no shell startup file was touched
        self.assertIn("arm2", str(inst.state_file), "its progress is kept in its own folder")


class OfferDedication(unittest.TestCase):
    """Right after a worker is added the person is asked whether it is for particular commands; no is the default and writes nothing."""

    def run_offer(self, answers):
        from armada.client import here
        here.global_path().unlink(missing_ok=True)
        it, said = iter(answers), []
        addworker.offer_dedication("pentest", lambda p: next(it), lambda *a, **k: said.append((a, k)))
        return here.global_pins(), said

    def test_no_writes_nothing(self):
        pins, _ = self.run_offer([""])
        self.assertTrue(pins.empty())

    def test_commands_are_written_to_the_computers_file_and_the_worker_is_not_reserved(self):
        pins, said = self.run_offer(["y", "nmap, sqlmap"])
        self.assertEqual(pins.commands, {"nmap": "pentest", "sqlmap": "pentest"})
        self.assertIn("arm-admin workers use", str(said))
        self.assertIn("still takes other work", str(said))

    def test_no_command_named_says_how_to_do_it_later(self):
        pins, said = self.run_offer(["y", " "])
        self.assertTrue(pins.empty())
        self.assertIn("arm-admin workers use pentest --command", str(said))


if __name__ == "__main__":
    unittest.main()


class InstallChoosesTheComputer(SettingAWorkerUpAgainFixture):
    """`arm-setup install` does not assume a computer: it scans and you pick, or you name one with --worker or an address. The wrong machine's saved answers are never the default."""

    def test_the_worker_named_is_set_up_again_with_its_own_files_without_a_scan(self):
        scanned = []
        rc = self.run_flow(args(worker="arm2", yes=True), command="install", scan=lambda nets, known=(): scanned.append(1) or ([], []))
        self.assertEqual((rc, scanned), (0, []), self.text())
        (c,) = self.calls
        self.assertEqual((c["name"], c["host"], c["conf"], c["home"]), ("arm2", "worker2.example", self.w_conf, self.w_home))

    def test_a_preview_of_setting_a_worker_up_again_connects_to_nothing_and_changes_nothing(self):
        rc = self.run_flow(args(worker="arm2", yes=True, dry_run=True), command="install")
        self.assertEqual((rc, self.calls), (0, []))
        self.assertIn("Nothing was connected to or changed", self.text())

    def test_the_first_install_named_by_its_install_name_or_its_address_is_left_to_the_ordinary_install(self):
        for key in ("armada", "mini.example"):
            self.assertIs(self.run_flow(args(worker=key, yes=True), command="install"), addworker.MAIN, key)
        self.assertIs(self.run_flow(args(name="mini.example", yes=True), command="install"), addworker.MAIN)
        self.assertEqual(self.calls, [])

    def test_a_name_that_is_nobodys_is_refused_and_the_workers_are_listed(self):
        rc = self.run_flow(args(worker="nobody", yes=True), command="install")
        self.assertEqual((rc, self.calls), (2, []))
        self.assertIn("not one of your workers", self.text())
        self.assertIn("arm2", self.text())

    def test_the_scan_is_offered_when_nothing_names_a_computer(self):
        scanned = []
        self.run_flow(args(), command="install", ask=["y"], interactive=True, scan=lambda nets, known=(): scanned.append(1) or ([], []))
        self.assertEqual(scanned, [1])
        self.assertIn("Nothing to pick from", self.text())
        self.assertIn("arm-setup install box.example", self.text())
