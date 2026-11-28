"""The change planner's table must know every file of the project, and name only things that exist."""
import subprocess
import unittest
from pathlib import Path

import support  # noqa: F401
import impact

ROOT = Path(__file__).resolve().parent.parent


def tracked():
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT, capture_output=True, text=True).stdout
    return [p for p in out.splitlines() if (ROOT / p).is_file()]


class TheTableKnowsTheProject(unittest.TestCase):
    def test_every_file_of_the_project_matches_a_rule(self):
        unknown = [p for p in tracked() if not any(impact.matches(p, r[0]) for r in impact.RULES)]
        self.assertEqual(unknown, [], "add a rule to tests/impact.py for these (what to run when they change)")

    def test_every_rule_names_tests_probes_and_routes_that_exist(self):
        for pattern, unit, probes, routes in impact.RULES:
            for u in unit:
                self.assertTrue((ROOT / "tests" / f"{u}.py").is_file(), f"{pattern}: no tests/{u}.py")
            for p in probes:
                self.assertTrue((ROOT / "tests" / "probes" / f"{p}.sh").is_file(), f"{pattern}: no probe {p}")
            for r in routes:
                self.assertTrue((ROOT / "tests" / "e2e" / "routes" / f"{r}.sh").is_file(), f"{pattern}: no route {r}")

    def test_every_route_and_probe_on_disk_can_be_planned(self):
        for r in (ROOT / "tests/e2e/routes").glob("*.sh"):
            self.assertIn(r.stem, impact.ALL_ROUTES)
        for p in (ROOT / "tests/probes").glob("*.sh"):
            if p.stem != "record":
                self.assertIn(p.stem, impact.ALL_PROBES)


class ThePlan(unittest.TestCase):
    def test_a_mailbox_change_plans_its_unit_test_the_ownership_probe_and_the_direct_route(self):
        p = impact.plan(["src/ct/mailshell.py"])
        self.assertEqual((p["unit"], p["probes"], p["routes"], p["unmapped"]), (["test_intake", "test_mailshell"], ["ownership"], ["direct"], []))

    def test_a_docs_change_runs_no_probe_and_no_route(self):
        p = impact.plan(["docs/components/testing.md"])
        self.assertEqual((p["probes"], p["routes"]), ([], []))

    def test_an_unknown_file_plans_everything_and_says_so(self):
        p = impact.plan(["mystery.bin"])
        self.assertEqual(p["unmapped"], ["mystery.bin"])
        self.assertEqual(p["routes"], sorted(impact.ALL_ROUTES))

    def test_a_changed_route_or_probe_plans_itself(self):
        self.assertEqual(impact.plan(["tests/e2e/routes/move.sh"])["routes"], ["move"])
        self.assertEqual(impact.plan(["tests/probes/kernel.sh"])["probes"], ["kernel"])


class TheThemeGate(unittest.TestCase):
    """A pull request into a theme branch runs the theme's gate; everything else runs the full suite (tests/themes.toml, scripts/test-gate)."""

    def test_a_theme_base_gets_the_guards_the_mapped_tests_and_the_themes_own(self):
        g = impact.theme_gate(["src/ct/client/cmdlog.py"], "theme/orchestrator")
        self.assertEqual(g["theme"], "orchestrator")
        self.assertFalse(g["full"])
        for m in ("test_printout_rules", "test_docs", "test_cmdlog", "test_candidacy"):
            self.assertIn(m, g["modules"])
        self.assertNotIn("test_mailshell", g["modules"])

    def test_main_or_an_unlisted_base_is_the_full_suite(self):
        for base in ("main", "release/1", "theme/unlisted"):
            g = impact.theme_gate(["src/ct/client/cmdlog.py"], base)
            self.assertEqual((g["theme"], g["full"], g["modules"]), (None, True, []), base)

    def test_a_file_the_table_does_not_know_makes_the_theme_gate_full(self):
        self.assertTrue(impact.theme_gate(["somewhere/new-thing.xyz"], "theme/orchestrator")["full"])

    def test_every_manifest_module_exists(self):
        import tomllib
        data = tomllib.loads((ROOT / "tests" / "themes.toml").read_text())
        for name, mods in [("always", data["always"])] + [(n, t["modules"]) for n, t in data.items() if isinstance(t, dict)]:
            for m in mods:
                self.assertTrue((ROOT / "tests" / f"{m}.py").is_file(), f"themes.toml {name}: no tests/{m}.py")

    def test_the_script_says_full_without_a_base_and_modules_for_a_theme(self):
        run = lambda *a: subprocess.run([str(ROOT / "scripts" / "test-gate"), *a], capture_output=True, text=True).stdout.strip()     # noqa: E731
        self.assertEqual(run(), "full")
        self.assertEqual(run("main"), "full")


if __name__ == "__main__":
    unittest.main()
