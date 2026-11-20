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


if __name__ == "__main__":
    unittest.main()
