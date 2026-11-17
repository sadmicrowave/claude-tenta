import unittest

import support  # noqa: F401
from test_setup import FakeHost, cfg_of
from cw import config, render
from cw.setup import rename
from cw.setup.host import Result
from cw.setup.server import SetupError

H = "/" + "home"
OLD, NEW = "claude-ballast", "ballast-two"


class Installed(unittest.TestCase):
    """A fake machine with an install named OLD on it: folders, services, sudo rule, protection files, ssh aliases."""

    def setUp(self):
        self.h = FakeHost()
        self.cfg = cfg_of(alerts={"kind": "none"})
        self.h.put(self.cfg.path("config_file"), 'identity = {}\n[server]\nhost = "worker.example"\nadmin_user = "me"\n[github]\nowner = "someone"\n')
        for d in (self.cfg.paths.code, self.cfg.paths.config, self.cfg.paths.state_root, self.cfg.path("watch_state"), self.cfg.path("source_clone")):
            self.h.put(d + "/marker", "x")
        for part in render.UNITS:
            self.h.put(f"/etc/systemd/system/{self.cfg.unit(part)}", "[Unit]\n")
        self.h.put(f"/etc/sudoers.d/{OLD}", "rules\n")
        self.h.put(f"{self.cfg.paths.code}/bin/{OLD}", "wrapper\n")
        self.h.put(f"/etc/sysctl.d/61-{OLD}-userns.conf", f"# Written for {OLD}\nkernel.apparmor_restrict_unprivileged_userns = 1\n")
        self.h.put(f"/etc/audit/rules.d/60-{OLD}.rules", "-w /home/x/.aws/credentials -p r -k cw-canary\n")
        self.h.replies["systemctl is-enabled"] = Result(0, "enabled\n")
        self.h.put("/opt/src/src/cw/__init__.py", "")
        self.said = []

    def rn(self, new=NEW, **kw):
        return rename.Rename(self.h, self.cfg, new, source="/opt/src", rev="a" * 40, say=self.said.append, **kw)

    def plan(self, new=NEW):
        r = self.rn(new)
        r.check()
        return r, dict(r.steps())


class Plan(Installed):
    def test_the_configuration_gets_the_new_name_and_nothing_else_changes(self):
        r, steps = self.plan()
        (act,) = steps["Write the new name into the configuration"]
        self.assertEqual(act.path, f"/etc/{NEW}/cw.toml")
        loaded = config.Config(config.validate(__import__("tomllib").loads(act.text), require_alert_url=False)[0])
        self.assertEqual((loaded.identity.name, loaded.github.owner, loaded.server.admin_user), (NEW, "someone", "me"))

    def test_the_folders_that_follow_the_name_are_moved_and_a_custom_one_is_not(self):
        r, steps = self.plan()
        moved = {(a.argv[1], a.argv[2]) for a in steps["Move the folders"]}
        self.assertIn((f"/opt/{OLD}", f"/opt/{NEW}"), moved)
        self.assertIn((f"/etc/{OLD}", f"/etc/{NEW}"), moved)
        self.assertIn((f"/var/lib/{OLD}", f"/var/lib/{NEW}"), moved)
        self.assertEqual(len(moved), 5)

    def test_every_service_of_the_old_name_is_stopped_then_removed_and_the_enabled_timers_start_under_the_new_name(self):
        r, steps = self.plan()
        stopped = [a.argv[-1] for a in steps["Stop the services"]]
        self.assertTrue(all(u.startswith(OLD + "-") for u in stopped))
        removed = {a.path for a in steps["Remove what is named after the old name"] if a.kind == "remove"}
        for part in render.UNITS:
            self.assertIn(f"/etc/systemd/system/{OLD}-{part}", removed)
        self.assertIn(f"/etc/sudoers.d/{OLD}", removed)
        started = [a.argv[-1] for a in steps["Start the services again"]]
        self.assertTrue(started and all(u.startswith(NEW + "-") and u.endswith(".timer") for u in started))

    def test_the_release_installer_writes_the_files_named_after_the_install_for_the_new_name(self):
        r, steps = self.plan()
        (act,) = steps["Write every file that is named after the install (the same installer an update uses)"]
        self.assertIn(f"CW_CONFIG=/etc/{NEW}/cw.toml", act.argv)
        self.assertEqual(act.argv[-5:-2], ["-m", "cw.update", "--local"])

    def test_the_sandbox_check_is_on_unless_a_test_machine_asks_to_skip_it(self):
        _, steps = self.plan()
        (act,) = steps["Write every file that is named after the install (the same installer an update uses)"]
        self.assertNotIn("--no-sandbox-test", act.argv)
        r = self.rn(skip_sandbox_test=True)
        r.check()
        (act,) = dict(r.steps())["Write every file that is named after the install (the same installer an update uses)"]
        self.assertIn("--no-sandbox-test", act.argv)

    def test_the_protection_files_are_renamed_with_their_content(self):
        r, steps = self.plan()
        acts = steps["Rename the files the protections wrote"]
        writes = {a.path: a.text for a in acts if a.kind == "write"}
        self.assertIn(f"/etc/sysctl.d/61-{NEW}-userns.conf", writes)
        self.assertIn(NEW, writes[f"/etc/sysctl.d/61-{NEW}-userns.conf"])
        self.assertNotIn(OLD, writes[f"/etc/sysctl.d/61-{NEW}-userns.conf"])
        self.assertIn(f"/etc/audit/rules.d/60-{NEW}.rules", writes)
        self.assertTrue(any(a.kind == "remove" and a.path == f"/etc/audit/rules.d/60-{OLD}.rules" for a in acts))
        self.assertTrue(any(a.kind == "run" and a.argv[0] == "augenrules" for a in acts))

    def test_the_workers_github_aliases_and_both_clone_remotes_follow_the_new_name(self):
        r, steps = self.plan()
        acts = steps["Point the worker's GitHub access at the new name"]
        ssh_text = next(a.text for a in acts if a.kind == "write")
        self.assertIn(f"Host github-{NEW}-queue", ssh_text)
        self.assertNotIn(OLD, ssh_text)
        remotes = [a.argv for a in acts if a.kind == "run" and "set-url" in a.argv]
        self.assertEqual(len(remotes), 2)
        self.assertTrue(all(f"git@github-{NEW}-" in argv[-1] for argv in remotes))
        self.assertTrue(all(argv[:4] == ["runuser", "-u", "cwworker", "--"] for argv in remotes))                      # git runs as the worker, never as root


class Refusals(Installed):
    def test_a_bad_name_the_same_name_and_a_taken_folder_are_refused_before_anything_changes(self):
        for bad, why in (("Bad Name", "not a valid"), (OLD, "already has that name")):
            with self.assertRaises(SetupError) as e:
                self.rn(bad).check()
            self.assertIn(why, str(e.exception) + " " + (e.exception.why or "") + e.exception.title)
        self.h.put(f"/opt/{NEW}/x", "taken")
        with self.assertRaises(SetupError) as e:
            self.rn().check()
        self.assertIn("already exists", e.exception.title)
        self.assertEqual([c for c in self.h.calls if c[0] in ("mv", "systemctl") and c[1:2] != ["is-enabled"]], [])

    def test_a_running_job_stops_the_rename(self):
        self.h.replies["sh -c ls"] = Result(0, "job-0001\n")
        with self.assertRaises(SetupError) as e:
            self.rn().check()
        self.assertIn("A job is running", e.exception.title)

    def test_a_missing_release_checkout_is_refused(self):
        r = rename.Rename(self.h, self.cfg, NEW, source="/nowhere", rev="a" * 40, say=self.said.append)
        with self.assertRaises(SetupError):
            r.check()


class Apply(Installed):
    def test_a_preview_changes_nothing(self):
        r = self.rn(dry_run=True)
        report = r.run_all()
        self.assertEqual(report[-1][1], "planned")
        self.assertEqual([c for c in self.h.calls if c[0] in ("mv", "runuser", "env")], [])
        self.assertFalse(self.h.exists(f"/etc/{NEW}/cw.toml"))

    def test_applying_runs_the_steps_in_order_stop_move_write_install_remove_start(self):
        r = self.rn(dry_run=False)
        report = r.run_all()
        self.assertEqual(report[-1][1], "done")
        order = [c[0] if c[0] != "systemctl" else c[0] + " " + c[1] for c in self.h.calls if c[0] in ("systemctl", "mv", "env", "runuser")]
        first = lambda word: next(i for i, w in enumerate(order) if w == word)      # noqa: E731
        self.assertLess(first("systemctl disable"), first("mv"))
        self.assertLess(first("mv"), first("runuser"))                               # the clone remotes after the folders moved
        self.assertLess(first("runuser"), first("env"))                              # the release installer after the worker's access follows the new name
        self.assertLess(first("env"), first("systemctl daemon-reload"))
        self.assertLess(first("systemctl daemon-reload"), first("systemctl enable"))
        self.assertTrue(self.h.exists(f"/etc/{NEW}/cw.toml"))

    def test_saying_no_changes_nothing(self):
        report = self.rn(dry_run=False, confirm=lambda p: False).run_all()
        self.assertEqual(report[-1][1], "declined")
        self.assertEqual([c for c in self.h.calls if c[0] in ("mv", "env")], [])

    def test_a_failing_step_stops_there_and_says_how_to_get_back(self):
        self.h.replies["env "] = Result(1, "boom")
        with self.assertRaises(SetupError) as e:
            self.rn(dry_run=False).run_all()
        self.assertIn(f"cw-setup rename {OLD}", " ".join(e.exception.steps))
        self.assertFalse(any(c[0] == "systemctl" and c[1] == "enable" for c in self.h.calls))        # nothing was started on a half-renamed machine


class GitHub(unittest.TestCase):
    def setUp(self):
        self.h = FakeHost(env={"TOOLS": "git ssh gh"})
        self.cfg = cfg_of()

    def test_only_repositories_that_carry_the_install_name_are_renamed(self):
        self.assertEqual(rename.github_renames(self.cfg, NEW), [(f"someone/{OLD}-queue", f"{NEW}-queue"), (f"someone/{OLD}-results", f"{NEW}-results")])
        custom = cfg_of(github={"owner": "someone", "queue_repo": "my-queue", "results_repo": "my-results"})
        self.assertEqual(rename.github_renames(custom, NEW), [])
        self.assertEqual(rename.rename_repositories(self.h, custom, NEW), [])

    def test_the_repositories_are_renamed_with_gh_and_a_rerun_skips_the_ones_already_renamed(self):
        self.h.replies["gh repo view someone/ballast-two-queue"] = Result(1)
        self.h.replies["gh repo view someone/ballast-two-results"] = Result(0)
        notes = rename.rename_repositories(self.h, self.cfg, NEW)
        self.assertEqual([n[1] for n in notes], ["renamed", "exists"])
        self.assertTrue(self.h.ran(f"gh repo rename {NEW}-queue --repo someone/{OLD}-queue --yes"))
        self.assertFalse(self.h.ran(f"gh repo rename {NEW}-results"))

    def test_a_failed_rename_is_reported_not_hidden(self):
        self.h.replies["gh repo view"] = Result(1)
        self.h.replies["gh repo rename"] = Result(1, "HTTP 403: not allowed")
        notes = rename.rename_repositories(self.h, self.cfg, NEW)
        self.assertEqual({n[1] for n in notes}, {"failed"})
        self.assertIn("403", notes[0][2])

    def test_without_gh_it_says_what_to_do_by_hand(self):
        bare = FakeHost(env={"TOOLS": "git ssh"})
        (note,) = rename.rename_repositories(bare, self.cfg, NEW)
        self.assertEqual(note[1], "manual")

    def test_the_deploy_keys_get_the_new_name_in_their_title_with_the_same_key_and_access(self):
        listing = ('[{"id": 7, "title": "%s-server", "key": "ssh-ed25519 AAAAKEY", "readOnly": true}, {"id": 8, "title": "someone-elses-key", "key": "ssh-ed25519 AAAAOTHER", "readOnly": true}]' % OLD)
        self.h.replies["gh repo deploy-key list -R someone/ballast-two-queue"] = Result(0, listing)
        self.h.replies["gh repo deploy-key list -R someone/ballast-two-results"] = Result(0, listing.replace('"readOnly": true}, {"id": 8', '"readOnly": false}, {"id": 8'))
        notes = rename.retitle_keys(self.h, self.cfg, NEW)
        self.assertEqual([n[1] for n in notes], ["retitled", "retitled"])                     # only the keys named after the install, never anyone else's
        self.assertTrue(self.h.ran("gh repo deploy-key delete 7 -R someone/ballast-two-queue"))
        adds = self.h.ran("gh repo deploy-key add")
        self.assertEqual(len(adds), 2)
        self.assertTrue(all(f"{NEW}-server" in " ".join(a) for a in adds))
        self.assertNotIn("--allow-write", adds[0])
        self.assertIn("--allow-write", adds[1])                                                # the results key keeps its write access


if __name__ == "__main__":
    unittest.main()
