#!/usr/bin/env python3
"""
Dev-only CLI: exercise the full resolve() pipeline (street type split,
house/corpus folding, alternative-name retry, numbered-street disambiguation)
without running Gemini or Telegram.

Usage:
  python dev_resolve.py "<street>" "<house>" [district] [transcript]

Examples:
  python dev_resolve.py "вул. Садова" "3"
  python dev_resolve.py "Хрещатик" "22" Печерський
  python dev_resolve.py "Бульварно-Кудрявська" "15" "" "бульварно-квадрявская 15"
"""

from __future__ import annotations

import json
import sys

from app.core.resolve import resolve


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    street = sys.argv[1]
    house = sys.argv[2]
    district = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] else None
    transcript = sys.argv[4] if len(sys.argv) > 4 else f"{street} {house}"

    payload = {
        "transcription": transcript,
        "has_address": True,
        "address": {
            "street": street,
            "house_number": house,
            "district": district,
        },
    }
    verdict = resolve(payload)
    print(json.dumps(verdict, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
