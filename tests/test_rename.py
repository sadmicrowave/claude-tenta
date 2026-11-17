import unittest

import support  # noqa: F401
from test_setup import FakeHost, cfg_of
from ct import config, render
from ct.setup import rename
from ct.setup.host import Result
from ct.setup.server import SetupError

H = "/" + "home"
OLD, NEW = "claude-tenta", "tenta-two"


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
        self.h.put(f"/etc/audit/rules.d/60-{OLD}.rules", "-w /home/x/.aws/credentials -p r -k ct-canary\n")
        self.h.replies["systemctl is-enabled"] = Result(0, "enabled\n")
        self.h.put("/opt/src/src/ct/__init__.py", "")
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
        self.assertEqual(act.path, f"/etc/{NEW}/ct.toml")
        loaded = config.Config(config.validate(__import__("tomllib").loads(act.text), require_alert_url=False)[0])
        self.assertEqual((loaded.identity.name, loaded.github.owner, loaded.server.admin_user), (NEW, "someone", "me"))

    def test_the_folders_that_follow_the_name_are_moved_and_a_custom_one_is_not(self):
        r, steps = self.plan()
        moved = {(a.argv[1], a.argv[2]) for a in steps["Move the folders"]}
        self.assertIn((f"/opt/{OLD}", f"/opt/{NEW}"), moved)
        self.assertIn((f"/etc/{OLD}", f"/etc/{NEW}"), moved)
        self.assertIn((f"/var/lib/{OLD}", f"/var/lib/{NEW}"), moved)
        self.assertEqual(len(moved), 5)

    def test_the_root_level_helpers_record_of_what_ran_moves_with_the_install(self):
        self.h.put(self.cfg.path("helper_state") + "/ran-job-1", "x")
        r, steps = self.plan()
        moved = {(a.argv[1], a.argv[2]) for a in steps["Move the folders"]}
        self.assertIn((f"/var/lib/{OLD}-helper", f"/var/lib/{NEW}-helper"), moved)

    def test_every_service_of_the_old_name_is_stopped_then_removed_and_the_enabled_timers_start_under_the_new_name(self):
        r, steps = self.plan()
        stopped = [a.argv[-1] for a in steps["Stop the services"]]
        self.assertTrue(all(u.startswith(OLD + "-") for u in stopped))
        removed = {a.path for a in steps["Remove what is named after the old name"] if a.kind == "remove"}
        for part in render.UNITS:
            self.assertIn(f"/etc/systemd/system/{OLD}-{part}", removed)
        self.assertIn(f"/etc/sudoers.d/{OLD}", removed)
        started = [a.argv[-1] for a in steps["Start the services again"]]
        self.assertTrue(started and all(u.startswith(NEW + "-") and u.endswith((".timer", ".socket")) for u in started))

    def test_the_release_installer_writes_the_files_named_after_the_install_for_the_new_name(self):
        r, steps = self.plan()
        (act,) = steps["Write every file that is named after the install (the same installer an update uses)"]
        self.assertIn(f"CT_CONFIG=/etc/{NEW}/ct.toml", act.argv)
        self.assertEqual(act.argv[-5:-2], ["-m", "ct.update", "--local"])

    def test_a_current_install_has_none_of_the_old_name_cleanup(self):
        _, steps = self.plan()
        gone = steps["Remove what is named after the old name"]
        self.assertFalse([a for a in gone if a.kind == "run" and a.argv and a.argv[0] in ("sed", "rm")])

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
        self.assertTrue(all(argv[:4] == ["runuser", "-u", "ctworker", "--"] for argv in remotes))                      # git runs as the worker, never as root


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
        self.assertFalse(self.h.exists(f"/etc/{NEW}/ct.toml"))

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
        self.assertTrue(self.h.exists(f"/etc/{NEW}/ct.toml"))

    def test_saying_no_changes_nothing(self):
        report = self.rn(dry_run=False, confirm=lambda p: False).run_all()
        self.assertEqual(report[-1][1], "declined")
        self.assertEqual([c for c in self.h.calls if c[0] in ("mv", "env")], [])

    def test_a_failing_step_stops_there_and_says_how_to_get_back(self):
        self.h.replies["env "] = Result(1, "boom")
        with self.assertRaises(SetupError) as e:
            self.rn(dry_run=False).run_all()
        self.assertIn("run the same command again", " ".join(e.exception.steps))
        self.assertIn(f"Do not run `ct-setup rename {OLD}`", " ".join(e.exception.steps))
        self.assertFalse(any(c[0] == "systemctl" and c[1] == "enable" for c in self.h.calls))        # nothing was started on a half-renamed machine


class Local(unittest.TestCase):
    def test_the_configuration_the_saved_answers_and_the_clones_all_get_the_new_name(self):
        from pathlib import Path
        h = FakeHost()
        cfg = cfg_of()
        cfg.source = h.path("/me/.config/claude-tenta/ct.toml").as_posix()
        text = '[identity]\nname = "%s"\n[github]\nowner = "someone"\n' % OLD
        h.write(cfg.source, text)
        answers = Path(cfg.paths.client_home).expanduser() / "install-answers.toml"
        h.write(str(answers), text)
        cfg2 = rename.load_new_config(text, NEW)
        h.put(cfg2.path("client_queue") + "/.git/HEAD", "x")
        changed = rename.rewrite_local(h, cfg, cfg2, NEW)
        self.assertEqual(changed, [cfg.source, str(answers)])
        for path in changed:
            self.assertIn(f'name = "{NEW}"', h.read(path))
            self.assertNotIn(OLD, h.read(path))
        self.assertTrue(h.ran(f"git -C {cfg2.path('client_queue')} remote set-url origin git@github.com:someone/{NEW}-queue.git"))

    def test_without_saved_answers_only_the_configuration_changes(self):
        h = FakeHost()
        cfg = cfg_of()
        cfg.source = h.path("/me/.config/claude-tenta/ct.toml").as_posix()
        h.write(cfg.source, '[identity]\nname = "%s"\n[github]\nowner = "someone"\n' % OLD)
        self.assertEqual(rename.rewrite_local(h, cfg, rename.load_new_config(h.read(cfg.source), NEW), NEW), [cfg.source])


class LegacyInstall(Installed):
    """An install made before the project was renamed: its settings are in cw.toml and its commands, package folder and secrets keys carry the old prefix."""

    def setUp(self):
        super().setUp()
        text = self.h.read(self.cfg.path("config_file"))
        import os
        os.remove(self.h.path(self.cfg.path("config_file")))
        self.h.put(f"{self.cfg.paths.config}/cw.toml", text)
        self.cfg.source = f"{self.cfg.paths.config}/cw.toml"                      # what CT_CONFIG points at when the command starts on the server
        for p in ("/usr/local/bin/cw-worker", "/usr/local/sbin/cw-update", f"{self.cfg.paths.code}/lib/cw/cli.py", self.cfg.path("secrets")):
            self.h.put(p, "x\n")

    def test_the_old_settings_file_is_read_and_the_new_one_written(self):
        r, steps = self.plan()
        self.assertTrue(r.legacy)
        (act,) = steps["Write the new name into the configuration"]
        self.assertEqual(act.path, f"/etc/{NEW}/ct.toml")
        self.assertIn(f'name = "{NEW}"', act.text)

    def test_what_the_old_version_wrote_under_old_names_is_removed_without_showing_secrets(self):
        r, steps = self.plan()
        gone = steps["Remove what is named after the old name"]
        removed = {a.path for a in gone if a.kind == "remove"}
        for path in (f"/etc/{NEW}/cw.toml", "/usr/local/bin/cw-worker", "/usr/local/sbin/cw-update"):
            self.assertIn(path, removed)
        runs = [a.argv for a in gone if a.kind == "run" and a.argv]
        self.assertIn(["rm", "-rf", f"/opt/{NEW}/lib/cw"], runs)
        sed = next(a for a in runs if a[0] == "sed")
        self.assertEqual(sed[-2:], ["s/^CW_/CT_/", f"/etc/{NEW}/secrets.env"])         # the keys are renamed in place; no value is read or printed


class LegacyClient(unittest.TestCase):
    def test_a_computer_set_up_before_the_rename_moves_its_folder_and_settings_to_the_new_names(self):
        from pathlib import Path
        h = FakeHost()
        cfg = cfg_of(paths={"client_home": "~/.cw"})
        cfg.source = str(config.legacy_config_paths()[0])
        text = '[identity]\nname = "%s"\n[github]\nowner = "someone"\n' % OLD
        h.write(cfg.source, text)
        h.write(str(Path("~/.cw").expanduser() / "approval" / "key"), "k")
        cfg2 = rename.load_new_config(text, NEW)
        changed = rename.rewrite_local(h, cfg, cfg2, NEW)
        new_cfg = str(Path.home() / ".config" / "claude-tenta" / "ct.toml")
        self.assertIn(new_cfg, changed)
        self.assertIn(f'name = "{NEW}"', h.read(new_cfg))
        self.assertTrue(h.ran(f"rm -f {cfg.source}"))                                       # the old file is removed
        self.assertTrue(h.ran(f"mv {Path('~/.cw').expanduser()} {Path('~/.ct').expanduser()}"))
        self.assertTrue([c for c in h.calls if c[0] == "sh" and "cw-run" in c[-1]])         # the old command links


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
        self.h.replies["gh repo view someone/tenta-two-queue"] = Result(1)
        self.h.replies["gh repo view someone/tenta-two-results"] = Result(0)
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
        self.h.replies["gh repo deploy-key list -R someone/tenta-two-queue"] = Result(0, listing)
        self.h.replies["gh repo deploy-key list -R someone/tenta-two-results"] = Result(0, listing.replace('"readOnly": true}, {"id": 8', '"readOnly": false}, {"id": 8'))
        notes = rename.retitle_keys(self.h, self.cfg, NEW)
        self.assertEqual([n[1] for n in notes], ["retitled", "retitled"])                     # only the keys named after the install, never anyone else's
        self.assertTrue(self.h.ran("gh repo deploy-key delete 7 -R someone/tenta-two-queue"))
        adds = self.h.ran("gh repo deploy-key add")
        self.assertEqual(len(adds), 2)
        self.assertTrue(all(f"{NEW}-server" in " ".join(a) for a in adds))
        self.assertNotIn("--allow-write", adds[0])
        self.assertIn("--allow-write", adds[1])                                                # the results key keeps its write access


if __name__ == "__main__":
    unittest.main()


class SameName(unittest.TestCase):
    def test_asking_for_the_name_it_already_has_says_what_that_name_is_and_where_it_came_from(self):
        import argparse
        import io
        from contextlib import redirect_stderr
        h = FakeHost()
        cfg_file = h.path("/me/.config/claude-tenta/ct.toml").as_posix()
        h.write(cfg_file, '[identity]\nname = "%s"\n[server]\nhost = "worker.example"\nadmin_user = "me"\n[github]\nowner = "someone"\n' % OLD)
        err = io.StringIO()
        real = rename.config.load
        rename.config.load = lambda *a, **k: (lambda c: (setattr(c, "source", cfg_file), c)[1])(rename.load_new_config(h.read(cfg_file), OLD))
        try:
            with redirect_stderr(err):
                rc = rename.rename_command(argparse.Namespace(name=OLD, source=None, apply=False, yes=False), h, lambda p: "n", None, lambda *a: None)
        finally:
            rename.config.load = real
        out = err.getvalue()
        self.assertEqual(rc, 0)
        for needle in ("already has that name", OLD, cfg_file, "worker.example", f"{OLD}-queue"):
            self.assertIn(needle, out)


class PlanCard(Installed):
    def test_the_plan_lines_name_what_each_action_touches_and_never_show_what_is_written(self):
        r = self.rn()
        r.check()
        lines = dict(r.plan_lines())
        self.assertIn(f"mv /opt/{OLD} /opt/{NEW}", lines["Move the folders"])
        self.assertEqual(lines["Write the new name into the configuration"], [f"write /etc/{NEW}/ct.toml"])        # the path, not the text
        self.assertTrue(all(len(ln) < 400 for ls in lines.values() for ln in ls))

    def test_the_preview_is_one_card_with_a_heading_per_step_and_what_is_kept(self):
        import io
        import os
        from contextlib import redirect_stderr
        from unittest import mock

        class Tty(io.StringIO):
            def isatty(self):
                return True
        r = self.rn()
        r.check()
        err = Tty()
        with mock.patch.dict(os.environ, {"CW_COLOR": "always", "CT_COLOR": "always"}), redirect_stderr(err):
            rename.render_plan(r.plan_lines(), OLD, NEW, where="on worker.example")
        import re
        text = re.sub(r"\x1b\[[0-9;]*m", "", err.getvalue())
        for needle in (f"Rename {OLD} to {NEW} on worker.example", "PREVIEW", "MOVE THE FOLDERS", f"mv /opt/{OLD} /opt/{NEW}", "KEPT", "job history"):
            self.assertIn(needle, text.upper() if needle.isupper() else text)


class RepositoryNamesFollow(unittest.TestCase):
    def test_repository_names_that_carry_the_old_name_follow_it_and_custom_ones_stay(self):
        text = '[identity]\nname = "%s"\n[github]\nowner = "someone"\nqueue_repo = "%s-queue"\nresults_repo = "my-own-results"\n' % (OLD, OLD)
        out = rename.tomllib.loads(rename.with_name(text, NEW))
        self.assertEqual((out["identity"]["name"], out["github"].get("queue_repo", f"{NEW}-queue"), out["github"]["results_repo"]), (NEW, f"{NEW}-queue", "my-own-results"))          # a value equal to the default is not written

    def test_an_old_file_that_never_wrote_its_name_is_told_what_it_meant(self):
        text = '[github]\nowner = "someone"\nqueue_repo = "claude-ballast-queue"\n'
        out = rename.tomllib.loads(rename.with_name(text, NEW, "claude-ballast"))
        self.assertEqual((out["identity"]["name"], out["github"].get("queue_repo", f"{NEW}-queue")), (NEW, f"{NEW}-queue"))

    def test_the_plan_and_the_new_configuration_carry_the_new_repository_names(self):
        h = FakeHost()
        cfg = cfg_of(alerts={"kind": "none"}, github={"owner": "someone", "queue_repo": f"{OLD}-queue", "results_repo": f"{OLD}-results"})
        cfg2 = rename.load_new_config(rename.with_name('[github]\nowner = "someone"\nqueue_repo = "%s-queue"\nresults_repo = "%s-results"\n' % (OLD, OLD), NEW, OLD), NEW, OLD)
        self.assertEqual((cfg2.github.queue_repo, cfg2.github.results_repo), (f"{NEW}-queue", f"{NEW}-results"))


class ResumeAndLegacyAccount(Installed):
    """A rename that stopped partway (the folders moved, the settings file under the new folder name) is finished by running the same command again, and an old install
    keeps the worker account it actually has."""

    def test_an_old_settings_file_that_never_named_the_worker_account_keeps_the_old_default(self):
        out = rename.tomllib.loads(rename.with_name('[server]\nhost = "h"\nadmin_user = "me"\n', NEW, OLD, legacy=True))
        self.assertEqual(out["server"]["worker_user"], "cwworker")
        out = rename.tomllib.loads(rename.with_name('[server]\nhost = "h"\nadmin_user = "me"\nworker_user = "mine"\n', NEW, OLD, legacy=True))
        self.assertEqual(out["server"]["worker_user"], "mine")                                   # an explicit account is never replaced
        out = rename.tomllib.loads(rename.with_name('[server]\nhost = "h"\n', NEW, OLD))
        self.assertNotIn("worker_user", out["server"])                                          # a current file is left alone

    def test_the_plan_chowns_the_account_that_exists_not_todays_default(self):
        text = '[server]\nhost = "worker.example"\nadmin_user = "me"\n[github]\nowner = "someone"\n'
        self.h.put(f"{self.cfg.paths.config}/cw.toml", text)
        import os
        os.remove(self.h.path(self.cfg.path("config_file")))
        cfg = config.load(self.h.path(f"{self.cfg.paths.config}/cw.toml"), env={})          # what the command line does with CT_CONFIG
        cfg.source = f"{self.cfg.paths.config}/cw.toml"
        r = rename.Rename(self.h, cfg, NEW, source="/opt/src", rev="a" * 40, say=self.said.append)
        r.check()
        chown = [a for _, acts in r.steps() for a in acts if a.kind == "run" and a.argv[:1] == ["chown"]]
        self.assertEqual(chown[0].argv[1], "cwworker:cwworker")

    def test_run_again_after_the_folders_moved_it_reads_the_old_file_from_under_the_new_folder_and_finishes(self):
        text = '[server]\nhost = "worker.example"\nadmin_user = "me"\n[github]\nowner = "someone"\n'
        import os
        import shutil
        for old, new in ((self.cfg.paths.code, f"/opt/{NEW}"), (self.cfg.paths.config, f"/etc/{NEW}"), (self.cfg.paths.state_root, f"/var/lib/{NEW}")):
            shutil.move(self.h.path(old), self.h.path(new))                                        # the folders are where the failed run left them
        os.remove(self.h.path(f"/etc/{NEW}/ct.toml")) if os.path.exists(self.h.path(f"/etc/{NEW}/ct.toml")) else None
        self.h.put(f"/etc/{NEW}/cw.toml", text)
        cfg = config.load(self.h.path(f"/etc/{NEW}/cw.toml"), env={})
        cfg.source = f"/etc/{NEW}/cw.toml"
        r = rename.Rename(self.h, cfg, NEW, source="/opt/src", rev="a" * 40, say=self.said.append)
        r.check()                                                                                    # nothing is "already there": the old folders are simply gone
        steps = dict(r.steps())
        self.assertEqual(steps["Move the folders"], [])
        (act,) = steps["Write the new name into the configuration"]
        self.assertIn('worker_user = "cwworker"', act.text)
        self.assertIn(f'name = "{NEW}"', act.text)


class WorkerAccountMustExist(Installed):
    def test_a_missing_worker_account_stops_the_rename_before_anything_is_touched(self):
        self.h.replies["id "] = Result(1, "id: no such user")
        with self.assertRaises(SetupError) as e:
            self.rn().check()
        self.assertIn("does not exist", e.exception.title)
        self.assertFalse([c for c in self.h.calls if c[0] in ("mv", "systemctl")])
