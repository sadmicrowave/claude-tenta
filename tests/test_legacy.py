"""ct-setup notices what is left of the install this project came from, and names the one command that clears each leftover; it removes nothing itself."""
import json
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401

from ct.setup import legacy
from ct.setup.host import Host


def host_with(files=(), dirs=(), path=()):
    root = Path(tempfile.mkdtemp(prefix="ct-legacy-"))
    h = Host(root, env={"HOME": "/h", "PATH": ":".join(path) or "/nonexistent"})
    for p, text in dict(files).items():
        h.write(p, text)
    for d in dirs:
        h.path(d).mkdir(parents=True, exist_ok=True)
    return h


class Find(unittest.TestCase):
    def test_a_clean_computer_has_nothing_to_report(self):
        self.assertEqual(legacy.find(host_with({"/h/.zshrc": 'export PATH="$HOME/.local/bin:$PATH"\n# old: .cw-venv\n'})), [])

    def test_every_kind_of_leftover_is_found_with_its_own_command(self):
        hooks = json.dumps({"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "python3 ~/Shared/old/queue/client/cw-log hook pre"}]}]}})
        h = host_with({"/h/.zshrc": 'export CW_HOME=~/Shared/old\nexport PATH="$HOME/.cw-venv/bin:$PATH"\n', "/h/.claude/settings.json": hooks,
                       "/h/.claude/skills/worker-offload/SKILL.md": "use cw-run"}, dirs=["/h/.cw-venv", "/h/.cw", "/h/.config/claude-ballast"])
        found = legacy.find(h)
        text = "\n".join(f"{w} | {c}" for w, c in found)
        self.assertIn("~/.zshrc line 1, 2", text)
        self.assertIn("ct-run offload log hook pre", text)
        for fix in ("rm -rf ~/.claude/skills/worker-offload", "rm -rf ~/.cw-venv", "rm -rf ~/.cw", "rm -rf ~/.config/claude-ballast"):
            self.assertIn(fix, text)
        self.assertEqual(len(found), 6)
        self.assertTrue(h.exists("/h/.cw-venv"))                                    # looking never removes anything

    def test_old_commands_still_on_the_path_are_named(self):
        h = host_with(dirs=["/h/bin"], path=["/h/bin"])
        h.write("/h/bin/cw-admin", "x")
        h.env["PATH"] = str(h.path("/h/bin"))
        self.assertIn("cw-admin", legacy.find(h)[0][0])


if __name__ == "__main__":
    unittest.main()
