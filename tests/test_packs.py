"""Packs: strict manifests, policy that can only tighten, no silent override of a class, and the rules enforced at job time."""
import importlib.util
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import support  # noqa: F401  (points HOME at a throwaway folder)

from cw import jobs, packs, registry

ROOT = Path(__file__).resolve().parents[1]
HAVE_YAML = importlib.util.find_spec("yaml") is not None
CLASS = "classes:\n  {name}:\n    tier: auto\n    cost: light\n    run: [/bin/echo, {name}]\n"


def make_pack(name="mine", kinds=("classes",), classes=("one",), policy=None, extra_manifest="", root=None):
    root = Path(root or tempfile.mkdtemp(prefix="cw-packs-"))
    d = root / name
    (d / "classes").mkdir(parents=True)
    (d / "policy").mkdir()
    (d / "pack.toml").write_text(f'name = "{name}"\nversion = "1.0"\ndescription = "test pack"\nkinds = {list(kinds)!r}\n'.replace("'", '"') + extra_manifest)
    if "classes" in kinds:
        body = "classes:\n" + "".join(f"  {c}:\n    tier: auto\n    cost: light\n    run: [/bin/echo, {c}]\n" for c in classes)
        (d / "classes" / "c.yaml").write_text(body)
    if "policy" in kinds:
        (d / "policy" / "p.toml").write_text(policy or '[[deny.command]]\nnames = ["nsenter"]\nreason = "no"\n')
    return root


@unittest.skipUnless(HAVE_YAML, "PyYAML is not installed")
class LoadTests(unittest.TestCase):
    def test_a_valid_pack_loads_with_a_stable_digest(self):
        root = make_pack(kinds=("classes", "policy"))
        p = packs.load_pack(root / "mine")
        self.assertEqual((p.name, p.version, sorted(p.classes), sorted(p.policy.deny_commands)), ("mine", "1.0", ["one"], ["nsenter"]))
        self.assertEqual(p.digest, packs.load_pack(root / "mine").digest)
        (root / "mine" / "classes" / "c.yaml").write_text(CLASS.format(name="one") + "    timeout_s: 5\n")
        self.assertNotEqual(p.digest, packs.load_pack(root / "mine").digest)

    def problems(self, root, name="mine"):
        with self.assertRaises(packs.PackError) as e:
            packs.load_pack(Path(root) / name)
        return " | ".join(e.exception.problems)

    def test_the_manifest_is_strict(self):
        root = make_pack(extra_manifest='colour = "red"\n')
        self.assertIn("unknown key 'colour'", self.problems(root))
        root = make_pack()
        (root / "mine" / "pack.toml").write_text('name = "other"\nversion = "1"\ndescription = "x"\nkinds = ["classes"]\n')
        self.assertIn("must match the folder name", self.problems(root))
        (root / "mine" / "pack.toml").write_text('name = "mine"\nkinds = ["magic"]\n')
        text = self.problems(root)
        self.assertIn("kinds must be", text)
        self.assertIn("version is required", text)
        self.assertIn("no pack.toml", self.problems(root, "absent") if (root / "absent").mkdir() is None else "")

    def test_folders_and_kinds_must_agree(self):
        root = make_pack(kinds=("classes",))
        (root / "mine" / "policy" / "p.toml").write_text("")
        self.assertIn("does not list 'policy'", self.problems(root))
        root = make_pack(kinds=("classes", "policy"))
        (root / "mine" / "policy" / "p.toml").unlink()
        self.assertIn("no policy/ folder with files", self.problems(root))

    def test_class_files_may_only_contain_classes(self):
        root = make_pack()
        (root / "mine" / "classes" / "c.yaml").write_text(CLASS.format(name="one") + "limits:\n  output_max_chars: 99999999\n")
        self.assertIn("only a `classes:` mapping", self.problems(root))

    def test_a_class_defined_twice_in_one_pack_is_an_error(self):
        root = make_pack()
        (root / "mine" / "classes" / "d.yaml").write_text(CLASS.format(name="one"))
        self.assertIn("defined twice", self.problems(root))

    def test_load_named_reports_a_missing_pack_and_the_wrong_os(self):
        with self.assertRaises(packs.PackError) as e:
            packs.load_named(["nowhere"], [tempfile.mkdtemp()])
        self.assertIn("nowhere", str(e.exception))
        root = make_pack(extra_manifest='[requires]\nos = ["debian"]\n')
        with self.assertRaises(packs.PackError) as e:
            packs.load_named(["mine"], [root], os_id="fedora")
        self.assertIn("fedora", str(e.exception))
        self.assertIn("mine", packs.load_named(["mine"], [root], os_id="debian"))


class PolicyTests(unittest.TestCase):
    def test_deny_param_and_approval_rules_parse(self):
        p = packs.parse_policy('[[deny.param]]\nregex = "x+"\nreason = "r"\n[[require.approval]]\nwhen = { network = true }\nreason = "net"\n')
        self.assertEqual(p.deny_params, [("x+", "r")])
        self.assertEqual(p.require_approval, [({"network": True}, "net")])

    def test_there_is_no_way_to_allow_or_override(self):
        for text, needle in (('[allow]\ncommand = ["sudo"]\n', "unknown key 'allow'"),
                             ('[[deny.command]]\nnames = ["x"]\nreason = "r"\nallow = true\n', "unknown key 'allow'"),
                             ('[override]\nx = 1\n', "unknown key 'override'")):
            with self.assertRaises(packs.PackError) as e:
                packs.parse_policy(text)
            self.assertIn(needle, str(e.exception))

    def test_every_rule_needs_a_reason_and_a_valid_shape(self):
        for text in ('[[deny.command]]\nnames = ["x"]\n', '[[deny.command]]\nnames = ["/bin/x"]\nreason = "r"\n', '[[deny.param]]\nregex = "("\nreason = "r"\n',
                     '[[require.approval]]\nwhen = { colour = "red" }\nreason = "r"\n', '[[require.approval]]\nwhen = {}\nreason = "r"\n'):
            with self.assertRaises(packs.PackError, msg=text):
                packs.parse_policy(text)

    def test_merging_only_adds(self):
        a = packs.parse_policy('[[deny.command]]\nnames = ["x"]\nreason = "first"\n')
        b = packs.parse_policy('[[deny.command]]\nnames = ["x", "y"]\nreason = "second"\n')
        a.add(b)
        self.assertEqual(sorted(a.deny_commands), ["x", "y"])
        self.assertEqual(a.deny_commands["x"], "first")         # the earlier reason stays; nothing is replaced

    def test_apply_policy_refuses_denied_programs_and_forces_approval(self):
        p = packs.parse_policy('[[deny.command]]\nnames = ["nsenter"]\nreason = "no"\n[[require.approval]]\nwhen = { network = true }\nreason = "net"\n')
        classes = {"a": {"tier": "auto", "cost": "light", "run": ["/usr/bin/nsenter", "-t", "1"]},
                   "b": {"tier": "auto", "cost": "light", "network": True, "run": ["/bin/curl"]},
                   "c": {"tier": "auto", "cost": "light", "run": ["/bin/true"]}}
        out, notes, problems = packs.apply_policy(p, classes)
        self.assertEqual(len(problems), 1)
        self.assertIn("nsenter", problems[0])
        self.assertEqual((out["b"]["tier"], out["c"]["tier"]), ("approve", "auto"))
        self.assertEqual(len(notes), 1)
        self.assertEqual(classes["b"]["tier"], "auto")           # the input is not changed

    def test_a_class_without_a_tier_already_asks(self):
        p = packs.parse_policy('[[require.approval]]\nwhen = { kind = "claude" }\nreason = "model"\n')
        _, notes, _ = packs.apply_policy(p, {"xx": {"kind": "claude", "allowed_tools": ["Read"]}})
        self.assertEqual(notes, [])


@unittest.skipUnless(HAVE_YAML, "PyYAML is not installed")
class ComposeTests(unittest.TestCase):
    def load(self, *specs):
        root = Path(tempfile.mkdtemp())
        for kw in specs:
            make_pack(root=root, **kw)
        return packs.load_named([s.get("name", "mine") for s in specs], [root])

    def test_classes_from_packs_and_your_registry_are_composed_with_your_sections_kept(self):
        loaded = self.load({"name": "pa", "classes": ("one",)}, {"name": "pb", "classes": ("two",)})
        reg, _ = packs.compose(loaded, ["pa", "pb"], [], {"classes": {"mine": {"tier": "auto", "cost": "light", "run": ["/bin/true"]}}, "limits": {"output_max_chars": 5000}})
        self.assertEqual(sorted(reg["classes"]), ["mine", "one", "two"])
        self.assertEqual(reg["limits"], {"output_max_chars": 5000})
        self.assertEqual(registry.validate(reg, "/opt/x/tools"), [])

    def test_two_packs_cannot_define_the_same_class(self):
        loaded = self.load({"name": "pa", "classes": ("one",)}, {"name": "pb", "classes": ("one",)})
        with self.assertRaises(packs.PackError) as e:
            packs.compose(loaded, ["pa", "pb"], [], {})
        self.assertIn("both pack 'pa' and pack 'pb'", str(e.exception))

    def test_your_registry_cannot_replace_a_pack_class(self):
        loaded = self.load({"name": "pa", "classes": ("one",)})
        with self.assertRaises(packs.PackError) as e:
            packs.compose(loaded, ["pa"], [], {"classes": {"one": {"tier": "auto", "cost": "light", "run": ["/bin/true"]}}})
        self.assertIn("redefines class 'one'", str(e.exception))

    def test_a_user_policy_section_cannot_loosen_what_packs_set(self):
        loaded = self.load({"name": "pa", "kinds": ("policy",), "classes": ()})
        reg, _ = packs.compose(loaded, [], ["pa"], {"classes": {}, "policy": {"deny_commands": []}})
        self.assertIn("nsenter", reg["policy"]["deny_commands"])
        self.assertIn("sudo", reg["policy"]["deny_commands"])          # the baseline is always there

    def test_a_pack_that_only_has_policy_still_composes_with_the_baseline(self):
        reg, _ = packs.compose({}, [], [], {"classes": {"xx": {"tier": "auto", "cost": "light", "run": ["/bin/true"]}}})
        self.assertEqual(sorted(reg["policy"]["deny_commands"]), sorted(registry.BASELINE_DENY))

    def test_the_core_pack_that_ships_is_valid_and_forbids_privilege_tools(self):
        p = packs.load_pack(ROOT / "packs" / "core")
        reg, _ = packs.compose({"core": p}, ["core"], ["core"], {})
        self.assertEqual(registry.validate(reg, "/opt/x/tools"), [])
        for name in ("sudo", "su", "nsenter", "mount"):
            self.assertIn(name, reg["policy"]["deny_commands"])
        self.assertEqual(reg["classes"]["profile-run"]["kind"], "profile")


class RuntimeEnforcement(unittest.TestCase):
    """What the worker does with the composed policy at job time."""
    POLICY = registry.policy_of({"policy": {"deny_commands": ["nsenter"], "deny_params": [{"regex": r"(^|/)\.\.(/|$)", "reason": "no climbing out"}]}})

    def test_policy_of_always_includes_the_baseline_and_ignores_bad_patterns(self):
        self.assertEqual(registry.policy_of({})["deny_commands"], set(registry.BASELINE_DENY))
        p = registry.policy_of({"policy": {"deny_params": [{"regex": "("}, {"regex": "ok"}]}})
        self.assertEqual(len(p["deny_params"]), 1)

    def test_a_param_that_matches_a_deny_pattern_is_refused_with_the_reason(self):
        cls = {"run": ["/bin/cat", "{path}"], "params": {"path": {"regex": r"[a-z./]+"}}}
        self.assertEqual(jobs.build_argv(cls, {"path": "ok/file"}, self.POLICY), ["/bin/cat", "ok/file"])
        with self.assertRaises(ValueError) as e:
            jobs.build_argv(cls, {"path": "../etc/x"}, self.POLICY)
        self.assertIn("no climbing out", str(e.exception))

    def test_without_a_policy_nothing_changes(self):
        cls = {"run": ["/bin/cat", "{path}"], "params": {"path": {"regex": r"[a-z./]+"}}}
        self.assertEqual(jobs.build_argv(cls, {"path": "../x"}), ["/bin/cat", "../x"])

    def test_a_profile_run_of_a_policy_denied_program_is_refused(self):
        job = {"argv": ["/usr/bin/nsenter", "-t", "1"], "what": "w", "expect": "e", "est_time_s": 1, "est_mem_mb": 1}
        with self.assertRaises(ValueError) as e:
            jobs.build_profile(job, {"kind": "profile"}, registry.limits_of({}), is_executable=lambda p: True, policy=self.POLICY)
        self.assertIn("nsenter is not allowed", str(e.exception))

    def test_a_profile_argument_matching_a_deny_pattern_is_refused(self):
        job = {"argv": ["/bin/cat", "../secret"], "what": "w", "expect": "e", "est_time_s": 1, "est_mem_mb": 1}
        with self.assertRaises(ValueError) as e:
            jobs.build_profile(job, {"kind": "profile"}, registry.limits_of({}), is_executable=lambda p: True, policy=self.POLICY)
        self.assertIn("no climbing out", str(e.exception))

    def test_the_policy_section_itself_is_validated(self):
        base = {"classes": {"xx": {"tier": "auto", "cost": "light", "run": ["/bin/true"]}}}
        self.assertEqual(registry.validate({**base, "policy": {"deny_commands": ["x"], "deny_params": [{"regex": "a"}]}}, "/t"), [])
        for bad in ({"allow": []}, {"deny_commands": "sudo"}, {"deny_params": [{"regex": "("}]}, {"deny_params": ["x"]}):
            self.assertTrue(registry.validate({**base, "policy": bad}, "/t"), bad)


@unittest.skipUnless(HAVE_YAML, "PyYAML is not installed")
class WorkerUsesThePolicy(support.Base):
    def test_a_job_whose_parameter_breaks_a_policy_rule_is_rejected_by_the_worker(self):
        self.registry_path.write_text(support.REGISTRY + "  pathy:\n    tier: auto\n    cost: light\n    run: [/bin/echo, \"{p}\"]\n    params: {p: {regex: \"[a-z./]+\"}}\n"
                                      "policy:\n  deny_commands: [nsenter]\n  deny_params:\n    - {regex: '(^|/)\\.\\.(/|$)', reason: no climbing out}\n")
        self.job("job-bad1", cls="pathy", params={"p": "../etc"})
        self.job("job-good", cls="pathy", params={"p": "fine"})
        self.run_all()
        bad, good = self.result("job-bad1"), self.result("job-good")
        self.assertEqual(bad["status"], "rejected")
        self.assertIn("no climbing out", str(bad))
        self.assertEqual(good["status"], "ok")


@unittest.skipUnless(HAVE_YAML, "PyYAML is not installed")
class CheckCommand(unittest.TestCase):
    def test_check_reports_ok_and_failures(self):
        good, bad = make_pack(kinds=("classes", "policy")), make_pack(extra_manifest='colour = "red"\n')
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(packs.main(["check", str(good / "mine")]), 0)
            self.assertEqual(packs.main(["check", str(good / "mine"), str(bad / "mine")]), 1)
        self.assertIn("OK    mine 1.0", out.getvalue())
        self.assertIn("unknown key 'colour'", err.getvalue())
        with redirect_stderr(io.StringIO()):
            self.assertEqual(packs.main([]), 2)


if __name__ == "__main__":
    unittest.main()
