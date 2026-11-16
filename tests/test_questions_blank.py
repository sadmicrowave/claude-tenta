"""An optional question left blank is remembered, so it is not asked again by the other half of the wizard."""
import unittest

import support  # noqa: F401

from cw.setup import questions


class Blank(unittest.TestCase):
    def test_a_blank_optional_answer_is_not_asked_twice(self):
        asked = []

        class U:
            def ask(self, q, default, options, problem=None, shown=""):
                asked.append(q["key"])
                return {"server.host": "h.example", "server.admin_user": "admin", "github.owner": "someone"}.get(q["key"], "")

            def answered(self, q, shown):
                pass
        ui, blanks = U(), set()
        conn, _ = questions.collect("client", ui=ui, only={"server.host", "server.admin_user", "server.ssh_port"})
        given = {f"{s}.{k}": v for s, d in conn.items() for k, v in d.items()}
        client, _ = questions.collect("client", ui=ui, given=given, blanks=blanks)
        self.assertIn("updates.source", blanks)
        questions.collect("server", ui=ui, given={**given, **{f"{s}.{k}": v for s, d in client.items() for k, v in d.items()}, **{k: "" for k in blanks}})
        self.assertEqual(asked.count("updates.source"), 1)
        self.assertEqual(len(asked), questions.count_to_ask(("client", "server"), {}))

    def test_without_blanks_collection_nothing_changes(self):
        nested, missing = questions.collect("client", use_defaults=True, given={"server.host": "h.example", "server.admin_user": "a", "github.owner": "o"})
        self.assertEqual(missing, [])
        self.assertNotIn("source", nested.get("updates", {}))


if __name__ == "__main__":
    unittest.main()
