"""The worker's runtime modules import on a computer that has none of the POSIX-only Python modules (fcntl, pwd, grp, resource, termios, tty, pty). On Windows that is every import; here the same is proved by
refusing those imports while each module is loaded afresh. A module that fails is one the Windows port could not even start. Deliberately independent of `support`, so the Windows CI job can run it alone."""
import importlib
import importlib.abc
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

ROOT = Path(__file__).resolve().parents[1] / "src" / "armada"
POSIX_ONLY = {"fcntl", "pwd", "grp", "resource", "termios", "tty", "pty", "syslog"}
NOT_THE_WORKER = {"jail", "sandboxtest", "canarywatch", "helper", "privileged", "update", "firewall", "mailshell", "clonediag", "rediscover", "__main__"}      # Linux by nature (see test_platform_seam)


class Refuse(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name.split(".")[0] in POSIX_ONLY:
            raise ImportError(f"No module named {name!r} (refused: this test pretends to be Windows)")
        return None


class WorkerModulesImportWithoutPosix(unittest.TestCase):
    def test_every_module_of_the_workers_runtime_imports(self):
        names = [f.stem for f in sorted(ROOT.glob("*.py")) if f.stem not in NOT_THE_WORKER]
        saved = {k: sys.modules.pop(k) for k in list(sys.modules) if k in POSIX_ONLY or k.split(".")[0] == "armada"}
        finder = Refuse()
        sys.meta_path.insert(0, finder)
        failed = []
        try:
            for n in names:
                try:
                    importlib.import_module(f"armada.{n}")
                except ImportError as e:
                    failed.append(f"armada.{n}: {e}")
        finally:
            sys.meta_path.remove(finder)
            for k in [k for k in sys.modules if k.split(".")[0] == "armada"]:
                del sys.modules[k]
            sys.modules.update(saved)
        self.assertEqual(failed, [], "these modules need a POSIX-only Python module at import time: call armada.plat from inside a function instead")


if __name__ == "__main__":
    unittest.main()
