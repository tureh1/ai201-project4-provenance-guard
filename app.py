import json
import os
import re
import statistics
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from groq import Groq

load_dotenv()

APP_NAME = "Provenance Guard"
MODEL_NAME = "llama-3.3-70b-versatile"
LOG_PATH = Path("logs/audit.jsonl")
SUBMISSIONS_PATH = Path("data/submissions.json")

app = Flask(__name__)

client = Groq(api_key=os.getenv("GROQ_API_KEY")) if os.getenv("GROQ_API_KEY") else None


# ---------------------------------------------------------------------------
# Storage and audit log helpers
# ---------------------------------------------------------------------------

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_storage() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    SUBMISSIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not SUBMISSIONS_PATH.exists():
        SUBMISSIONS_PATH.write_text("{}", encoding="utf-8")


def load_submissions() -> Dict[str, Dict[str, Any]]:
    ensure_storage()
    try:
        return json.loads(SUBMISSIONS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_submissions(records: Dict[str, Dict[str, Any]]) -> None:
    ensure_storage()
    SUBMISSIONS_PATH.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")


def log_event(entry: Dict[str, Any]) -> None:
    """Append one structured JSON object per line to the audit log."""
    ensure_storage()
    entry = {"timestamp": utc_now(), **entry}
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_log(limit: int = 20) -> List[Dict[str, Any]]:
    ensure_storage()
    if not LOG_PATH.exists():
        return []
    lines = LOG_PATH.read_text(encoding="utf-8").splitlines()
    entries = []
    for line in lines[-limit:]:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            entries.append({"parse_error": True, "raw_line": line})
    return entries


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


# ---------------------------------------------------------------------------
# Signal 1: LLM-based attribution score
# ---------------------------------------------------------------------------

def extract_json_object(text: str) -> Dict[str, Any]:
    """Best-effort JSON extraction for LLM responses."""
    try:
        return json.loads(text)
    except Exception:
        pass

    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise ValueError("No JSON object found in LLM response")
    return json.loads(match.group(0))


def llm_detection_signal(text: str) -> Dict[str, Any]:
    """
    Ask Groq to estimate whether the text appears AI-generated.
    Returns ai_score where 0.0 = likely human and 1.0 = likely AI.
    """
    if client is None:
        return {
            "name": "llm_judge",
            "ai_score": 0.50,
            "reason": "GROQ_API_KEY is not set; returned neutral score.",
            "available": False,
        }

    prompt = f"""
You are an attribution-signal judge for a creative writing platform.
Estimate whether the submitted text appears AI-generated or human-written.

Return ONLY valid JSON with this schema:
{{
  "ai_score": 0.0 to 1.0,
  "reason": "one short reason"
}}

Scoring guide:
- 0.00 to 0.25: strongly human-like
- 0.26 to 0.45: somewhat human-like
- 0.46 to 0.60: uncertain/mixed
- 0.61 to 0.75: somewhat AI-like
- 0.76 to 1.00: strongly AI-like

Do not claim certainty. Consider tone, specificity, repetitiveness, generic phrasing,
awkward uniformity, and whether the text feels like a polished template.

Text to evaluate (between the markers):
<<<TEXT
{text[:4000]}
TEXT
""".strip()

    try:
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": "Return only JSON. Do not include markdown."},
                {"role": "user", "content": prompt},
            ],
            temperature=0,
            max_tokens=180,
        )
        raw = response.choices[0].message.content.strip()
        parsed = extract_json_object(raw)
        score = clamp(float(parsed.get("ai_score", 0.5)))
        return {
            "name": "llm_judge",
            "ai_score": round(score, 4),
            "reason": str(parsed.get("reason", "No reason provided."))[:300],
            "available": True,
        }
    except Exception as exc:
        return {
            "name": "llm_judge",
            "ai_score": 0.50,
            "reason": f"LLM signal failed; returned neutral score. Error: {exc}",
            "available": False,
        }


# ---------------------------------------------------------------------------
# Signal 2: Stylometric heuristics
# ---------------------------------------------------------------------------

def split_sentences(text: str) -> List[str]:
    sentences = [s.strip() for s in re.split(r"[.!?]+", text) if s.strip()]
    return sentences if sentences else [text.strip()]


def tokenize_words(text: str) -> List[str]:
    return re.findall(r"[A-Za-z']+", text.lower())


def stylometric_signal(text: str) -> Dict[str, Any]:
    """
    Measures structural regularity. AI text often has more uniform sentence lengths,
    moderate vocabulary diversity, and lower punctuation/emotional irregularity.
    Returns ai_score where higher means more AI-like.
    """
    words = tokenize_words(text)
    sentences = split_sentences(text)
    word_count = max(len(words), 1)

    sentence_lengths = [len(tokenize_words(s)) for s in sentences if tokenize_words(s)]
    avg_sentence_length = sum(sentence_lengths) / max(len(sentence_lengths), 1)
    length_variance = statistics.pvariance(sentence_lengths) if len(sentence_lengths) > 1 else 0.0
    type_token_ratio = len(set(words)) / word_count
    punctuation_count = len(re.findall(r"[,;:!?()\-—]", text))
    punctuation_density = punctuation_count / word_count

    # Low variance and moderate/low vocabulary diversity push AI-like.
    uniformity_score = 1.0 - clamp(length_variance / 80.0)
    long_sentence_score = clamp((avg_sentence_length - 10.0) / 18.0)
    low_diversity_score = clamp((0.72 - type_token_ratio) / 0.35)
    low_punctuation_score = 1.0 - clamp(punctuation_density / 0.16)

    ai_score = (
        0.35 * uniformity_score
        + 0.25 * long_sentence_score
        + 0.25 * low_diversity_score
        + 0.15 * low_punctuation_score
    )

    return {
        "name": "stylometric_heuristics",
        "ai_score": round(clamp(ai_score), 4),
        "metrics": {
            "word_count": word_count,
            "sentence_count": len(sentences),
            "avg_sentence_length": round(avg_sentence_length, 2),
            "sentence_length_variance": round(length_variance, 2),
            "type_token_ratio": round(type_token_ratio, 3),
            "punctuation_density": round(punctuation_density, 3),
        },
        "reason": "Scores structural regularity, vocabulary diversity, and punctuation density.",
    }


# ---------------------------------------------------------------------------
# Signal 3: AI phrase / generic-polish heuristic (ensemble signal)
# ---------------------------------------------------------------------------

def phrase_pattern_signal(text: str) -> Dict[str, Any]:
    """
    Detects generic AI-like phrasing and overly polished connective language.
    This is a third distinct signal for an ensemble approach.
    """
    lower = text.lower()
    ai_phrases = [
        "it is important to note",
        "in conclusion",
        "furthermore",
        "moreover",
        "as a result",
        "plays a crucial role",
        "transformative",
        "paradigm shift",
        "various sectors",
        "stakeholders",
        "ethical implications",
        "responsible deployment",
        "in today's rapidly evolving",
        "delve into",
        "tapestry",
    ]
    casual_markers = [
        "lol", "lmao", "idk", "honestly", "tbh", "ngl", "kinda", "sorta",
        "like", "wtf", "???", "!!!", "ok so", "i mean"
    ]

    phrase_hits = sum(1 for phrase in ai_phrases if phrase in lower)
    casual_hits = sum(1 for marker in casual_markers if marker in lower)
    words = tokenize_words(text)
    word_count = max(len(words), 1)

    phrase_score = clamp(phrase_hits / 4.0)
    formality_score = clamp((word_count - 60) / 180.0) if phrase_hits else 0.0
    casual_offset = clamp(casual_hits / 4.0)
    ai_score = clamp(0.75 * phrase_score + 0.25 * formality_score - 0.45 * casual_offset)

    return {
        "name": "phrase_pattern_heuristic",
        "ai_score": round(ai_score, 4),
        "metrics": {
            "ai_phrase_hits": phrase_hits,
            "casual_marker_hits": casual_hits,
            "word_count": word_count,
        },
        "reason": "Detects generic AI-like phrasing and offsets with casual human markers.",
    }


# ---------------------------------------------------------------------------
# Combining signals into a calibrated confidence score
# ---------------------------------------------------------------------------

def combine_signals(signals: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """
    Ensemble weighting:
    - LLM judge: 50% because it captures holistic semantic/stylistic cues.
    - Stylometrics: 30% because it captures structural regularity.
    - Phrase pattern: 20% because it catches generic AI phrasing but is easy to fool.
    """
    weights = {
        "llm_judge": 0.50,
        "stylometric_heuristics": 0.30,
        "phrase_pattern_heuristic": 0.20,
    }
    weighted_score = 0.0
    total_weight = 0.0
    for name, weight in weights.items():
        signal = signals[name]
        weighted_score += weight * float(signal["ai_score"])
        total_weight += weight
    ai_score = clamp(weighted_score / total_weight)
    confidence = max(ai_score, 1.0 - ai_score)

    # Asymmetric thresholds: a false positive (calling a human's work AI) is the
    # worst outcome on a writing platform, so the AI band requires a high score
    # (>= 0.70) while the human band is generous (<= 0.40). Borderline content
    # lands in "uncertain" and is shown without a strong claim.
    if ai_score >= 0.70:
        attribution = "likely_ai"
    elif ai_score <= 0.40:
        attribution = "likely_human"
    else:
        attribution = "uncertain"

    return {
        "ai_score": round(ai_score, 4),
        "confidence": round(confidence, 4),
        "attribution": attribution,
        "weights": weights,
    }


def analyze_content(text: str) -> Dict[str, Any]:
    signals = {
        "llm_judge": llm_detection_signal(text),
        "stylometric_heuristics": stylometric_signal(text),
        "phrase_pattern_heuristic": phrase_pattern_signal(text),
    }
    combined = combine_signals(signals)
    return {**combined, "signals": signals}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "app": APP_NAME,
        "endpoints": {
            "POST /submit": "Analyze text for attribution and confidence.",
            "GET /log": "Return recent structured audit log entries.",
            "GET /content/<content_id>": "Return one stored content record.",
        },
    })


@app.route("/submit", methods=["POST"])
def submit():
    data = request.get_json(silent=True) or {}
    text = str(data.get("text", "")).strip()
    creator_id = str(data.get("creator_id", "")).strip()

    if not text:
        return jsonify({"error": "Missing required field: text"}), 400
    if not creator_id:
        return jsonify({"error": "Missing required field: creator_id"}), 400
    if len(text) < 40:
        return jsonify({"error": "Text must be at least 40 characters for meaningful analysis."}), 400

    content_id = str(uuid.uuid4())
    analysis = analyze_content(text)

    # Milestone 4: real confidence scoring is wired in. The transparency label
    # is still a placeholder; the three reader-facing variants arrive in M5.
    label = "Placeholder label — the three transparency-label variants arrive in M5."

    record = {
        "content_id": content_id,
        "creator_id": creator_id,
        "created_at": utc_now(),
        "text": text,
        "status": "classified",
        "attribution": analysis["attribution"],
        "ai_score": analysis["ai_score"],
        "confidence": analysis["confidence"],
        "transparency_label": label,
        "signals": analysis["signals"],
        "appeal": None,
    }
    submissions = load_submissions()
    submissions[content_id] = record
    save_submissions(submissions)

    log_event({
        "event_type": "submission_classified",
        "content_id": content_id,
        "creator_id": creator_id,
        "status": "classified",
        "attribution": analysis["attribution"],
        "ai_score": analysis["ai_score"],
        "confidence": analysis["confidence"],
        "signals": analysis["signals"],
        "text_preview": text[:250],
    })

    return jsonify({
        "content_id": content_id,
        "creator_id": creator_id,
        "status": "classified",
        "attribution": analysis["attribution"],
        "ai_score": analysis["ai_score"],
        "confidence": analysis["confidence"],
        "transparency_label": label,
        "signals": analysis["signals"],
    })


@app.route("/log", methods=["GET"])
def get_log():
    limit = request.args.get("limit", default=20, type=int)
    limit = max(1, min(limit, 100))
    return jsonify({"entries": read_log(limit=limit)})


@app.route("/content/<content_id>", methods=["GET"])
def get_content(content_id: str):
    submissions = load_submissions()
    record = submissions.get(content_id)
    if not record:
        return jsonify({"error": "Unknown content_id"}), 404
    safe_record = {k: v for k, v in record.items() if k != "text"}
    safe_record["text_preview"] = record.get("text", "")[:250]
    return jsonify(safe_record)


if __name__ == "__main__":
    ensure_storage()
    app.run(host="0.0.0.0", port=5000, debug=True)
