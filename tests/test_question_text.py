import unittest

import support  # noqa: F401
from cw.setup import questions


class QuestionText(unittest.TestCase):
    def test_every_question_explains_itself_and_shows_choices_or_examples(self):
        for q in questions.QUESTIONS:
            with self.subTest(q["key"]):
                self.assertGreater(len(q["why"]), 40, "the description should say what it is for")
                self.assertTrue(q.get("choices") or q.get("examples"), "every question shows choices or examples")

    def test_a_short_first_pass_keeps_the_rest_for_advanced(self):
        short = questions.count_to_ask(("client", "server"), {})
        full = questions.count_to_ask(("client", "server"), {}, advanced=True)
        self.assertLessEqual(short, 8)
        self.assertGreater(full, short)


if __name__ == "__main__":
    unittest.main()
