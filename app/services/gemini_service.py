from __future__ import annotations
import asyncio
import json
import logging
from pathlib import Path
from typing import Optional, Union, List
from pydantic import BaseModel, Field
from google import genai
from google.genai import types
from app.config import settings

logger = logging.getLogger(__name__)

PROMPT_FILE_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "address_prompt.md"


class AddressInfo(BaseModel):
    city: str = Field(default="Київ", description="Місто (за замовчуванням Київ)")
    street: Optional[str] = Field(None, description="Офіційна сучасна назва вулиці українською мовою (наприклад, 'вул. Велика Васильківська', 'просп. Берестейський')")
    street_old_or_alternative_name: Optional[str] = Field(None, description="Альтернативна/російська назва, якщо згадувалась (наприклад, 'просп. Перемоги', 'вул. Червоноармійська')")
    house_number: Optional[str] = Field(None, description="Номер будинку (наприклад, '12', '45А', '7/2')")
    building_block: Optional[str] = Field(None, description="Корпус, секція або будова (наприклад, 'корпус 2', 'секція Б')")
    apartment_or_office: Optional[str] = Field(None, description="Номер квартири або офісу")
    residential_complex: Optional[str] = Field(None, description="Назва житлового комплексу або бізнес-центру (наприклад, 'ЖК Французький квартал', 'БЦ Парус')")
    district: Optional[str] = Field(None, description="Район Києва (Печерський, Шевченківський, Голосіївський, Оболонський, Подільський, Солом'янський, Святошинський, Дарницький, Дніпровський, Деснянський)")
    metro_or_landmark: Optional[str] = Field(None, description="Найближча станція метро або помітний орієнтир (наприклад, 'м. Либідська, біля ТРЦ Ocean Plaza')")
    full_formatted_address: str = Field(description="Повний нормалізований рядок адреси для пошуку на карті")


class ProcessedResult(BaseModel):
    transcription: str = Field(description="Повний дослівний текст розпізнаного голосового повідомлення")
    has_address: bool = Field(description="Чи було в голосовому повідомленні названо адресу")
    language: str = Field(description="Мова аудіоповідомлення ('uk', 'ru', 'mixed')")
    address: Optional[AddressInfo] = Field(None, description="Детальна інформація про адресу, якщо has_address == true")


FALLBACK_MODELS = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-2.5-flash"]


def load_system_prompt() -> str:
    """Loads the system prompt from prompts/address_prompt.md and fills in
    the city name from settings — the prompt file itself names no city, so
    switching to a different address registry only means swapping the
    registry data and settings.city, not editing this file. Plain string
    replace, not str.format(): the prompt's JSON example is full of '{' '}'
    that aren't placeholders, and .format() would choke on them."""
    if not PROMPT_FILE_PATH.exists():
        raise FileNotFoundError(f"Файл системного промпту не знайдено: {PROMPT_FILE_PATH}")

    content = PROMPT_FILE_PATH.read_text(encoding="utf-8").strip()
    if not content:
        raise ValueError(f"Файл промпту {PROMPT_FILE_PATH} порожній!")

    return content.replace("{city}", settings.city)



class GeminiService:
    """Speech transcription and analysis service via the Google GenAI SDK."""

    def __init__(self, api_key: Optional[str] = None, model_name: Optional[str] = None):
        self.api_key = api_key or settings.gemini_api_key
        self.model_name = model_name or settings.gemini_model
        
        # Ініціалізація клієнта google-genai
        self.client = genai.Client(api_key=self.api_key)

    def get_prompt(self) -> str:
        """Returns the current prompt strictly from prompts/address_prompt.md."""
        return load_system_prompt()

    async def _process_via_live_stream(self, path: Path, mime_type: str) -> str:
        """
        Transcription via the Gemini Live API WebSocket (bidiGenerateContent).
        """
        logger.info(f"Використовується Gemini Live WebSocket стрімінг для {self.model_name}...")
        audio_bytes = await asyncio.to_thread(path.read_bytes)
        
        transcript_parts = []
        live_config = types.LiveConnectConfig(
            response_modalities=["TEXT"]
        )

        async with self.client.aio.live.connect(model=self.model_name, config=live_config) as session:
            # Відправляємо аудіопотік через WebSocket
            await session.send(
                input=types.LiveClientRealtimeInput(
                    media_chunks=[types.Blob(data=audio_bytes, mime_type=mime_type)]
                ),
                end_of_turn=True
            )

            async for response in session.receive():
                server_content = response.server_content
                if server_content and server_content.model_turn:
                    for part in server_content.model_turn.parts:
                        if part.text:
                            transcript_parts.append(part.text)
                if server_content and server_content.turn_complete:
                    break

        return "".join(transcript_parts).strip()

    async def process_audio(self, audio_path: Union[Path, str]) -> ProcessedResult:
        """
        Uploads the audio file to Gemini, transcribes it, and extracts the address.
        """
        path = Path(audio_path)
        if not path.exists():
            raise FileNotFoundError(f"Файл {path} не знайдено.")

        logger.info(f"Відправка аудіо {path.name} у Gemini ({self.model_name})...")

        # Визначаємо MIME-тип
        mime_type = "audio/ogg"
        if path.suffix.lower() in [".mp3"]:
            mime_type = "audio/mp3"
        elif path.suffix.lower() in [".wav"]:
            mime_type = "audio/wav"
        elif path.suffix.lower() in [".m4a"]:
            mime_type = "audio/m4a"
        elif path.suffix.lower() in [".mp4"]:
            mime_type = "video/mp4"

        # 1. Якщо модель суто Live WebSocket (наприклад, gemini-3.5-transcribe-live)
        if "live" in self.model_name.lower():
            try:
                transcript = await self._process_via_live_stream(path, mime_type)
                if transcript:
                    logger.info(f"Live транскрипція отримана: {transcript}")
                    return await self.process_text(transcript)
            except Exception as live_err:
                logger.warning(f"Live WebSocket помилка: {live_err}. Використовуємо мультимодальний fallback...")

        # 2. Якщо модель transcribe (наприклад, gemini-3.5-transcribe без Live):
        # Вона не підтримує system_instruction, тому спочатку отримуємо чисту транскрипцію
        if "transcribe" in self.model_name.lower() and "live" not in self.model_name.lower():
            try:
                logger.info(f"Спроба прямої транскрибації через {self.model_name}...")
                audio_bytes = await asyncio.to_thread(path.read_bytes)
                audio_part = types.Part.from_bytes(data=audio_bytes, mime_type=mime_type)
                
                # Викликаємо без system_instruction
                resp = await self.client.aio.models.generate_content(
                    model=self.model_name,
                    contents=[audio_part]
                )
                raw_transcript = (resp.text or "").strip()
                if raw_transcript:
                    logger.info(f"Транскрипція отримана від {self.model_name}: {raw_transcript}")
                    return await self.process_text(raw_transcript)
            except Exception as tr_err:
                logger.warning(f"Помилка транскрибації {self.model_name}: {tr_err}. Переходимо до fallback моделей...")

        # 3. Мультимодальна обробка з fallback по Flash моделях
        return await self._process_multimodal_audio(path, mime_type)

    async def _process_multimodal_audio(self, path: Path, mime_type: str) -> ProcessedResult:
        """Processes the audio file directly via generate_content, with automatic fallback."""
        audio_bytes = await asyncio.to_thread(path.read_bytes)
        audio_part = types.Part.from_bytes(
            data=audio_bytes,
            mime_type=mime_type
        )

        prompt_text = self.get_prompt()

        models_to_try = [self.model_name] + [m for m in FALLBACK_MODELS if m != self.model_name]
        last_exception = None

        for model in models_to_try:
            # Пропускаємо суто Live або Transcribe-only моделі для комплексного generate_content
            if "live" in model.lower() or "transcribe" in model.lower():
                continue

            try:
                logger.info(f"Спроба обробки аудіо моделлю {model}...")
                
                config = types.GenerateContentConfig(
                    system_instruction=prompt_text,
                    response_mime_type="application/json",
                    temperature=0.1,
                )

                response = await self.client.aio.models.generate_content(
                    model=model,
                    contents=[
                        audio_part,
                        f"Розпізнай мовлення, витягни адресу у місті {settings.city} та поверни структурований JSON."
                    ],
                    config=config
                )

                raw_text = (response.text or "").strip()
                logger.debug(f"Gemini raw response ({model}): {raw_text}")

                if raw_text.startswith("```json"):
                    raw_text = raw_text.replace("```json", "").replace("```", "").strip()

                data = json.loads(raw_text)
                result = ProcessedResult(**data)

                logger.info("=" * 60)
                logger.info(f"🎙 [RAW ТРАНСКРИПЦІЯ АУДІО]: {result.transcription}")
                if result.has_address and result.address:
                    logger.info(f"📍 [ОЧИЩЕНА АДРЕСА]: {result.address.full_formatted_address}")
                    if result.address.street_old_or_alternative_name:
                        logger.info(f"🔄 [СТАРА НАЗВА]: {result.address.street_old_or_alternative_name}")
                    logger.info(f"🏢 [ДЕТАЛІ]: будинок {result.address.house_number or '-'}, кв. {result.address.apartment_or_office or '-'}")
                else:
                    logger.info("❌ [АДРЕСУ НЕ ВИЯВЛЕНО В ТЕКСТІ]")
                logger.info("=" * 60)

                return result

            except Exception as e:
                last_exception = e
                logger.warning(f"Модель {model} повернула помилку: {e}. Перевіряємо наступну модель...")

        if last_exception:
            raise last_exception
        raise RuntimeError("Не вдалося обробити аудіо жодною моделлю Gemini.")

    async def process_text(self, text: str, text_model: Optional[str] = None) -> ProcessedResult:
        """
        Processes a text query to extract a Kyiv address, with fallback support.
        """
        prompt_text = self.get_prompt()
        models_to_try = [text_model or self.model_name] + [m for m in FALLBACK_MODELS]
        last_exception = None

        for model in models_to_try:
            if "live" in model.lower() or "transcribe" in model.lower():
                continue
            try:
                config = types.GenerateContentConfig(
                    system_instruction=prompt_text,
                    response_mime_type="application/json",
                    temperature=0.1,
                )

                response = await self.client.aio.models.generate_content(
                    model=model,
                    contents=[f"Проаналізуй цей текст, витягни адресу у місті {settings.city} та поверни JSON:\n\n{text}"],
                    config=config
                )

                raw_text = (response.text or "").strip()
                if raw_text.startswith("```json"):
                    raw_text = raw_text.replace("```json", "").replace("```", "").strip()

                data = json.loads(raw_text)
                result = ProcessedResult(**data)

                logger.info("=" * 60)
                logger.info(f"📝 [АНАЛІЗ ТЕКСТУ]: {text}")
                if result.has_address and result.address:
                    logger.info(f"📍 [ОЧИЩЕНА АДРЕСА]: {result.address.full_formatted_address}")
                    if result.address.street_old_or_alternative_name:
                        logger.info(f"🔄 [СТАРА НАЗВА]: {result.address.street_old_or_alternative_name}")
                else:
                    logger.info("❌ [АДРЕСУ НЕ ВИЯВЛЕНО В ТЕКСТІ]")
                logger.info("=" * 60)

                return result

            except Exception as e:
                last_exception = e
                logger.warning(f"Текстова обробка {model} повернула помилку: {e}...")

        if last_exception:
            raise last_exception
        raise RuntimeError("Не вдалося обробити текст.")
