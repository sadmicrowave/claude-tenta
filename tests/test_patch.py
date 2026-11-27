import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401
from ct import patch


def tree(files):
    d = Path(tempfile.mkdtemp())
    for name, text in files.items():
        (d / name).parent.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(text)
    return d


def apply(text, into):
    return subprocess.run(["git", "apply", "-"], input=text, text=True, capture_output=True, cwd=into)


class Snapshot(unittest.TestCase):
    def test_text_files_are_recorded_and_the_git_folder_is_not(self):
        d = tree({"a.txt": "x\n", "sub/b.py": "y\n", ".git/config": "[core]\n"})
        files, notes = patch.snapshot(d)
        self.assertEqual(sorted(files), ["a.txt", "sub/b.py"])

    def test_links_binary_and_oversize_files_are_left_out_with_a_note_and_a_link_is_never_followed(self):
        d = tree({"ok.txt": "x\n"})
        (d / "bin").write_bytes(b"\xff\xfe\x00\x01")
        (d / "big").write_text("a" * (patch.MAX_FILE_BYTES + 1))
        os.symlink("/etc/passwd", d / "link")
        files, notes = patch.snapshot(d)
        self.assertEqual(list(files), ["ok.txt"])
        text = " ".join(notes)
        self.assertIn("link is a symbolic link", text)
        self.assertIn("bin is not text", text)
        self.assertIn("big is over", text)


class Make(unittest.TestCase):
    def roundtrip(self, before, after):
        d = tree(before)
        b, _ = patch.snapshot(d)
        e = tree(after)
        a, _ = patch.snapshot(e)
        text, changed, note = patch.make(b, a, 100_000)
        r = apply(text, d)
        self.assertEqual(r.returncode, 0, r.stderr)
        got, _ = patch.snapshot(d)
        self.assertEqual(got, a)
        return text, changed

    def test_a_patch_git_apply_accepts_for_a_change_an_addition_and_a_deletion(self):
        text, changed = self.roundtrip({"keep.txt": "1\n2\n3\n", "gone.txt": "bye\n", "same.txt": "s\n"},
                                       {"keep.txt": "1\ntwo\n3\n", "new.txt": "hello\n", "same.txt": "s\n"})
        self.assertEqual(changed, ["gone.txt", "keep.txt", "new.txt"])
        self.assertIn("new file mode 100644", text)
        self.assertIn("deleted file mode 100644", text)

    def test_a_file_with_no_trailing_newline_still_applies(self):
        self.roundtrip({"a": "x"}, {"a": "y"})

    def test_no_change_is_an_empty_patch(self):
        text, changed, note = patch.make({"a": "x\n"}, {"a": "x\n"}, 1000)
        self.assertEqual((text, changed, note), ("", [], ""))

    def test_a_patch_over_the_limit_is_not_returned_half_and_says_so(self):
        text, changed, note = patch.make({}, {"a": "line\n" * 5000}, 1000)
        self.assertEqual(text, "")
        self.assertEqual(changed, ["a"])
        self.assertIn("over the class's limit", note)


class Problems(unittest.TestCase):
    def test_a_clean_patch_has_none(self):
        self.assertEqual(patch.problems(patch.make({}, {"a/b.txt": "x\n"}, 1000)[0]), [])

    def test_paths_outside_the_folder_or_inside_git_are_refused_whatever_the_worker_said(self):
        for path in ("../etc/passwd", "/etc/passwd", ".git/hooks/pre-commit", "x/../../y"):
            text = f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-a\n+b\n"
            self.assertTrue(patch.problems(text), path)

    def test_links_binary_changes_and_renames_are_refused(self):
        for text in ("diff --git a/x b/x\nnew file mode 120000\n", "GIT binary patch\nliteral 1\n", "Binary files a/x and b/x differ\n", "rename from a\nrename to b\n"):
            self.assertTrue(patch.problems(text), text)


if __name__ == "__main__":
    unittest.main()
