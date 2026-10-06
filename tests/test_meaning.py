"""The delivered-meaning comparison (EG-19): normalisation, word error rate, the words that differ and
the differences that can change meaning."""
import unittest

from app.meaning import align, by_turn, compare, median, normalise, number_words


class NormaliseTests(unittest.TestCase):
    def test_case_punctuation_and_hyphens_do_not_count(self):
        self.assertEqual(normalise("So, it is NOT instant?"), ["so", "it", "is", "not", "instant"])
        self.assertEqual(normalise("First, decide the scope: repository or organization."),
                         normalise("First, decide the scope, repository, or organization."))
        self.assertEqual(compare("Use e-mail, not chat.", "use email not chat")["wer"], 0.0)
        self.assertEqual(compare("Set an organization-wide policy.", "Set an organization wide policy")["errors"], 0)
        self.assertEqual(compare("\u201cDon\u2019t\u201d \u2014 really.", "Do not really")["errors"], 0)

    def test_numbers_written_and_spoken_are_the_same(self):
        self.assertEqual(number_words("30"), ["thirty"])
        self.assertEqual(number_words("1,400"), ["one", "thousand", "four", "hundred"])
        self.assertEqual(number_words("3.5"), ["three", "point", "five"])
        self.assertEqual(number_words("0800"), ["zero", "eight", "zero", "zero"])
        for approved, heard in (("It takes up to 30 minutes.", "It takes up to thirty minutes."),
                                ("About 8% of requests.", "About eight percent of requests."),
                                ("Pick the 2nd option.", "Pick the second option."),
                                ("Version 1.2.3 is out.", "Version one point two point three is out."),
                                ("It costs $15 per 1,000,000 characters.", "It costs fifteen dollars per one million characters.")):
            self.assertEqual(compare(approved, heard)["wer"], 0.0, (approved, heard))

    def test_contractions_and_words_split_differently(self):
        self.assertEqual(compare("Copilot won't use it.", "Copilot will not use it.")["wer"], 0.0)
        self.assertEqual(compare("It isn't sent.", "It is not sent.")["wer"], 0.0)
        self.assertEqual(compare("Open GitHub Codespaces.", "Open Git Hub code spaces.")["wer"], 0.0)
        self.assertEqual(align(["github"], ["git", "hub"]), [("merge", 0, 1, 0, 2)])
        # Only a real split merges for free: two different words are still two errors.
        self.assertEqual(compare("the cat", "concatenate")["errors"], 2)


class WordErrorRateTests(unittest.TestCase):
    def test_counts_substitutions_deletions_and_insertions(self):
        r = compare("the cat sat on the mat", "the cat sat on a mat today")
        self.assertEqual((r["substituted"], r["deleted"], r["inserted"], r["errors"], r["words"]), (1, 0, 1, 2, 6))
        self.assertEqual(r["wer"], round(2 / 6, 4))
        r = compare("the cat sat on the mat", "the cat on the mat")
        self.assertEqual((r["deleted"], r["errors"], r["wer"]), (1, 1, round(1 / 6, 4)))
        self.assertEqual(compare("", "")["wer"], 0.0)
        self.assertEqual(compare("", "something")["wer"], 1.0)
        self.assertEqual(compare("all of this", "")["wer"], 1.0)

    def test_lists_and_marks_the_differing_words_on_both_sides(self):
        r = compare("Settings may take up to 30 minutes to take effect.", "Settings may take up to 13 minutes to take effect.")
        self.assertEqual(r["diffs"], [{"approved": "30", "heard": "13", "flags": ["number"]}])
        self.assertEqual([w for w, mark in r["approved"] if mark], ["30"])
        self.assertEqual([mark for w, mark in r["heard"] if w == "13"], [2])
        self.assertEqual(r["approved"][-1], ["effect.", 0], "the words are shown as written")
        r = compare("Pick the scope first.", "Pick a scope first.")
        self.assertEqual([[w, m] for w, m in r["approved"] if m], [["the", 1]])

    def test_median(self):
        self.assertIsNone(median([]))
        self.assertEqual(median([0.3, 0.1, 0.2]), 0.2)
        self.assertEqual(median([0.0, 0.1]), 0.05)


class MeaningFlagTests(unittest.TestCase):
    def test_numbers_names_and_negations_are_flagged_above_noise(self):
        self.assertEqual(compare("Wait 30 minutes.", "Wait 13 minutes.")["flags"], ["number"])
        self.assertEqual(compare("Wait thirty minutes.", "Wait thirteen minutes.")["flags"], ["number"])
        self.assertEqual(compare("Use the thirteenth column.", "Use the thirtieth column.")["flags"], ["number"])
        self.assertEqual(compare("It is the 20th release.", "It is the twelfth release.")["flags"], ["number"])
        self.assertEqual(compare("Copilot won't use excluded files.", "Copilot will use excluded files.")["flags"], ["negation"])
        self.assertEqual(compare("It is not instant.", "It is instant.")["flags"], ["negation"])
        self.assertEqual(compare("Ask Azure about it.", "Ask Asher about it.")["flags"], ["name"])
        self.assertEqual(compare("Then open the IDE.", "Then open the idea.")["flags"], ["name"])
        noise = compare("So you pick the scope first.", "So you pick a scope first.")
        self.assertEqual((noise["errors"], noise["flags"], noise["diffs"][0]["flags"]), (1, [], []))

    def test_flagged_differences_are_listed_first(self):
        r = compare("Pick the scope, then wait 30 minutes.", "Pick a scope, then wait 13 minutes.")
        self.assertEqual([(d["approved"], d["heard"], d["flags"]) for d in r["diffs"]],
                         [("30", "13", ["number"]), ("the", "a", [])])
        self.assertEqual(r["flags"], ["number"])

    def test_a_difference_inside_a_word_shows_the_whole_word_on_both_sides(self):
        r = compare("No. GitHub Copilot won't use excluded files.", "No, GitHub Copilot will use excluded files.")
        self.assertEqual((r["errors"], r["words"]), (1, 8), "only the missing 'not' counts")
        self.assertEqual(r["diffs"], [{"approved": "won't", "heard": "will", "flags": ["negation"]}])
        self.assertEqual([[w, m] for w, m in r["heard"] if m], [["will", 2]])
        r = compare("It holds up to 1,998 dimensions.", "It holds up to 1990 dimensions.")
        self.assertEqual((r["errors"], r["diffs"]), (1, [{"approved": "1,998", "heard": "1990", "flags": ["number"]}]))
        r = compare("Pick the scope first.", "Pick a scope first.")
        self.assertEqual(r["diffs"], [{"approved": "the", "heard": "a", "flags": []}], "neighbours are not pulled in")


class ByTurnTests(unittest.TestCase):
    def test_words_go_to_the_turn_their_middle_falls_in(self):
        # Timings from a real presenter video: three turns with long breaks between them.
        spans = [{"start": 0.05, "end": 4.15}, {"start": 5.75, "end": 10.575}, {"start": 12.175, "end": 16.337}]
        heard = [{"text": "First,", "start": 0.11, "end": 0.5}, {"text": "decide.", "start": 0.6, "end": 1.0},
                 {"text": "Yes.", "start": 5.87, "end": 6.2}, {"text": "hmm", "start": 11.0, "end": 11.3},
                 {"text": "Look", "start": 12.27, "end": 12.5}, {"text": "when.", "start": 16.2, "end": 16.6}]
        self.assertEqual(by_turn(heard, spans), (["First, decide.", "Yes.", "Look when."], 1))
        self.assertEqual(by_turn([], spans), (["", "", ""], 0))


if __name__ == "__main__":
    unittest.main()
