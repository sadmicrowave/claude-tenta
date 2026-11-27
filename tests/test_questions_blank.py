"""An optional question left blank is remembered, so it is not asked again by the other half of the wizard."""
import unittest

import support  # noqa: F401

from ct.setup import questions


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
        conn, _ = questions.collect("client", ui=ui, only={"server.host", "server.admin_user", "server.ssh_port"}, advanced=True)
        given = {f"{s}.{k}": v for s, d in conn.items() for k, v in d.items()}
        given["alerts.kind"] = "ntfy"                      # so the alert address (an optional answer with no default) applies
        client, _ = questions.collect(("client", "server"), ui=ui, given=given, blanks=blanks, only={"alerts.url"}, advanced=True)
        self.assertIn("alerts.url", blanks)
        questions.collect("server", ui=ui, given={**given, **{k: "" for k in blanks}}, only={"alerts.url"}, advanced=True)
        self.assertEqual(ui.asked.count("alerts.url"), 1)

    def test_one_flow_over_both_halves_asks_each_question_once(self):
        ui = FakeUi()
        questions.collect(("client", "server"), ui=ui, given={"server.host": "h.example", "server.admin_user": "admin", "server.ssh_port": 22}, advanced=True)
        self.assertEqual(len(ui.asked), len(set(ui.asked)))
        self.assertEqual(len(ui.asked), questions.count_to_ask(("client", "server"), {"server.host": "h.example", "server.admin_user": "admin", "server.ssh_port": 22}, advanced=True))

    def test_without_blanks_collection_nothing_changes(self):
        nested, missing = questions.collect("client", use_defaults=True, given={"server.host": "h.example", "server.admin_user": "a", "github.owner": "o"})
        self.assertEqual(missing, [])
        self.assertNotIn("url", nested.get("alerts", {}))


if __name__ == "__main__":
    unittest.main()


class DefaultsFollowTheChosenName(unittest.TestCase):
    """A default written in terms of another answer follows that answer. A reinstall under another name once defaulted its repositories to the built-in name."""

    def test_the_repository_defaults_use_the_name_that_was_chosen(self):
        from ct.setup import questions
        self.assertEqual(questions.default_for("github.queue_repo", None, {"identity.name": "tenta-two"}), "tenta-two-queue")
        self.assertEqual(questions.default_for("github.results_repo", None, {"identity.name": "tenta-two"}), "tenta-two-results")
        self.assertEqual(questions.default_for("github.queue_repo"), "armada-queue")                   # nothing chosen: the built-in name

    def test_collecting_with_a_given_name_gives_repositories_of_that_name(self):
        from ct.setup import questions
        nested, missing = questions.collect("client", given={"identity.name": "tenta-two", "server.host": "worker.example", "server.admin_user": "me", "github.owner": "someone"}, use_defaults=True)
        self.assertEqual((nested["identity"]["name"], nested["github"]["queue_repo"], nested["github"]["results_repo"]), ("tenta-two", "tenta-two-queue", "tenta-two-results"))
