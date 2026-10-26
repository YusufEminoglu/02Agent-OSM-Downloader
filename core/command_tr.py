# -*- coding: utf-8 -*-
"""Turkish commands for the offline router.

The router in :mod:`catalog` understands English. This module recognises a
Turkish command and rewrites it into the English form the router already
handles, so both languages share one preset vocabulary, one place extractor
and one exclusion rule:

* dataset words are matched by stem, so the case and plural suffixes Turkish
  attaches to a noun (``park``, ``parklar``, ``parkları``, ``parklarını``)
  all reach the same preset;
* a place is the capitalised name before a locative or genitive suffix
  (``Konak'ta``, ``İzmir'deki``, ``Kadıköy'ün``) or before a postposition
  (``Konak içinde``, ``Bornova çevresinde``, ``Çankaya ilçesinde``), and it is
  handed on with its own spelling, because Nominatim matches ``Kadıköy`` and
  not ``kadikoy``;
* ``X hariç``, ``X dışında`` and ``X olmadan`` become ``without X``.

A command with no Turkish signal is returned unchanged (``None``), so English
behaviour cannot move. Only what the command box *accepts* widens; the
interface itself stays English.
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

#: (Turkish stems, English phrase the router knows). Multi-word stems come
#: first so "otobüs durakları" is not read as "otobüs" alone. A stem matches a
#: word that starts with it and carries at most a few suffix letters.
_DATASETS: Tuple[Tuple[Tuple[str, ...], str], ...] = (
    (("otobüs durağı", "otobüs durak", "otobus durak"), "bus stops"),
    (("bisiklet yolu", "bisiklet yol", "bisiklet ağı", "bisiklet ag"), "cycle network"),
    (("bisiklet park",), "bike parking"),
    (("tren istasyon", "metro istasyon", "raylı sistem", "rayli sistem"), "rail transit"),
    (("toplu taşıma", "toplu tasima"), "public transport"),
    (("yeşil alan", "yesil alan"), "green space"),
    (("arazi kullanım", "arazi kullanim", "alan kullanım"), "land use"),
    (("kentsel doku", "kent dokusu"), "urban form"),
    (("ibadet yer",), "places of worship"),
    (("tarihi yer", "tarihi yapı", "tarihi eser"), "historic"),
    (("mahalle sınır", "mahalle sinir"), "admin level 8"),
    (("ilçe sınır", "ilce sinir"), "admin level 6"),
    (("il sınır", "il sinir"), "admin level 4"),
    (("yol ağı", "yol agi", "sokak ağı", "sokak agi"), "road network"),
    (("bina", "yapı", "yapi"), "buildings"),
    (("yol", "sokak", "cadde", "bulvar"), "road network"),
    (("park",), "parks"),
    (("orman",), "forest"),
    (("ağaç", "agac"), "trees"),
    (("okul", "üniversite", "universite", "eğitim", "egitim"), "school"),
    (("hastane", "klinik", "sağlık", "saglik"), "hospital"),
    (("eczane",), "pharmacy"),
    (("cami",), "mosque"),
    (("kilise",), "church"),
    (("durak",), "bus stop"),
    (("otobüs", "otobus"), "bus"),
    (("metro", "tramvay"), "metro"),
    (("demiryol", "demir yol", "tren"), "railway"),
    (("otopark",), "parking"),
    (("akaryakıt", "akaryakit", "benzinlik"), "fuel"),
    (("şarj", "sarj"), "charging"),
    (("spor", "stadyum", "stadyum", "saha"), "sport"),
    (("otel", "müze", "muze", "turizm", "turistik"), "tourism"),
    (("tarihi", "miras"), "heritage"),
    (("itfaiye", "polis", "acil", "toplanma alan"), "emergency"),
    (("nehir", "dere", "göl", "gol"), "water"),
    (("trafik ışık", "trafik isik", "sinyal"), "traffic signal"),
    (("yaya geçidi", "yaya gecidi"), "crossing"),
    (("mahalle", "semt", "köy", "koy", "kasaba", "şehir", "sehir"), "places"),
)

_VERBS = ("indir", "getir", "göster", "goster", "yükle", "yukle", "bul",
          "listele", "çek", "cek")
_NEGATIONS = ("hariç", "haric", "dışında", "disinda", "olmadan", "olmaksızın",
              "olmaksizin")
_POSTPOSITIONS = ("içinde", "icinde", "içindeki", "icindeki", "çevresinde",
                  "cevresinde", "civarında", "civarinda", "yakınında",
                  "yakininda", "ilçesinde", "ilcesinde", "ilçesindeki",
                  "ilinde", "ilindeki", "mahallesinde", "mahallesindeki",
                  "şehrinde", "sehrinde", "için", "icin")
#: Locative, ablative and genitive endings after an apostrophe.
_SUFFIXES = ("ndaki", "ndeki", "ndan", "nden", "nda", "nde", "na", "ne",
             "daki", "deki", "taki", "teki", "dan", "den", "tan", "ten",
             "nın", "nin", "nun", "nün", "ın", "in", "un", "ün",
             "da", "de", "ta", "te", "a", "e", "ya", "ye")
_UPPER = "A-ZÇĞİÖŞÜ"
_WORD = r"[A-Za-zÇĞİÖŞÜçğıöşüâîû.\-]+"
_NAME = rf"[{_UPPER}]{_WORD}"
_PLACE_APOSTROPHE = re.compile(
    rf"((?:{_NAME}(?:\s*,\s*|\s+))*{_NAME})['’](?:{'|'.join(_SUFFIXES)})\b"
)
_PLACE_POSTPOSITION = re.compile(
    rf"((?:{_NAME}(?:\s*,\s*|\s+))*{_NAME})\s+(?:{'|'.join(_POSTPOSITIONS)})\b"
)
_MAX_SUFFIX = 7


def _lower(text: str) -> str:
    """Turkish-aware lower case: İ -> i and I -> ı, then the usual fold."""
    return text.replace("İ", "i").replace("I", "ı").lower()


#: Every (stem, phrase) pair, longest stem first, so a compound such as
#: "otopark" or "tren istasyonu" claims its words before "park" or "tren" can.
_STEMS: Tuple[Tuple[str, str], ...] = tuple(sorted(
    ((stem, english) for stems, english in _DATASETS for stem in stems),
    key=lambda item: -len(item[0]),
))


def _find_datasets(text: str) -> List[str]:
    """English phrases for every Turkish dataset word in ``text``, in order."""
    lowered = f" {_lower(text)} "
    found: List[Tuple[int, str]] = []
    taken: List[Tuple[int, int]] = []
    for stem, english in _STEMS:
        pattern = re.compile(
            rf"(?<![a-zçğıöşü]){re.escape(stem)}[a-zçğıöşü]{{0,{_MAX_SUFFIX}}}"
        )
        for match in pattern.finditer(lowered):
            start, end = match.span()
            if any(s < end and start < e for s, e in taken):
                continue
            taken.append((start, end))
            found.append((start, english))
    found.sort()
    phrases: List[str] = []
    for _pos, english in found:
        if english not in phrases:
            phrases.append(english)
    return phrases


def _is_command_word(word: str) -> bool:
    lowered = _lower(word)
    return lowered.startswith(_VERBS) or bool(_find_datasets(word))


#: Stems that are also ordinary English words, so they alone do not make a
#: command Turkish ("parks in Kadıköy" is English with a Turkish place name).
_SHARED_WITH_ENGLISH = {"park", "metro"}
_ENGLISH_START = re.compile(r"^\s*(?:download|get|fetch|load|show)\b", re.IGNORECASE)


def _is_turkish(text: str) -> bool:
    """True on a Turkish grammatical signal; Turkish letters alone are not one.

    A place name such as Kadıköy carries Turkish letters inside an English
    command, and the router's own English output ("download parks in
    Kadıköy") must never be translated a second time.
    """
    if _ENGLISH_START.match(text):
        return False
    lowered = _lower(text)
    words = re.findall(r"[a-zçğıöşü]+", lowered)
    if any(word.startswith(_VERBS) for word in words):
        return True
    if any(word in _NEGATIONS or word in _POSTPOSITIONS for word in words):
        return True
    if _PLACE_APOSTROPHE.search(text):
        return True
    for stem, _english in _STEMS:
        if stem in _SHARED_WITH_ENGLISH:
            continue
        if re.search(rf"(?<![a-zçğıöşü]){re.escape(stem)}", lowered):
            return True
    return False


def _extract_place(text: str) -> Tuple[str, str]:
    """``(place, text without it)``; the place keeps its own spelling."""
    for pattern in (_PLACE_APOSTROPHE, _PLACE_POSTPOSITION):
        match = pattern.search(text)
        if match:
            words = match.group(1).strip(" ,").split()
            # A sentence-initial capital ("Parkları Konak'ta indir") is not part
            # of the place: drop leading dataset words and verbs.
            dropped = 0
            while len(words) > 1 and _is_command_word(words[0].strip(",")):
                words.pop(0)
                dropped += 1
            place = " ".join(words).strip(" ,")
            if not place or _is_command_word(place):
                continue
            start = match.start(1) + len(" ".join(match.group(1).split()[:dropped]))
            rest = (text[:start] + " " + text[match.end():]).strip()
            return place, rest
    return "", text


def _split_negation(text: str) -> Tuple[str, str]:
    """``(positive part, negated part)`` around hariç / dışında / olmadan."""
    lowered = _lower(text)
    for word in _NEGATIONS:
        match = re.search(rf"(?<![a-zçğıöşü]){word}(?![a-zçğıöşü])", lowered)
        if not match:
            continue
        before = text[: match.start()]
        after = text[match.end():]
        # The negated phrase is the clause right before the keyword, after
        # the last comma or "ama"/"fakat"; whatever follows it stays positive.
        parts = re.split(r",|\bama\b|\bfakat\b|\bancak\b", before)
        negated = parts[-1]
        positive = ",".join(parts[:-1]) + " " + after
        return positive.strip(), negated.strip()
    return text, ""


def translate(text: object) -> Optional[str]:
    """English rendering of a Turkish command, or ``None`` if it is not one."""
    raw = " ".join(str(text or "").split())
    if not raw or not _is_turkish(raw):
        return None
    tag = re.search(r"\b[A-Za-z0-9_:.~-]+\s*=\s*[^\s,;]*", raw)
    place, rest = _extract_place(raw)
    positive, negated = _split_negation(rest)
    phrases = _find_datasets(positive)
    excluded = _find_datasets(negated)
    tag_text = ""
    if tag and tag.group(0) in positive:
        tag_text = tag.group(0).replace(" ", "")
    if not phrases and not tag_text and not place:
        return None
    parts = ["download"]
    if tag_text:
        parts.append(tag_text)
    if phrases:
        parts.append(" and ".join(phrases))
    if excluded:
        parts.append("without " + excluded[0])
    elif negated and re.search(r"[A-Za-z0-9_:.~-]+=", negated):
        parts.append("without " + negated.split()[0])
    if place:
        parts.append(f"in {place}")
    return " ".join(parts)
