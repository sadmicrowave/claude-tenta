import tomllib
import unittest

import support  # noqa: F401
from test_setup import FakeHost, cfg_of
from ct import config, render, transport
from ct.setup import migrate
from ct.setup.host import Result
from ct.setup.server import SetupError

PUB = "ssh-ed25519 AAAAC3Nza ct-mailbox"


class Installed(unittest.TestCase):
    """A fake worker set up for GitHub, with both clones, timers and a release to install."""

    def setUp(self):
        self.h = FakeHost()
        self.cfg = cfg_of(alerts={"kind": "none"})
        self.h.put(self.cfg.path("config_file"), 'identity = {}\n[server]\nhost = "worker.example"\nadmin_user = "me"\n[github]\nowner = "someone"\n')
        for part in render.UNITS:
            self.h.put(f"/etc/systemd/system/{self.cfg.unit(part)}", "[Unit]\n")
        for which in ("queue", "results"):
            self.h.put(self.cfg.path(f"{which}_clone") + "/.git/HEAD", "ref: refs/heads/main\n")
        self.h.put("/opt/src/src/ct/__init__.py", "")
        self.said = []

    def mg(self, to="direct", **kw):
        kw.setdefault("mailbox_pubkey", PUB)
        return migrate.Migrate(self.h, self.cfg, to, source="/opt/src", rev="a" * 40, say=self.said.append, **kw)

    def plan(self, **kw):
        m = self.mg(**kw)
        m.check()
        return m, dict(m.steps())


class Plan(Installed):
    def test_the_configuration_gets_the_new_connection_and_nothing_else_changes(self):
        m, steps = self.plan()
        (act,) = steps["Write the new connection into the configuration"]
        loaded = config.Config(config.validate(tomllib.loads(act.text), require_alert_url=False)[0])
        self.assertEqual((transport.for_config(loaded).kind, loaded.github.owner, loaded.server.admin_user), ("direct", "someone", "me"))

    def test_the_history_is_copied_with_a_mirror_push_before_anything_points_at_the_mailbox(self):
        m, steps = self.plan()
        titles = [t for t, _ in m.steps()]
        self.assertLess(titles.index("Copy the history into the mailbox"), titles.index("Point the worker's clones at the mailbox"))
        pushes = [a.argv for a in steps["Copy the history into the mailbox"] if "push" in a.argv]
        self.assertEqual(len(pushes), 4)                                         # two repositories, each: what the clone saw of its origin, then its own branches
        self.assertTrue(all("--mirror" not in p for p in pushes))                # --mirror from a clone would store the origin's branches where nothing looks for them
        self.assertTrue(any("refs/remotes/origin/*:refs/heads/*" in p for p in pushes))

    def test_the_mailbox_is_made_before_the_history_is_copied_into_it(self):
        titles = [t for t, _ in self.plan()[0].steps()]
        self.assertLess(titles.index("Make the mailbox (as a fresh install would)"), titles.index("Copy the history into the mailbox"))

    def test_the_workers_clones_point_at_the_local_mailbox_not_at_the_network(self):
        m, steps = self.plan()
        urls = [a.argv[-1] for a in steps["Point the worker's clones at the mailbox"]]
        self.assertEqual(urls, [m.cfg2.path("mailbox") + "/queue.git", m.cfg2.path("mailbox") + "/results.git"])

    def test_the_job_loop_is_paused_first_and_started_last(self):
        m, _ = self.plan()
        titles = [t for t, _ in m.steps()]
        self.assertEqual((titles[0], titles[-1]), ("Pause the job loop", "Start the job loop again"))

    def test_the_preview_never_shows_a_configuration_text(self):
        m, _ = self.plan()
        for _, lines in m.plan_lines():
            for line in lines:
                self.assertNotIn("[transport]", line)


class Refusals(Installed):
    def refused(self, m):
        with self.assertRaises(SetupError) as e:
            m.check()
        return e.exception

    def test_an_install_already_on_direct_has_nothing_to_move(self):
        self.cfg = cfg_of(alerts={"kind": "none"}, transport={"kind": "direct"})
        e = self.refused(self.mg())
        self.assertIn("already uses that method", e.title)
        self.assertIn("direct", str(e.facts))

    def test_going_back_to_github_is_refused_with_the_way_to_do_it(self):
        self.cfg = cfg_of(alerts={"kind": "none"}, transport={"kind": "direct"})
        e = self.refused(self.mg(to="github"))
        self.assertIn("ct-setup install", " ".join(e.steps))

    def test_an_unknown_method_is_refused(self):
        e = self.refused(self.mg(to="carrier-pigeon"))
        self.assertIn("not a connection method", e.title)
        self.assertIn("carrier-pigeon", str(e.facts))

    def test_a_missing_clone_stops_before_anything_is_touched(self):
        __import__("shutil").rmtree(self.h.path(self.cfg.path("queue_clone") + "/.git"))
        e = self.refused(self.mg(dry_run=False))
        self.assertIn("no clone", e.title)
        self.assertIn("queue", str(e.facts))
        self.mg(dry_run=True).check()          # a preview is run by the administrator, who cannot see those folders: it must not say they are missing

    def test_a_running_job_stops_the_move(self):
        self.h.replies["sh -c ls"] = Result(0, "job-1\n")
        self.assertIn("A job is running", self.refused(self.mg()).title)

    def test_a_missing_key_stops_a_real_run_but_not_a_preview(self):
        self.assertIn("key did not arrive", self.refused(self.mg(mailbox_pubkey=None, dry_run=False)).title)
        self.mg(mailbox_pubkey=None, dry_run=True).check()


class Words(unittest.TestCase):
    def test_with_transport_keeps_every_other_setting(self):
        out = tomllib.loads(migrate.with_transport('[server]\nhost = "h"\n[github]\nowner = "o"\n', "direct"))
        self.assertEqual((out["transport"]["kind"], out["server"]["host"], out["github"]["owner"]), ("direct", "h", "o"))


if __name__ == "__main__":
    unittest.main()
