"""An optional question left blank is remembered, so it is not asked again by the other half of the wizard."""
import unittest

import support  # noqa: F401

from cw.setup import questions


class FakeUi:
    """The terminal screens' interface, answering from a table."""

    def __init__(self):
        self.asked, self.rawtext, self.review = [], {}, None

    def ask(self, q, default, options, problem=None, shown="", view=None):
        self.asked.append(q["key"])
        return ("answer", {"server.host": "h.example", "server.admin_user": "admin", "github.owner": "someone"}.get(q["key"], ""))

    def confirm(self, items, total, message):
        return "go"


class Blank(unittest.TestCase):
    def test_a_blank_optional_answer_is_not_asked_twice_across_two_calls(self):
        ui, blanks = FakeUi(), set()
        conn, _ = questions.collect("client", ui=ui, only={"server.host", "server.admin_user", "server.ssh_port"})
        given = {f"{s}.{k}": v for s, d in conn.items() for k, v in d.items()}
        client, _ = questions.collect("client", ui=ui, given=given, blanks=blanks)
        self.assertIn("updates.source", blanks)
        questions.collect("server", ui=ui, given={**given, **{f"{s}.{k}": v for s, d in client.items() for k, v in d.items()}, **{k: "" for k in blanks}})
        self.assertEqual(ui.asked.count("updates.source"), 1)

    def test_one_flow_over_both_halves_asks_each_question_once(self):
        ui = FakeUi()
        questions.collect(("client", "server"), ui=ui, given={"server.host": "h.example", "server.admin_user": "admin", "server.ssh_port": 22})
        self.assertEqual(len(ui.asked), len(set(ui.asked)))
        self.assertEqual(len(ui.asked), questions.count_to_ask(("client", "server"), {"server.host": "h.example", "server.admin_user": "admin", "server.ssh_port": 22}))

    def test_without_blanks_collection_nothing_changes(self):
        nested, missing = questions.collect("client", use_defaults=True, given={"server.host": "h.example", "server.admin_user": "a", "github.owner": "o"})
        self.assertEqual(missing, [])
        self.assertNotIn("source", nested.get("updates", {}))


if __name__ == "__main__":
    unittest.main()
