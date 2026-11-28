"""Worker aliases: names for the arm- commands, kept on this computer only (armada.alias, arm-admin workers rename)."""
import io
import re
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import support  # noqa: F401
from armada import alias, sched
from armada.client import cli, here, place
from armada.client.repo import ClientError
import test_here
from test_here import in_dir, run
from test_place import Setup

ANSI = re.compile(r"\x1b\[[0-9;]*m")


class Store(unittest.TestCase):
    def tearDown(self):
        alias.path().unlink(missing_ok=True)

    def test_no_file_means_real_names_everywhere(self):
        self.assertEqual(alias.show("arm3"), "arm3")
        self.assertEqual(alias.resolve("arm3", ["arm3"]), "arm3")

    def test_an_alias_is_shown_with_the_real_name_beside_it_and_masked_on_request(self):
        alias.save({"arm3": "lab-vm"})
        self.assertEqual(alias.show("arm3"), "lab-vm  (arm3)")
        alias.save({"arm3": "lab-vm"}, show_real=False)
        self.assertEqual(alias.show("arm3"), "lab-vm")
        self.assertEqual(alias.show("other"), "other")

    def test_the_alias_or_the_real_name_means_the_same_worker(self):
        alias.save({"arm3": "lab-vm"})
        self.assertEqual(alias.resolve("lab-vm", ["arm3", "arm2"]), "arm3")
        self.assertEqual(alias.resolve("arm3", ["arm3", "arm2"]), "arm3")
        self.assertEqual(alias.resolve("nobody", ["arm3"]), "nobody")

    def test_a_name_cannot_be_another_workers_real_name_or_alias_or_badly_formed(self):
        alias.save({"arm3": "lab-vm"})
        names = ["arm2", "arm3"]
        self.assertIsNone(alias.check("fresh", "arm2", names))
        self.assertIn("real name", alias.check("arm3", "arm2", names))
        self.assertIn("already", alias.check("lab-vm", "arm2", names))
        self.assertIsNone(alias.check("lab-vm", "arm3", names))                       # its own alias again is fine
        self.assertIn("own name", alias.check("arm3", "arm3", names))
        self.assertIn("letters", alias.check("not ok!", "arm2", names))

    def test_a_broken_file_is_ignored_not_fatal(self):
        alias.path().parent.mkdir(parents=True, exist_ok=True)
        alias.path().write_text("[aliases\n")
        self.assertEqual(alias.load(), ({}, True))


class RenameCommand(unittest.TestCase):
    def tearDown(self):
        alias.path().unlink(missing_ok=True)

    def admin(self, argv, answers=None, tty=False):
        from armada.admin import cli as admin_cli
        a = admin_cli.build_parser()[0].parse_args(["workers", "rename", *argv])
        out = io.StringIO()
        it = iter(answers or [])
        ctx = SimpleNamespace(fleet=lambda: [("main", None), ("pentest", None)], is_tty=tty, ask=lambda p: next(it))
        with redirect_stdout(out), redirect_stderr(out):
            try:
                rc = admin_cli.workers_rename(ctx, a)
            except ClientError as e:
                rc = cli.fail_from(e)
        return rc, ANSI.sub("", out.getvalue())

    def test_one_worker_is_named_cleared_and_collisions_are_refused(self):
        rc, out = self.admin(["pentest", "lab-vm"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(alias.load()[0], {"pentest": "lab-vm"})
        self.assertIn("lab-vm  (pentest)", out)
        rc, out = self.admin(["main", "lab-vm"])
        self.assertEqual(rc, 2)
        self.assertIn("already", out)
        self.assertEqual(self.admin(["lab-vm", "--clear"])[0], 0)                        # by its alias
        self.assertEqual(alias.load()[0], {})
        self.assertFalse(alias.path().exists())

    def test_the_wizard_asks_each_worker_enter_keeps_dash_clears_and_a_bad_name_asks_again(self):
        alias.save({"main": "old"})
        rc, out = self.admin([], answers=["-", "main", "arm2"], tty=True)       # main: cleared; pentest: refused (real name of another), then arm2
        self.assertEqual(rc, 0, out)
        self.assertEqual(alias.load()[0], {"pentest": "arm2"})
        self.assertIn("real name of another worker", out)
        self.assertIn("changed", out)

    def test_the_wizard_needs_a_terminal_and_masking_is_a_setting(self):
        self.assertEqual(self.admin([])[0], 2)
        self.admin(["pentest", "x1"])
        self.admin(["--mask"])
        self.assertEqual(alias.show("pentest"), "x1")
        self.admin(["--show-real"])
        self.assertEqual(alias.show("pentest"), "x1  (pentest)")


class UseEverywhere(Setup):
    def tearDown(self):
        alias.path().unlink(missing_ok=True)

    def test_a_forced_alias_goes_to_its_worker_and_the_where_card_shows_the_alias(self):
        self.add("mini")
        self.add("vmx")
        self.add("ghx")
        alias.save({"vmx": "lab-vm"})
        (ctx, ch), _, _ = self.route(forced="lab-vm")
        self.assertEqual(ctx.product, "vmx")
        rows = place.rows(sched.Choice("vmx", [("vmx", "CHOSEN", "free"), ("mini", "not chosen", "vmx is freer")]))
        self.assertEqual(rows[0][1], "lab-vm  (vmx)")                                     # every card shows a worker through alias.show

    def test_the_folder_file_keeps_the_real_name_when_set_by_alias(self):
        t = test_here.FolderFile("test_use_writes_an_editable_file_that_is_read_back")
        t.setUp()
        try:
            alias.save({"pentest": "lab-vm"})
            with in_dir(t.d):
                self.assertEqual(run(["lab-vm"], t.cfg)[0], 0)
                self.assertIn('worker = "pentest"', (t.d / here.FILE).read_text())
        finally:
            t.t.cleanup()


class NewWorkerNames(unittest.TestCase):
    def tearDown(self):
        alias.path().unlink(missing_ok=True)

    def test_the_next_default_is_the_first_unused_arm_number(self):
        self.assertEqual(alias.next_default(["arm2"]), "arm1")
        alias.save({"arm2": "arm1", "arm3": "arm2"})
        self.assertEqual(alias.next_default(["arm4"]), "arm3")
        self.assertEqual(alias.next_default(["arm4", "arm3"]), "arm5")                 # a real name that looks like one is taken too

    def offer(self, answer, names=("arm4", "arm2")):
        from armada.setup import addworker
        said, asked = [], []
        addworker.offer_alias("arm4", lambda p: asked.append(p) or answer, lambda *a, **k: said.append((a, k)), list(names))
        return said, asked

    def test_enter_accepts_the_default_a_name_replaces_it_and_dash_skips(self):
        said, asked = self.offer("")
        self.assertEqual(alias.load()[0], {"arm4": "arm1"})
        self.assertIn("[arm1;", asked[0])
        alias.path().unlink()
        self.offer("lab-vm")
        self.assertEqual(alias.load()[0], {"arm4": "lab-vm"})
        alias.path().unlink()
        said, _ = self.offer("-")
        self.assertEqual(alias.load()[0], {})
        self.assertEqual(said, [])

    def test_a_name_that_cannot_be_used_names_nothing_and_says_how_to_do_it_later(self):
        said, _ = self.offer("arm2")                                                     # the real name of another worker
        self.assertEqual(alias.load()[0], {})
        self.assertIn("arm-admin workers rename arm4", str(said))


if __name__ == "__main__":
    unittest.main()
