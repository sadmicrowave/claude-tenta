import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401
from ct import updatecheck  # noqa: E402

T = 1_800_000_000          # a realistic "now": the rules are about hours and days, so tiny fake clocks would hide mistakes
LS = ("aaa111\trefs/tags/release-0.1.0\nbbb222\trefs/tags/release-0.2.0\nccc333\trefs/tags/release-0.2.0^{}\n"
      "ddd444\trefs/tags/release-0.10.0\nfff666\trefs/tags/not-a-release\neee555\trefs/tags/release-1.0.0-rc1\n")


class Tags(unittest.TestCase):
    def test_only_release_tags_count_and_an_annotated_tag_gives_its_commit(self):
        tags = updatecheck.parse_tags(LS)
        self.assertEqual(sorted(tags), ["release-0.1.0", "release-0.10.0", "release-0.2.0"])
        self.assertEqual(tags["release-0.2.0"], "ccc333")                       # the commit behind the annotated tag, not the tag object

    def test_the_latest_is_the_highest_version_not_the_alphabetical_last(self):
        self.assertEqual(updatecheck.latest(updatecheck.parse_tags(LS)), ("release-0.10.0", "ddd444"))
        self.assertIsNone(updatecheck.latest({}))

    def test_versions_compare_as_numbers(self):
        self.assertGreater(updatecheck.version_tuple("release-0.10.0"), updatecheck.version_tuple("0.9.9"))
        self.assertEqual(updatecheck.version_tuple("v1.2.3"), (1, 2, 3))
        self.assertIsNone(updatecheck.version_tuple("main"))


class Pending(unittest.TestCase):
    def test_a_newer_tag_than_this_computer_is_pending(self):
        self.assertEqual(updatecheck.pending({"tag": "release-0.2.0", "commit": "ccc333"}, "0.1.0"), {"tag": "release-0.2.0", "this_computer": True, "worker": False})
        self.assertIsNone(updatecheck.pending({"tag": "release-0.1.0", "commit": "aaa111"}, "0.1.0"))
        self.assertIsNone(updatecheck.pending({}, "0.1.0"))

    def test_a_worker_BEHIND_the_release_is_pending_even_when_this_computer_is_current(self):
        info = updatecheck.pending({"tag": "release-0.1.0", "commit": "aaa1111222"}, "0.1.0", worker_release="zzz9999", behind=lambda worker, tag: True)
        self.assertEqual((info["this_computer"], info["worker"]), (False, True))

    def test_a_worker_AHEAD_of_the_newest_tag_is_not_called_out_of_date(self):
        """Seen on a real fleet: workers running main, newer than the old release-0.1.0 tag, were told 'a newer release is available: release-0.1.0'."""
        self.assertIsNone(updatecheck.pending({"tag": "release-0.1.0", "commit": "aaa1111222"}, "0.1.0", worker_release="zzz9999", behind=lambda worker, tag: False))

    def test_when_it_cannot_tell_it_says_nothing(self):
        self.assertIsNone(updatecheck.pending({"tag": "release-0.1.0", "commit": "aaa1111222"}, "0.1.0", worker_release="zzz9999", behind=lambda worker, tag: None))
        self.assertIsNone(updatecheck.pending({"tag": "release-0.1.0", "commit": "aaa1111222"}, "0.1.0", worker_release="zzz9999"))             # no way to tell was given

    def test_behind_is_asked_of_git_and_unknown_when_git_cannot_say(self):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, {"behind": 0, "ahead": 1}.get(self.outcome, 128), "", "")
        self.outcome = "behind"
        self.assertTrue(updatecheck.is_behind("/home", "wwww", "tttt", run=run))
        self.assertIn("--is-ancestor", calls[0])
        self.outcome = "ahead"
        self.assertFalse(updatecheck.is_behind("/home", "wwww", "tttt", run=run))
        self.outcome = "broken"
        self.assertIsNone(updatecheck.is_behind("/home", "wwww", "tttt", run=run))
        self.assertFalse(updatecheck.is_behind("/home", "wwww", "wwww", run=run))                # the same commit is not behind itself
        self.assertIsNone(updatecheck.pending({"tag": "release-0.1.0", "commit": "aaa1111222"}, "0.1.0", worker_release="aaa1111"))     # same commit, short form


class Check(unittest.TestCase):
    def fake(self, out="", rc=0, err=""):
        return lambda argv, **kw: subprocess.CompletedProcess(argv, rc, out, err)

    def test_it_asks_for_tags_only_and_reads_the_newest(self):
        seen = []
        entry = updatecheck.check_now("https://example.invalid/p.git", run=lambda argv, **kw: seen.append(argv) or subprocess.CompletedProcess(argv, 0, LS, ""), now=lambda: 5)
        self.assertEqual(seen[0][:4], ["git", "ls-remote", "--tags", "https://example.invalid/p.git"])
        self.assertEqual(entry, {"checked": 5, "tag": "release-0.10.0", "commit": "ddd444"})

    def test_an_unreachable_source_is_an_error_entry_not_an_exception(self):
        self.assertIn("error", updatecheck.check_now("u", run=self.fake(rc=128, err="could not resolve host"), now=lambda: 5))
        def boom(argv, **kw):
            raise OSError("git missing")
        self.assertIn("error", updatecheck.check_now("u", run=boom, now=lambda: 5))

    def test_the_cache_is_due_after_a_day(self):
        self.assertTrue(updatecheck.due({}, now=lambda: T))
        self.assertFalse(updatecheck.due({"checked": T - 3600}, now=lambda: T))
        self.assertTrue(updatecheck.due({"checked": T - 90000}, now=lambda: T))


class Tty(io.StringIO):
    def isatty(self):
        return True


class Settings:
    check, source = True, "https://example.invalid/p.git"


class Banner(unittest.TestCase):
    def setUp(self):
        updatecheck._SHOWN.clear()                                 # the once-per-run guard is per process: each test is a new run
        self.home = Path(tempfile.mkdtemp())
        updatecheck.save(self.home, {"checked": T, "tag": "release-0.2.0", "commit": "ccc3334"})
        self.refreshed = []
        os.environ.pop("CT_NO_UPDATE_CHECK", None)

    def test_it_is_shown_once_per_run_even_when_two_workers_each_have_their_own_cache(self):
        """Seen on a real `ct-admin apply`: the same banner printed twice, once from each worker's client context."""
        self.assertTrue(self.run_it()[0])
        import tempfile
        other_home = Path(tempfile.mkdtemp())
        shown = updatecheck.after_command(Settings, other_home, "0.1.0", stream=Tty(), now=lambda: T, refresh=lambda h, u: None)
        self.assertFalse(shown)

    def run_it(self, stream=None, cfg=Settings, **kw):
        stream = stream if stream is not None else Tty()
        shown = updatecheck.after_command(cfg, self.home, "0.1.0", stream=stream, now=kw.pop("now", lambda: T), refresh=lambda h, u: self.refreshed.append(u), **kw)
        return shown, stream.getvalue()

    def test_it_is_one_quiet_line_that_says_what_to_run(self):
        shown, text = self.run_it()
        self.assertTrue(shown)
        self.assertEqual(len([l for l in text.splitlines() if l]), 1)
        self.assertIn("release-0.2.0", text)
        self.assertIn("ct-admin update", text)

    def test_it_is_shown_at_most_twice_a_day_not_on_every_command(self):
        self.assertTrue(self.run_it(now=lambda: T)[0])
        self.assertFalse(self.run_it(now=lambda: T + 3600)[0])
        updatecheck._SHOWN.clear()                                            # a later command is a new run
        self.assertTrue(self.run_it(now=lambda: T + 50000)[0])

    def test_nothing_is_shown_when_piped_or_switched_off(self):
        self.assertFalse(self.run_it(stream=io.StringIO())[0])                       # not a terminal: a script's output stays clean
        os.environ["CT_NO_UPDATE_CHECK"] = "1"
        self.assertFalse(self.run_it()[0])
        os.environ.pop("CT_NO_UPDATE_CHECK")
        off = type("S", (), {"check": False, "source": "u"})
        self.assertFalse(self.run_it(cfg=off)[0])

    def test_an_old_cache_starts_a_background_refresh_and_never_waits_for_it(self):
        updatecheck.save(self.home, {"checked": T - 200000, "tag": "release-0.2.0", "commit": "ccc3334"})
        shown, _ = self.run_it(now=lambda: T)
        self.assertEqual(self.refreshed, [Settings.source])
        self.assertTrue(shown)                                                       # it still tells what it already knows

    def test_up_to_date_says_nothing(self):
        updatecheck.save(self.home, {"checked": T, "tag": "release-0.1.0", "commit": "aaa1112"})
        self.assertFalse(self.run_it()[0])

    def test_a_failure_inside_is_swallowed(self):
        class Bad:
            @property
            def check(self):
                raise RuntimeError("boom")
        self.assertFalse(self.run_it(cfg=Bad())[0])


class Refresh(unittest.TestCase):
    def test_an_offline_refresh_keeps_the_last_good_answer_and_only_moves_the_time(self):
        home = Path(tempfile.mkdtemp())
        updatecheck.save(home, {"checked": 1, "tag": "release-0.2.0", "commit": "c", "banner": 7})
        orig = updatecheck.check_now
        updatecheck.check_now = lambda url, **kw: {"checked": 99, "error": "offline"}
        try:
            updatecheck.main([str(home), "u"])
        finally:
            updatecheck.check_now = orig
        got = updatecheck.load(home)
        self.assertEqual((got["tag"], got["checked"], got["banner"], got["error"]), ("release-0.2.0", 99, 7, "offline"))


if __name__ == "__main__":
    unittest.main()
