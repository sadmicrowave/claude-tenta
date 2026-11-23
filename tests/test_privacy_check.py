"""The privacy check's history scan follows the same skip rule as the staged and whole-tree scans: recorded tool output under tests/fixtures/ (the host keys of throwaway containers) is not a leak.

Found by CI: it failed on the public host keys in the recorded fixtures, which the commit-time check had skipped."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "privacy-check"
KEY = "ssh-" + "ed25519 " + "AAAAC3NzaC1lZDI1NTE5AAAAIHydw7kP9aelENfu8kRyX7cP0ynpRXnqVcLiyRjZ75AD"        # built in pieces: this file must not itself look like a leak


def repo_with(path):
    d = Path(tempfile.mkdtemp())
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@users.noreply.github.com", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@users.noreply.github.com", "CT_PRIVACY_DENYLIST": str(d / "none")}
    for argv in (["init", "-q"], ["config", "commit.gpgsign", "false"]):
        subprocess.run(["git", "-C", str(d), *argv], check=True, env=env)
    (d / path).parent.mkdir(parents=True, exist_ok=True)
    (d / path).write_text(f"127.0.0.1 {KEY}\n")
    subprocess.run(["git", "-C", str(d), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(d), "commit", "-q", "-m", "x"], check=True, env=env)
    return d, env


def history(d, env):
    return subprocess.run([sys.executable, str(SCRIPT), "--history"], cwd=d, env=env, capture_output=True, text=True)


def merged_on_github(committer_email):
    """A repository whose last commit has a noreply author and the given committer, as a merge made in GitHub's own interface has."""
    d, env = repo_with("src/ok.txt")
    (d / "src" / "ok.txt").write_text("nothing private\n")
    subprocess.run(["git", "-C", str(d), "commit", "-q", "-a", "-m", "y", "--amend", "--reset-author"], check=True, env=env)
    env2 = {**env, "GIT_COMMITTER_NAME": "GitHub", "GIT_COMMITTER_EMAIL": committer_email}
    subprocess.run(["git", "-C", str(d), "commit", "-q", "--amend", "--no-edit"], check=True, env=env2)
    return d, env


class History(unittest.TestCase):
    def test_github_s_own_web_merge_identity_is_accepted_and_any_other_committer_is_not(self):
        d, env = merged_on_github("noreply" + "@" + "github.com")
        self.assertEqual(history(d, env).returncode, 0, history(d, env).stderr)
        d, env = merged_on_github("someone" + "@" + "gmail.com")
        r = history(d, env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("not a GitHub noreply address", r.stderr)

    def test_a_host_key_in_a_recorded_fixture_is_not_a_leak(self):
        d, env = repo_with("tests/fixtures/real/x/keyscan.txt")
        r = history(d, env)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_the_vendored_third_party_ignore_templates_are_not_scanned(self):
        d, env = repo_with("src/ct/client/ignore_templates/lang/Python.gitignore")
        self.assertEqual(history(d, env).returncode, 0)

    def _with_message(self, message):
        d, env = repo_with("tests/fixtures/ok.txt")
        deny = Path(tempfile.mkdtemp()) / "deny.txt"                                       # outside the repository: inside, its own patch would match
        deny.write_text("acctname\n")
        env = {**env, "CT_PRIVACY_DENYLIST": str(deny)}
        (d / "more.txt").write_text("more\n")
        subprocess.run(["git", "-C", str(d), "add", "-A"], check=True, env=env)
        subprocess.run(["git", "-C", str(d), "commit", "-q", "-m", message], check=True, env=env)
        return history(d, env)

    def test_githubs_merge_subject_naming_the_account_is_not_a_leak(self):
        self.assertEqual(self._with_message("Merge pull request #5 from acctname/some-branch\n\nA clean title").returncode, 0)

    def test_the_account_anywhere_else_in_a_message_is_still_caught(self):
        for m in ("fixes for acctname", "Merge pull request #5 from acctname/b\n\nthanks acctname", "see Merge pull request #5 from acctname/b", "Merge pull request #5 from acctname/b and acctname"):
            self.assertEqual(self._with_message(m).returncode, 1, m)

    def test_the_same_text_anywhere_else_is_still_caught(self):
        d, env = repo_with("src/notes.txt")
        r = history(d, env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("ssh-public-key", r.stderr)


if __name__ == "__main__":
    unittest.main()
