"""Language-coach domain logic: languages, CEFR levels, scenarios, prompts,
and the structured end-of-session feedback report."""
from __future__ import annotations

from dataclasses import dataclass

# --- Supported languages -----------------------------------------------------
# `code` is the BCP-47 tag we pass to Deepgram (STT) and Cartesia (TTS).
# `cartesia_voice` is a public Cartesia voice id that speaks that language well.
@dataclass(frozen=True)
class Language:
    key: str
    name: str
    stt_code: str          # Deepgram language code
    tts_language: str      # Cartesia language code
    cartesia_voice: str    # Cartesia voice id


LANGUAGES: dict[str, Language] = {
    "es": Language("es", "Spanish", "es", "es", "846d6cb0-2301-48b6-9683-48f5618ea2f6"),
    "fr": Language("fr", "French", "fr", "fr", "a8a1eb38-5f15-4c1d-8722-7ac0f329727d"),
    "de": Language("de", "German", "de", "de", "b9de4a89-2257-424b-94c2-db18ba68c81a"),
    "en": Language("en", "English", "en", "en", "a0e99841-438c-4a64-b679-ae501e7d6091"),
}

# --- CEFR proficiency levels -------------------------------------------------
LEVELS: dict[str, str] = {
    "A1": "absolute beginner: use very short, simple sentences, basic present-tense "
          "vocabulary, and speak slowly. Repeat key words.",
    "A2": "elementary: short everyday sentences, common vocabulary, simple past/future. "
          "Keep turns brief.",
    "B1": "intermediate: normal everyday conversation, a wider vocabulary, mix of tenses. "
          "Speak at a relaxed natural pace.",
    "B2": "upper-intermediate: natural pace, idioms, more complex sentences and opinions.",
    "C1": "advanced: speak fully naturally, nuanced vocabulary, abstract topics, native-like.",
}

# --- Roleplay scenarios ------------------------------------------------------
SCENARIOS: dict[str, str] = {
    "free": "Open-ended free conversation. Pick friendly everyday topics and ask the learner questions.",
    "cafe": "You are a friendly server at a café. Take the learner's order and chat casually.",
    "directions": "You are a local helping the learner, a tourist, find their way around the city.",
    "interview": "You are a recruiter conducting a relaxed job interview in the target language.",
    "smalltalk": "You just met the learner at a social event. Make light small talk.",
}


def system_prompt(language: str, level: str, scenario: str, corrections: bool) -> str:
    lang = LANGUAGES.get(language, LANGUAGES["es"])
    level_desc = LEVELS.get(level, LEVELS["A2"])
    scenario_desc = SCENARIOS.get(scenario, SCENARIOS["free"])

    correction_rule = (
        "If the learner makes a notable grammar or word-choice mistake, gently and very "
        "briefly correct it in ONE short clause, then continue the conversation naturally. "
        "Do not over-correct or break the flow."
        if corrections
        else "Do not correct mistakes during the conversation; just keep it flowing naturally."
    )

    return f"""You are a warm, encouraging {lang.name} conversation tutor having a SPOKEN \
conversation with a learner. Your replies are converted to speech, so:

- Reply ONLY in {lang.name}. Never switch to English (unless {lang.name} IS English).
- This is voice: keep each reply to 1-3 short sentences. No lists, no markdown, no emoji, \
no stage directions — only words that should be spoken aloud.
- Write numbers and symbols as words.
- Learner level is {level} ({level_desc}). Match your vocabulary and pace to this level.
- Scenario: {scenario_desc}
- {correction_rule}
- Always end your turn with a short question or prompt so the learner keeps talking.

Begin by greeting the learner and asking an opening question."""


# --- End-of-session feedback report -----------------------------------------
# JSON schema (Gemini responseSchema / generic structured-output contract).
REPORT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "overall_comment": {"type": "string"},
        "fluency_score": {"type": "integer"},   # 0-100
        "cefr_estimate": {"type": "string"},     # e.g. "A2"
        "corrections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "you_said": {"type": "string"},
                    "better": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["you_said", "better", "why"],
            },
        },
        "new_vocabulary": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "word": {"type": "string"},
                    "meaning": {"type": "string"},
                },
                "required": ["word", "meaning"],
            },
        },
        "next_focus": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "overall_comment", "fluency_score", "cefr_estimate",
        "corrections", "new_vocabulary", "next_focus",
    ],
}


def report_prompt(language: str, level: str, transcript: str) -> str:
    lang = LANGUAGES.get(language, LANGUAGES["es"])
    return f"""You are a {lang.name} teacher reviewing a conversation a {level}-level learner \
just had. Below is the transcript (LEARNER = the student, COACH = the tutor).

Produce an encouraging, honest feedback report as JSON matching the required schema:
- overall_comment: 1-2 warm sentences in ENGLISH summarizing how they did.
- fluency_score: 0-100 estimate of spoken fluency in this session.
- cefr_estimate: your CEFR estimate (A1/A2/B1/B2/C1) based ONLY on what the learner said.
- corrections: up to 5 of the learner's most useful mistakes. `you_said` = what they said, \
`better` = the corrected {lang.name}, `why` = a short ENGLISH explanation.
- new_vocabulary: up to 6 useful {lang.name} words/phrases from the chat with ENGLISH meanings.
- next_focus: 2-3 short ENGLISH tips on what to practice next.

If the learner barely spoke, say so kindly and keep arrays short. Base everything ONLY on \
the transcript; do not invent mistakes.

TRANSCRIPT:
{transcript}"""
