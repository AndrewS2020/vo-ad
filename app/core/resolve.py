#!/usr/bin/env python3
"""
Adapter between GeminiService.ProcessedResult and addr_index matching.

The Gemini schema and the registry index disagree on three points:
  1. Gemini returns 'вул. Велика Васильківська' — type and name in one string,
     abbreviated. The index stores them separately, unabbreviated.
  2. Gemini returns building_block as free text ('корпус 2'). The index expects
     the corpus folded into the house number.
  3. Gemini surfaces old/alternative street names. The index learns them as
     aliases, but only once the user confirms.

This module does that translation and nothing else. Neither side is modified.
"""

from __future__ import annotations

import logging
import re

from app.core.addr_index import (
    learn_alias,
    load,
    log_interaction,
    match_address,
    match_street,
    norm_text,
    street_key,
)

logger = logging.getLogger(__name__)

# Abbreviated forms Gemini emits -> the 14 values present in the registry.
TYPE_MAP = {
    "вул": "вулиця", "вулиця": "вулиця",
    "просп": "проспект", "пр": "проспект", "проспект": "проспект",
    "пр-т": "проспект", "пр-кт": "проспект",
    "бул": "бульвар", "бульв": "бульвар", "бульвар": "бульвар",
    "б-р": "бульвар",
    "пров": "провулок", "провулок": "провулок",
    "пров-к": "провулок",
    "пл": "площа", "площа": "площа",
    "наб": "набережна", "набережна": "набережна",
    "наб-на": "набережна",
    "узвіз": "узвіз", "узв": "узвіз",
    "проїзд": "проїзд", "шосе": "шосе", "алея": "алея",
    "лінія": "лінія", "майдан": "майдан", "дорога": "дорога",
    "тупик": "тупик",
}


def split_street(raw: str) -> tuple:
    """'вул. Велика Васильківська' -> ('вулиця', 'Велика Васильківська').
    Returns (None, raw) when no type prefix is present."""
    s = (raw or "").strip()
    if not s:
        return None, ""

    # Type can lead ('вул. Садова') or trail ('Володимирський узвіз').
    # Hyphenated abbreviations ('пр-т', 'б-р') need the hyphen in the class too.
    head = re.match(r"^([А-Яа-яІіЇїЄєҐґ'\-]+)\.?\s+(.+)$", s)
    if head:
        key = head.group(1).lower().rstrip(".")
        if key in TYPE_MAP:
            return TYPE_MAP[key], head.group(2).strip()

    tail = re.match(r"^(.+?)\s+([А-Яа-яІіЇїЄєҐґ'\-]+)$", s)
    if tail and tail.group(2).lower() in TYPE_MAP:
        return TYPE_MAP[tail.group(2).lower()], tail.group(1).strip()

    # No known type prefix matched, but the leading word looks like an
    # abbreviation we don't recognize yet (ends with '.' or is a short
    # hyphenated token) — log it so the next unfamiliar form ('пр-т' before
    # it was added) surfaces on its own instead of silently losing the
    # street_type tie-breaker in match_address.
    leading_word = s.split(" ", 1)[0]
    bare_word = leading_word.rstrip(".")
    if head and (leading_word.endswith(".") or
                 ("-" in bare_word and len(bare_word) <= 6)):
        logger.warning("split_street: unrecognized type abbreviation %r in %r",
                       head.group(1), s)

    return None, s


def build_house(house_number: str | None, building_block: str | None) -> str:
    """'12' + 'корпус 2' -> '12к2'. Entrance, floor and apartment are not part
    of the address key and must not be folded in."""
    num = (house_number or "").strip()
    if not num:
        return ""
    if building_block:
        m = re.search(r"(\d+[а-яіїєґ]?)", building_block, re.IGNORECASE)
        if m:
            return f"{num}к{m.group(1)}"
    return num


def _numbered_street_option(st_name: str, house: str, district: str | None) -> dict | None:
    """324 registry streets have a bare number baked into their own name
    ('Садова 3', 'Лінія 5', 'Набережна 1') — a separate street from 'Садова'
    with its own house range. "Садова, будинок 3" spoken aloud is
    indistinguishable from a client naming the street 'Садова 3' — nothing in
    the audio marks which number is the street name and which is the house.
    Checks whether '<name> <house>' is itself a registry street; if so it's
    returned as an extra disambiguation candidate rather than silently
    assumed away."""
    combined = f"{st_name} {house}".strip()
    index = load()
    hits = match_street(combined, index, district=district, limit=1)
    if hits and hits[0][1] >= 99:
        s = hits[0][0]
        return {"key": street_key(s),
                "label": f"{s['type']} {s['name']} ({s['district']})",
                "score": 100.0,
                "street": f"{s['type']} {s['name']}",
                "street_district": s["district"],
                # The number that looked like a house number turned out to be
                # part of this street's own name, so it tells us nothing about
                # the actual house — the caller must re-ask for it, never
                # match this option's key against the original house number.
                "needs_house": True}
    return None


def resolve(result: dict) -> dict:
    """Takes ProcessedResult.model_dump(). Returns the match_address verdict,
    with the Gemini payload attached for the confirmation step."""
    if not result.get("has_address") or not result.get("address"):
        return {"status": "no_address",
                "transcript": result.get("transcription", "")}

    addr = result["address"]
    st_type, st_name = split_street(addr.get("street") or "")
    house = build_house(addr.get("house_number"), addr.get("building_block"))

    if not st_name or not house:
        return {"status": "incomplete",
                "have_street": bool(st_name), "have_house": bool(house),
                "transcript": result.get("transcription", "")}

    verdict = match_address(st_name, house,
                            district=addr.get("district"),
                            st_type=st_type)

    # A weak match on the modern name may still be a strong one on the old name
    # the client actually used.
    if verdict.get("status") in ("ambiguous", "no_street"):
        alt = addr.get("street_old_or_alternative_name")
        if alt:
            _, alt_name = split_street(alt)
            alt_verdict = match_address(alt_name, house,
                                        district=addr.get("district"))
            if alt_verdict.get("status") == "ok":
                alt_verdict["matched_via"] = "alternative_name"
                verdict = alt_verdict

    # A bare, unambiguous house number is exactly the case where the
    # "<street> <number>" name collision bites — check it regardless of how
    # confidently the plain interpretation matched.
    if house.isdigit():
        numbered = _numbered_street_option(st_name, house, addr.get("district"))
        if numbered:
            if verdict.get("status") == "ok":
                plain_option = {"key": verdict["key"], "label": verdict["formatted"],
                                "score": verdict.get("score", 100.0)}
                verdict = {"status": "ambiguous",
                          "options": [plain_option, numbered]}
            elif verdict.get("status") in ("no_street", "ambiguous"):
                options = verdict.get("options", [])
                if numbered["key"] not in {o["key"] for o in options}:
                    verdict = {"status": "ambiguous", "options": options + [numbered]}

    verdict["transcript"] = result.get("transcription", "")
    verdict["query"] = {"street": st_name, "type": st_type, "house": house,
                        "alt": addr.get("street_old_or_alternative_name")}
    # Not part of the registry key, but the courier needs them.
    verdict["extras"] = {k: addr.get(k) for k in
                         ("entrance", "floor", "apartment_or_office",
                          "intercom", "comment_for_courier",
                          "residential_complex", "metro_or_landmark")
                         if addr.get(k)}
    return verdict


def confirm(verdict: dict, chosen_key: str) -> None:
    """Call after the user taps a confirmation button. Turns this interaction
    into a permanent alias so the same misrecognition matches instantly."""
    q = verdict.get("query", {})
    if q.get("street"):
        learn_alias(norm_text(q["street"]), chosen_key, "stt_error")
    if q.get("alt"):
        _, alt_name = split_street(q["alt"])
        if alt_name:
            learn_alias(norm_text(alt_name), chosen_key, "old_name")

    log_interaction(verdict.get("transcript", ""), q,
                    verdict.get("options", []), chosen_key)


def reject(verdict: dict) -> None:
    """Call after the user taps 'Ні'/'Жодного'. No alias is learned — a
    rejection doesn't tell us which street was meant, only that this one
    wasn't — but the miss is still logged, since a wrong guess is exactly the
    kind of turn worth mining when tuning the prompt or the matcher."""
    q = verdict.get("query", {})
    log_interaction(verdict.get("transcript", ""), q,
                    verdict.get("options", []), None)


if __name__ == "__main__":
    sample = {
        "transcription": "Це на бульварно-квадрявская, дом пятнадцать, корпус 2",
        "has_address": True,
        "language": "ru",
        "address": {
            "city": "Київ",
            "street": "вул. Бульварно-Кудрявська",
            "house_number": "15",
            "building_block": "корпус 2",
            "district": None,
            "entrance": None,
            "full_formatted_address": "вул. Бульварно-Кудрявська, 15",
        },
    }
    import json
    print(json.dumps(resolve(sample), ensure_ascii=False, indent=2))