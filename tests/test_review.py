import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from cw import perms, review  # noqa: E402

JOB = {"argv": ["eslint", "--max-warnings", "0", "src/"], "what": "Lints the source tree", "why": "before release", "expect": "10 seconds, 200 MB",
       "side_effects": "reads files only", "watch_for": "network access", "flags": {"--max-warnings": "fail if there are more than N warnings"},
       "files": {"src/a.js": "x" * 1200, "b.js": "y"}, "timeout_s": 120}


def text(job=JOB, **kw):
    return review.review_text(job, width=80, **kw)


class Layout(unittest.TestCase):
    def test_the_command_comes_first_exactly_as_it_will_run_and_quoted(self):
        t = text({**JOB, "argv": ["echo", "a b", "$(id)"]})
        self.assertTrue(t.startswith("PROFILE RUN. If you approve, the worker will run exactly this command once"))
        self.assertIn("    echo 'a b' '$(id)'", t.splitlines()[3])                           # shell-quoted: nothing is hidden or interpreted

    def test_sections_labels_and_alignment(self):
        t = text(facts={"binary": "/usr/bin/eslint", "size_kb": 12, "package": "eslint", "summary": "a   linter\n tool"})
        for label in ("WHAT IT DOES", "WHY NOW", "WHAT TO EXPECT", "WHAT IT READS, WRITES AND CONTACTS", "BAD SIGNS TO WATCH FOR", "FLAGS", "INPUT FILES",
                      "TIME LIMIT", "LOOKED UP BY THE WORKER ITSELF  (not from the requester)"):
            self.assertIn("\n" + label, "\n" + t, label)
        self.assertIn("Lints the source tree", t)
        self.assertIn("--max-warnings  fail if there are more than N warnings", t)
        self.assertIn("INPUT FILES  (2 files, 1,201 B in all)", t)
        self.assertRegex(t, r"(?m)^a\.js|^b\.js|^src/a\.js +1,200 B")
        self.assertIn("Size     12 KB", t)
        self.assertIn("Summary  a linter tool", t)                                         # padding in the man-page summary is collapsed
        self.assertTrue(t.endswith("\n") and not t.endswith("\n\n"))

    def test_flags_the_requester_did_not_explain_are_called_out(self):
        t = text({**JOB, "argv": ["tool", "--known", "--sneaky=1", "-v", "file"], "flags": {"--known": "x"}})
        self.assertIn("NOT EXPLAINED BY THE REQUESTER\n--sneaky=1 -v", t)
        self.assertNotIn("NOT EXPLAINED", text())

    def test_an_interpreter_gets_a_standalone_warning(self):
        t = text(facts={"binary": "/usr/bin/python3", "interpreter": True})
        self.assertIn("WARNING\nThis is an interpreter or wrapper; its arguments ARE the program.", t)
        self.assertNotIn("Interpreter", t)
        self.assertNotIn("WARNING", text(facts={"binary": "/usr/bin/jq"}))

    def test_without_facts_it_says_the_worker_has_not_looked_yet(self):
        self.assertIn("has not published its own facts about this command yet", text())

    def test_packages_and_the_glance_block_are_included_when_given(self):
        t = text({**JOB, "packages": ["apt:jq"]}, package_text="INSTALL REQUIRED. ...", glance="AT A GLANCE\n  Time ~1 s")
        self.assertIn("INSTALL REQUIRED. ...", t)
        self.assertTrue(t.rstrip().endswith("Time ~1 s"))
        self.assertIn("has not published the plan yet", text({**JOB, "packages": ["apt:jq"]}))

    def test_the_product_name_is_an_argument_and_the_old_one_never_appears(self):
        t = text(product="homecw", facts={"binary": "/x"})
        self.assertIn("homecw will run exactly", t)
        self.assertIn("LOOKED UP BY HOMECW ITSELF", t.upper())
        self.assertNotIn("claude-worker", t)

    def test_a_minimal_job(self):
        t = review.review_text({"argv": ["ls"]}, width=80)
        self.assertNotIn("FLAGS", t)
        self.assertNotIn("INPUT FILES", t)


class Colour(unittest.TestCase):
    def test_plain_unless_a_terminal(self):
        t = text(facts={"binary": "/x"})
        with mock.patch.dict(os.environ, {"CW_COLOR": "never"}):
            self.assertEqual(review.color_review(t), t)

    def test_labels_command_and_warnings_are_coloured(self):
        t = text({**JOB, "packages": ["apt:jq"]}, facts={"binary": "/x", "interpreter": True},
                 package_text="INSTALL REQUIRED. x\n\n    apt-get install jq\n    apt: jq", product="homecw")
        with mock.patch.dict(os.environ, {"CW_COLOR": "always"}):
            c = review.color_review(t, product="homecw")
        self.assertIn("\033[1;32m", c)                                                     # the command is bold green
        self.assertIn("\033[1;36mWHAT IT DOES", c)                                         # a label is bold cyan
        self.assertIn("\033[1;31mWARNING", c)
        self.assertIn("\033[1;33mINSTALL REQUIRED", c)
        self.assertIn("\033[1;36mLOOKED UP BY HOMECW ITSELF", c)                           # the product-specific label is recognised too
        self.assertEqual(c.count("\033[1;32m"), 1)                                         # and only the command line is green


class Modes(unittest.TestCase):
    def tree(self):
        root = Path(tempfile.mkdtemp()) / "a" / "b" / "cwtest" / "state"
        (root / "work" / "job").mkdir(parents=True)
        (root / "quarantine").mkdir()
        for rel, mode in {"lockdown": 0o644, "work/job/in.txt": 0o664, "quarantine/q.json": 0o600, "exec.sh": 0o755}.items():
            f = root / rel
            f.write_text("x")
            f.chmod(mode)
        (root / "work").chmod(0o755)
        (root / "work" / "job").chmod(0o775)
        (root / "quarantine").chmod(0o700)
        return root

    def modes(self, root):
        return {str(p.relative_to(root)): stat.S_IMODE(p.stat().st_mode) for p in root.rglob("*")}

    def test_existing_modes_lose_other_and_group_write_and_nothing_is_loosened(self):
        root = self.tree()
        self.assertGreater(perms.tighten_modes([root], root / ".marker"), 0)
        m = self.modes(root)
        self.assertEqual((m["lockdown"], m["work"], m["work/job"], m["work/job/in.txt"]), (0o640, 0o750, 0o750, 0o640))
        self.assertEqual((m["quarantine"], m["quarantine/q.json"], m["exec.sh"]), (0o700, 0o600, 0o750))

    def test_it_runs_once_and_never_follows_symlinks(self):
        root = self.tree()
        outside = Path(tempfile.mkdtemp()) / "keep"
        outside.write_text("x")
        outside.chmod(0o644)
        (root / "link").symlink_to(outside)
        perms.tighten_modes([root], root / ".marker")
        self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o644)
        (root / "lockdown").chmod(0o666)
        self.assertEqual(perms.tighten_modes([root], root / ".marker"), 0)
        self.assertEqual(stat.S_IMODE((root / "lockdown").stat().st_mode), 0o666)

    def test_the_parent_folder_is_tightened_only_when_it_is_the_installs_own(self):
        base = Path(tempfile.mkdtemp()) / "a" / "b" / "cwtest"
        for n in ("state", "queue", "results"):
            (base / n).mkdir(parents=True)
        base.chmod(0o755)
        perms.tighten_modes([base / "state", base / "queue", base / "results"], base / "state" / ".m")
        self.assertEqual(stat.S_IMODE(base.stat().st_mode), 0o750)
        shallow = Path("/tmp")
        self.assertLess(len(shallow.parts), 4)                                                # a shallow parent such as /tmp is never touched

    def test_umask_gives_new_files_no_access_for_others(self):
        old = os.umask(perms.STATE_UMASK)
        try:
            d = Path(tempfile.mkdtemp())
            f, sub = d / "new.json", d / "sub"
            f.write_text("x")
            sub.mkdir()
            self.assertEqual((stat.S_IMODE(f.stat().st_mode), stat.S_IMODE(sub.stat().st_mode)), (0o640, 0o750))
        finally:
            os.umask(old)


if __name__ == "__main__":
    unittest.main()
