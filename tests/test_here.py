"""The folder's own settings (.tenta.toml, written by `ct-run use`): which worker this folder's jobs go to, and single commands that go to another."""
import io
import os
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path

import support  # noqa: F401
from ct.client import cli, here
from ct.client.repo import ClientError
from test_place import cfg_file


@contextmanager
def in_dir(d):
    old = os.getcwd()
    os.chdir(d)
    try:
        yield
    finally:
        os.chdir(old)


def run(argv, cfg=None, answer=None):
    out, err = io.StringIO(), io.StringIO()
    from unittest import mock
    with redirect_stdout(out), redirect_stderr(err), mock.patch("sys.stdin.isatty", return_value=answer is not None), mock.patch("builtins.input", return_value=answer or ""):
        try:
            rc = cli.cmd_use(None, cfg, cli.build_parser()[0].parse_args(["use", *argv]))
        except ClientError as e:
            rc = cli.fail_from(e)
    return rc, out.getvalue() + err.getvalue()


class FolderFileFixture(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.d = Path(self.t.name).resolve()
        self.addCleanup(self.t.cleanup)
        self.cfg = cfg_file(self.d, "main")
        (self.d / "workers.toml").write_text(f'[workers.pentest]\nconfig = "{cfg_file(self.d, "pentest").source}"\n')
        self.cfg = cfg_file(self.d, "main")


class FolderFile(FolderFileFixture):
    def test_use_writes_an_editable_file_that_is_read_back(self):
        with in_dir(self.d):
            rc, out = run(["pentest"], self.cfg)
            self.assertEqual(rc, 0, out)
            text = (self.d / here.FILE).read_text()
            self.assertIn('worker = "pentest"', text)
            self.assertIn("edit it freely", text)
            self.assertEqual(here.pin_for("anything")[0], "pentest")

    def test_one_command_goes_elsewhere_and_the_rest_follow_the_folder(self):
        with in_dir(self.d):
            run(["pentest", "--command", "nmap"], self.cfg)
            self.assertEqual(here.pin_for("nmap")[0], "pentest")
            self.assertEqual(here.pin_for("pytest"), (None, None))
            run(["main"], self.cfg)
            self.assertEqual(here.pin_for("pytest")[0], "main")
            self.assertEqual(here.pin_for("nmap")[0], "pentest")

    def test_the_nearest_file_above_applies_and_use_edits_that_one(self):
        (self.d / "sub" / "deep").mkdir(parents=True)
        with in_dir(self.d):
            run(["pentest"], self.cfg)
        with in_dir(self.d / "sub" / "deep"):
            self.assertEqual(here.pin_for("x")[0], "pentest")
            run(["main"], self.cfg)
        self.assertFalse((self.d / "sub" / "deep" / here.FILE).exists())
        self.assertIn('"main"', (self.d / here.FILE).read_text())

    def test_clear_removes_one_command_then_the_file(self):
        with in_dir(self.d):
            run(["pentest", "--command", "nmap"], self.cfg)
            run(["--clear", "--command", "nmap"], self.cfg)
            self.assertFalse((self.d / here.FILE).exists())

    def test_an_unknown_worker_is_refused_and_nothing_is_written(self):
        with in_dir(self.d):
            rc, out = run(["nope"], self.cfg)
            self.assertEqual(rc, 2)
            self.assertIn("There is no such worker", out)
            self.assertFalse((self.d / here.FILE).exists())

    def test_a_pin_to_a_worker_that_is_gone_names_the_file_and_the_fix(self):
        (self.d / here.FILE).write_text('worker = "gone"\n')
        with in_dir(self.d), self.assertRaises(ClientError) as c:
            cli.pinned_worker(self.cfg, "pytest")
        self.assertIn(here.FILE, str(c.exception.facts))
        self.assertIn("ct-run use --clear", c.exception.steps)

    def test_a_broken_file_is_reported_with_the_real_error(self):
        (self.d / here.FILE).write_text("worker = \n")
        with in_dir(self.d), self.assertRaises(ClientError) as c:
            here.pins_for()
        self.assertIn("cannot be read", c.exception.title)
        self.assertTrue(c.exception.why)

    def test_show_says_what_is_set_and_when_nothing_is(self):
        with in_dir(self.d):
            self.assertIn("Nothing is set", run([], self.cfg)[1])
            run(["pentest", "--command", "nmap"], self.cfg)
            out = run([], self.cfg)[1]
            self.assertIn("nmap", out)
            self.assertIn("pentest", out)


class Warnings(FolderFileFixture):
    def test_a_command_that_the_folders_worker_already_covers_is_warned_about_and_not_written(self):
        with in_dir(self.d):
            run(["pentest"], self.cfg)
            rc, out = run(["pentest", "--command", "nmap"], self.cfg)
            self.assertEqual(rc, 0)
            self.assertIn("Not added", out)
            self.assertIn("it would change nothing", out)
            self.assertEqual(here.pins_for().commands, {})
            run(["pentest", "--command", "nmap", "--force"], self.cfg)
            self.assertEqual(here.pins_for().commands, {"nmap": "pentest"})

    def test_setting_the_worker_lists_the_commands_that_differ_and_override_replaces_them(self):
        with in_dir(self.d):
            run(["pentest", "--command", "nmap"], self.cfg)
            rc, out = run(["main"], self.cfg, answer="y")
            self.assertIn("nmap", out)
            self.assertIn("pentest", out)                              # the worker is named beside each command
            self.assertEqual(here.pins_for().commands, {})
            self.assertEqual(here.pins_for().worker, "main")

    def test_retain_keeps_them_and_a_command_beats_the_folders_worker(self):
        with in_dir(self.d):
            run(["pentest", "--command", "nmap"], self.cfg)
            run(["main"], self.cfg, answer="n")
            self.assertEqual(here.pin_for("nmap")[0], "pentest")
            self.assertEqual(here.pin_for("pytest")[0], "main")

    def test_with_nobody_to_ask_they_are_retained_and_the_flags_decide(self):
        with in_dir(self.d):
            run(["pentest", "--command", "nmap"], self.cfg)
            run(["main"], self.cfg)
            self.assertEqual(here.pins_for().commands, {"nmap": "pentest"})
            run(["main", "--override"], self.cfg)
            self.assertEqual(here.pins_for().commands, {})


class ComputerFile(FolderFileFixture):
    """~/.tenta.toml: the same file for the whole computer, written by ct-admin workers use."""

    def admin(self, argv):
        from ct.admin import cli as admin_cli
        from types import SimpleNamespace
        out = io.StringIO()
        ap = admin_cli.build_parser()[0]
        a = ap.parse_args(["workers", "use", *argv])
        with redirect_stdout(out), redirect_stderr(out):
            try:
                rc = admin_cli.workers_use(SimpleNamespace(cfg=self.cfg), a)
            except ClientError as e:
                rc = cli.fail_from(e)
        return rc, out.getvalue()

    def tearDown(self):
        here.global_path().unlink(missing_ok=True)

    def test_a_dedicated_command_goes_there_from_every_folder_and_the_folder_command_still_wins(self):
        self.assertEqual(self.admin(["pentest", "--command", "nmap"])[0], 0)
        self.assertIn("nmap", here.global_path().read_text())
        with in_dir(self.d):
            self.assertEqual(here.pin_for("nmap")[0], "pentest")
            self.assertIn("every folder", here.pin_for("nmap")[1])
            run(["main", "--command", "nmap"], self.cfg)
            self.assertEqual(here.pin_for("nmap")[0], "main")

    def test_the_computers_file_is_never_taken_for_a_folders(self):
        self.admin(["pentest", "--command", "nmap"])
        self.assertIsNone(here.find(Path.home()))

    def test_a_dedicated_command_leaves_the_worker_free_for_other_work_and_release_undoes_it(self):
        self.admin(["pentest", "--command", "nmap"])
        self.assertNotIn("reserved", here.global_path().read_text())
        self.admin(["pentest", "--clear"])
        self.assertFalse(here.global_path().exists())

    def test_an_old_reserved_line_is_ignored_and_dropped_on_the_next_write(self):
        here.global_path().write_text('reserved = ["pentest"]\n[commands]\nnmap = "pentest"\n')
        self.assertEqual(here.global_pins().commands, {"nmap": "pentest"})
        self.admin(["pentest", "--command", "sqlmap"])
        self.assertNotIn("reserved", here.global_path().read_text())

    def test_the_workers_card_and_the_classes_table_show_it(self):
        from ct.admin import workers
        self.admin(["pentest", "--command", "nmap"])
        self.admin(["pentest", "--command", "sqlmap"])
        text = workers.dedicated_text(here.global_pins(), "pentest")
        self.assertEqual(text, "nmap, sqlmap")                                  # a plain list that can grow, nothing about what the machine may not do
        self.assertEqual(workers.dedicated_text(here.global_pins(), "main"), "")

    def test_the_classes_view_says_which_workers_offer_each_class_and_flags_a_dedication_to_one_that_does_not(self):
        import re
        from ct.client import view
        info = {"cost": "light", "tier": "auto", "kind": "command", "run": ["nmap"]}
        classes = {"nmap": {"info": info, "offered_by": ["a", "b"], "dedicated": "b"},
                   "sys": {"info": info, "offered_by": ["a"], "dedicated": None},
                   "scan": {"info": info, "offered_by": ["a"], "dedicated": "b"}}
        out = io.StringIO()
        view.render_fleet_classes(classes, [("c", "timed out")], folder_worker="a", out=out)
        text = re.sub(r"\x1b\[[0-9;]*m", "", out.getvalue())
        self.assertIn("b  DEDICATED", text)                                             # the worker's name and a DEDICATED chip on ONE line (no separate line for it)
        self.assertNotIn("only this worker", text)
        self.assertIn("DEDICATED  NOT OFFERED", text)                                   # a dedicated class its worker does not offer is flagged
        self.assertNotIn("every folder", text)
        self.assertIn("c could not be read (timed out)", text)
        self.assertIn("ct-run classes --worker", text)


class ClassesCarryTheirOwnInstallDate(unittest.TestCase):
    def render(self, info):
        import re
        from ct.client import view
        out = io.StringIO()
        view.render_classes(info, out=out, product="w1")
        return re.sub(r"\x1b\[[0-9;]*m", "", out.getvalue())

    def test_each_class_has_an_installed_row_and_the_worker_facts_come_first(self):
        info = {"updated": "2026-11-19T04:39:32Z", "mem_total_mb": 1935, "capacity": 4,
                "classes": {"a": {"cost": "light", "tier": "auto", "kind": "command", "run": ["x"], "installed": "2026-11-18T10:00:00Z"}, "b": {"cost": "heavy", "tier": "approve", "kind": "profile"}}}
        text = self.render(info)
        self.assertNotIn("Installed as of", text)                                       # one date for several classes said nothing about which
        self.assertIn("INSTALLED  2026-11-18 10:00 UTC", text)
        self.assertIn("not recorded (this worker runs an older release)", text)         # a worker that has not recorded it says so
        self.assertLess(text.index("MEMORY"), text.index("CLASS "))
        self.assertIn("REGISTRY   last changed 2026-11-19 04:39 UTC", text)

    def test_where_puts_a_warning_on_a_line_of_its_own(self):
        from ct import sched
        from ct.client import place
        rows = place.rows(sched.Choice("a", [("a", "CHOSEN", "4 of 4 free, WARNING: sends over github, but the worker listens on direct")]))
        self.assertEqual([r[1] if not hasattr(r[1], "plain") else r[1].plain() for r in rows], ["a", "4 of 4 free", "WARNING: sends over github, but the worker listens on direct"])


class ClassesShowTheirToolFolders(unittest.TestCase):
    def test_a_class_with_tool_folders_lists_them_one_per_line(self):
        import re
        from ct.client import view
        text = re.sub(r"\x1b\[[0-9;]*m", "", "\n".join(f"{k}  {v}" for k, v in view.class_rows("scan", {"cost": "light", "tier": "approve", "kind": "command", "run": ["nmap-wrapper"], "tools": ["/opt/tools/nmap", "/opt/tools/bin"]})))
        self.assertRegex(text, r"TOOLS  /opt/tools/nmap\n  /opt/tools/bin")


if __name__ == "__main__":
    unittest.main()
