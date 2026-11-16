"""The terminal screens: what is on screen after each answer, checked with a small virtual terminal that obeys the cursor and erase sequences the wizard uses."""
import io
import re
import unittest

import support  # noqa: F401  (points HOME at a throwaway folder)

from cw.setup import hardening, questions, tui
from cw.setup.hardening import Action, Check
from test_setup import FakeHost, cfg_of


class Screen:
    """Enough of a terminal for the wizard: text, newline, carriage return, cursor up, erase to the end of the screen. Colour codes are ignored."""

    def __init__(self):
        self.lines, self.r, self.c = [""], 0, 0

    def feed(self, s):
        i = 0
        while i < len(s):
            ch = s[i]
            if ch == "\x1b" and s[i + 1:i + 2] == "[":
                m = re.match(r"\x1b\[([0-9;]*)([A-Za-z])", s[i:])
                n = int(m.group(1) or 1) if m.group(1).isdigit() or not m.group(1) else 1
                if m.group(2) == "A":
                    self.r = max(0, self.r - n)
                elif m.group(2) == "J":
                    self.lines = self.lines[:self.r + 1]
                    self.lines[self.r] = self.lines[self.r][:self.c]
                i += len(m.group(0))
                continue
            if ch == "\r":
                self.c = 0
            elif ch == "\n":
                self.r += 1
                self.c = 0
                while len(self.lines) <= self.r:
                    self.lines.append("")
            else:
                line = self.lines[self.r].ljust(self.c)
                self.lines[self.r] = line[:self.c] + ch + line[self.c + 1:]
                self.c += 1
            i += 1

    def text(self):
        return "\n".join(line.rstrip() for line in self.lines).rstrip("\n")


class Terminal(io.StringIO):
    def __init__(self):
        super().__init__()
        self.screen = Screen()

    def write(self, s):
        self.screen.feed(s)
        return super().write(s)


def prompter(answers=(), keys=(), width=76, rows=40):
    out, answers = Terminal(), list(answers)

    def ask(prompt):
        a = answers.pop(0)
        out.write(prompt.replace("\x01", "").replace("\x02", "") + a + "\n")
        return a
    p = tui.TuiPrompter(ask=ask, secret=ask, out=out, width=width, rows=rows, keys=iter(keys))
    return p, out


def question(key="server.host"):
    return questions.BY_KEY[key]


class Progress(unittest.TestCase):
    def test_the_bar_fills_as_questions_are_answered(self):
        p, out = prompter(["worker.example"])
        p.begin(4, "cw-setup install")
        p.ask(question(), "", None, shown="")
        top = out.screen.lines[0]
        self.assertIn("cw-setup install", top)
        self.assertIn("0 of 4", top)
        self.assertNotIn("━", top)
        p.answered(question(), "worker.example")
        p.done = 2
        self.assertIn("2 of 4", p.progress())
        self.assertEqual(tui.strip(p.progress()).count("━") + tui.strip(p.progress()).count("╸"), 12)       # half of 24
        p.done = 4
        self.assertEqual(tui.strip(p.progress()).count("─"), 0)

    def test_the_progress_line_is_as_wide_as_the_screen(self):
        p, _ = prompter(width=76)
        p.begin(19)
        p.done = 8
        self.assertEqual(tui.vlen(p.progress()), 76)


class ActiveQuestion(unittest.TestCase):
    def test_the_question_has_a_rail_a_chip_the_help_and_an_input_line(self):
        p, out = prompter(["agent"])
        p.begin(19, "cw-setup install")
        text = p.ask(question("server.admin_user"), "", None, shown="")
        screen = out.screen.text()
        self.assertEqual(text, "agent")
        lines = screen.splitlines()
        self.assertIn(" SERVER ", screen)
        self.assertIn("1 of 19", screen)
        self.assertIn("Which account on the server is yours", screen)
        self.assertIn("┃ ❯ agent", screen)
        for line in lines[2:]:
            if line.strip():
                self.assertTrue(line.startswith("┃"), repr(line))                 # every line of the block is on the rail
        self.assertIn("needs an answer", screen)                                  # a required question with no default says so

    def test_the_default_is_shown_as_a_hint_and_enter_keeps_it(self):
        p, out = prompter([""])
        p.begin(3)
        p.ask(question("server.ssh_port"), 22, None, shown="22")
        self.assertIn("Enter keeps: 22", out.screen.text())

    def test_choices_and_problems_are_shown(self):
        p, out = prompter([""])
        p.begin(3)
        p.ask(question("network.ipv6"), "enabled", ["enabled", "disabled"], problem="must be one of enabled, disabled", shown="enabled")
        screen = out.screen.text()
        self.assertIn("Choices: enabled, disabled", screen)
        self.assertIn("That did not work: must be one of enabled, disabled", screen)

    def test_answering_erases_the_question_and_leaves_one_line_per_answer(self):
        p, out = prompter(["worker.example", "agent", "someone"])
        p.begin(3, "cw-setup install")
        for key, val in (("server.host", "worker.example"), ("server.admin_user", "agent"), ("github.owner", "someone")):
            q = question(key)
            p.ask(q, "", None, shown="")
            p.answered(q, val)
        p.finish()
        screen = out.screen.text()
        self.assertNotIn("Which account on the server", screen)                   # the questions themselves are gone
        for label, value in (("Server address", "worker.example"), ("Admin account", "agent"), ("GitHub account", "someone")):
            self.assertRegex(screen, rf"✓ {label}\s+{value}")
        self.assertIn("3 of 3", screen)
        self.assertEqual(len([ln for ln in screen.splitlines() if "✓" in ln]), 3)

    def test_only_the_last_few_answers_stay_above_the_active_question_and_the_rest_are_counted(self):
        p, out = prompter(["x"] * 9)
        p.begin(9)
        keys = ["identity.name", "server.host", "server.admin_user", "server.worker_user", "server.ssh_port", "github.owner"]
        for key in keys:
            q = question(key)
            p.ask(q, "", None, shown="")
            p.answered(q, "v-" + key)
        p.ask(question("github.queue_repo"), "cw-queue", None, shown="cw-queue")
        screen = out.screen.text()
        self.assertEqual(len([ln for ln in screen.splitlines() if "✓" in ln]), tui.WINDOW)
        self.assertIn("… 2 earlier answers", screen)
        p.finish()
        self.assertEqual(len([ln for ln in out.screen.text().splitlines() if "✓" in ln]), 6)        # the review at the end lists every one

    def test_an_empty_answer_reads_as_none_and_labels_never_touch_their_values(self):
        p, out = prompter([""])
        p.begin(1)
        q = question("network.lan_allow_out")
        p.ask(q, [], None, shown="")
        p.answered(q, "")
        longest = max(questions.QUESTIONS, key=lambda x: len(x["label"]))
        p.items.append((longest["label"], "value"))
        p.finish()
        screen = out.screen.text()
        self.assertRegex(screen, r"✓ LAN exceptions\s+\(none\)")
        self.assertRegex(screen, rf"✓ {re.escape(longest['label'])}\s{{2,}}value")

    def test_a_secret_is_typed_without_echo_and_shown_as_set(self):
        p, out = prompter(["https://ntfy.example/private-topic"])
        p.begin(1)
        q = question("alerts.url")
        self.assertTrue(q["secret"])
        text = p.ask(q, "", None, shown="")
        p.answered(q, text)
        p.finish()
        self.assertRegex(out.screen.text(), r"✓ Alert address\s+set")
        self.assertNotIn("private-topic", out.screen.text().split("Alert address")[-1])

    def test_the_prompt_is_readline_safe_for_input_and_plain_for_secrets(self):
        p, _ = prompter()
        self.assertIn("\x01", p.prompt(wrap=True))
        self.assertNotIn("\x01", p.prompt(wrap=False))
        self.assertEqual(tui.strip(p.prompt(wrap=False).replace("\x01", "").replace("\x02", "")), "┃ ❯ ")

    def test_every_question_has_a_short_label_and_every_section_a_chip(self):
        for q in questions.QUESTIONS:
            self.assertTrue(q["label"] and len(q["label"]) <= 22, q["key"])
            self.assertIn(q["key"].partition(".")[0], tui.SECTIONS, q["key"])

    def test_no_screen_without_a_terminal(self):
        class NotATty(io.StringIO):
            def isatty(self):
                return False
        self.assertIsNone(tui.make(ask=input, interactive=True, stream=NotATty()))
        self.assertIsNone(tui.make(ask=input, interactive=False))


class Collecting(unittest.TestCase):
    def test_collect_drives_the_screen_and_counts_only_what_was_asked(self):
        p, out = prompter(["", "agent", "", "someone", "", "", ""])        # install name, admin, port, owner, queue, results, release source
        given = {"server.host": "worker.example"}
        total = questions.count_to_ask(("client",), given)
        p.begin(total, "cw-setup client")
        nested, missing = questions.collect("client", ask=None, ui=p, given=given)
        p.finish()
        self.assertEqual(missing, [])
        self.assertEqual(nested["server"]["admin_user"], "agent")
        screen = out.screen.text()
        self.assertIn("✓ Admin account", screen)
        self.assertNotIn("Server address", screen)                                 # given, not asked, so no line for it

    def test_a_bad_answer_is_asked_again_in_place_with_the_reason(self):
        p, out = prompter(["BAD USER!", "agent"])
        p.begin(1)
        nested, _ = questions.collect("server", ask=None, ui=p, only={"server.admin_user"})
        self.assertEqual(nested["server"]["admin_user"], "agent")
        self.assertEqual(len([ln for ln in out.screen.text().splitlines() if "Which account" in ln]), 1)

    def test_count_to_ask_skips_answered_and_inapplicable_questions(self):
        all_server = questions.count_to_ask(("server",), {})
        fewer = questions.count_to_ask(("server",), {"server.admin_user": "a", "network.lan_deny_out": False})
        self.assertEqual(all_server - fewer, 4)         # the answered one, the one given, and the two that only apply when the LAN is blocked


def info(**over):
    base = {"id": "ssh-keys-only", "title": "SSH logins with keys only", "why": "A password can be guessed; a key file cannot be typed by an attacker. This turns off password logins.",
            "risk": "If your key is not installed you would lock yourself out.", "now": "password logins are on", "index": 2, "total": 5, "lockout": True,
            "steps": ["write /etc/ssh/sshd_config.d/10-x.conf (mode 644):\n    PasswordAuthentication no", "sshd -t", "systemctl reload ssh"]}
    base.update(over)
    return base


class Protection(unittest.TestCase):
    def test_the_card_is_a_closed_frame_of_equal_width_lines(self):
        p, _ = prompter(width=76)
        lines = [tui.strip(x) for x in p.card(info(), 0, False)]
        self.assertEqual({len(x) for x in lines}, {76})
        self.assertTrue(lines[0].startswith("╭─ Protection 2 of 5") and lines[0].endswith("could lock you out ─╮"))
        self.assertTrue(lines[-1].startswith("╰─") and lines[-1].endswith("─╯"))
        for x in lines[1:-1]:
            self.assertTrue(x.startswith("│") and x.endswith("│"), x)

    def test_it_shows_what_it_does_the_risk_and_the_steps(self):
        p, _ = prompter()
        text = "\n".join(tui.strip(x) for x in p.card(info(), 0, False))
        for needle in ("SSH logins with keys only", "Risk", "lock yourself out", "Now", "password logins are on", "$ sshd -t", "$ systemctl reload ssh", "Apply", "Skip"):
            self.assertIn(needle, text)

    def test_more_than_four_steps_are_folded_until_s_is_pressed(self):
        many = info(steps=[f"step {i}" for i in range(7)])
        p, _ = prompter()
        folded = "\n".join(tui.strip(x) for x in p.card(many, 0, False))
        self.assertIn("and 3 more steps", folded)
        self.assertNotIn("step 6", folded)
        self.assertIn("step 6", "\n".join(tui.strip(x) for x in p.card(many, 0, True)))

    def test_a_protection_that_cannot_lock_you_out_says_recommended(self):
        p, _ = prompter()
        self.assertIn("recommended", tui.strip(p.card(info(lockout=False), 0, False)[0]))

    def test_keys_choose_and_confirm(self):
        for keys, want in ((["enter"], True), (["y"], True), (["n"], False), (["right", "enter"], False), (["left", "enter"], False), (["tab", "tab", "enter"], True),
                           (["s", "s", "enter"], True), (["right", "left", "enter"], True)):
            p, out = prompter(keys=keys)
            self.assertEqual(p.protection(info()), want, keys)

    def test_the_card_is_erased_when_you_decide_and_the_screen_stays_clean(self):
        p, out = prompter(keys=["right", "enter"])
        before = out.screen.text()
        p.protection(info())
        self.assertEqual(out.screen.text(), before)

    def test_changing_the_choice_redraws_in_place(self):
        p, out = prompter(keys=["right", "right", "enter"])
        p.protection(info())
        self.assertEqual(tui.strip(out.getvalue()).count("╭─ Protection"), 3)                  # drawn three times, always from the same place
        self.assertEqual(out.screen.text(), "")

    def test_ctrl_c_stops_cleanly(self):
        p, out = prompter(keys=["ctrl-c"])
        with self.assertRaises(KeyboardInterrupt):
            p.protection(info())
        self.assertEqual(out.screen.text(), "")


class Hardening(unittest.TestCase):
    def setUp(self):
        self.h, self.cfg = FakeHost(), cfg_of()

    def mod(self):
        class M(hardening.Module):
            id, title, why, risk, lockout = "demo", "A demo protection", "Why.", "Risk.", True

            def check(self, host, cfg):
                return Check(self.applied, "not yet")

            applied = False

            def plan(self, host, cfg):
                return [Action("run", argv=["echo", "one"]), Action("write", path="/etc/demo", text="x\n")]
        return M()

    def test_the_screen_replaces_the_printed_text_and_the_confirm_question(self):
        said, shown = [], []
        m = self.mod()
        status, _ = hardening.run_module(m, self.h, self.cfg, dry_run=False, out=said.append, confirm=lambda p: self.fail("asked in text"), present=lambda i: shown.append(i) or False)
        self.assertEqual(status, "declined")
        self.assertEqual(said, [])
        self.assertEqual(shown[0]["steps"][0], "echo one")
        self.assertTrue(shown[0]["lockout"])
        self.assertEqual(self.h.ran("echo"), [])

    def test_applying_through_the_screen_runs_the_steps(self):
        m = self.mod()
        m.check = lambda host, cfg: Check(bool(self.h.ran("echo")), "now ok")
        status, _ = hardening.run_module(m, self.h, self.cfg, dry_run=False, out=lambda s: None, present=lambda i: True)
        self.assertEqual(status, "applied")

    def test_a_preview_always_prints_the_text(self):
        said = []
        status, _ = hardening.run_module(self.mod(), self.h, self.cfg, dry_run=True, out=said.append, present=lambda i: self.fail("a preview must not prompt"))
        self.assertEqual(status, "planned")
        self.assertTrue(any("A demo protection" in s for s in said))

    def test_the_two_lockout_modules_are_marked(self):
        self.assertTrue(hardening.MODULES["ssh-keys-only"].lockout)
        self.assertTrue(hardening.MODULES["ufw-lan-egress"].lockout)
        self.assertFalse(hardening.MODULES["unattended-upgrades"].lockout)


if __name__ == "__main__":
    unittest.main()
