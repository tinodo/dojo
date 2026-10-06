"""What GitHub code scanning (CodeQL) found in October 2026, pinned so it stays fixed: no regular expression that
a long run of one character can make slow, character filters that remove exactly what the formats forbid, and a
log line a request value cannot break into two."""
import os
import tempfile
import time
import unittest
from xml.sax.saxutils import escape as xml_escape

os.environ.setdefault("DOJO_LOCAL", "1")
os.environ.setdefault("DOJO_DATA_DIR", tempfile.mkdtemp(prefix="dojo-import-"))

from app import podcast  # noqa: E402
from app.core import one_line  # noqa: E402
from app.pagefinder import label  # noqa: E402
from app.plan import ics_text  # noqa: E402

EVERY = "".join(map(chr, range(0x110000)))   # every code point, surrogates included


def xml_char(cp: int) -> bool:
    """XML 1.0, production [2] Char."""
    return cp in (0x9, 0xA, 0xD) or 0x20 <= cp <= 0xD7FF or 0xE000 <= cp <= 0xFFFD or 0x10000 <= cp <= 0x10FFFF


class Scanning(unittest.TestCase):
    def test_the_feed_keeps_exactly_the_characters_xml_allows(self):
        kept = "".join(c for c in EVERY if xml_char(ord(c)))
        self.assertEqual(podcast._text(EVERY), xml_escape(kept))
        self.assertEqual(podcast._attr(EVERY), xml_escape(kept, {'"': "&quot;"}))
        self.assertEqual(podcast._cdata(EVERY), "<![CDATA[" + kept.replace("]]>", "]]]]><![CDATA[>") + "]]>")
        self.assertEqual(podcast._text("a\U0001F600b\ufffe\uffff\ud800c\x7f"), "a\U0001F600bc\x7f", "astral kept; non-characters and surrogates go")

    def test_a_calendar_text_drops_control_characters_and_escapes_the_rest(self):
        ascii_ = "".join(map(chr, range(0x80)))
        printable = "".join(map(chr, range(0x20, 0x7F)))
        want = ("\t" + "\\n\\n" + printable.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,"))
        self.assertEqual(ics_text(ascii_), want, "tab, the line feed and the carriage return (as a line feed) stay, escaped; "
                                                 "every other control character goes")
        self.assertEqual(ics_text("a\r\nb\rc\x85d\u2028e"), "a\\nb\\nc\x85d\u2028e", "only ASCII controls are removed")

    def test_a_search_title_loses_its_tags_in_linear_time(self):
        self.assertEqual(label("Use <b>bold</b> &amp; <i>more</i>"), "Use bold & more")
        self.assertEqual(label("a <b>c"), "a c")
        started = time.monotonic()
        self.assertEqual(label("<" * 200_000, limit=10), "<" * 10)
        self.assertEqual(label("<a " * 50_000 + ">", limit=10), "<a <a <a <")
        self.assertLess(time.monotonic() - started, 2.0, "a run of '<' without '>' must not take quadratic time")

    def test_a_request_value_in_a_log_line_cannot_start_a_new_entry(self):
        self.assertEqual(one_line("dp-800\r\n2026-10-06 INFO forged\nentry\rhere"), "dp-800 2026-10-06 INFO forged entry here")
        self.assertEqual(one_line("x" * 500), "x" * 200)
        self.assertEqual(one_line(None), "None")


if __name__ == "__main__":
    unittest.main()
