"""Regex mode reads a value the way the Overpass server does.

The server matches with POSIX extended regular expressions (glibc `regcomp`
with REG_EXTENDED|REG_NOSUB in the C.UTF-8 locale); this plugin matches the
returned objects again with Python's `re`.  Every expected result marked
"glibc" below was measured with glibc 2.43 `regexec` in that locale, so the
table is the server's reading, not this code's.

The live answer in tests/fixtures/overpass_building_levels_regex.json is
OpenStreetMap data, (c) OpenStreetMap contributors, ODbL 1.0.
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from zero2agent_osm_downloader.core.catalog import TagSpec, get_preset
from zero2agent_osm_downloader.core.places import build_place_query
from zero2agent_osm_downloader.core.postpass import render_sql
from zero2agent_osm_downloader.core.query import (
    POSIX_CLASSES,
    QueryError,
    TileRequest,
    advanced_specs,
    build_query,
    client_regex,
    matching_specs,
    normalize_tag,
    server_regex,
)
from zero2agent_osm_downloader.core import query

FIXTURES = Path(__file__).resolve().parent / "fixtures"
#: The Konak box of the live answers, (south, west, north, east).
BOX = (38.4180, 27.1250, 38.4225, 27.1320)

#: (pattern, value, glibc regexec result).
GLIBC = (
    ("[[:digit:]]+", "12", True), ("[[:digit:]]+", "a", False),
    ("^[[:digit:]]+$", "12", True), ("^[[:digit:]]+$", "12]", False),
    ("^[[:digit:]]+$", "a", False), ("^[[:digit:]]+$", "٣", False),
    ("^[[:alpha:]]+$", "abc", True), ("^[[:alpha:]]+$", "a1", False),
    ("^[[:alpha:]]+$", "şehir", True), ("^[[:alpha:]]+$", "İzmir", True),
    ("^[[:alpha:]]+$", "東京", True), ("^[[:alpha:]]+$", "Kemeraltı Çarşısı", False),
    ("[[:upper:]]", "Abc", True), ("[[:upper:]]", "abc", False),
    ("^[[:upper:]]", "İzmir", True), ("^[[:upper:]]", "şehir", False),
    ("^[[:lower:]]", "şehir", True), ("^[[:lower:]]", "Şehir", False),
    ("^[[:alnum:]]+$", "Konak35", True), ("^[[:alnum:]]+$", "Konak-35", False),
    ("[[:space:]]", "a b", True), ("[[:space:]]", "ab", False),
    ("[[:space:]]", "a b", False),
    ("[[:punct:]]", "a-b", True), ("[[:punct:]]", "ab", False),
    ("[[:punct:]]", "a–b", True), ("^[[:punct:]]+$", "€", True),
    ("a{2}", "aa", True), ("a{2}", "a", False), ("a{,2}b", "b", True), ("a{,2}b", "aab", True),
    ("a|", "b", True), ("(|a)", "b", True), ("()", "b", True), ("]", "]", True),
    ("[]a]", "]", True), ("[]a]", "a", True), ("[^]a]", "b", True), ("[^]a]", "a", False),
    (".", "ş", True), ("^.$", "ş", True), ("^.$", "東", True), ("^.$", "\U0001F600", True),
    ("^..$", "ş", False), ("[ıİşŞ]", "ı", True), ("[ıİşŞ]", "ş", True),
    ("^ş", "şehir", True), ("İ", "İzmir", True), ("İ", "izmir", False),
    ("^residential$", "residential", True), ("^residential$", "Residential", False),
    ("^(garage|shed)$", "garage", True), ("^(garage|shed)$", "shed", True),
    ("^(garage|shed)$", "garages", False),
    ("a.c", "abc", True), ("a.c", "a\nc", True), ("^a$", "a\n", False), ("^a$", "a", True),
    ("^[0-9]+$", "12", True), ("(ab)+", "abab", True), ("^(a|b)+$", "abba", True),
    ("[[:digit:]_-]+", "12_3-4", True), ("^[^[:digit:]]+$", "abc", True),
    ("^[^[:digit:]]+$", "a1", False),
    ("[a-]", "-", True), ("[-a]", "-", True), ("[[a]", "[", True), ("[a^]", "^", True),
    ("^[[:alpha:]]+ [[:digit:]]+$", "Sokak 12", True),
)

#: Characters glibc puts in each class (U+00AA ª, U+0663 ٣, U+24B6 Ⓐ,
#: U+24D0 ⓐ, U+2013 –, U+00AB «, U+00A0 no-break space, U+0301 combining
#: acute, U+00B2 ², U+1F600 grinning face, U+3000 ideographic space).
GLIBC_MEMBERS = {
    "alpha": "aZşİı東ª٣Ⓐⓐ",
    "digit": "1",
    "alnum": "aZ1şİı東ª٣Ⓐⓐ",
    "upper": "ZİⒶ",
    "lower": "aşıªⓐ",
    "space": " \t　",
    "punct": "-_€–« ́²\U0001F600",
}


def matches(pattern: str, value: str) -> bool:
    spec = TagSpec("k", pattern, "line")
    return bool(matching_specs({"k": value}, (spec,), "line", value_mode="regex"))


class ServerReadingTests(unittest.TestCase):
    def test_client_matches_what_glibc_matches(self) -> None:
        for pattern, value, expected in GLIBC:
            with self.subTest(pattern=pattern, value=value):
                normalize_tag("k", pattern, "regex")
                self.assertEqual(matches(pattern, value), expected)

    def test_posix_classes_never_refuse_a_glibc_member(self) -> None:
        for name, members in GLIBC_MEMBERS.items():
            compiled = re.compile("[" + query._class_text(name) + "]")
            for char in members:
                with self.subTest(cls=name, char=f"U+{ord(char):04X}"):
                    self.assertIsNotNone(compiled.fullmatch(char))
        self.assertEqual(set(GLIBC_MEMBERS), set(POSIX_CLASSES))

    def test_posix_classes_are_exact_on_ascii(self) -> None:
        # glibc's ASCII members, by definition of the POSIX classes.
        expected = {
            "alpha": lambda c: c.isalpha(),
            "digit": lambda c: c.isdigit(),
            "alnum": lambda c: c.isalnum(),
            "upper": lambda c: c.isupper(),
            "lower": lambda c: c.islower(),
            "space": lambda c: c in " \t\n\v\f\r",
            "punct": lambda c: 33 <= ord(c) <= 126 and not c.isalnum(),
        }
        for name, rule in expected.items():
            compiled = re.compile("[" + query._class_text(name) + "]")
            for code in range(128):
                char = chr(code)
                with self.subTest(cls=name, code=code):
                    self.assertEqual(compiled.fullmatch(char) is not None, rule(char))

    def test_translation_is_plain_python(self) -> None:
        self.assertEqual(client_regex("^[[:digit:]]+$"), "^[0-9]+\\Z")
        self.assertEqual(client_regex("^(garage|shed)$"), "^(garage|shed)\\Z")
        self.assertEqual(client_regex("[[a]"), "[\\[a]")
        self.assertEqual(client_regex("[^]a-]"), "[^\\]a\\-]")

    def test_the_live_posix_answer_is_kept(self) -> None:
        # The server returned these three buildings for ^[[:digit:]]+$; the
        # client used to refuse every one of them and report "matched nothing".
        answer = json.loads(
            (FIXTURES / "overpass_building_levels_regex.json").read_text(encoding="utf-8")
        )
        specs = advanced_specs(
            (("building:levels", "^[[:digit:]]+$"),), ("polygon",), value_mode="regex"
        )
        self.assertEqual(len(answer["elements"]), 3)
        for element in answer["elements"]:
            with self.subTest(way=element["id"]):
                self.assertEqual(
                    matching_specs(element["tags"], specs, "polygon", value_mode="regex"),
                    specs,
                )
        query_text = build_query(
            specs, (38.4204691, 27.1315155, 38.4209691, 27.1320471), value_mode="regex"
        )
        # The server still receives the POSIX pattern itself.
        self.assertIn('way["building:levels"~"^[[:digit:]]+$"]', query_text)


class RefusalTests(unittest.TestCase):
    #: pattern -> words the refusal must contain, naming the problem.
    REFUSED = {
        "(?i)park": "(?",
        "(?:garage|shed)": "(?",
        "^(?!house)": "(?",
        "foo(?=bar)": "(?",
        "(?<=a)b": "(?",
        "(?P<x>a)": "(?",
        "(a+)+": "repeats a group",
        "(a*)*": "repeats a group",
        "(a|b+)+": "repeats a group",
        "^([a-zA-Z]+ ?)*$": "repeats a group",
        "((a+)b)+": "repeats a group",
        "(a{2,3})*": "repeats a group",
        "a+?": "two repeat operators",
        "a*?b": "two repeat operators",
        "a++": "two repeat operators",
        "a*+": "two repeat operators",
        "a{2}+": "two repeat operators",
        "a{b": "'{'",
        "{": "'{'",
        "x{1,2": "'{'",
        "a{300}": "more than 255",
        "[[:foo:]]": "unknown class [:foo:]",
        "[[.a.]]": "does not support",
        "[[=e=]]": "does not support",
        "\\d+": "unsupported query characters",
        "a;b": "unsupported query characters",
    }

    def test_refused_with_a_reason_that_names_the_problem(self) -> None:
        for pattern, words in self.REFUSED.items():
            with self.subTest(pattern=pattern):
                with self.assertRaises(QueryError) as raised:
                    normalize_tag("name", pattern, "regex")
                self.assertIn(words, str(raised.exception))

    def test_invalid_patterns_are_still_refused(self) -> None:
        for pattern in ("(", "[", "*a", "+a", "^*", "a**", ")", "[a-", "[z-a]", "a{3,2}"):
            with self.subTest(pattern=pattern):
                with self.assertRaises(QueryError):
                    normalize_tag("name", pattern, "regex")

    def test_ordinary_patterns_pass(self) -> None:
        for pattern in (
            "^(garage|shed)$", "^[0-9]+$", "(ab)+", "^(a|b)$", ".", "a{2}", "a{0,2}b",
            "a{1,3}", "(ab)?c", "(a+)?", "^[[:digit:]]+$", "[[:alpha:] -]+",
            "Hanı|東京", "^ş", "[]a]", "[^]a]", "a|", "()", "a{2,}",
        ):
            with self.subTest(pattern=pattern):
                self.assertEqual(normalize_tag("name", pattern, "regex"), ("name", pattern))

    def test_a_repeat_count_without_its_lower_bound_is_sent_with_it(self) -> None:
        # glibc reads {,2} as {0,2}; PostgreSQL, behind Postpass, refuses it
        # with HTTP 400, which stopped the whole download.
        for typed, sent in (
            ("a{,2}b", "a{0,2}b"), ("(ab){,3}", "(ab){0,3}"), ("^x{,12}$", "^x{0,12}$"),
            ("[{,]x{,1}", "[{,]x{0,1}"),
        ):
            with self.subTest(typed=typed):
                self.assertEqual(normalize_tag("name", typed, "regex"), ("name", sent))
                self.assertEqual(server_regex(typed), sent)
                # This side reads both spellings alike.
                self.assertEqual(matches(typed, "aab"), matches(sent, "aab"))
        # Inside brackets "{,2}" is three characters, not a repeat.
        self.assertEqual(server_regex("[{,2}]"), "[{,2}]")
        specs = advanced_specs((("name", "a{,2}b"),), ("point",), value_mode="regex")
        self.assertEqual(specs[0].value, "a{0,2}b")
        overpass = TileRequest(specs, BOX, value_mode="regex").render()
        self.assertIn('node["name"~"a{0,2}b"]', overpass)
        statement = render_sql(specs, BOX, "any", "regex")
        self.assertIn("a{0,2}b".encode("utf-8").hex(), statement)
        self.assertNotIn("a{,2}b".encode("utf-8").hex(), statement)

    def test_a_catastrophic_pattern_never_reaches_the_client(self) -> None:
        # It held the GIL past a 20 s watchdog on a 47-character name.
        with self.assertRaises(QueryError):
            advanced_specs((("name", "^([a-zA-Z]+ ?)*$"),), ("point",), value_mode="regex")


# ------------------------------------------------- the server's own grammar
class PosixError(ValueError):
    """What glibc `regcomp` would answer instead of compiling."""


#: The class names POSIX defines for `[[:name:]]`.
POSIX_CLASS_NAMES = frozenset((
    "alpha", "digit", "alnum", "upper", "lower", "space", "punct", "print",
    "graph", "cntrl", "xdigit", "blank",
))


def _posix_bracket(pattern: str, start: int) -> int:
    """The index after the bracket expression that starts at `start`."""
    index = start + 1
    if pattern.startswith("^", index):
        index += 1
    if pattern.startswith("]", index):
        index += 1                      # a leading "]" is a literal
    while index < len(pattern):
        char = pattern[index]
        if char == "]":
            return index + 1
        kind = pattern[index + 1:index + 2]
        if char == "[" and kind in (":", ".", "="):
            close = pattern.find(kind + "]", index + 2)
            if close < 0:
                raise PosixError("Unmatched [, [^, [:, [., or [=")
            if kind == ":" and pattern[index + 2:close] not in POSIX_CLASS_NAMES:
                raise PosixError("Invalid character class name")
            index = close + 2
            continue
        index += 1
    raise PosixError("Unmatched [, [^, [:, [., or [=")


def posix_compile(pattern: str) -> None:
    """Raise PosixError where glibc regcomp(REG_EXTENDED) refuses `pattern`.

    Written from POSIX.1-2017 section 9.4 and glibc's syntax bits for
    REG_EXTENDED, not from the plugin's reader: under RE_CONTEXT_INVALID_OPS
    a repeat operator with nothing before it -- at the start, after "(",
    "|" or an anchor -- is REG_BADRPT, "Invalid preceding regular
    expression", which is how "(?:" fails.  It covers what the plugin sends:
    literals, escapes, brackets with classes, anchors, groups, alternation
    and repeats; it does not check the order of range ends.
    """
    depth = 0
    repeatable = False
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "\\":
            if index + 1 >= len(pattern):
                raise PosixError("Trailing backslash")
            index += 2
            repeatable = True
        elif char == "[":
            index = _posix_bracket(pattern, index)
            repeatable = True
        elif char == "(":
            depth += 1
            index += 1
            repeatable = False
        elif char == ")":
            if not depth:
                raise PosixError("Unmatched ) or \\)")
            depth -= 1
            index += 1
            repeatable = True
        elif char in "|^$":
            index += 1
            repeatable = False
        elif char in "*+?{":
            if not repeatable:
                raise PosixError("Invalid preceding regular expression")
            if char == "{":
                bound = re.match(r"\{(\d*)(,?)(\d*)\}", pattern[index:])
                if bound is None or not (bound.group(1) or bound.group(3)):
                    raise PosixError("Invalid content of \\{\\}")
                low = int(bound.group(1) or 0)
                high = int(bound.group(3)) if bound.group(3) else None
                if high is not None and high < low:
                    raise PosixError("Invalid content of \\{\\}")
                index += bound.end()
            else:
                index += 1
        else:
            index += 1
            repeatable = True
    if depth:
        raise PosixError("Unmatched ( or \\(")


#: Every regular expression string in an Overpass QL text: ~"..." .
_QL_REGEX = re.compile(r'~"((?:[^"\\]|\\.)*)"')


class PluginPatternsTests(unittest.TestCase):
    """Every regular expression the plugin writes itself compiles on the
    servers: the place search, the wildcard's selectors, and regex values as
    they leave normalize_tag."""

    PLACES = (
        "Konak", "Izmir Konak", "Saint-Denis", "St. Petersburg", "Kadıköy",
        "Bergama (Pergamon)", "100. Yıl Mahallesi", "Çiğli/Karşıyaka",
    )

    def test_the_grammar_reads_as_glibc_did(self) -> None:
        # Measured with glibc 2.43 regcomp(REG_EXTENDED|REG_NOSUB|REG_ICASE)
        # for the review: the old place pattern is refused, a plain group is
        # not.  Every pattern of the GLIBC table compiled there as well.
        for pattern in ("(?:^Izmir\\s+Konak$|^Konak$)", "(?:^Konak$)", "(?i)park",
                        "^(?!house)", "(?<=a)b", "(", "[a", "*a", "a{b", "[[:foo:]]"):
            with self.subTest(refused=pattern):
                with self.assertRaises(PosixError):
                    posix_compile(pattern)
        for pattern in ("(^Izmir\\s+Konak$|^Konak$)", "(^Saint\\-Denis$)",
                        "(^St\\.\\s+Petersburg$|^Petersburg$)", "(^Kadıköy$)",
                        *dict.fromkeys(item[0] for item in GLIBC)):
            with self.subTest(accepted=pattern):
                posix_compile(pattern)

    def _sent(self, text: str):
        patterns = _QL_REGEX.findall(text)
        self.assertTrue(patterns, text[:200])
        return patterns

    def test_the_place_search_sends_plain_groups(self) -> None:
        for name in self.PLACES:
            text = build_place_query(name)
            for pattern in self._sent(text):
                with self.subTest(place=name, pattern=pattern):
                    self.assertNotIn("(?", pattern)
                    posix_compile(pattern)
                    # Still the whole name, or its last word, and nothing else.
                    compiled = re.compile(pattern, re.IGNORECASE)
                    self.assertTrue(compiled.search(name.casefold()))
                    self.assertTrue(compiled.search(name.split()[-1]))
                    self.assertIsNone(compiled.search(f"x{name}"))
        self.assertIn('["name"~"(^Izmir\\s+Konak$|^Konak$)",i]', build_place_query("Izmir Konak"))

    def test_the_wildcard_and_regex_values_compile_on_the_servers(self) -> None:
        wildcard = TileRequest(get_preset("osm_as_is_full_visible_stack").tags, BOX).render()
        sent = self._sent(wildcard)
        self.assertEqual(set(sent), {".", "^(multipolygon|boundary)$"})
        values = ("^(garage|shed)$", "^[[:digit:]]+$", "a{,2}b", "[]a]x", "^ş", "(ab)?c")
        specs = advanced_specs(
            tuple(("name", value) for value in values), ("point", "line"), value_mode="regex"
        )
        regex_text = TileRequest(specs, BOX, value_mode="regex").render()
        for pattern in sent + self._sent(regex_text):
            with self.subTest(pattern=pattern):
                self.assertNotIn("(?", pattern)
                posix_compile(pattern)


if __name__ == "__main__":
    unittest.main()
