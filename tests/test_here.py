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


def run(argv, cfg=None):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            rc = cli.cmd_use(None, cfg, cli.build_parser()[0].parse_args(["use", *argv]))
        except ClientError as e:
            rc = cli.fail_from(e)
    return rc, out.getvalue() + err.getvalue()


class FolderFile(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.d = Path(self.t.name).resolve()
        self.addCleanup(self.t.cleanup)
        self.cfg = cfg_file(self.d, "main")
        (self.d / "workers.toml").write_text(f'[workers.pentest]\nconfig = "{cfg_file(self.d, "pentest").source}"\n')
        self.cfg = cfg_file(self.d, "main")

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
            self.assertIn("no worker called", out)
            self.assertFalse((self.d / here.FILE).exists())

    def test_a_pin_to_a_worker_that_is_gone_names_the_file_and_the_fix(self):
        (self.d / here.FILE).write_text('worker = "gone"\n')
        with in_dir(self.d), self.assertRaises(ClientError) as c:
            cli.pinned_worker(self.cfg, "pytest")
        self.assertIn(here.FILE, c.exception.title)
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


if __name__ == "__main__":
    unittest.main()
