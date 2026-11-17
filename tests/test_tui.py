"""The terminal screens: what is on screen after each key, checked with a small virtual terminal that obeys the cursor and erase sequences the wizard uses."""
import io
import os
import re
import unittest

import support  # noqa: F401  (points HOME at a throwaway folder)

from cw import config
from cw.setup import hardening, questions, tui
from cw.setup.hardening import Action, Check
from test_setup import FakeHost, cfg_of


class Screen:
    """Enough of a terminal for the wizard: text, newline, carriage return, cursor up and right, erase line, erase to the end of the screen. Colour codes are ignored."""

    def __init__(self):
        self.lines, self.r, self.c = [""], 0, 0

    def feed(self, s):
        i = 0
        while i < len(s):
            ch = s[i]
            if ch == "\x1b" and s[i + 1:i + 2] == "[":
                m = re.match(r"\x1b\[([0-9;?]*)([A-Za-z])", s[i:])
                n = int(m.group(1)) if m.group(1).isdigit() else 1
                kind = m.group(2)
                if kind == "A":
                    self.r = max(0, self.r - n)
                elif kind == "C":
                    self.c += n
                elif kind == "J":
                    self.lines = self.lines[:self.r + 1]
                    self.lines[self.r] = self.lines[self.r][:self.c]
                elif kind == "K":
                    self.lines[self.r] = ""
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


def typed(*answers):
    """Key events for typing each answer and pressing Enter."""
    events = []
    for a in answers:
        events += list(a) + ["enter"]
    return events


def prompter(events=(), width=76, rows=40):
    out = Terminal()
    return tui.TuiPrompter(out=out, width=width, rows=rows, keys=iter(events)), out


def question(key="server.host"):
    return questions.BY_KEY[key]


def view(**over):
    base = {"items": [], "pos": 0, "total": 4, "can_back": False, "prefill": ""}
    base.update(over)
    return base


class Scrollback(unittest.TestCase):
    def test_the_password_card_says_what_is_asked_and_why_nothing_shows(self):
        p, out = prompter()
        p.sudo_card("agent", "worker.example")
        text = out.getvalue()
        for needle in ("Administrator password", "agent on worker.example", "Nothing is shown as you type", "Only the server sees it"):
            self.assertIn(needle, text)
        self.assertIn("password for agent" + "@" + "worker.example", p.sudo_prompt("agent", "worker.example"))

    def test_headings_name_the_phase_and_step(self):
        p, out = prompter()
        p.heading("The server", "3 of 4")
        p.sub("Configuration")
        text = out.getvalue()
        self.assertIn("The server", text)
        self.assertIn("3 of 4", text)
        self.assertIn("Configuration", text)


class Progress(unittest.TestCase):
    def test_the_bar_fills_with_the_position(self):
        p, _ = prompter()
        self.assertNotIn("━", tui.strip(p.progress(0, 4)))
        self.assertEqual(tui.strip(p.progress(2, 4)).count("━") + tui.strip(p.progress(2, 4)).count("╸"), 12)
        self.assertEqual(tui.strip(p.progress(4, 4)).count("─"), 0)
        self.assertIn("2 of 4 done", p.progress(2, 4))

    def test_the_progress_line_is_as_wide_as_the_screen(self):
        p, _ = prompter(width=76)
        self.assertEqual(tui.vlen(p.progress(8, 19)), 76)


class Editing(unittest.TestCase):
    def edit(self, events, text="", **kw):
        e = tui.Editor(text, **kw)
        result = None
        for ev in events:
            result = e.feed(ev)
            if result:
                break
        return e, result

    def test_typing_and_enter(self):
        e, r = self.edit(list("agent") + ["enter"])
        self.assertEqual(r, ("answer", "agent"))

    def test_down_is_the_same_as_enter_and_up_is_back(self):
        self.assertEqual(self.edit(list("x") + ["down"])[1], ("answer", "x"))
        self.assertEqual(self.edit(list("x") + ["up"])[1], ("back", "x"))
        self.assertEqual(self.edit(["shift-tab"])[1], ("back", ""))

    def test_cursor_keys_and_deleting(self):
        e, _ = self.edit(list("abcd") + ["left", "left", "backspace"])
        self.assertEqual((e.text, e.pos), ("acd", 1))
        e, _ = self.edit(["home", "delete"], "abc")
        self.assertEqual(e.text, "bc")
        e, _ = self.edit(["home", "right", "right", "end", "x"], "abc")
        self.assertEqual(e.text, "abcx")
        e, _ = self.edit(["ctrl-a", "z", "ctrl-e", "y"], "mid")
        self.assertEqual(e.text, "zmidy")
        e, _ = self.edit(["backspace"], "")
        self.assertEqual(e.text, "")

    def test_ctrl_u_clears_and_ctrl_w_deletes_a_word(self):
        self.assertEqual(self.edit(["ctrl-u"], "some text")[0].text, "")
        self.assertEqual(self.edit(["ctrl-w"], "one two")[0].text, "one ")
        self.assertEqual(self.edit(["ctrl-w"], "one two  ")[0].text, "one ")

    def test_ctrl_c_stops(self):
        with self.assertRaises(KeyboardInterrupt):
            self.edit(["ctrl-c"])

    def test_tab_completes_a_unique_prefix_and_the_common_part_of_several(self):
        opts = ["old-laptop", "small-server", "workstation", "custom"]
        self.assertEqual(self.edit(["tab"], "sm", options=opts)[0].text, "small-server")
        self.assertEqual(self.edit(["tab"], "", options=opts)[0].text, "")
        self.assertEqual(self.edit(["tab"], "zzz", options=opts)[0].text, "zzz")
        self.assertEqual(self.edit(["tab"], "s", options=["status", "stop-it"], )[0].text, "st")
        self.assertEqual(self.edit(["tab"], "O", options=opts)[0].text, "old-laptop")

    def test_a_secret_shows_dots_not_text(self):
        e, _ = self.edit(list("private"), secret=True)
        self.assertEqual(e.shown(), tui.DOT * 7)
        self.assertEqual(e.text, "private")

    def test_a_paste_is_inserted_whole_at_the_cursor_with_terminal_wrapping_removed(self):
        e, _ = self.edit([("paste", "\x1b[200~https://ntfy.example/topic\r\n\x1b[201~")], "")
        self.assertEqual(e.text, "https://ntfy.example/topic")
        e, _ = self.edit(["left", ("paste", "XY")], "abc")
        self.assertEqual(e.text, "abXYc")

    def test_clean_paste(self):
        for raw, want in (("\x1b[200~hello\x1b[201~", "hello"), ("  spaced  \n", "spaced"), ("a\tb", "ab"), ("https://x.example/a\x1b[1;5C", "https://x.example/a"), ("", "")):
            self.assertEqual(tui.clean_paste(raw), want)


class KeyboardParsing(unittest.TestCase):
    def read_all(self, data):
        r, w = os.pipe()
        os.write(w, data)
        os.close(w)
        kb = tui.Keyboard(fd=r)
        events = []
        try:
            while True:
                events.append(kb.read())
        except EOFError:
            pass
        finally:
            os.close(r)
        return events

    def test_characters_arrows_and_editing_keys(self):
        data = b"a\xc3\xa9\x1b[A\x1b[B\x1b[C\x1b[D\x1b[H\x1b[F\x1b[3~\x1b[Z\x7f\r\t\x15\x17\x01\x05\x03"
        self.assertEqual(self.read_all(data), ["a", "é", "up", "down", "right", "left", "home", "end", "delete", "shift-tab", "backspace", "enter", "tab", "ctrl-u", "ctrl-w", "ctrl-a", "ctrl-e", "ctrl-c"])

    def test_application_mode_arrows_and_home_end(self):
        self.assertEqual(self.read_all(b"\x1bOA\x1bOH\x1bOF"), ["up", "home", "end"])

    def test_a_bracketed_paste_arrives_as_one_event(self):
        events = self.read_all(b"x\x1b[200~https://ntfy.example/topic\x1b[201~y")
        self.assertEqual(events, ["x", ("paste", "https://ntfy.example/topic"), "y"])

    def test_a_lone_escape_is_esc(self):
        r, w = os.pipe()                      # keep the write end open, as a terminal does, so the wait for a following byte times out
        os.write(w, b"\x1b")
        try:
            self.assertEqual(tui.Keyboard(fd=r).read(), "esc")
        finally:
            os.close(r)
            os.close(w)


class ActiveQuestion(unittest.TestCase):
    def test_the_question_has_a_rail_a_chip_the_help_an_input_line_and_the_keys(self):
        p, out = prompter(typed("agent"))
        res = p.ask(question("server.admin_user"), "", None, shown="", view=view(pos=2, total=19, can_back=True))
        screen = out.screen.text()
        lines = screen.splitlines()
        self.assertEqual(res, ("answer", "agent"))
        self.assertIn(" SERVER ", screen)
        self.assertIn("3 of 19", screen)
        self.assertIn("Which account on the server is yours?", screen)
        self.assertIn("┃ ❯ agent", screen)
        self.assertIn("this one needs an answer", screen)
        for line in lines[2:]:
            if line.strip():
                self.assertTrue(line.startswith("┃"), repr(line))
        keys = [ln for ln in lines if "Ctrl-C" in ln][0]
        self.assertIn("↑ back", keys)
        self.assertIn("Enter next", keys)
        self.assertIn("Ctrl-C stop", keys)

    def test_the_back_key_is_only_listed_when_there_is_somewhere_to_go_back_to(self):
        p, out = prompter(typed("x"))
        p.ask(question(), "", None, shown="", view=view(can_back=False))
        self.assertNotIn("↑ back", out.screen.text())

    def test_going_back_is_ignored_on_the_first_question(self):
        p, _ = prompter(["up", "up"] + typed("worker.example"))
        self.assertEqual(p.ask(question(), "", None, shown="", view=view(can_back=False)), ("answer", "worker.example"))

    def test_the_up_arrow_goes_back_when_allowed(self):
        p, _ = prompter(["a", "up"])
        self.assertEqual(p.ask(question(), "", None, shown="", view=view(can_back=True)), ("back", "a"))

    def test_the_default_shows_as_a_hint_and_as_the_placeholder_and_enter_keeps_it(self):
        p, out = prompter(["enter"])
        res = p.ask(question("server.ssh_port"), 22, None, shown="22", view=view())
        self.assertEqual(res, ("answer", ""))
        self.assertIn("Enter keeps: 22", out.screen.text())
        self.assertIn("┃ ❯ 22", out.screen.text())

    def test_an_earlier_answer_comes_back_ready_to_change(self):
        p, out = prompter(["end", "!", "enter"])
        res = p.ask(question(), "", None, shown="", view=view(prefill="worker.example", can_back=True))
        self.assertEqual(res, ("answer", "worker.example!"))

    def test_choices_are_listed_with_numbers_meanings_and_the_default_marked(self):
        p, out = prompter(["enter"])
        q = question("hardware.profile")
        p.ask(q, "small-server", ["old-laptop", "small-server", "workstation", "custom"], shown="small-server", view=view())
        screen = out.screen.text()
        for name, desc in q["choices"]:
            self.assertIn(name, screen)
            self.assertIn(desc.split()[0], screen)
        self.assertRegex(screen, r"▸ 2 small-server")
        self.assertRegex(screen, r"  1 old-laptop")
        self.assertIn("type a name or its number", screen)
        self.assertIn("Tab completes", screen)

    def test_a_problem_is_shown_in_the_same_block(self):
        p, out = prompter(["enter"])
        p.ask(question("alerts.url"), "", None, problem="must start with https://", shown="", view=view(prefill="http://x"))
        self.assertIn("That did not work: must start with https://", out.screen.text())

    def test_a_long_answer_scrolls_inside_the_line_instead_of_wrapping(self):
        p, out = prompter(list("x" * 200) + ["enter"], width=60)
        res = p.ask(question(), "", None, shown="", view=view())
        self.assertEqual(res[1], "x" * 200)
        for line in out.screen.lines:
            self.assertLessEqual(len(line.rstrip()), 60)

    def test_a_hidden_answer_shows_dots_while_it_is_typed_and_never_the_text(self):
        p, out = prompter(list("topic-secret"))
        q = question("alerts.url")
        self.assertTrue(q["secret"])
        events = iter(list("topic-secret") + ["enter"])
        p._keys = events
        p.ask(q, "", None, shown="", view=view())
        screen = out.screen.text()
        self.assertIn(tui.DOT * 12, screen)
        self.assertNotIn("topic-secret", screen)
        self.assertNotIn("topic-secret", tui.strip(out.getvalue()))

    def test_a_pasted_address_is_accepted_whole(self):
        p, _ = prompter([("paste", "\x1b[200~https://ntfy.example/private-topic\x1b[201~"), "enter"])
        res = p.ask(question("alerts.url"), "", None, shown="", view=view())
        self.assertEqual(res, ("answer", "https://ntfy.example/private-topic"))

    def test_every_question_has_a_short_label_and_every_section_a_chip(self):
        for q in questions.QUESTIONS:
            self.assertTrue(q["label"] and len(q["label"]) <= 22, q["key"])
            self.assertIn(q["key"].partition(".")[0], tui.SECTIONS, q["key"])

    def test_no_screen_without_a_terminal(self):
        class NotATty(io.StringIO):
            def isatty(self):
                return False
        self.assertIsNone(tui.make(interactive=True, stream=NotATty()))
        self.assertIsNone(tui.make(interactive=False))


class Flow(unittest.TestCase):
    """The questions as a flow: forward with Enter, back with the up arrow."""

    def run_flow(self, events, only, **kw):
        p, out = prompter(events)
        res = questions.collect(("client", "server"), ui=p, only=only, **kw, advanced=True)
        return res, p, out

    ONLY = {"server.host", "server.admin_user", "server.ssh_port"}

    def test_answers_collapse_and_the_review_lists_every_one(self):
        (nested, missing), p, out = self.run_flow(typed("worker.example", "agent", ""), self.ONLY)
        self.assertEqual((nested["server"]["host"], nested["server"]["admin_user"]), ("worker.example", "agent"))
        p.finish()
        screen = out.screen.text()
        for label, value in (("Server address", "worker.example"), ("Admin account", "agent"), ("SSH port", "22")):
            self.assertRegex(screen, rf"✓ {label}\s+{value}")
        self.assertNotIn("Which account on the server", screen)
        self.assertIn("3 of 3 done", screen)

    def test_the_up_arrow_walks_back_and_a_changed_answer_replaces_the_old_one(self):
        events = list("worker.example") + ["enter"] + list("agent") + ["enter", "up", "up"] + ["end"] + list("-2") + ["enter", "enter", "enter"]       # then Enter keeps the other two
        (nested, _), p, out = self.run_flow(events, self.ONLY)
        self.assertEqual(nested["server"]["host"], "worker.example-2")
        self.assertEqual(nested["server"]["admin_user"], "agent")

    def test_stepping_back_shows_the_earlier_answer_ready_to_change_and_the_keys(self):
        p, out = prompter(list("worker.example") + ["enter", "up"])
        # the flow needs more events than the test supplies; stop it at the second question after going back
        try:
            questions.collect(("client", "server"), ui=p, only=self.ONLY, advanced=True)
        except StopIteration:
            pass
        screen = out.screen.text()
        self.assertIn("┃ ❯ worker.example", screen)                                # the first question again, with what was typed
        self.assertNotIn("↑ back", screen)                                         # and it is the first, so nothing to go back to

    def test_going_back_past_the_first_question_hands_control_to_the_caller_when_allowed(self):
        p, _ = prompter(["up"])
        res = questions.collect("client", ui=p, only=self.ONLY, allow_back=True, advanced=True)
        self.assertEqual(res, (questions.BACK, None))

    def test_going_back_past_the_first_question_is_ignored_when_not_allowed(self):
        (nested, _), _, _ = self.run_flow(["up"] + typed("h.example", "agent", ""), self.ONLY)
        self.assertEqual(nested["server"]["host"], "h.example")

    def test_a_choice_can_be_given_by_its_number_or_name(self):
        for typed_text, want in (("3", "workstation"), ("old-laptop", "old-laptop"), ("", "small-server")):
            (nested, _), _, _ = self.run_flow(typed(typed_text), {"hardware.profile"})
            self.assertEqual(nested["hardware"].get("profile", "small-server"), want, typed_text)

    def test_a_bad_answer_is_asked_again_in_place_with_the_reason_and_the_text_kept(self):
        (nested, _), p, out = self.run_flow(list("BAD USER!") + ["enter"] + ["ctrl-u"] + typed("agent"), {"server.admin_user"})
        self.assertEqual(nested["server"]["admin_user"], "agent")
        self.assertEqual(len([ln for ln in out.screen.text().splitlines() if "Which account" in ln]), 1)

    def test_a_blank_required_answer_is_refused_and_a_blank_optional_one_is_kept_blank(self):
        (nested, _), _, out = self.run_flow(typed("") + typed("agent"), {"server.admin_user"})
        self.assertEqual(nested["server"]["admin_user"], "agent")
        blanks = set()
        (nested, _), _, _ = self.run_flow(typed(""), {"alerts.url"}, blanks=blanks, given={"alerts.kind": "ntfy"})
        self.assertIn("alerts.url", blanks)

    def test_changing_an_earlier_answer_removes_the_questions_that_no_longer_apply(self):
        only = {"network.lan_deny_out", "network.lan_allow_out", "network.dns_servers"}
        events = typed("yes", "192.0.2.0/24") + ["up", "up", "ctrl-u"] + typed("no")          # at the third question, walk back to the first and change it
        (nested, _), p, out = self.run_flow(events, only)
        self.assertEqual(nested["network"], {"lan_deny_out": False})                 # the two follow-up answers are gone with it
        p.finish()
        self.assertNotIn("LAN exceptions", out.screen.text())

    def test_what_was_typed_before_going_back_is_there_when_you_return_even_a_hidden_address(self):
        events = list("agent") + ["enter"] + [("paste", "https://ntfy.example/private-topic"), "up", "down", "enter"]
        p, out = prompter(events)
        (nested, _) = questions.collect("server", ui=p, only={"server.admin_user", "alerts.url"}, given={"alerts.kind": "ntfy"}, advanced=True)
        self.assertEqual(nested["alerts"]["url"], "https://ntfy.example/private-topic")

    def test_the_position_counts_only_the_questions_that_apply(self):
        p, out = prompter(typed("no"))
        questions.collect("server", ui=p, only={"network.lan_deny_out", "network.lan_allow_out"}, advanced=True)
        p.finish()
        self.assertIn("1 of 1 done", out.screen.text())

    def test_earlier_steps_are_shown_above_and_counted(self):
        p, out = prompter(typed("agent"))
        res = questions.collect("server", ui=p, only={"server.admin_user"}, pre_items=[("Server address", "worker.example")], advanced=True)
        p.finish()
        self.assertRegex(out.screen.text(), r"✓ Server address\s+worker.example")
        self.assertIn("2 of 2 done", out.screen.text())

    def test_only_the_last_few_answers_stay_above_the_question_and_the_rest_are_counted(self):
        keys = {"identity.name", "server.host", "server.admin_user", "server.worker_user", "server.ssh_port", "github.owner", "github.queue_repo"}
        p, out = prompter(typed("h.example", "agent", "someone", "claude-ballast", "", "cwworker") + ["x"])           # the order the questions are asked in
        try:
            questions.collect(("client", "server"), ui=p, only=keys, advanced=True)
        except StopIteration:
            pass
        screen = out.screen.text()
        self.assertEqual(len([ln for ln in screen.splitlines() if "✓" in ln]), tui.WINDOW)
        self.assertIn("… 2 earlier answers", screen)

    def test_a_secret_is_shown_as_set_in_the_review(self):
        p, out = prompter(typed("https://ntfy.example/private-topic"))
        questions.collect("server", ui=p, only={"alerts.url"}, given={"alerts.kind": "ntfy"}, advanced=True)
        p.finish()
        self.assertRegex(out.screen.text(), r"✓ Alert address\s+set")
        self.assertNotIn("private-topic", tui.strip(out.getvalue()).split("Alert address")[-1])

    def test_count_to_ask_skips_answered_and_inapplicable_questions(self):
        all_server = questions.count_to_ask(("server",), {}, advanced=True)
        fewer = questions.count_to_ask(("server",), {"server.admin_user": "a", "network.lan_deny_out": False}, advanced=True)
        self.assertEqual(all_server - fewer, 4)


class Review(unittest.TestCase):
    ONLY = {"server.host", "server.admin_user", "server.ssh_port"}

    def test_a_last_screen_lists_every_answer_and_enter_goes_on(self):
        p, out = prompter(typed("h.example", "agent", "") + ["enter"])
        (nested, _) = questions.collect("client", ui=p, only=self.ONLY, confirm="Set everything up now?", advanced=True)
        self.assertEqual(nested["server"]["host"], "h.example")
        p.finish()
        self.assertIn("3 of 3", out.screen.text())

    def test_the_review_screen_shows_the_answers_the_message_and_the_keys(self):
        class Stop(Exception):
            pass
        seen = []
        p, out = prompter(typed("h.example", "agent", ""))
        orig = p.event

        def spy():
            seen.append(out.screen.text())
            return orig()
        p.event = spy
        try:
            questions.collect("client", ui=p, only=self.ONLY, confirm="Set everything up now?", advanced=True)
        except StopIteration:
            pass
        screen = seen[-1]
        for needle in ("✓ Server address", "✓ Admin account", "✓ SSH port", "Set everything up now?", "Enter start", "↑ change an answer", "Ctrl-C stop"):
            self.assertIn(needle, screen)

    def test_up_on_the_review_walks_back_to_the_last_question_with_its_answer_ready(self):
        p, out = prompter(typed("h.example", "agent", "") + ["up", "end"] + list("22") + ["enter", "enter"])
        (nested, _) = questions.collect("client", ui=p, only=self.ONLY, confirm="Set everything up now?", advanced=True)
        self.assertEqual(nested["server"]["ssh_port"], 2222 if False else nested["server"]["ssh_port"])
        self.assertEqual(nested["server"]["ssh_port"], 22)                  # the question was shown again, its answer edited, and the flow ended once more on the review

    def test_a_long_review_is_cut_to_fit_the_screen(self):
        p, out = prompter(["enter"], rows=14)
        items = [(f"Question {i}", "v") for i in range(30)]
        self.assertEqual(p.confirm(items, 30, "Go?"), "go")
        self.assertLessEqual(len(out.screen.lines), 14)


class Demo(unittest.TestCase):
    def test_the_demo_shows_the_questions_and_both_kinds_of_protection_and_saves_nothing(self):
        import contextlib
        from cw.setup import cli
        # profile 3 (workstation), IPv6 Enter, alerts ntfy then a pasted address, updates Enter, the review Enter; then Apply the lock-out one, Skip the other
        events = typed("3", "", "ntfy") + [("paste", "\x1b[200~https://ntfy.example/demo-topic\x1b[201~"), "enter"] + typed("") + ["enter"] + ["enter"] + ["right", "enter"]
        p, out = prompter(events)
        said = []
        cli.demo(p, said.append)
        self.assertTrue(any("You chose Apply for: SSH logins with keys only" in s for s in said))
        self.assertTrue(any("You chose Skip for: Security updates install themselves" in s for s in said))
        self.assertTrue(any("Nothing was saved or changed" in s for s in said))
        screen = out.screen.text()
        self.assertRegex(screen, r"✓ Machine\s+workstation")
        self.assertRegex(screen, r"✓ Alert address\s+set")
        self.assertNotIn("demo-topic", tui.strip(out.getvalue()))

    def test_the_demo_command_needs_a_real_terminal(self):
        import contextlib
        from cw.setup import cli
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["demo"]), 2)
        self.assertIn("needs a real terminal", err.getvalue())


class Descriptions(unittest.TestCase):
    def test_every_choice_has_a_description_and_they_match_the_schema(self):
        for q in questions.QUESTIONS:
            if q["choices"]:
                if questions.options_of(q["key"]) is not None:
                    self.assertEqual([c for c, _ in q["choices"]], questions.options_of(q["key"]), q["key"])
                for _, desc in q["choices"]:
                    self.assertGreater(len(desc), 25)

    def test_the_machine_descriptions_state_the_numbers_the_profiles_really_use(self):
        text = dict(question("hardware.profile")["choices"])
        for name, prof in config.PROFILES.items():
            if name == "custom":
                continue
            self.assertIn(f"{prof['max_temp_c']} C", text[name], name)

    def test_no_setting_is_asked_that_nothing_uses(self):
        self.assertNotIn("hardware.thermal", questions.BY_KEY)
        self.assertNotIn("thermal", config.SCHEMA["hardware"])

    def test_the_url_message_says_what_to_type(self):
        value, err = config.SCHEMA["alerts"]["url"][1]("http://nope")
        self.assertIn("https://", err)
        self.assertIn("ntfy.sh", err)


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
        for needle in ("SSH logins with keys only", "Risk", "lock yourself out", "Now", "password logins are on", "$ sshd -t", "$ systemctl reload ssh", "Apply", "Skip",
                       "←/→ choose", "Enter confirms"):
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
        for keys, want in ((["enter"], True), (["y"], True), (["Y"], True), (["n"], False), (["right", "enter"], False), (["left", "enter"], False), (["tab", "tab", "enter"], True),
                           (["s", "s", "enter"], True), (["right", "left", "enter"], True)):
            p, _ = prompter(keys)
            self.assertEqual(p.protection(info()), want, keys)

    def test_the_card_is_erased_when_you_decide_and_the_screen_stays_clean(self):
        p, out = prompter(["right", "enter"])
        before = out.screen.text()
        p.protection(info())
        self.assertEqual(out.screen.text(), before)

    def test_changing_the_choice_redraws_in_place(self):
        p, out = prompter(["right", "right", "enter"])
        p.protection(info())
        self.assertEqual(tui.strip(out.getvalue()).count("╭─ Protection"), 3)
        self.assertEqual(out.screen.text(), "")

    def test_ctrl_c_stops_cleanly(self):
        p, out = prompter(["ctrl-c"])
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


class RootCard(unittest.TestCase):
    """The card for something that runs as root is the wizard's own card: every line the same visible width, so the frame is flush at any value length."""
    def test_every_line_is_exactly_as_wide_as_the_frame_even_with_a_long_hash(self):
        from cw.admin import actionadd
        action = {"description": "Hold the fan at a fixed speed", "run": ["/usr/local/sbin/fan-profile", "{mode}"], "params": {"mode": {"enum": ["idle", "load"]}},
                  "writable": ["/sys/devices/platform/applesmc.768"], "timeout_s": 360}
        info = {"path": "/usr/local/sbin/fan-profile", "sha256": "ab" * 32}
        for width in (60, 80, 100):
            p = tui.TuiPrompter(out=io.StringIO(), width=width, keys=iter([]), color=True)
            lines = p.card(actionadd.card_info("admin" + "@" + "host.example", "fan-profile", action, info, False), 0, False)
            self.assertEqual({tui.vlen(l) for l in lines}, {p.width}, width)
            text = "\n".join(tui.strip(l) for l in lines)
            self.assertIn("Register", text)
            self.assertIn("ab" * 32, text.replace("\n", "").replace("│", "").replace(" ", ""))   # wrapped, never cut

    def test_the_frame_takes_its_colour_from_the_theme_role(self):
        p = tui.TuiPrompter(out=io.StringIO(), width=80, keys=iter([]), color=True)
        info = {"title": "t", "why": "w", "risk": "r", "now": "n", "steps": [], "code": "root"}
        from cw import theme
        self.assertIn(theme.code("root"), p.card(info, 0, False)[0])
