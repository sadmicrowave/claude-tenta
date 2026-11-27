"""One private queue+results repository pair per worker, never shared. A deploy key covers a whole repository, so two workers on one pair could read or forge each other's jobs and results."""
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401
from ct import config, refusals
from ct.admin import workers as workers_mod


def toml(name, owner="someone", queue=None, results=None):
    extra = (f'queue_repo = "{queue}"\n' if queue else "") + (f'results_repo = "{results}"\n' if results else "")
    return f'[identity]\nname = "{name}"\n[server]\nhost = "{name}.example"\nadmin_user = "me"\n[github]\nowner = "{owner}"\n{extra}[transport]\nkind = "github"\n'


class Isolation(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.wfile = self.d / "workers.toml"
        (self.d / "ct.toml").write_text(toml("mainw"))

    def conf(self, name, **kw):
        p = self.d / f"{name}.toml"
        p.write_text(toml(name, **kw))
        return p

    def test_a_worker_with_its_own_repositories_is_added(self):
        workers_mod.add(self.wfile, "wk-a", self.conf("wk-a"))
        self.assertEqual(list(workers_mod.load_list(self.wfile)), ["wk-a"])

    def test_a_worker_on_the_main_workers_repositories_is_refused_and_not_listed(self):
        with self.assertRaises(refusals.Refused) as cm:
            workers_mod.add(self.wfile, "wk-a", self.conf("wk-a", queue="mainw-queue", results="mainw-results"))
        self.assertEqual(cm.exception.code, "worker.repo.shared")
        self.assertEqual(workers_mod.load_list(self.wfile), {})

    def test_two_listed_workers_cannot_share_one_repository_even_when_only_one_of_the_two_is_shared(self):
        workers_mod.add(self.wfile, "wk-a", self.conf("wk-a"))
        with self.assertRaises(refusals.Refused):
            workers_mod.add(self.wfile, "wk-b", self.conf("wk-b", queue="wk-a-queue"))                  # the case differs in no way GitHub cares about
        with self.assertRaises(refusals.Refused):
            workers_mod.add(self.wfile, "wk-b", self.conf("wk-b", queue="WK-A-Queue"))

    def test_a_workers_queue_and_results_cannot_be_one_repository(self):
        with self.assertRaises(refusals.Refused):
            workers_mod.add(self.wfile, "wk-a", self.conf("wk-a", queue="same", results="same"))

    def test_listing_the_same_worker_again_is_not_a_conflict_with_itself(self):
        p = self.conf("wk-a")
        workers_mod.add(self.wfile, "wk-a", p)
        workers_mod.add(self.wfile, "wk-a", p)

    def test_the_install_refuses_before_anything_is_created_when_its_answers_name_a_used_repository(self):
        cfg = config.load(self.conf("wk-c", queue="wk-a-queue"), env={"CT_CONFIG": ""})
        workers_mod.add(self.wfile, "wk-a", self.conf("wk-a"))
        self.assertEqual(workers_mod.repo_conflict(cfg, self.d / "wk-c.toml", self.wfile)[0], "someone/wk-a-queue")


class EveryPathChecks(unittest.TestCase):
    """The check is wired into every code path that configures a worker, not only the one that was tried by hand."""

    def test_the_commands_that_configure_or_list_a_worker_call_the_one_check(self):
        root = Path(__file__).resolve().parent.parent / "src" / "ct"
        for rel in ("admin/workers.py", "setup/client.py"):
            self.assertRegex((root / rel).read_text(), r"repo_conflict|require_own_repos", rel)


class GithubWorkerSteps(unittest.TestCase):
    """prepare (workers add --github), the fleet's names and topic, and what happens to the repositories on remove."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.wfile = self.d / "workers.toml"
        (self.d / "ct.toml").write_text(toml("mainw"))
        self.calls = []

    def run_gh(self, missing=()):
        def run(argv):
            self.calls.append(argv)
            return (1, "not found") if argv[:2] == ["repo", "view"] and argv[2].split("/")[1] in missing else (0, "")
        return run

    def cfg(self, name, **kw):
        p = self.d / f"{name}.toml"
        p.write_text(toml(name, **kw))
        return config.load(p, env={"CT_CONFIG": ""}), p

    def test_prepare_creates_only_what_is_missing_private_with_the_fleet_topic_and_clones(self):
        from ct.admin import workerrepos
        cfg, p = self.cfg("wk-a")
        rows = workerrepos.prepare(cfg, p, self.wfile, run=self.run_gh(missing={"wk-a-queue"}), clone=lambda: [("cloned", "/x/queue"), ("cloned", "/x/results")])
        self.assertIn(["repo", "create", "someone/wk-a-queue", "--private", "--description", "armada queue (private)"], self.calls)
        self.assertEqual([c for c in self.calls if c[:2] == ["repo", "create"]].__len__(), 1)
        self.assertEqual(sum(c[:2] == ["repo", "edit"] and c[-1] == "armada" for c in self.calls), 2)
        self.assertEqual([r[2] for r in rows], ["created, private", "exists", "cloned", "cloned"])

    def test_prepare_refuses_a_shared_repository_before_it_calls_gh(self):
        from ct.admin import workerrepos
        cfg, p = self.cfg("wk-a", queue="mainw-queue")
        with self.assertRaises(refusals.Refused):
            workerrepos.prepare(cfg, p, self.wfile, run=self.run_gh(), clone=lambda: [])
        self.assertEqual(self.calls, [])

    def test_a_new_workers_suggested_names_group_with_the_fleet_and_are_its_own(self):
        from ct.setup import addworker
        main = config.load(self.d / "ct.toml", env={"CT_CONFIG": ""})
        self.assertEqual(addworker.fleet_names(main, "tenta2", False), {"github.queue_repo": "mainw-tenta2-queue", "github.results_repo": "mainw-tenta2-results"})
        self.assertEqual(addworker.fleet_names(main, "tenta2", True), {})

    def test_the_names_are_suggested_when_the_first_worker_is_direct_and_has_no_github_account(self):
        from ct.setup import addworker
        (self.d / "direct.toml").write_text('[identity]\nname = "mainw"\n[server]\nhost = "m.example"\nadmin_user = "me"\n[transport]\nkind = "direct"\n')
        main = config.load(self.d / "direct.toml", env={"CT_CONFIG": ""})
        self.assertEqual(addworker.fleet_names(main, "wkb", False)["github.queue_repo"], "mainw-wkb-queue")

    def test_removing_a_worker_offers_its_repositories_but_never_one_another_worker_lists(self):
        from ct.admin import workerrepos
        _, a = self.cfg("wk-a")
        workers_mod.add(self.wfile, "wk-a", a)
        self.assertEqual(workerrepos.removable("wk-a", a, self.wfile), [("queue", "someone/wk-a-queue"), ("results", "someone/wk-a-results")])
        self.assertEqual(workerrepos.retire(["someone/wk-a-queue"], "archive", run=self.run_gh())[0][:2], ("someone/wk-a-queue", True))
        self.assertEqual(self.calls[-1], ["repo", "archive", "someone/wk-a-queue", "--yes"])

    def test_the_choice_is_never_silent_and_delete_always_asks_in_words(self):
        from types import SimpleNamespace
        from ct.admin import cli
        mine = [("queue", "someone/wk-a-queue")]
        ctx = SimpleNamespace(is_tty=lambda: False)
        self.assertEqual(cli.repos_choice(ctx, SimpleNamespace(repos=None), mine), "keep")             # no terminal and no flag: kept
        self.assertEqual(cli.repos_choice(ctx, SimpleNamespace(repos="archive"), mine), "archive")
        asked = []
        ctx = SimpleNamespace(is_tty=lambda: True, ask=lambda q: "d", confirm=lambda q, yes=False: asked.append(q) or False)
        self.assertEqual(cli.repos_choice(ctx, SimpleNamespace(repos=None), mine), "keep")             # said delete, did not confirm: kept
        self.assertIn("someone/wk-a-queue", asked[0])

    def test_a_refused_repository_choice_leaves_the_worker_listed_and_yes_confirms_a_delete(self):
        from types import SimpleNamespace
        from ct.admin import cli
        from ct.client.repo import ClientError
        _, a = self.cfg("wk-a")
        workers_mod.add(self.wfile, "wk-a", a)
        ctx = SimpleNamespace(workers_file=self.wfile, is_tty=lambda: False, confirm=lambda q, yes=False: True if yes else (_ for _ in ()).throw(ClientError("no terminal", "", [])))
        with self.assertRaises(ClientError):
            cli.cmd_workers(ctx, SimpleNamespace(action="remove", name="wk-a", config=None, github=False, repos="delete", yes=False))
        self.assertIn("wk-a", workers_mod.load_list(self.wfile))                                # nothing changed when it was refused
        self.assertEqual(cli.repos_choice(ctx, SimpleNamespace(repos="delete", yes=True), [("queue", "someone/wk-a-queue")]), "delete")


if __name__ == "__main__":
    unittest.main()
