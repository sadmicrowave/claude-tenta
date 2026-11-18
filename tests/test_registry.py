import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401  (points HOME at a throwaway folder)
from ct import pkgspec, registry  # noqa: E402

TOOLS = "/opt/example/tools"


def errs(cls, **top):
    return registry.validate({"classes": {"x1": cls}, **top}, TOOLS)


class ClaudeClassTools(unittest.TestCase):
    def check(self, tools, **extra):
        reg = {"classes": {"askme": {"kind": "claude", "tier": "approve", "cost": "light", "allowed_tools": tools, **extra}}}
        return registry.validate(reg, "/nonexistent-tools-root", allow_empty=True)

    def test_read_tools_are_fine(self):
        self.assertEqual(self.check(["Read", "Grep", "Glob"]), [])

    def test_helper_agents_are_never_allowed_even_for_a_reviewed_class(self):
        for tool in ("Task", "Agent", "Task(general)"):
            errs = self.check([tool], reviewed=True)
            self.assertTrue(any("starts helper agents" in e for e in errs), (tool, errs))

    def test_tools_that_write_run_or_fetch_need_the_class_to_say_it_was_reviewed(self):
        for tool in ("Bash", "Bash(git log:*)", "Edit", "WebFetch", "mcp__x__y"):
            self.assertTrue(any("reviewed: true" in e for e in self.check(["Read", tool])), tool)
            self.assertEqual(self.check(["Read", tool], reviewed=True), [], tool)

    def test_max_running_is_accepted_as_a_positive_number(self):
        self.assertEqual(self.check(["Read"], max_running=2), [])

    def test_a_patch_returning_class_needs_files_and_only_the_value_patch_is_known(self):
        self.assertEqual(self.check(["Read", "Edit"], reviewed=True, accepts_files=True, returns="patch", patch_max_kb=100), [])
        self.assertTrue(any("accepts_files" in e for e in self.check(["Read", "Edit"], reviewed=True, returns="patch")))
        self.assertTrue(any("returns may only be patch" in e for e in self.check(["Read"], returns="branch")))
        self.assertTrue(any("only a claude class" in e for e in registry.validate(
            {"classes": {"c": {"kind": "command", "tier": "auto", "cost": "light", "run": ["/bin/true"], "returns": "patch"}}}, "/nonexistent-tools-root", allow_empty=True)))


class Packages(unittest.TestCase):
    def test_names_are_validated_sorted_and_deduplicated(self):
        self.assertEqual(pkgspec.check_packages(["pip:pyflakes==3.2.0", "apt:jq", "npm:@scope/pkg@1.2.3", "apt:jq"]),
                         ["apt:jq", "npm:@scope/pkg@1.2.3", "pip:pyflakes==3.2.0"])
        self.assertEqual(pkgspec.check_packages(None), [])
        for bad in (["apt:jq; rm -rf /"], ["apt:-oFoo"], ["gem:rails"], ["jq"], ["apt:Jq"], ["apt:" + "a" * 70], "apt:jq",
                    [f"apt:p{i}x" for i in range(9)], [None]):
            with self.assertRaises(ValueError, msg=str(bad)):
                pkgspec.check_packages(bad)

    def test_split_and_helpers(self):
        self.assertEqual(pkgspec.split_pkgs(["apt:jq", "pip:x==1", "apt:git"]), {"apt": ["jq", "git"], "pip": ["x==1"]})
        self.assertEqual(pkgspec.npm_split("@types/node@20.1.0"), ["@types/node", "20.1.0"])
        self.assertEqual(pkgspec.npm_split("eslint"), ["eslint", ""])
        self.assertEqual(pkgspec.pip_norm("Foo.Bar-baz"), "foo_bar_baz")

    def test_which_entries_count_as_pinned(self):
        for ok in ("apt:jq", "npm:eslint@9.12.0", "npm:@types/node@20.1.0", "npm:left-pad@1.3", "pip:pyflakes==3.2.0", "pip:Foo_Bar==1.0.0rc1"):
            self.assertTrue(pkgspec.is_pinned(ok), ok)
        for bad in ("npm:eslint", "npm:eslint@latest", "npm:eslint@^9.1.0", "npm:@types/node", "pip:pyflakes", "pip:pyflakes==latest"):
            self.assertFalse(pkgspec.is_pinned(bad), bad)


class Validation(unittest.TestCase):
    def test_a_sound_registry_has_no_problems(self):
        self.assertEqual(errs({"run": ["a", "{p}"], "params": {"p": {"int": [1, 2]}}, "max_rss_mb": 100}), [])
        self.assertEqual(errs({"run": ["a"], "accepts_files": True, "batchable": True}), [])

    def test_catches_mistakes(self):
        self.assertTrue(errs({"cost": "huge", "run": ["a"]}))
        self.assertTrue(errs({"run": []}))
        self.assertTrue(errs({"run": ["a", "{p}"]}))                       # undeclared param
        self.assertTrue(errs({"run": ["a"], "tier": "yes"}))
        self.assertTrue(errs({"run": ["a"], "timeout_s": "60"}))
        self.assertTrue(errs({"run": ["a"], "params": {"p": {"int": [1, 2], "enum": [1]}}}))
        self.assertTrue(errs({"kind": "claude"}))
        self.assertTrue(errs({"run": ["a"], "batchable": True}))              # needs accepts_files
        self.assertTrue(errs({"run": ["a"], "accepts_files": True, "batchable": "yes"}))
        self.assertTrue(errs({"kind": "profile", "run": ["a"]}))
        self.assertTrue(registry.validate({"classes": {}}, TOOLS))
        self.assertTrue(registry.validate({"classes": {"Bad Name": {"run": ["a"]}}}, TOOLS))
        self.assertTrue(registry.validate("not a mapping", TOOLS))

    def test_new_keys(self):
        bad = errs({"run": ["/bin/true"], "network": "yes", "output_allow": ["nope"], "output_keys": []})
        for needle in ("network must be", "output_allow may only", "output_keys must"):
            self.assertIn(needle, " ".join(bad))
        self.assertEqual(errs({"run": ["/bin/true"], "network": False, "output_allow": ["private_ip"], "output_max_chars": 100}), [])
        self.assertTrue(errs({"run": ["/bin/true"], "output_allow": ["canary"]}))      # a canary can never be allowed

    def test_packages_and_env(self):
        ok = {"run": ["/usr/bin/x"], "packages": ["apt:jq", "pip:pyflakes==3.2.0"], "env": {"PYTHONPATH": TOOLS + "/pip"}}
        self.assertEqual(errs(ok), [])
        self.assertTrue(errs({**ok, "packages": ["pip:b==1.0", "apt:a"]}))                  # must be sorted
        self.assertTrue(errs({**ok, "packages": ["apt:jq", "pip:pyflakes"]}))              # pip must be pinned
        self.assertTrue(errs({**ok, "packages": ["apt:ok; id"]}))
        self.assertTrue(errs({**ok, "env": {"PYTHONPATH": "/tmp/evil"}}))
        self.assertTrue(errs({**ok, "env": {"LD_PRELOAD": TOOLS + "/x"}}))
        self.assertTrue(errs({**ok, "env": {"PYTHONPATH": TOOLS + "/pip:/tmp/evil"}}))      # every entry must be under the tools folder
        self.assertTrue(errs({**ok, "env": {"PYTHONPATH": TOOLS + "-evil/pip"}}))           # a sibling folder with the same prefix is not "under"

    def test_pin_policy_can_be_turned_off_in_the_registry(self):
        loose = {"run": ["/bin/x"], "packages": ["npm:eslint"]}
        self.assertIn("pin an exact version for npm:eslint", " ".join(errs(loose)))
        self.assertEqual(errs(loose, limits={"packages": {"require_pins": False}}), [])
        self.assertIn("must be true or false", " ".join(registry.limits_problems({"packages": {"require_pins": 1}})))


class Limits(unittest.TestCase):
    def test_defaults_and_overrides(self):
        self.assertEqual(registry.limits_of({})["files"]["max_count"], 200)
        lim = registry.limits_of({"limits": {"files": {"max_count": 7}, "output_max_chars": 99}})
        self.assertEqual((lim["files"]["max_count"], lim["files"]["max_total_kb"], lim["output_max_chars"]), (7, 2000, 99))
        registry.limits_of({"limits": {"files": {"max_count": 1}}})
        self.assertEqual(registry.LIMITS_DEFAULTS["files"]["max_count"], 200)             # the defaults are never modified

    def test_bad_limits_are_refused(self):
        base = {"run": ["/bin/true"]}
        self.assertEqual(errs(base, limits={"files": {"max_count": 3}}), [])
        for bad in ({"files": {"max_count": 0}}, {"files": {"nope": 1}}, {"nope": 1}, {"output_max_chars": "9"}, "x", {"files": 3}):
            self.assertTrue(errs(base, limits=bad), bad)

    def test_profiling_rules_merge_over_the_defaults(self):
        rules = registry.profiling_rules({"profiling": {"temp_ceiling_c": 70, "fit": {"wall_s_max": 60}}})
        self.assertEqual((rules["temp_ceiling_c"], rules["fit"]["wall_s_max"], rules["fit"]["rss_frac_max"]), (70, 60, 0.5))
        self.assertEqual(registry.profiling_rules({})["mem_ceiling_frac"], 0.6)
        self.assertEqual(registry.PROFILING_DEFAULTS["temp_ceiling_c"], 85)                # never modified


class Loading(unittest.TestCase):
    def test_load_reads_validates_and_reports_everything(self):
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("PyYAML is not installed")
        d = Path(tempfile.mkdtemp())
        good = d / "good.yaml"
        good.write_text("limits:\n  files:\n    max_count: 3\nclasses:\n  sys-status:\n    tier: auto\n    cost: light\n    run: [/usr/bin/uptime]\n")
        reg, lim = registry.load(good, TOOLS)
        self.assertIn("sys-status", reg["classes"])
        self.assertEqual(lim["files"]["max_count"], 3)
        bad = d / "bad.yaml"
        bad.write_text("classes:\n  Bad Name:\n    run: []\n  ok-one:\n    cost: huge\n    run: [a]\n")
        with self.assertRaises(ValueError) as cm:
            registry.load(bad, TOOLS)
        self.assertIn("Bad Name", str(cm.exception))
        self.assertIn("cost must be", str(cm.exception))                                       # every problem at once
        with self.assertRaises(ValueError):
            registry.load(d / "missing.yaml", TOOLS)
        broken = d / "broken.yaml"
        broken.write_text("classes: [unclosed")
        with self.assertRaises(ValueError) as cm:
            registry.load(broken, TOOLS)
        self.assertIn("not valid YAML", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
