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
        self.assertEqual((p["unit"], p["probes"], p["routes"], p["unmapped"]), (["test_mailshell"], ["ownership"], ["direct"], []))

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


if __name__ == "__main__":
    unittest.main()
