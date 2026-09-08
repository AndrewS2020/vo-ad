# Pipeline

End-to-end flow from a Telegram voice message to a confirmed, registry-normalized
Kyiv address. See [CLAUDE.md](CLAUDE.md) for the full project context and the
matching rules this pipeline depends on.

```
Telegram voice (OGG/Opus)
        │
        ▼
┌─────────────────────┐
│  1. Download          │  app/bot.py: on_voice
│  bot.get_file +       │  no ffmpeg — Gemini accepts audio/ogg directly
│  bot.download_file    │
└──────────┬───────────┘
           ▼
┌─────────────────────┐
│  2. Gemini call       │  GeminiService.process_audio()
│  audio in →           │  one call: transcription + address extraction,
│  ProcessedResult      │  not two separate steps
└──────────┬───────────┘
           ▼
┌─────────────────────┐
│  3. resolve()         │  app/core/resolve.py
│  split_street +       │  splits "просп. X" -> (type, name),
│  build_house +        │  folds building_block into the house key,
│  match_address        │  calls into addr_index for the actual match
└──────────┬───────────┘
           ▼
     status branch (app/bot.py: _handle_verdict)
           │
   ┌───────┼────────┬─────────────┬──────────────┬─────────────┐
   ▼       ▼        ▼             ▼              ▼              ▼
  ok   ambiguous  recheck_house  no_street   incomplete    no_address
```

## 1. Download

`app/bot.py: on_voice` downloads the Telegram voice file as-is to
`settings.temp_audio_dir` and deletes it in a `finally` block after the Gemini
call, success or failure. No conversion, no ffmpeg anywhere in this project —
Gemini's `audio/ogg` MIME type handles Telegram's native format directly.

## 2. Gemini call

`GeminiService.process_audio()` (`app/services/gemini_service.py`) sends the
audio plus the system prompt (`prompts/address_prompt.md`) in one multimodal
call and gets back a `ProcessedResult`:

- `transcription` — verbatim transcript, always present, even when no address
  was found.
- `has_address`, `language`.
- `address: Optional[AddressInfo]` — `street` (type abbreviation inline, e.g.
  `"просп. Академіка Палладіна"`), `street_old_or_alternative_name`,
  `house_number`, `building_block`, `apartment_or_office`,
  `residential_complex`, `district`, `metro_or_landmark`,
  `full_formatted_address`.

On a 429/503 from the primary model, `GeminiService` retries against
`FALLBACK_MODELS` before giving up; `bot.py` catches whatever exception
survives that and replies with a plain-language "too many requests" message
instead of a stack trace.

## 3. resolve()

`app/core/resolve.py` adapts the Gemini payload into an `addr_index.match_address()`
call:

- `split_street()` — splits `"просп. X"` into `("проспект", "X")` using
  `TYPE_MAP`. Losing this split means `st_type` is `None` and the tie-breaker
  in `match_address` never fires — this has caused real disambiguation bugs
  (see the `app/core/resolve.py` fix history for `"пр-т"`).
- `build_house()` — folds `building_block` ("корпус 2") into the house key
  ("12к2"). Entrance/floor/apartment are not part of the address key.
- `match_address()` (`app/core/addr_index.py`) — fuzzy-matches the street
  (`rapidfuzz.fuzz.WRatio`, street_type as a tie-breaker only, never a
  filter), then matches the house number by exact integer + letter/corpus,
  never fuzzily.
- If the primary name comes back `ambiguous`/`no_street`, resolve() retries
  with `street_old_or_alternative_name` — this is what makes renamed streets
  (e.g. the historical name Gemini sometimes puts in the wrong field) still
  resolve correctly.

## 4. Status branch

`app/bot.py: _handle_verdict` branches on `verdict["status"]`:

| Status | Meaning | Bot response |
|---|---|---|
| `ok` | Street + house matched (`exact`, `in_range`, or a `partial` registry entry) | Shows the formatted address with a ✅/❌ confirm keyboard |
| `ambiguous` | Same street name in multiple districts, or no candidate beats the runner-up by ≥15 points | Shows up to 4 street options as buttons |
| `recheck_house` | Street found, but the house number is implausible (`> 1.5× max` and `> max + 20`) | Asks to redictate just the house number |
| `no_street` | No street candidate at all | Asks to redictate the whole address |
| `incomplete` | `resolve()` couldn't extract a street and/or house from the payload | Names exactly what's missing |
| `no_address` | Gemini returned `has_address: false` | Asks to redictate |

All of these except `ambiguous`/`recheck_house`/`ok` are dead ends — the user
just tries again. The other three keep state in `pending[user_id]` (an
in-memory dict; fine for a single long-polling process) so the next message
can be interpreted in context instead of as a fresh address:

- **`recheck_house` → voice or text retry**: `on_voice`/`on_house_retry` check
  `pending` first. If the pending verdict is `recheck_house`, the new message
  is treated as *just the house number*, not run through Gemini's full address
  extraction again — a bare spoken number ("вісімнадцять") never contains a
  street, so re-extracting would just come back `has_address: false`.
  `_normalize_house_number()` converts spelled-out Ukrainian/Russian numbers
  to digits first, since both Gemini's prompt and `addr_index.parse_house()`
  expect digits.
- **`ambiguous` → button pick**: `on_pick_option` matches the house number
  directly against the *chosen* street (`_match_house_on_chosen_street()`),
  bypassing `match_street()` entirely. Re-resolving by name + district can
  still land on >1 candidate with a near-tied score (e.g. several
  "Академіка X" streets in the same district) and send the user right back
  into another disambiguation round instead of resolving the house.
- **`ok` → confirm button**: `on_confirm` calls `confirm(verdict, verdict["key"])`
  from `app/core/resolve.py`, which writes the interaction to `log` and, on a fuzzy
  (non-exact) match, promotes it into `aliases` — the feedback loop described
  in CLAUDE.md.

## Files in this pipeline

```
app/bot.py                       aiogram handlers, the state machine above
app/services/gemini_service.py   GeminiService: step 2
app/core/resolve.py              step 3 adapter
app/core/addr_index.py           fuzzy matching + SQLite persistence
prompts/address_prompt.md        system prompt for step 2
```
