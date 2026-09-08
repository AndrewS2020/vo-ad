# Voice Address Bot (Kyiv Delivery)

An intelligent Telegram bot designed to transcribe voice messages, extract Kyiv addresses, validate them against the official municipal address registry, and confirm normalized addresses with users via an interactive chat flow.

---

## 📌 Problem & Motivation

When customers dictate delivery addresses via voice messages, traditional speech-to-text (STT) systems and geocoding APIs face common challenges:

- **Phonetic & STT drifts:** Misspellings, dialect variations (Surzhyk, Ukrainian, Russian), and background acoustic noise.
- **Decommunization & historical names:** Callers frequently use historical or informal street names (e.g., *вул. Червоноармійська* instead of *Велика Васильківська*).
- **Name collisions:** Over 130 street names in Kyiv exist across multiple administrative districts (e.g., *вул. Садова* exists in five different districts).
- **Cost & Complexity:** Running heavy database infrastructure (PostGIS, Elasticsearch) or paying per-request commercial geocoding APIs is often overkill for medium-scale city deployments.

This project delivers a **cost-efficient, lightweight, and self-learning** pipeline that solves these challenges.

---

## 🚀 Key Features & Pipeline

```
Telegram Voice Message (OGG/Opus)
              │
              ▼
    1. Direct Audio Ingestion (no ffmpeg transcoding)
              │
              ▼
    2. Single-pass Multimodal LLM (Google Gemini)
       ├── Speech-to-Text transcription
       └── Structured Address Entity Extraction (JSON)
              │
              ▼
    3. Adapter & Normalization (app/core/resolve.py)
       ├── Street type mapping (e.g. "просп." -> "проспект")
       └── Corpus/Block folding (e.g. "12" + "корпус 2" -> "12к2")
              │
              ▼
    4. Fast In-Memory Registry Matcher (app/core/addr_index.py)
       ├── Instant Alias Lookup (O(1) SQLite cache)
       ├── Blended Fuzzy Matching (Rapidfuzz WRatio + Jaro-Winkler)
       └── Multi-tier House Number Range Validation
              │
              ▼
    5. Interactive Telegram Flow (aiogram 3.x)
       ├── ✅ Single exact match -> Quick confirmation button
       ├── 🔀 Ambiguity -> District / Street selector buttons
       └── ⚠️ Plausibility issue -> Spoken/text house number re-prompt
              │
              ▼
    6. Self-Learning Feedback Loop
       └── User confirmation promotes misrecognitions into permanent aliases
```

---

## 🛠️ Architecture & Core Components

1. **Telegram Bot Layer (**`app/bot.py`**):**

   - Built with **aiogram 3.x** using long polling.
   - Handles voice notes, interactive inline keyboards, and fallback text input for house number retries (including spoken number word conversion, e.g., *"вісімнадцять"* → `"18"`).

2. **Multimodal LLM Service (**`app/services/gemini_service.py`**):**

   - Employs the **Google GenAI SDK** (`gemini-3.5-flash` / live models).
   - Executes transcription and structured JSON extraction in a single API call, avoiding multi-stage latency.
   - Implements automated model fallbacks on transient errors or quota limits.

3. **Domain Adapter (**`app/core/resolve.py`**):**

   - Implements the **Adapter Pattern**, bridging Gemini's LLM response schema and the registry's strict indexing format.
   - Handles abbreviations, corpus formats, and secondary attempts using alternative street names.

4. **Address Index & Matching Engine (**`app/core/addr_index.py`**):**

   - **In-Memory Pickle Index:** 2,157 streets and house ranges serialized in memory — no PostgreSQL needed.
   - **Normalization Invariant:** Unified text and house normalizers (`norm_text`, `norm_house`) eliminate Latin/Cyrillic homoglyphs and punctuation mismatches.
   - **Three-tier House Verification:** Classifies house numbers as `exact` (found), `in_range` (plausible for the street), or `out_of_range` (likely an STT digit drop).

5. **Self-Learning Alias Store (**`data/addr.db`**):**

   - SQLite persistence containing confirmed user selections.
   - Learned misrecognitions (`stt_error`) and historical names (`old_name`) immediately match at 100% score on subsequent queries, bypassing fuzzy search.

---

## 💻 Tech Stack

- **Language:** Python 3.9+ (`from __future__ import annotations`)
- **Bot Framework:** `aiogram` (3.22.0)
- **LLM / AI:** `google-genai` (1.47.0)
- **Fuzzy Search:** `rapidfuzz` (3.13.0) with custom `blend` scorer (WRatio + Jaro-Winkler)
- **Validation & Settings:** `pydantic` (2.11.x) & `pydantic-settings` (2.11.x)
- **Database:** In-memory index (`pickle`) + `sqlite3`

---

## ⚙️ Quick Start

### 1. Requirements & Setup

Clone the repository and install dependencies in a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Environment Configuration

Create a `.env` file in the project root:

```env
TELEGRAM_BOT_TOKEN="your_telegram_bot_token"
GEMINI_API_KEY="your_google_gemini_api_key"
GEMINI_MODEL="gemini-2.5-flash"
```

### 3. Build the Address Index

Compile the CSV address registry into the high-speed index:

```bash
python -m app.core.addr_index build data/addr.csv
```

### 4. Run the Bot

```bash
python -m app.bot
```