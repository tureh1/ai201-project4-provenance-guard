import json
import os
import re
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
# Routes
# ---------------------------------------------------------------------------

@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "app": APP_NAME,
        "endpoints": {
            "POST /submit": "Analyze text for attribution (signal 1 only for now).",
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

    # Milestone 3: a single signal (the LLM judge). Confidence scoring and the
    # transparency label are placeholders here and are implemented in M4 and M5.
    signal = llm_detection_signal(text)
    llm_score = signal["ai_score"]
    if llm_score >= 0.70:
        attribution = "likely_ai"
    elif llm_score <= 0.40:
        attribution = "likely_human"
    else:
        attribution = "uncertain"

    confidence = 0.5  # placeholder until M4 confidence scoring
    label = "Placeholder label — confidence scoring and transparency label arrive in later milestones."

    record = {
        "content_id": content_id,
        "creator_id": creator_id,
        "created_at": utc_now(),
        "text": text,
        "status": "classified",
        "attribution": attribution,
        "llm_score": llm_score,
        "confidence": confidence,
        "transparency_label": label,
        "signals": {"llm_judge": signal},
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
        "attribution": attribution,
        "llm_score": llm_score,
        "confidence": confidence,
        "text_preview": text[:250],
    })

    return jsonify({
        "content_id": content_id,
        "creator_id": creator_id,
        "status": "classified",
        "attribution": attribution,
        "llm_score": llm_score,
        "confidence": confidence,
        "transparency_label": label,
        "signals": {"llm_judge": signal},
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
