"""Which tests are worth running for a change: the table behind scripts/test-plan.

A change to a file maps to (a) the unit modules named after it, (b) the probes (one environment fact in a throwaway container) and (c) the e2e routes whose behaviour it can change.
Anything that matches no rule is "unmapped": the planner then says so and falls back to everything, and tests/test_impact.py fails until the file gets a rule, so the table cannot rot.
"""
import fnmatch
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALL_ROUTES = ("install", "direct", "locate", "switch", "move", "rename", "uninstall", "shared", "special", "upgrade")
ALL_PROBES = ("ownership", "release", "kernel", "locate", "fixes", "tooldirs")

# (glob on the path from the repository root, extra unit modules, probes, routes). First match that fits adds to the plan; every matching rule counts.
RULES = [
    ("tests/themes.toml", ("test_impact",), (), ()),
    ("scripts/test-gate", ("test_impact",), (), ()),
    ("scripts/tparallel.py", ("test_parallel_runner",), (), ()),
    ("tests/serial.txt", ("test_parallel_runner",), (), ()),
    ("src/armada/transport.py", ("test_transport", "test_connection"), ("ownership",), ("direct", "install")),
    ("src/armada/rediscover.py", ("test_rediscover", "test_admin_workers"), ("locate",), ("locate",)),
    ("src/armada/mailshell.py", ("test_mailshell",), ("ownership",), ("direct",)),
    ("src/armada/clonediag.py", ("test_clonediag", "test_real_outputs"), ("ownership",), ("direct",)),
    ("src/armada/render.py", ("test_render",), ("kernel",), ("install", "upgrade")),
    ("templates/*", ("test_render",), ("kernel",), ("install", "direct", "upgrade")),
    ("src/armada/update.py", ("test_update",), (), ("upgrade",)),
    ("src/armada/setup/hardening.py", ("test_install",), ("kernel", "release"), ("install", "direct", "shared", "special")),
    ("src/armada/setup/{discover,assess,addworker}.py", ("test_real_outputs", "test_assess", "test_discover"), ("release", "fixes"), ("direct", "locate")),
    ("src/armada/setup/detect.py", ("test_real_outputs",), ("release",), ("install",)),
    ("src/armada/setup/{questions,server}.py", (), (), ("shared", "special")),
    ("src/armada/setup/*", (), (), ("install", "direct", "switch", "move", "rename", "uninstall")),
    ("src/armada/admin/*", (), (), ("switch", "move", "rename", "uninstall")),
    ("src/armada/client/*", (), (), ("install", "direct")),
    ("src/armada/sandboxtest.py", ("test_connection",), ("kernel",), ("install",)),
    ("src/armada/jail.py", (), ("kernel", "tooldirs"), ("install",)),
    ("src/armada/{runner,worker}.py", (), ("tooldirs",), ()),
    ("src/armada/*", (), (), ()),                                    # every other module of the package: its own unit tests are enough
    ("tests/e2e/lib.sh", ("test_e2e_lib",), (), ALL_ROUTES),
    ("tests/e2e/baseline.sh", ("test_e2e_lib",), (), ALL_ROUTES),
    ("tests/e2e/routes/*.sh", (), (), ()),                       # the route itself is added by name below
    ("tests/probes/*.sh", ("test_probes",), (), ()),             # the probe itself is added by name below
    ("tests/fixtures/*", ("test_real_outputs",), (), ()),
    ("tests/*.py", (), (), ()),                                  # a test file runs itself
    ("scripts/*", (), (), ()),
    ("docs/*", ("test_docs",), (), ()),
    ("*.md", ("test_docs",), (), ()),
    ("pyproject.toml", (), (), ()),
    ("packs/*", ("test_packs",), (), ()),
    ("skills/*", (), (), ()),
    ("LICENSE", (), (), ()),
    ("config/*", ("test_config",), (), ()),
    ("tests/e2e/smoke*.sh", (), (), ()),
    (".*", (), (), ()),
]


def expand(pattern):
    """fnmatch has no braces: 'a/{x,y}.py' -> ['a/x.py', 'a/y.py']."""
    if "{" not in pattern:
        return [pattern]
    head, rest = pattern.split("{", 1)
    options, tail = rest.split("}", 1)
    return [head + o + tail for o in options.split(",")]


def matches(path, pattern):
    return any(fnmatch.fnmatch(path, p) for p in expand(pattern))


def plan(changed, tests_dir=None):
    """changed paths -> {'unit': [...], 'probes': [...], 'routes': [...], 'unmapped': [...]}. Pure: nothing is run."""
    tests_dir = Path(tests_dir or ROOT / "tests")
    unit, probes, routes, unmapped = set(), set(), set(), []
    for path in changed:
        hit = False
        for pattern, u, p, r in RULES:
            if matches(path, pattern):
                hit = True
                unit.update(u), probes.update(p), routes.update(r)
        if not hit:
            unmapped.append(path)
        stem = Path(path).stem
        if path.endswith(".py") and (tests_dir / f"test_{stem}.py").is_file() and (path.startswith("src/") or path.startswith("tests/") and stem.startswith("test_") is False):
            unit.add(f"test_{stem}")
        if path.startswith("tests/test_") and path.endswith(".py"):
            unit.add(stem)
        if path.startswith("tests/e2e/routes/") and path.endswith(".sh"):
            routes.add(stem)
        if path.startswith("tests/probes/") and path.endswith(".sh") and stem != "record":
            probes.add(stem)
    if unmapped:                                                 # never guess smaller than the truth: a file the table does not know could affect anything
        routes.update(ALL_ROUTES)
        probes.update(ALL_PROBES)
    return {"unit": sorted(unit), "probes": sorted(probes), "routes": sorted(r for r in ALL_ROUTES if r in routes), "unmapped": unmapped}


def theme_gate(changed, base_ref, manifest_path=None, tests_dir=None):
    """The unit modules that gate a pull request whose base is `base_ref`: the `always` guards, the modules the changed files map to, and, when the base is a
    theme branch listed in tests/themes.toml, that theme's own modules. Returns {'modules': [...], 'theme': name or None, 'full': bool}.
    A base that is not a listed theme (main, a release) is gated by the FULL suite: nothing is guessed smaller than the truth."""
    import tomllib
    tests_dir = Path(tests_dir or ROOT / "tests")
    data = tomllib.loads(Path(manifest_path or tests_dir / "themes.toml").read_text())
    theme = next((n for n, t in data.items() if isinstance(t, dict) and any(fnmatch.fnmatch(base_ref, b) for b in t.get("branches", []))), None)
    if theme is None:
        return {"modules": [], "theme": None, "full": True}
    p = plan(changed, tests_dir)
    mods = set(data.get("always", [])) | set(p["unit"]) | set(data[theme].get("modules", []))
    return {"modules": sorted(m for m in mods if (tests_dir / f"{m}.py").is_file()), "theme": theme, "full": bool(p["unmapped"])}
