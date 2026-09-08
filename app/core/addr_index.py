#!/usr/bin/env python3
"""
Address index for the Ukrainian Address Registry CSV export.

Build:   python -m app.core.addr_index build data/addr.csv
Query:   python -m app.core.addr_index match "бульварно квадрявская" 15
Stats:   python -m app.core.addr_index stats

No Postgres. The street list is small enough to fuzzy-match in memory.
SQLite holds only what must persist: learned aliases and the interaction log.
"""

from __future__ import annotations

import csv
import json
import pickle
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

from rapidfuzz import fuzz, process
from rapidfuzz.distance import JaroWinkler

# Anchored to this file's location (app/core/addr_index.py -> project root),
# not the process's current working directory, so `data/` resolves correctly
# regardless of where the bot or CLI is launched from.
BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
INDEX_FILE = DATA_DIR / "addr_index.pkl"
DB_FILE = DATA_DIR / "addr.db"

# Latin glyphs that are visually identical to Cyrillic ones. The registry
# mixes both; without this mapping you get invisible duplicates.
LAT2CYR = str.maketrans("abcehkmoptxyABCEHKMOPTXY",
                        "авсенкмортхуАВСЕНКМОРТХУ")

APOSTROPHES = str.maketrans("", "", "'\u02bc\u2019`\u00b4")

# Registry placeholders for "no number assigned".
SKIP_VALUES = {"", "null", "none", "безномера", "б/н", "бн"}


def norm_text(s: str) -> str:
    """Street/city names. Hyphens become spaces so 'Бульварно-Кудрявська'
    matches dictated 'бульварно кудрявская'."""
    s = (s or "").lower().translate(APOSTROPHES)
    s = re.sub(r"[\s\-\u2013\u2014]+", " ", s)
    return s.strip()


def norm_house(s: str) -> str:
    """House numbers. Registry writes '37-Д' and '22/26 буд. 10';
    users say '37Д' and '22/26 корпус 10'."""
    s = re.sub(r"[\s.\-\u2013\u2014]", "", (s or "").lower())
    s = s.translate(LAT2CYR)
    return re.sub(r"(?:корп|кор|буд|к)(?=\d)", "к", s)


# main [letter] [/slash] [letter] [к corpus]
# The letter sits on either side of the slash: '37-Д' and '2/5-А' both occur.
HOUSE_RE = re.compile(
    r"^(\d+)([а-яіїєґ])?(?:/(\d+))?([а-яіїєґ])?(?:к(\d+))?$"
)

# Last resort: take the leading number and letter, ignore the rest.
FALLBACK_RE = re.compile(r"^(\d+)\s*[-/]?\s*([а-яіїєґ])?")

# Noise the registry puts inside house numbers.
NOISE_CHARS = re.compile(r"[«»\"'()_]")
LITERA_RE = re.compile(r"\bліт(?:ер[аи]?)?\b", re.IGNORECASE)
APT_RE = re.compile(r"\s*(?:кв|квартира)\s*№?\s*\d+.*$", re.IGNORECASE)


def parse_house(raw: str) -> dict:
    cleaned = NOISE_CHARS.sub("", (raw or "").strip())
    cleaned = LITERA_RE.sub("", cleaned)
    cleaned = APT_RE.sub("", cleaned)

    norm = norm_house(cleaned)
    blank = {"raw": raw, "norm": norm, "main": None, "letter": None,
             "slash": None, "corpus": None, "partial": False, "skip": False}

    if norm in SKIP_VALUES:
        return {**blank, "skip": True}

    m = HOUSE_RE.match(norm)
    if m:
        main, letter1, slash, letter2, corpus = m.groups()
        return {"raw": raw, "norm": norm, "main": int(main),
                "letter": letter1 or letter2,
                "slash": int(slash) if slash else None,
                "corpus": corpus, "partial": False, "skip": False}

    # Non-standard form ('30 літер А', '13/15/3', '12-А-14-А').
    # Keep the leading number, flag it so matches are confirmed, not silent.
    f = FALLBACK_RE.match(norm)
    if f:
        main, letter = f.groups()
        return {"raw": raw, "norm": f"{main}{letter or ''}", "main": int(main),
                "letter": letter, "slash": None, "corpus": None,
                "partial": True, "skip": False}

    return blank


PURE_NUM = re.compile(r"\d+[а-яіїєґА-ЯІЇЄҐ]?")


def parse_house_variants(raw: str) -> list:
    """A comma means two numbers for one building ('100,102') only when both
    sides are bare numbers. '28, буд. 49' is a single address."""
    parts = [p.strip() for p in (raw or "").split(",") if p.strip()]

    if len(parts) > 1 and all(PURE_NUM.fullmatch(p) for p in parts):
        out = []
        for p in parts:
            h = parse_house(p)
            h["raw"] = raw          # keep the registry form for display
            out.append(h)
        return out

    return [parse_house((raw or "").replace(",", " "))]


# ---------------------------------------------------------------- build

_index_cache: dict | None = None


def build(csv_path: str) -> dict:
    streets: dict = {}
    unparsed: list = []
    partial: list = []
    skipped = 0

    with open(csv_path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh, delimiter=";"):
            city = re.sub(r"^(місто|смт|селище|село)\s+", "",
                          row["addressAdminUnitL2"].strip())
            district = (row.get("district") or "").strip()
            st_type = row["addressThoroughfareType"].strip()
            st_name = row["addressThoroughfare"].strip()

            key = (norm_text(city), district, st_type, norm_text(st_name))
            street = streets.setdefault(key, {
                "city": city, "district": district, "type": st_type,
                "name": st_name, "norm": norm_text(st_name), "houses": {},
            })

            for h in parse_house_variants(row["addressLocatorDesignator"]):
                if h["skip"]:
                    skipped += 1
                    continue
                if h["main"] is None:
                    unparsed.append(h["raw"])
                    continue
                if h["partial"]:
                    partial.append(h["raw"])
                street["houses"].setdefault(h["norm"], h)

    # Per-street number range, used by the out_of_range sanity check.
    for s in streets.values():
        mains = [h["main"] for h in s["houses"].values() if h["main"]]
        s["range"] = (min(mains), max(mains)) if mains else (None, None)

    index = {
        "streets": list(streets.values()),
        "unparsed": unparsed,
        "partial": partial,
        "skipped": skipped,
    }
    DATA_DIR.mkdir(exist_ok=True)
    INDEX_FILE.write_bytes(pickle.dumps(index))
    init_db()
    global _index_cache
    _index_cache = index
    return index


def load() -> dict:
    """Loads the address index, caching it in memory after the first call.
    match_address()/match_street() call this on every request, and
    re-reading + unpickling a 2MB+ file each time blocks the asyncio event
    loop in the caller for no reason — the index never changes at runtime."""
    global _index_cache
    if _index_cache is None:
        if not INDEX_FILE.exists():
            sys.exit("Index not built. Run: python -m app.core.addr_index build data/addr.csv")
        _index_cache = pickle.loads(INDEX_FILE.read_bytes())
    return _index_cache


def street_key(s: dict) -> str:
    return f"{s['district']}|{s['type']}|{s['norm']}"


# ---------------------------------------------------------------- sqlite

_db_cache: sqlite3.Connection | None = None


def init_db() -> sqlite3.Connection:
    """Opens (once) and caches the SQLite connection. Every call used to
    re-open the file from scratch, which is blocking I/O that adds up across
    a busy bot process for no benefit — the schema only needs creating once,
    and the connection is cheap to reuse. check_same_thread=False because
    callers may run this from a worker thread via asyncio.to_thread; WAL mode
    lets that concurrent access happen without 'database is locked' errors."""
    global _db_cache
    if _db_cache is not None:
        return _db_cache

    DATA_DIR.mkdir(exist_ok=True)
    db = sqlite3.connect(DB_FILE, check_same_thread=False)
    db.execute("PRAGMA journal_mode = WAL;")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS aliases (
            alias_norm TEXT NOT NULL,
            street_key TEXT NOT NULL,
            kind       TEXT NOT NULL,
            hits       INTEGER DEFAULT 1,
            PRIMARY KEY (alias_norm, street_key)
        );
        CREATE TABLE IF NOT EXISTS log (
            id         INTEGER PRIMARY KEY,
            ts         TEXT DEFAULT (datetime('now')),
            transcript TEXT,
            extracted  TEXT,
            candidates TEXT,
            confirmed  TEXT
        );
    """)
    db.commit()
    _db_cache = db
    return db


def learn_alias(alias_norm: str, key: str, kind: str = "stt_error") -> None:
    db = init_db()
    db.execute("""INSERT INTO aliases (alias_norm, street_key, kind)
                  VALUES (?, ?, ?)
                  ON CONFLICT DO UPDATE SET hits = hits + 1""",
               (alias_norm, key, kind))
    db.commit()


def log_interaction(transcript: str, extracted: dict,
                    candidates: list, confirmed: str | None) -> None:
    db = init_db()
    db.execute("INSERT INTO log (transcript, extracted, candidates, confirmed) "
               "VALUES (?, ?, ?, ?)",
               (transcript,
                json.dumps(extracted, ensure_ascii=False),
                json.dumps(candidates, ensure_ascii=False, default=str),
                confirmed))
    db.commit()


# ---------------------------------------------------------------- match

def match_street(query: str, index: dict, district: str | None = None,
                 st_type: str | None = None, limit: int = 5) -> list:
    q = norm_text(query)

    # A learned alias wins outright.
    db = init_db()
    row = db.execute("SELECT street_key FROM aliases WHERE alias_norm = ? "
                     "ORDER BY hits DESC LIMIT 1", (q,)).fetchone()
    if row:
        for s in index["streets"]:
            if street_key(s) == row[0]:
                return [(s, 100.0)]

    pool = index["streets"]
    if district:
        pool = [s for s in pool if s["district"] == district] or pool

    def blend(a: str, b: str, **kw) -> float:
        # Jaro-Winkler weights the prefix, which is where Ukrainian street
        # names differ; WRatio alone rewards the shared '-ська' ending.
        return 0.4 * fuzz.WRatio(a, b) + 0.6 * JaroWinkler.similarity(a, b) * 100

    hits = process.extract(q, [s["norm"] for s in pool],
                           scorer=blend, limit=limit)
    
    scored = [(pool[i], float(score)) for _, score, i in hits]

    # street_type is a tie-breaker only, never a filter: the registry has
    # 'лінія 2' as both лінія and вулиця, and clients say 'вулиця' regardless.
    if st_type:
        scored = [(s, sc + (0.5 if s["type"] == st_type else 0.0))
                  for s, sc in scored]
        scored.sort(key=lambda x: -x[1])
    return scored


def match_house(street: dict, raw: str) -> tuple:
    """Returns (verdict, house).
    verdict: exact | in_range | out_of_range | unparsed"""
    h = parse_house(raw)
    if h["main"] is None:
        return "unparsed", h

    hit = street["houses"].get(h["norm"])
    if hit:
        # A registry entry parsed by fallback is not trustworthy enough
        # to accept silently — send it to confirmation instead.
        return ("in_range" if hit["partial"] else "exact"), hit

    lo, hi = street["range"]
    if lo is None:
        return "in_range", h

    # Slack on top: the registry has gaps on individual streets, so only
    # flag numbers that are implausible by a wide margin.
    if h["main"] > hi * 1.5 and h["main"] > hi + 20:
        return "out_of_range", h
    return "in_range", h


def match_address(street_q: str, house_q: str, district: str | None = None,
                  st_type: str | None = None) -> dict:
    index = load()
    cands = match_street(street_q, index, district, st_type)
    if not cands:
        return {"status": "no_street"}

    def as_option(s, sc):
        return {"key": street_key(s),
                "label": f"{s['type']} {s['name']} ({s['district']})",
                "score": round(sc, 1)}

    # Near-exact hits. Several of them means the same name in several
    # districts — that is a real question for the user, not a weak match.
    strong = [(s, sc) for s, sc in cands if sc >= 95]
    if len(strong) > 1:
        return {"status": "ambiguous",
                "options": [as_option(s, sc) for s, sc in strong[:4]]}

    if strong:
        top, top_score = strong[0]
    else:
        top, top_score = cands[0]
        runner_up = cands[1][1] if len(cands) > 1 else 0.0
        if top_score - runner_up < 8:
            return {"status": "ambiguous",
                    "options": [as_option(s, sc) for s, sc in cands[:3]]}

    verdict, house = match_house(top, house_q)
    if verdict == "out_of_range":
        return {"status": "recheck_house",
                "street": f"{top['type']} {top['name']}",
                "district": top["district"],
                "house_range": top["range"],
                "reason": f"house {house['main']} vs range {top['range']}"}

    result = {
        "status": "ok",
        "verdict": verdict,
        "score": round(top_score, 1),
        "key": street_key(top),
        "formatted": f"{top['type']} {top['name']}, {house['raw']}",
        "district": top["district"],
        "city": top["city"],
    }

    # in_range from a same-number-different-letter/corpus miss (not just an
    # absent number) is a confident near-match, not a blind guess — surface
    # the registry's actual variants so the caller doesn't present it with
    # the same silent confidence as an exact hit.
    if verdict == "in_range":
        same_number = [h["raw"] for h in top["houses"].values()
                       if h["main"] == house["main"] and h["norm"] != house["norm"]]
        if same_number:
            result["similar_houses"] = same_number

    return result

# ---------------------------------------------------------------- cli

def stats() -> None:
    index = load()
    streets = index["streets"]
    by_type: dict = defaultdict(int)
    for s in streets:
        by_type[s["type"]] += 1
    total_houses = sum(len(s["houses"]) for s in streets)

    print(f"streets: {len(streets)}   houses: {total_houses}")
    print(f"avg houses/street: {total_houses / max(len(streets), 1):.1f}")
    print(f"skipped (no number): {index.get('skipped', 0)}")

    print("\nstreet_type enum for the Gemini schema:")
    print(json.dumps(sorted(by_type), ensure_ascii=False))

    part = index.get("partial", [])
    print(f"\nparsed by fallback: {len(part)}")
    if part:
        print("samples:", part[:15])

    if index["unparsed"]:
        print(f"\nstill unparsed: {len(index['unparsed'])}")
        print("samples:", index["unparsed"][:20])
    else:
        print("\nstill unparsed: 0")

    dupes: dict = defaultdict(list)
    for s in streets:
        dupes[(s["city"], s["norm"])].append(f"{s['district']}/{s['type']}")
    collisions = {k: v for k, v in dupes.items() if len(v) > 1}
    print(f"\nname collisions across district/type: {len(collisions)}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    if cmd == "build":
        idx = build(sys.argv[2])
        print(f"built: {len(idx['streets'])} streets\n")
        stats()
    elif cmd == "match":
        print(json.dumps(
            match_address(sys.argv[2], sys.argv[3],
                          sys.argv[4] if len(sys.argv) > 4 else None),
            ensure_ascii=False, indent=2, default=str))
    else:
        stats()