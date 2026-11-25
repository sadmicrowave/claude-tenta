"""Shared by test_offload_e2e and test_offload_autoadopt."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import RepoFixture, sh  # noqa: E402,F401


class OffloadFixture(RepoFixture):
    """The worker fixture plus the helpers every offload end-to-end test uses."""

    def push_main_registry(self, text="classes:\n  existing-one:\n    tier: auto\n    cost: light\n    run: [\"/bin/true\"]\n"):
        req = self.tmp / "requester"
        sh("git", "checkout", "-q", "-B", "main", "origin/main", cwd=req)
        (req / "registry.yaml").write_text(text)
        sh("git", "add", "-A", cwd=req)
        sh("git", "commit", "-q", "-m", "registry", cwd=req)
        sh("git", "push", "-q", "origin", "main", cwd=req)

    def profile(self, *extra, argv=("ls", "-l")):
        args = ["offload", "profile", "--what", "lists files", "--expect", "instant", "--est-time-s", "1", "--est-mem-mb", "10", *extra, "--", *argv]
        return self.run_cli(*args)

    def run_profile_to_a_result(self, *extra):
        code, out, err = self.profile(*extra)
        self.assertEqual(code, 0, out + err)
        jid = out.strip()
        self.w.fetch()
        self.assertEqual(self.result(jid)["status"], "awaiting_approval")
        self.assertEqual(self.w.approve(jid), (True, ""))
        self.w.fetch()
        self.w.execute(jid)
        return jid
