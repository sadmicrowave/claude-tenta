"""Part 2 of 2 of the test_admin_actions_jobs tests (ActionBase, ActionAdd, ActionRemoveAndList, ClassSetTests)."""
import unittest
import re
import json
from pathlib import Path
from admin_support import AdminBase, INSTALLED, REGISTRY  # noqa: F401  (also points HOME at a throwaway folder)
from ct import registry


class ActionBase(AdminBase):
    """Shared setup: a worker that answers `action-hash --json`, and the fan-control pack."""
    PIN = "ab" * 32

    def health(self, rev):
        self.ssh.answers["systemctl"] = "timer-fetch=active\ntimer-step=active\ntimer-canary=active\nfailed=0\nregistry=0\nrev=" + rev + "\n"

    def setUp(self):
        super().setUp()
        self.ssh.answers[INSTALLED] = self.rel[0] + "\n"
        self.health(self.rel[0])
        self.ssh.answers["action-hash --json"] = json.dumps({"path": "/usr/local/sbin/fan-profile", "sha256": self.PIN, "ok": True, "problem": None}) + "\n"
        self.pack = Path(__file__).resolve().parents[1] / "packs" / "fan-control"

    def main_registry(self):
        self.repos.fetch_queue()
        return self.repos.show_file("origin/main", "registry.yaml")


class ActionAdd(ActionBase):
    """`ct-admin action add`: registering a root-level action is one command with one question, like adopt."""
    def test_one_command_adds_the_action_and_its_class_pinned_to_what_the_worker_reported(self):
        code, out, err = self.admin("action", "add", str(self.pack), "--yes")
        self.assertEqual(code, 0, err)
        text = self.main_registry()
        reg = registry.parse(text)
        self.assertEqual(reg["actions"]["fan-profile"]["sha256"], self.PIN)
        self.assertEqual(reg["classes"]["fan-profile"], {"kind": "privileged", "action": "fan-profile", "cost": "heavy"})
        self.assertIn("careful", reg["classes"])                                                     # what was there is still there
        self.assertEqual(registry.validate(reg, "/nonexistent-tools-root", allow_empty=True) and [x for x in registry.validate(reg, "/nonexistent-tools-root", allow_empty=True) if "tools" not in x], [])
        self.assertTrue(any(c.startswith("sudo ct-update --rev ") for c in self.ssh.calls))        # installed, like adopt

    def test_the_preview_says_it_runs_as_root_and_what_it_may_touch_before_anything_is_asked(self):
        self.tty, self.answers = True, ["n"]
        code, out, err = self.admin("action", "add", str(self.pack))
        self.assertIn("RUNS AS ROOT", err)
        for needle in ("/usr/local/sbin/fan-profile", self.PIN, "/sys/devices/platform/applesmc.768", "none", "needs your signature"):
            self.assertIn(needle, err)
        self.assertNotIn("fan-profile", self.main_registry().split("classes:")[0])                  # said no: nothing was changed

    def test_a_program_the_worker_says_anybody_could_change_is_refused(self):
        self.ssh.answers["action-hash --json"] = json.dumps({"path": "/usr/local/sbin/fan-profile", "sha256": self.PIN, "ok": False, "problem": "it is writable by others"}) + "\n"
        code, _, err = self.admin("action", "add", str(self.pack), "--yes")
        self.assertEqual(code, 2)
        self.assertIn("cannot be a root-level action", err)
        self.assertNotIn("fan-profile", self.main_registry())

    def test_a_definition_with_a_shell_is_refused_before_the_worker_is_asked(self):
        bad = self.tmp / "bad.yaml"
        bad.write_text("actions:\n  x:\n    run: [/usr/bin/bash, '-c', '{cmd}']\n    params: {cmd: {regex: '.+'}}\n")
        code, _, err = self.admin("action", "add", str(bad), "--yes")
        self.assertEqual(code, 2)
        self.assertEqual(self.ssh.captured, [])

    def test_running_it_again_after_the_program_changed_only_moves_the_pin(self):
        self.admin("action", "add", str(self.pack), "--yes")
        new = "cd" * 32
        self.ssh.answers["action-hash --json"] = json.dumps({"path": "/usr/local/sbin/fan-profile", "sha256": new, "ok": True, "problem": None}) + "\n"
        code, _, err = self.admin("action", "add", str(self.pack), "--yes")
        self.assertEqual(code, 0, err)
        reg = registry.parse(self.main_registry())
        self.assertEqual(reg["actions"]["fan-profile"]["sha256"], new)
        self.assertEqual(len(reg["classes"]), len(registry.parse(REGISTRY)["classes"]) + 1)


class ActionRemoveAndList(ActionBase):
    """Removing mirrors adding, and a wrong word gets help instead of argparse's usage text."""
    def setUp(self):
        super().setUp()
        self.admin("action", "add", str(self.pack), "--yes")

    def test_remove_takes_the_action_and_the_class_that_runs_it(self):
        code, out, err = self.admin("action", "remove", "fan-profile", "--yes")
        self.assertEqual(code, 0, err)
        reg = registry.parse(self.main_registry())
        self.assertNotIn("fan-profile", reg.get("actions") or {})
        self.assertNotIn("fan-profile", reg["classes"])
        self.assertIn("careful", reg["classes"])
        self.assertIn("stays on the worker", err)

    def test_remove_also_accepts_the_pack_folder_it_was_added_from(self):
        code, _, err = self.admin("action", "remove", str(self.pack), "--yes")
        self.assertEqual(code, 0, err)
        self.assertNotIn("fan-profile", self.main_registry())

    def test_a_misspelt_name_lists_what_is_registered_and_suggests_the_close_one(self):
        code, _, err = self.admin("action", "remove", "fan-profil", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("Registered: fan-profile", err)
        self.assertIn("ct-admin action remove fan-profile", err)

    def test_the_same_action_twice_changes_nothing_and_says_so(self):
        before = self.main_registry()
        code, _, err = self.admin("action", "add", str(self.pack), "--yes")
        self.assertEqual(code, 0, err)
        self.assertIn("Already registered", err)
        self.assertEqual(self.main_registry(), before)

    def test_the_same_program_under_a_second_name_is_refused(self):
        other = self.tmp / "other.yaml"
        other.write_text("actions:\n  fan-two:\n    run: [/usr/local/sbin/fan-profile, '{mode}']\n    params: {mode: {enum: [idle]}}\n    writable: []\n    timeout_s: 30\n"
                         "classes:\n  fan-two:\n    kind: privileged\n    action: fan-two\n")
        code, _, err = self.admin("action", "add", str(other), "--yes")
        self.assertEqual(code, 2)
        self.assertIn("That program is already registered", err)
        self.assertIn("fan-profile", err)
        self.assertNotIn("fan-two", self.main_registry())

    def test_list_shows_each_action_with_its_pin_and_class(self):
        code, out, err = self.admin("action", "list")
        self.assertEqual(code, 0, err)
        for needle in ("fan-profile", "/usr/local/sbin/fan-profile", self.PIN[:16], "/sys/devices/platform/applesmc.768"):
            self.assertIn(needle, out)
        self.admin("action", "remove", "fan-profile", "--yes")
        self.assertIn("No root-level actions are registered", self.admin("action", "list")[2])

    def test_a_wrong_word_gets_the_choices_and_the_nearest_command_not_argparse_usage_text(self):
        code, _, err = self.admin("action", "lst")
        self.assertEqual(code, 2)
        self.assertNotIn("usage:", err)
        self.assertIn("The choices are: add, list, remove", err)
        self.assertIn("lst", err)
        self.assertIn("ct-admin action list", err)
        code, _, err = self.admin("statu")
        self.assertIn("ct-admin status", err)
        self.assertNotIn("usage:", err)
        code, _, err = self.admin("action", "add")
        self.assertNotIn("usage:", err)


class ClassSetTests(AdminBase):
    """ct-admin class set / show: change a class's tier, network access and tool folders without editing registry.yaml by hand."""

    def setUp(self):
        super().setUp()
        self.ssh.answers[INSTALLED] = self.rel[0] + "\n"
        self.ssh.answers["systemctl"] = "timer-fetch=active\ntimer-step=active\ntimer-canary=active\nfailed=0\nregistry=0\nrev={}\n".format(self.rel[0])
        self.ctx.sleep = lambda s: None

    def registry(self):
        self.repos.fetch_queue()
        return self.repos.show_file("origin/main", "registry.yaml")

    def test_the_tier_changes_in_place_and_the_change_is_installed(self):
        code, out, err = self.admin("class", "set", "careful", "--tier", "auto", "--yes")
        self.assertEqual(code, 0, err)
        self.assertIn("approve  ->  auto", out + err)
        self.assertRegex(self.registry(), r"(?m)^  careful:\n(?:    .*\n)*?    tier: auto\n")
        self.assertTrue(any(c.startswith("sudo ct-update --rev ") for c in self.ssh.calls))

    def test_turning_the_network_on_for_an_automatic_class_also_makes_it_need_approval(self):
        code, out, err = self.admin("class", "set", "echo", "--network", "on", "--yes")
        self.assertEqual(code, 0, err)
        self.assertIn("also set to approve", out + err)
        block = re.search(r"(?m)^  echo:\n((?:    .*\n)+)", self.registry()).group(1)
        self.assertIn("network: true", block)
        self.assertIn("tier: approve", block)

    def test_network_without_approval_needs_the_explicit_flag_and_a_person_answering_yes_does_not_count(self):
        code, out, err = self.admin("class", "set", "echo", "--network", "on", "--tier", "auto", "--yes")
        self.assertEqual(code, 2)                                                           # no terminal: --yes does not answer it
        self.assertIn("answering this question yourself", err)
        self.assertNotIn("network: true", self.registry())
        self.ctx.is_tty, self.ctx.ask = (lambda: True), (lambda p: "n")
        self.assertEqual(self.admin("class", "set", "echo", "--network", "on", "--tier", "auto")[0], 1)
        self.assertNotIn("network: true", self.registry())
        self.ctx.ask = lambda p: "y"
        self.assertEqual(self.admin("class", "set", "echo", "--network", "on", "--tier", "auto")[0], 0)
        self.assertIn("network: true", self.registry())

    def test_tool_folders_can_be_listed_or_repeated_and_removed_with_none(self):
        self.assertEqual(self.admin("class", "set", "careful", "--tools", "/opt/tools/nmap", "/opt/tools/bin", "--tools", "/srv/more", "--yes")[0], 0)
        self.assertIn("tools: [/opt/tools/nmap, /opt/tools/bin, /srv/more]", self.registry())
        self.assertEqual(self.admin("class", "set", "careful", "--tools", "none", "--yes")[0], 0)
        self.assertNotIn("tools:", self.registry())

    def test_a_folder_that_is_not_plain_and_a_missing_change_and_an_unchanged_class_are_explained(self):
        code, _, err = self.admin("class", "set", "careful", "--tools", "relative/bin", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("invalid registry", err)
        self.assertEqual(self.admin("class", "set", "careful", "--yes")[0], 2)
        code, out, err = self.admin("class", "set", "careful", "--tier", "approve", "--yes")
        self.assertEqual(code, 0)
        self.assertIn("Nothing to change", out + err)

    def test_show_says_what_a_class_may_do(self):
        self.admin("class", "set", "careful", "--tools", "/opt/tools/nmap", "--yes")
        code, out, err = self.admin("class", "show", "careful")
        self.assertEqual(code, 0, err)
        for needle in ("Tier", "approve", "Network", "/opt/tools/nmap", "NOT inside a folder this worker allows"):
            self.assertIn(needle, out + err)

    def test_a_class_that_is_not_anywhere_is_refused_with_the_real_ones(self):
        code, _, err = self.admin("class", "show", "nope")
        self.assertEqual(code, 2)
        self.assertIn("No such class", err)
        self.assertIn("careful", err)

    def test_a_class_from_a_pack_can_be_shown_but_not_set(self):
        self.repos.registry_info = lambda: {"classes": {"fromapack": {"tier": "auto", "cost": "light", "network": True, "tools": ["/opt/x"]}}}
        code, out, err = self.admin("class", "show", "fromapack")
        self.assertEqual(code, 0, err)
        for needle in ("a pack (change it there)", "auto", "yes: jobs of this class can reach the network", "/opt/x"):
            self.assertIn(needle, out + err)
        code, _, err = self.admin("class", "set", "fromapack", "--tier", "approve", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("comes from a pack", err)


if __name__ == "__main__":
    unittest.main()
