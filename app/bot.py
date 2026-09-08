from __future__ import annotations

import asyncio
import logging
import uuid

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Voice,
)
from google.genai.errors import APIError

from app.config import settings
from app.core.addr_index import load as load_addr_index
from app.core.addr_index import match_house, street_key
from app.core.resolve import confirm, resolve
from app.services.gemini_service import GeminiService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

router = Router()
gemini = GeminiService()

# user_id -> verdict dict from resolve(), kept until confirmed or replaced.
# Cost matters more than polish: an in-memory dict is enough for a test bot
# running a single long-polling process.
pending: dict[int, dict] = {}

CONFIRM_YES = "confirm"
CONFIRM_NO = "reject"
PICK_PREFIX = "pick:"

# Telegram caps callback_data at 64 bytes. street_key() is
# "district|type|norm_name" in Cyrillic (2 bytes/char in UTF-8) and easily
# blows past that, so buttons carry a short index into `verdict["options"]`
# (or the literal "0" for the single ok-status key) instead of the key itself.


def _confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Так, вірно", callback_data=f"{CONFIRM_YES}:0"),
        InlineKeyboardButton(text="❌ Ні", callback_data=CONFIRM_NO),
    ]])


def _options_keyboard(options: list) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=opt["label"], callback_data=f"{PICK_PREFIX}{i}")]
        for i, opt in enumerate(options)
    ]
    rows.append([InlineKeyboardButton(text="❌ Жодного", callback_data=CONFIRM_NO)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _handle_verdict(message: Message, user_id: int, verdict: dict) -> None:
    status = verdict.get("status")
    logger.info("[%s] handling status=%s", user_id, status)

    if status == "ok":
        pending[user_id] = verdict
        note = ""
        similar = verdict.get("similar_houses")
        if similar:
            # in_range from a same-number-different-letter/corpus miss: say so
            # plainly instead of presenting a guess with exact-match confidence.
            note = (f"\n⚠️ Точно такого номера в реєстрі немає. "
                    f"На цій вулиці є: {', '.join(similar)}.\n")
        await message.answer(
            f"Розпізнано адресу:\n<b>{verdict['formatted']}</b>\n"
            f"Район: {verdict.get('district') or '—'}\n{note}\nВсе вірно?",
            reply_markup=_confirm_keyboard(),
        )
        return

    if status == "ambiguous":
        pending[user_id] = verdict
        await message.answer(
            "Декілька схожих вулиць. Оберіть потрібну:",
            reply_markup=_options_keyboard(verdict["options"]),
        )
        return

    if status == "recheck_house":
        pending[user_id] = verdict
        logger.info("[%s] recheck_house reason: %s", user_id, verdict.get("reason", ""))
        if verdict.get("reason") == "street name absorbed the spoken number":
            await message.answer(
                f"<b>{verdict['street']}</b> — саме так називається ця вулиця, "
                f"номер будинку окремо. Продиктуйте, будь ласка, номер будинку."
            )
            return
        range_note = ""
        house_range = verdict.get("house_range")
        if house_range and house_range[0] is not None:
            lo, hi = house_range
            range_note = f" На ній є номери приблизно від {lo} до {hi}."
        await message.answer(
            f"<b>{verdict['street']}</b> знайдена, але такого номера будинку на ній немає."
            f"{range_note}\nПродиктуйте, будь ласка, ще раз номер будинку."
        )
        return

    if status == "no_street":
        await message.answer(
            "Не вдалося знайти таку вулицю в реєстрі Києва. "
            "Спробуйте, будь ласка, продиктувати адресу ще раз."
        )
        return

    if status == "incomplete":
        missing = []
        if not verdict.get("have_street"):
            missing.append("вулицю")
        if not verdict.get("have_house"):
            missing.append("номер будинку")
        await message.answer(
            "Не почув " + " та ".join(missing) + ". Продиктуйте, будь ласка, ще раз."
        )
        return

    # no_address, or anything unforeseen.
    await message.answer(
        "Не вдалося розпізнати адресу в повідомленні. "
        "Продиктуйте, будь ласка, адресу ще раз."
    )


# Ukrainian/Russian number words for house-number retries. STT on a bare
# spoken number ("вісімнадцять") never carries a street, so Gemini's address
# extraction reliably returns has_address=false for it — parse_house() also
# only understands digits, so this has to happen before either.
_ONES = {
    "нуль": 0, "один": 1, "одна": 1, "два": 2, "дві": 2,
    "три": 3, "чотири": 4, "п'ять": 5, "пять": 5, "шість": 6,
    "сім": 7, "вісім": 8, "дев'ять": 9, "девять": 9,
    "одинадцять": 11, "дванадцять": 12, "тринадцять": 13,
    "чотирнадцять": 14, "п'ятнадцять": 15, "пятнадцять": 15,
    "шістнадцять": 16, "сімнадцять": 17, "вісімнадцять": 18,
    "дев'ятнадцять": 19, "девятнадцять": 19, "десять": 10,
}
_TENS = {
    "двадцять": 20, "тридцять": 30, "сорок": 40, "п'ятдесят": 50,
    "пятдесят": 50, "шістдесят": 60, "сімдесят": 70, "вісімдесят": 80,
    "дев'яносто": 90, "девяносто": 90,
}
_HUNDREDS = {
    "сто": 100, "двісті": 200, "триста": 300, "чотириста": 400,
    "п'ятсот": 500, "пятсот": 500, "шістсот": 600, "сімсот": 700,
    "вісімсот": 800, "дев'ятсот": 900,
}


def _words_to_number(text: str) -> int | None:
    words = text.lower().replace("-", " ").split()
    total, matched = 0, False
    for w in words:
        if w in _HUNDREDS:
            total += _HUNDREDS[w]
            matched = True
        elif w in _TENS:
            total += _TENS[w]
            matched = True
        elif w in _ONES:
            total += _ONES[w]
            matched = True
    return total if matched else None


def _normalize_house_number(transcript: str) -> str:
    """'вісімнадцять' -> '18'. Already-numeric or mixed input ('18 а',
    digits with a letter) passes through untouched — only pure number words
    get converted, everything else goes to addr_index.parse_house as-is."""
    if any(ch.isdigit() for ch in transcript):
        return transcript
    n = _words_to_number(transcript)
    return str(n) if n is not None else transcript


def _retry_house_verdict(verdict: dict, transcript: str, house_number: str,
                          district: str | None = None) -> dict:
    """Builds a resolve() call that reuses the street from a pending
    recheck_house verdict, with an explicit house number filled in."""
    query = verdict.get("query", {})
    return resolve({
        "transcription": transcript,
        "has_address": True,
        "address": {
            "street": f"{query.get('type') or ''} {query.get('street') or ''}".strip(),
            "house_number": house_number,
            "district": district if district is not None else verdict.get("district"),
        },
    })


def _match_house_on_chosen_street(chosen_key: str, house_raw: str, transcript: str) -> dict:
    """Matches a house number against one specific street already picked from
    an ambiguous list, instead of re-running match_street(). Re-resolving by
    name+district can still land on >1 street with a near-identical score
    (several "Академіка X" streets in the same district, for instance) and
    send the user right back into another disambiguation round — this skips
    that entirely since the street is no longer in question."""
    index = load_addr_index()
    street = next((s for s in index["streets"] if street_key(s) == chosen_key), None)
    if street is None:
        return {"status": "no_street", "transcript": transcript}

    verdict, house = match_house(street, house_raw)
    if verdict == "out_of_range":
        return {
            "status": "recheck_house",
            "street": f"{street['type']} {street['name']}",
            "district": street["district"],
            "house_range": street["range"],
            "reason": f"house {house['main']} vs range {street['range']}",
            "key": chosen_key,
            "query": {"street": street["name"], "type": street["type"],
                      "house": house_raw, "alt": None},
        }

    result = {
        "status": "ok",
        "verdict": verdict,
        "score": 100.0,
        "key": chosen_key,
        "formatted": f"{street['type']} {street['name']}, {house['raw']}",
        "district": street["district"],
        "city": street["city"],
        "transcript": transcript,
    }

    if verdict == "in_range":
        same_number = [h["raw"] for h in street["houses"].values()
                       if h["main"] == house["main"] and h["norm"] != house["norm"]]
        if same_number:
            result["similar_houses"] = same_number

    return result


def _resolve_house_retry(verdict: dict, transcript: str, house_number: str) -> dict:
    """Resolves a retried house number against a pending recheck_house
    verdict. If the street is already pinned to a specific registry key (set
    when the street itself was picked explicitly, e.g. from the numbered-
    street disambiguation branch), match directly against it — re-resolving
    by name can re-trigger disambiguation among near-identical street names
    ('Садова 3' vs 'Садова 31' vs 'Садова 23') instead of finishing the house
    lookup. Otherwise falls back to a full name-based resolve()."""
    if verdict.get("key"):
        return _match_house_on_chosen_street(verdict["key"], house_number, transcript)
    return _retry_house_verdict(verdict, transcript, house_number)


@router.message(CommandStart())
async def on_start(message: Message) -> None:
    await message.answer(
        "Надішліть голосове повідомлення з адресою доставки в Києві."
    )


@router.message(F.voice)
async def on_voice(message: Message) -> None:
    voice: Voice = message.voice
    bot = message.bot
    user_id = message.from_user.id

    logger.info("[%s] voice received: file_id=%s duration=%ss",
                user_id, voice.file_id, voice.duration)

    ogg_path = settings.temp_audio_dir / f"{uuid.uuid4()}.ogg"
    file = await bot.get_file(voice.file_id)
    await bot.download_file(file.file_path, destination=ogg_path)
    logger.info("[%s] audio downloaded: %s", user_id, ogg_path)

    try:
        try:
            result = await gemini.process_audio(ogg_path)
        except TelegramRetryAfter:
            raise
        except APIError as exc:  # Gemini 429 / free-tier limits / transient API errors.
            logger.warning("[%s] Gemini API error: %s", user_id, exc)
            await message.answer(
                "Зараз забагато запитів, спробуйте, будь ласка, за хвилину ще раз."
            )
            return
        except Exception:  # Actual bugs (bad prompt file, malformed JSON, etc.) —
            # never mask these as a rate limit; log the full traceback so they
            # surface instead of silently looking like normal Gemini backpressure.
            logger.exception("[%s] Unexpected error processing voice message", user_id)
            await message.answer(
                "Сталася технічна помилка. Спробуйте, будь ласка, ще раз."
            )
            return
    finally:
        ogg_path.unlink(missing_ok=True)

    logger.info("[%s] Gemini result: transcription=%r has_address=%s address=%s",
                user_id, result.transcription, result.has_address,
                result.address.model_dump() if result.address else None)

    pending_verdict = pending.get(user_id)
    if pending_verdict and pending_verdict.get("status") == "recheck_house":
        # We already know the street; this voice message is just the retried
        # house number, so route it through the retry path instead of
        # re-extracting a full address from a transcript that is often just
        # a bare number ("вісімнадцять") with no street in it at all.
        house_number = _normalize_house_number(result.transcription)
        logger.info("[%s] treating voice as house-number retry: raw=%r normalized=%r",
                    user_id, result.transcription, house_number)
        verdict = await asyncio.to_thread(
            _resolve_house_retry, pending_verdict, result.transcription, house_number)
    else:
        verdict = await asyncio.to_thread(resolve, result.model_dump())
    logger.info("[%s] verdict: %s", user_id, verdict)
    await _handle_verdict(message, user_id, verdict)


@router.message(F.text)
async def on_house_retry(message: Message) -> None:
    user_id = message.from_user.id
    verdict = pending.get(user_id)
    if not verdict or verdict.get("status") != "recheck_house":
        logger.info("[%s] text message with no pending recheck_house: %r",
                    user_id, message.text)
        await message.answer("Надішліть голосове повідомлення з адресою.")
        return

    house_number = _normalize_house_number(message.text)
    logger.info("[%s] text house-number retry: raw=%r normalized=%r",
                user_id, message.text, house_number)
    new_verdict = await asyncio.to_thread(
        _resolve_house_retry, verdict, message.text, house_number)
    logger.info("[%s] verdict: %s", user_id, new_verdict)
    await _handle_verdict(message, user_id, new_verdict)


@router.callback_query(F.data == CONFIRM_NO)
async def on_reject(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    logger.info("[%s] rejected verdict: %s", user_id, pending.get(user_id))
    pending.pop(user_id, None)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        "Добре, продиктуйте, будь ласка, адресу ще раз голосовим повідомленням."
    )
    await callback.answer()


@router.callback_query(F.data.startswith(CONFIRM_YES + ":"))
async def on_confirm(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    verdict = pending.pop(user_id, None)

    if verdict and verdict.get("key"):
        logger.info("[%s] confirmed: key=%s formatted=%s",
                    user_id, verdict["key"], verdict.get("formatted"))
        await asyncio.to_thread(confirm, verdict, verdict["key"])
    else:
        logger.warning("[%s] confirm pressed with no pending verdict", user_id)

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("Дякуємо! Адресу підтверджено.")
    await callback.answer()


@router.callback_query(F.data.startswith(PICK_PREFIX))
async def on_pick_option(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    verdict = pending.pop(user_id, None)
    index = int(callback.data[len(PICK_PREFIX):])
    await callback.message.edit_reply_markup(reply_markup=None)

    options = verdict.get("options", []) if verdict else []
    if not verdict or not (0 <= index < len(options)):
        logger.warning("[%s] pick_option with stale/missing verdict: index=%s verdict=%s",
                       user_id, index, verdict)
        await callback.message.answer("Не вдалося обробити вибір, спробуйте ще раз.")
        await callback.answer()
        return

    chosen = options[index]
    chosen_key = chosen["key"]

    if chosen.get("needs_house"):
        # The number Gemini took for a house number turned out to be part of
        # this street's own name ('Садова 3' is a distinct street from
        # 'Садова') — it says nothing about the real house, so ask instead of
        # guessing with it.
        logger.info("[%s] picked numbered-street option %s: key=%s, house unknown",
                    user_id, index, chosen_key)
        new_verdict = {
            "status": "recheck_house",
            "street": chosen["street"],
            "district": chosen["street_district"],
            "house_range": None,
            "reason": "street name absorbed the spoken number",
            "key": chosen_key,
            "query": {"street": chosen["street"], "type": None,
                      "house": "", "alt": None},
        }
        await _handle_verdict(callback.message, user_id, new_verdict)
        await callback.answer()
        return

    # ambiguous only resolves the street; the house was never matched against
    # it. Match directly against the chosen street rather than re-resolving
    # by name+district, which can land on another >1-candidate tie (several
    # "Академіка X" streets in one district) and loop back into disambiguation.
    house = verdict.get("query", {}).get("house", "")
    logger.info("[%s] picked option %s: key=%s house=%r",
                user_id, index, chosen_key, house)
    new_verdict = await asyncio.to_thread(
        _match_house_on_chosen_street, chosen_key, house, verdict.get("transcript", ""))
    logger.info("[%s] verdict: %s", user_id, new_verdict)
    await _handle_verdict(callback.message, user_id, new_verdict)
    await callback.answer()


async def main() -> None:
    logger.info("Starting bot: gemini_model=%s temp_audio_dir=%s",
                settings.gemini_model, settings.temp_audio_dir)
    bot = Bot(token=settings.telegram_bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    me = await bot.get_me()
    logger.info("Connected as @%s (id=%s)", me.username, me.id)
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
