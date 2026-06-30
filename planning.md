# Provenance Guard — Planning

Provenance Guard is a backend API for a creative sharing platform. It analyzes submitted text, combines multiple attribution signals, returns a confidence score and transparency label, logs every decision, and allows creators to appeal classifications.

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

A submitted piece of text first reaches the `/submit` endpoint, which validates `text` and `creator_id`. The detection pipeline runs three signals, combines them into a single AI-likelihood score and confidence score, maps the result to a plain-language transparency label, stores the decision, writes a structured audit log entry, and returns a JSON response. If the creator disagrees, `/appeal` updates the stored status to `under_review` and logs the appeal beside the original decision.

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

---

## Confidence Scoring and Uncertainty

Each signal returns an `ai_score` between `0.0` and `1.0`:

- `0.0` = strongly human-like
- `0.5` = uncertain or mixed
- `1.0` = strongly AI-like

The combined score is a weighted ensemble:

```text
combined_ai_score = 0.50 * llm_score
                  + 0.30 * stylometric_score
                  + 0.20 * phrase_pattern_score
```

The LLM signal receives the largest weight because it captures holistic context. Stylometrics receives the second-largest weight because it is independent and measurable. Phrase patterns receive the smallest weight because they are useful but brittle.

The system then calculates confidence as:

```text
confidence = max(combined_ai_score, 1 - combined_ai_score)
```

This means a score near `0.50` has low confidence because the system is unsure, while scores near `0.0` or `1.0` have higher confidence.

### Thresholds

| Combined AI score | Attribution | Label variant |
|---:|---|---|
| `0.00–0.40` | `likely_human` | High-confidence human |
| `0.41–0.69` | `uncertain` | Uncertain |
| `0.70–1.00` | `likely_ai` | High-confidence AI |

The thresholds are **deliberately asymmetric**. A false positive — labeling a real human's work as AI-generated — is the worst outcome on a creative writing platform, so the `likely_ai` band requires a high score (`>= 0.70`) while the `likely_human` band is generous (`<= 0.40`). Borderline content falls into `uncertain` and is shown without a strong claim. This asymmetry also reflects a calibration reality discovered during testing: because the stylometric signal rarely scores short text above ~0.55 (short human and AI samples both have high vocabulary diversity), a `0.75` AI threshold was effectively unreachable, so it was lowered to `0.70`.

A combined score of `0.60` means the system is only mildly leaning toward AI. It should not display a strong AI or human claim, so it maps to the `uncertain` label.

---

## Transparency Label Design

The label must be understandable to a non-technical reader and must not pretend the system can prove authorship.

| Variant | Exact label text |
|---|---|
| High-confidence AI | “This submission appears likely AI-generated. Our system found strong AI-like patterns, but this is not a final judgment; the creator may appeal.” |
| High-confidence human | “This submission appears likely human-written. Our system found mostly human-like patterns, but no automated check can prove authorship.” |
| Uncertain | “We are not confident enough to label this submission as AI- or human-written. It will be shown without a strong attribution claim, and the creator can provide more context.” |

---

## Appeals Workflow

A creator can submit an appeal if they believe their work was misclassified. The appeal requires:

- `content_id`: the submission being appealed
- `creator_reasoning`: the creator’s explanation, such as drafting history, writing context, or why the text may appear AI-like

When an appeal is received, the system:

1. Finds the original content record.
2. Updates status from `classified` to `under_review`.
3. Stores the creator’s reasoning with the content record.
4. Writes a new audit log entry with the original attribution, original confidence, and appeal reasoning.
5. Returns a confirmation JSON response.

A human reviewer would see the original text preview, attribution, confidence, individual signal scores, and the appeal reasoning.

---

## Anticipated Edge Cases

1. **Formal academic or business writing by a human.** This may be misclassified as AI-like because it can be polished, uniform, and phrase-heavy. Stylometric and phrase signals may over-score it.

2. **Poetry with repetition and simple vocabulary.** A human poem may have repeated phrases and low vocabulary diversity, which can look AI-like to stylometric heuristics.

3. **AI-generated text edited with casual slang.** The phrase-pattern signal may under-score it as human-like if the user adds casual markers like “lol” or “honestly.”

4. **Short submissions.** Very short text gives weak stylometric evidence, so the system requires at least 40 characters and may still return uncertain for short inputs.

---

## Rate Limiting Plan

The `/submit` endpoint uses:

```text
10 submissions per minute; 100 submissions per day
```

A real writer is unlikely to submit more than 10 pieces in one minute, so the minute limit blocks scripts or spam. The daily limit still allows normal creative use while preventing one user from flooding the detection pipeline or exhausting API tokens.

---

## Audit Log Plan

The system uses JSONL at `logs/audit.jsonl`. Every line is one structured JSON object. Submission log entries include timestamp, content ID, creator ID, attribution, confidence, combined AI score, signal scores, transparency label, status, and text preview. Appeal log entries include timestamp, content ID, original attribution/confidence, creator reasoning, and updated status.

JSONL was chosen because it is append-only, structured, easy to inspect, and production log tools can process it line by line.

---

## AI Tool Plan

### M3 — Submission endpoint + first signal

I will give the AI tool my architecture diagram, API surface, and Groq LLM signal section. I will ask it to generate a Flask app skeleton with `/submit`, `/log`, and the LLM signal function. I will verify by sending a test curl request and checking the JSON response.

### M4 — Second signal + confidence scoring

I will provide the detection signals section and uncertainty scoring section. I will ask the AI tool to generate stylometric and phrase-pattern functions plus the weighted confidence scorer. I will verify with clearly AI-like, clearly human-like, and borderline inputs to confirm the scores vary meaningfully.

### M5 — Production layer

I will provide the transparency label variants, appeals workflow, rate limit plan, and architecture diagram. I will ask the AI tool to generate the label function, `/appeal` endpoint, and rate limiter setup. I will verify that all three label variants can be reached, that appeals update status to `under_review`, and that rapid requests trigger 429 rate-limit responses.
