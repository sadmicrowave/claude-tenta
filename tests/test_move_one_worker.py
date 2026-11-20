"""Moving ONE registered worker from GitHub to Direct while others exist: the required journey (docs/components/testing.md). The server half is covered by test_migrate; this is the part on YOUR computer, with real
files and real git clones: only the named worker's configuration and its two clones change, the others (and the main install) are byte for byte what they were, and the scheduler's view of each worker follows."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401
from ct import config, transport
from ct.client import place
from ct.setup import migrate
from ct.setup.host import Host


def conf(name, host, home, kind="github"):
    return (f'[identity]\nname = "{name}"\n[server]\nhost = "{host}"\nadmin_user = "agent"\n[transport]\nkind = "{kind}"\n'
            f'[github]\nowner = "someone"\nqueue_repo = "{name}-queue"\nresults_repo = "{name}-results"\n[paths]\nclient_home = "{home}"\n')


class MoveOne(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.addCleanup(self.t.cleanup)
        self.d = Path(self.t.name)
        self.names = ("main", "tenta2", "tenta3")
        self.files, self.clones = {}, {}
        for n in self.names:
            home = self.d / "ct" / n
            self.files[n] = self.d / f"{n}.toml"
            self.files[n].write_text(conf(n, f"{n}.example", home))
            for which in ("queue", "results"):
                clone = home / which
                clone.mkdir(parents=True)
                subprocess.run(["git", "-C", str(clone), "init", "-q"], check=True)
                subprocess.run(["git", "-C", str(clone), "remote", "add", "origin", f"https://github.com/someone/{n}-{which}.git"], check=True)
                self.clones[n, which] = clone
        (self.d / "workers.toml").write_text(f'[workers.tenta2]\nconfig = "{self.files["tenta2"]}"\n[workers.tenta3]\nconfig = "{self.files["tenta3"]}"\n')
        self.before = {p.name: p.read_bytes() for p in self.d.glob("*.toml")}

    def load(self, n):
        return config.load(str(self.files[n]), env={"CT_CONFIG": ""})

    def origin(self, n, which):
        return subprocess.run(["git", "-C", str(self.clones[n, which]), "remote", "get-url", "origin"], capture_output=True, text=True).stdout.strip()

    def move(self, n):
        cfg = self.load(n)
        cfg2 = migrate.load_new_config(self.files[n].read_text(), "direct")
        notes, _ = migrate.rewrite_local(Host(), cfg, cfg2, "direct")
        return notes

    def test_only_the_named_worker_is_changed_and_every_other_file_is_byte_for_byte_the_same(self):
        notes = self.move("tenta2")
        self.assertEqual([s for s, status, _ in notes if status in ("pointed", "written")], ["configuration", "queue clone", "results clone"])
        self.assertEqual(transport.for_config(self.load("tenta2")).kind, "direct")
        after = {p.name: p.read_bytes() for p in self.d.glob("*.toml")}
        for name in ("main.toml", "tenta3.toml", "workers.toml"):
            self.assertEqual(after[name], self.before[name], name)
        self.assertNotEqual(after["tenta2.toml"], self.before["tenta2.toml"])
        for which in ("queue", "results"):
            self.assertNotIn("github.com", self.origin("tenta2", which))                       # its clones fetch from the mailbox now
            for other in ("main", "tenta3"):
                self.assertEqual(self.origin(other, which), f"https://github.com/someone/{other}-{which}.git")

    def test_the_schedulers_view_of_each_worker_follows_the_move(self):
        self.move("tenta2")
        main = self.load("main")
        main_source = main.source
        self.assertTrue(main_source)
        ents = {name: transport.for_config(c).kind for name, c, _ in place.entries(main)}
        self.assertEqual(ents, {"main": "github", "tenta2": "direct", "tenta3": "github"})
        # and what each pick-up is assumed to take follows the connection: a Direct worker is read in seconds, a GitHub one at its idle pace
        pick = {name: place.pickup_s(c) for name, c, _ in place.entries(main)}
        self.assertEqual(pick["tenta2"], 5)
        self.assertGreaterEqual(pick["tenta3"], 5)

    def test_moving_a_second_worker_after_the_first_changes_only_that_one_too(self):
        self.move("tenta2")
        self.move("tenta3")
        self.assertEqual({n: transport.for_config(self.load(n)).kind for n in self.names}, {"main": "github", "tenta2": "direct", "tenta3": "direct"})
        self.assertEqual((self.d / "main.toml").read_bytes(), self.before["main.toml"])


class BackToGitHub(unittest.TestCase):
    """ct-setup transport github: the way back. Local bare repositories stand in for GitHub; the installer is a recorder."""

    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.addCleanup(self.t.cleanup)
        self.d = Path(self.t.name)
        self.cfg_file = self.d / "tenta2.toml"
        home = self.d / "ct" / "tenta2"
        self.cfg_file.write_text(conf("tenta2", "tenta2.example", home, kind="direct"))
        self.gh, self.clones = {}, {}
        for which in ("queue", "results"):
            bare = self.d / "gh" / f"tenta2-{which}.git"
            subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
            clone = home / which
            clone.mkdir(parents=True)
            git = ["git", "-C", str(clone)]
            subprocess.run(git + ["init", "-q", "-b", "main"], check=True)
            subprocess.run(git + ["remote", "add", "origin", str(self.d / "mailbox" / f"{which}.git")], check=True)
            (clone / "f.txt").write_text(f"{which} history\n")
            subprocess.run(git + ["add", "-A"], check=True)
            subprocess.run(git + ["-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "work done on direct"], check=True)
            self.gh[which], self.clones[which] = bare, clone
        self.reinstalls = []

    def run_move(self, apply=True, yes=True, reinstall_rc=0, worker="tenta2"):
        from types import SimpleNamespace
        a = SimpleNamespace(name="github", apply=apply, yes=yes, worker=worker, here=False)
        cfg = config.load(str(self.cfg_file), env={"CT_CONFIG": ""})
        out = []
        rc = migrate.back_to_github(a, Host(), lambda p: "y", out.append, cfg, reinstall=lambda av: self.reinstalls.append(av) or reinstall_rc, url_of=lambda c, which: str(self.gh[which]))
        return rc, out

    def head(self, which):
        return subprocess.run(["git", "-C", str(self.gh[which]), "log", "--format=%s", "main"], capture_output=True, text=True).stdout.strip()

    def test_a_preview_changes_nothing(self):
        rc, _ = self.run_move(apply=False)
        self.assertEqual(rc, 0)
        self.assertEqual(self.head("queue"), "")                                              # nothing was pushed
        self.assertEqual(self.reinstalls, [])
        self.assertIn('kind = "direct"', self.cfg_file.read_text())

    def test_the_history_is_pushed_the_worker_is_set_up_again_for_github_and_this_computer_points_back(self):
        rc, _ = self.run_move()
        self.assertEqual(rc, 0)
        for which in ("queue", "results"):
            self.assertEqual(self.head(which), "work done on direct")
        self.assertEqual(self.reinstalls, [["install", "--worker", "tenta2", "--set", "transport.kind=github", "--yes"]])
        self.assertEqual(transport.for_config(config.load(str(self.cfg_file), env={"CT_CONFIG": ""})).kind, "github")
        for which in ("queue", "results"):
            origin = subprocess.run(["git", "-C", str(self.clones[which]), "remote", "get-url", "origin"], capture_output=True, text=True).stdout.strip()
            self.assertNotIn("mailbox", origin)

    def test_a_github_that_has_commits_the_clones_lack_stops_everything_with_nothing_changed(self):
        other = self.d / "other"
        subprocess.run(["git", "clone", "-q", str(self.gh["queue"]), str(other)], check=True)
        (other / "x.txt").write_text("someone else\n")
        subprocess.run(["git", "-C", str(other), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(other), "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "elsewhere"], check=True)
        subprocess.run(["git", "-C", str(other), "push", "-q", "origin", "HEAD:main"], check=True)
        rc, _ = self.run_move()
        self.assertEqual(rc, 1)
        self.assertEqual(self.reinstalls, [])
        self.assertIn('kind = "direct"', self.cfg_file.read_text())

    def test_a_failed_reinstall_leaves_this_computer_as_it_was(self):
        rc, _ = self.run_move(reinstall_rc=1)
        self.assertEqual(rc, 1)
        self.assertIn('kind = "direct"', self.cfg_file.read_text())

    def test_the_main_install_is_set_up_again_by_its_address(self):
        rc, _ = self.run_move(worker=None)
        self.assertEqual(rc, 0)
        self.assertEqual(self.reinstalls[0][:2], ["install", "tenta2.example"])


class BackToGitHubWithoutAnAccount(unittest.TestCase):
    """Found by a real run (scripts/real-github-check): a Direct install never records a GitHub account, so the way back refused with 'not named in the configuration' and advice that did not help."""

    head = BackToGitHub.head

    def setUp(self):
        BackToGitHub.setUp(self)
        self.addCleanup(lambda: None)
        self.cfg_file.write_text(self.cfg_file.read_text().replace('owner = "someone"\n', ""))        # what a Direct install leaves: no account

    def run_move(self, apply=True, yes=True, reinstall_rc=0, worker="tenta2", sets=(), login=None, interactive_answer=None):
        from types import SimpleNamespace
        a = SimpleNamespace(name="github", apply=apply, yes=yes, worker=worker, here=False, set=list(sets))
        cfg = config.load(str(self.cfg_file), env={"CT_CONFIG": ""})
        out, asked = [], []

        def ask(prompt):
            asked.append(prompt)
            return interactive_answer if interactive_answer is not None else "y"
        host = Host()
        real_run = host.run
        host.run = lambda argv, **kw: (__import__("ct.setup.host", fromlist=["Result"]).Result(0, login + "\n") if (login and argv[:3] == ["gh", "api", "user"]) else
                                       __import__("ct.setup.host", fromlist=["Result"]).Result(1, "not signed in") if argv[:3] == ["gh", "api", "user"] else real_run(argv, **kw))
        rc = migrate.back_to_github(a, host, ask, out.append, cfg, reinstall=lambda av: self.reinstalls.append(av) or reinstall_rc, url_of=lambda c, which: str(self.gh[which]))
        self.asked = asked
        return rc, out

    def test_the_account_can_be_given_on_the_command_line_and_is_kept(self):
        rc, _ = self.run_move(sets=["github.owner=someone"])
        self.assertEqual(rc, 0)
        self.assertIn("github.owner=someone", self.reinstalls[0])                                  # the installer is told the account too
        text = self.cfg_file.read_text()
        self.assertEqual(transport.for_config(config.load(str(self.cfg_file), env={"CT_CONFIG": ""})).kind, "github")        # GitHub is the default, so the file simply no longer says direct
        self.assertNotIn('kind = "direct"', text)
        self.assertIn('owner = "someone"', text)                                                   # and the configuration remembers it

    def test_the_signed_in_github_account_is_used_when_none_is_given(self):
        rc, _ = self.run_move(login="signed-in-user")
        self.assertEqual(rc, 0)
        self.assertIn("github.owner=signed-in-user", self.reinstalls[0])
        self.assertIn('owner = "signed-in-user"', self.cfg_file.read_text())

    def test_with_no_account_anywhere_it_says_the_one_command_that_fixes_it_and_changes_nothing(self):
        rc, out = self.run_move(yes=True)
        self.assertEqual(rc, 1)
        self.assertEqual(self.reinstalls, [])
        self.assertIn('kind = "direct"', self.cfg_file.read_text())
        self.assertEqual(self.head("queue"), "")
        import io
        from contextlib import redirect_stderr
        err = io.StringIO()
        with redirect_stderr(err):
            self.run_move(yes=True)
        self.assertIn("--set github.owner=", err.getvalue())                                       # the exact command, not "run ct-setup install"
        self.assertNotIn("Run this\n\nct-setup install", err.getvalue())

    def test_a_person_at_a_terminal_is_asked_for_the_account_and_enter_takes_the_signed_in_one(self):
        from types import SimpleNamespace
        cfg = config.load(str(self.cfg_file), env={"CT_CONFIG": ""})
        host = Host()
        host.run = lambda argv, **kw: __import__("ct.setup.host", fromlist=["Result"]).Result(0, "signed-in-user\n")
        a = SimpleNamespace(set=[])
        self.assertEqual(migrate.github_owner_for(a, host, cfg, lambda p: "typed-name", True), "typed-name")
        self.assertEqual(migrate.github_owner_for(a, host, cfg, lambda p: "", True), "signed-in-user")
        self.assertEqual(migrate.github_owner_for(a, host, cfg, None, False), "signed-in-user")


if __name__ == "__main__":
    unittest.main()
