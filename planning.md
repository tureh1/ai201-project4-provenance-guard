# Provenance Guard — Planning

Provenance Guard is a backend API for a creative sharing platform. It analyzes submitted text, combines multiple attribution signals, returns a confidence score and transparency label, logs every decision, and allows creators to appeal classifications.

> **Milestone 1 scope:** This document currently captures the architecture, the API contract, and the detection-signal choices. Confidence scoring, label variants, the appeals workflow, edge cases, and the AI tool plan are added in Milestone 2.

---

## Architecture

### Submission flow

```text
Client / Creative Platform
        |
        | POST /submit {text, creator_id}
        v
Flask API validation
        |
        | raw text
        v
Detection Pipeline
        |
        |--> Signal 1: Groq LLM judge -> ai_score 0.0-1.0 + reason
        |--> Signal 2: Stylometric heuristics -> ai_score 0.0-1.0 + metrics
        |--> Signal 3: Phrase-pattern heuristic -> ai_score 0.0-1.0 + metrics
        v
Confidence Scoring
        |
        | weighted combined ai_score + confidence
        v
Transparency Label Generator
        |
        | exact reader-facing label text
        v
Audit Logger + Submission Store
        |
        | JSONL audit entry + stored content status
        v
JSON response to client
```

### Appeal flow

```text
Creator
        |
        | POST /appeal {content_id, creator_reasoning}
        v
Flask API validation
        |
        | lookup original decision
        v
Submission Store
        |
        | status: classified -> under_review
        v
Audit Logger
        |
        | appeal entry with original attribution + creator reasoning
        v
JSON confirmation to creator
```

A submitted piece of text first reaches the `/submit` endpoint, which validates `text` and `creator_id`. The detection pipeline runs the signals, combines them into a single AI-likelihood score and confidence score, maps the result to a plain-language transparency label, stores the decision, writes a structured audit log entry, and returns a JSON response. If the creator disagrees, `/appeal` updates the stored status to `under_review` and logs the appeal beside the original decision.

---

## API Surface

### `POST /submit`

Accepts:

```json
{
  "text": "creative writing sample here",
  "creator_id": "creator-123"
}
```

Returns: `content_id`, `creator_id`, `status`, `attribution`, `ai_score`, `confidence`, `transparency_label`, and individual `signals`.

### `POST /appeal`

Accepts:

```json
{
  "content_id": "existing-content-id",
  "creator_reasoning": "I wrote this myself and can provide drafts."
}
```

Returns a confirmation and updates the stored status to `under_review`.

### `GET /log`

Returns recent structured audit log entries for documentation and debugging.

### `GET /content/<content_id>`

Returns the stored metadata for one submission without exposing the full original text.

---

## Detection Signals

### Signal 1 — Groq LLM Judge

**What it measures:** A holistic attribution judgment. The LLM reads the text and estimates whether it has AI-like qualities such as generic phrasing, overly smooth structure, repetitive framing, or template-like coherence.

**Output:** A dictionary with `ai_score` from `0.0` to `1.0`, where `0.0` means strongly human-like and `1.0` means strongly AI-like, plus a short reason.

**Why I chose it:** It captures semantic and stylistic cues that are hard to write as rules.

**Blind spot:** It may over-trust polished human writing or under-detect edited AI output. It is also not proof of authorship.

### Signal 2 — Stylometric Heuristics

**What it measures:** Structural properties of the text: sentence length variance, average sentence length, vocabulary diversity, and punctuation density. AI text often has more uniform sentence lengths and smoother structure, while human writing is often more irregular.

**Output:** A dictionary with `ai_score` from `0.0` to `1.0` and metrics like `sentence_length_variance`, `type_token_ratio`, and `punctuation_density`.

**Why I chose it:** It is independent from the LLM because it uses measurable text statistics instead of semantic judgment.

**Blind spot:** Formal human writing can look AI-like, and casual AI-generated writing can be edited to look irregular.

### Signal 3 — Phrase-Pattern Heuristic

**What it measures:** Generic AI-like phrasing, such as “it is important to note,” “paradigm shift,” or “ethical implications,” while offsetting for casual human markers like “lol,” “tbh,” or “ok so.”

**Output:** A dictionary with `ai_score` from `0.0` to `1.0`, plus counts of AI phrase hits and casual marker hits.

**Why I chose it:** It adds a third signal for an ensemble approach and catches language patterns that stylometrics alone may miss.

**Blind spot:** It is easy to fool by avoiding common AI phrases, and some human academic writing naturally uses formal transition words.
