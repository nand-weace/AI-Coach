import json
import os
import re
from datetime import datetime, timezone

from database import (
    get_all_org_slugs,
    get_user_messages,
    upsert_user_sentiment,
    get_org_user_messages_after,
    get_org_messages_by_user,
    get_org_users_with_new_messages,
    get_org_max_message_id,
    get_sentiment_cursor,
    update_sentiment_cursor,
    insert_user_sentiment_scores,
    upsert_org_sentiment,
)

# ── Constants ────────────────────────────────────────────────────────────────

_DIMS = [
    'work_life_balance', 'job_satisfaction', 'stress_anxiety', 'self_confidence',
    'empathy', 'frustration_disengagement', 'growth_mindset', 'psychological_safety',
]
_MAX_CHARS = 40_000   # org-level prompt cap (~10k tokens)
_USER_MAX_CHARS = 6_000  # per-user cap (keeps per-user calls cheap)
_SELF_MAX_CHARS = 24_000  # personal insights prompt cap (one user, richer output)
_MIN_SELF_MESSAGES = 5    # below this there is nothing meaningful to read
_MAX_USERS = 30       # cap to avoid excessive API calls on large orgs

# ── Prompts ──────────────────────────────────────────────────────────────────

_ORG_PROMPT = """You are a professional psycholinguistic analyst specialising in leadership psychology. \
Analyse the following collection of questions and messages written by organisational leaders during AI coaching sessions.

Score each dimension 0–100 based purely on the language patterns present:

- work_life_balance: 0=severe imbalance/always-on language, 100=healthy boundaries and balance
- job_satisfaction: 0=very dissatisfied/disengaged, 100=highly fulfilled and motivated
- stress_anxiety: 0=no stress signals, 100=extreme stress/anxiety/urgency language
- self_confidence: 0=very uncertain/self-doubting, 100=very assertive/confident
- empathy: 0=no empathy shown, 100=very high empathy toward others
- frustration_disengagement: 0=no frustration, 100=extreme frustration/cynicism/dismissiveness
- growth_mindset: 0=fixed-mindset language, 100=strong growth/learning/effort framing
- psychological_safety: 0=no vulnerability shared, 100=high openness about failures and fears

Return ONLY a valid JSON object with no markdown fences:
{
  "work_life_balance": {"score": <0-100 int>, "insight": "<one concise sentence>"},
  "job_satisfaction": {"score": <0-100 int>, "insight": "<one concise sentence>"},
  "stress_anxiety": {"score": <0-100 int>, "insight": "<one concise sentence>"},
  "self_confidence": {"score": <0-100 int>, "insight": "<one concise sentence>"},
  "empathy": {"score": <0-100 int>, "insight": "<one concise sentence>"},
  "frustration_disengagement": {"score": <0-100 int>, "insight": "<one concise sentence>"},
  "growth_mindset": {"score": <0-100 int>, "insight": "<one concise sentence>"},
  "psychological_safety": {"score": <0-100 int>, "insight": "<one concise sentence>"}
}

Messages to analyse:
---
{messages}
---"""

# Compact prompt — scores only, no insights. Keeps per-user calls cheap.
_USER_PROMPT = """Score these leadership coaching messages on 8 dimensions (0–100 each).
Return ONLY this compact JSON with no other text:
{"work_life_balance":N,"job_satisfaction":N,"stress_anxiety":N,"self_confidence":N,"empathy":N,"frustration_disengagement":N,"growth_mindset":N,"psychological_safety":N}

Scoring guide: work_life_balance(0=imbalanced,100=balanced), job_satisfaction(0=dissatisfied,100=fulfilled), \
stress_anxiety(0=calm,100=stressed), self_confidence(0=self-doubting,100=assertive), \
empathy(0=none,100=high), frustration_disengagement(0=none,100=high), \
growth_mindset(0=fixed,100=growth), psychological_safety(0=closed,100=open)

Messages:
---
{messages}
---"""

# Personal insights prompt — same dimensions as the org prompt, but the insight
# is written back to the person themselves, so it is second-person and coaching
# in tone rather than an observation about a population.
_SELF_PROMPT = """You are a professional psycholinguistic analyst supporting an executive coaching programme. \
Analyse the following messages written by ONE leader during their AI coaching sessions.

Score each dimension 0–100 based purely on the language patterns present:

- work_life_balance: 0=severe imbalance/always-on language, 100=healthy boundaries and balance
- job_satisfaction: 0=very dissatisfied/disengaged, 100=highly fulfilled and motivated
- stress_anxiety: 0=no stress signals, 100=extreme stress/anxiety/urgency language
- self_confidence: 0=very uncertain/self-doubting, 100=very assertive/confident
- empathy: 0=no empathy shown, 100=very high empathy toward others
- frustration_disengagement: 0=no frustration, 100=extreme frustration/cynicism/dismissiveness
- growth_mindset: 0=fixed-mindset language, 100=strong growth/learning/effort framing
- psychological_safety: 0=no vulnerability shared, 100=high openness about failures and fears

Write each insight in the second person ("you"), one concise sentence, describing what \
their own language suggests. Be specific and constructive, never clinical or diagnostic.

Return ONLY a valid JSON object with no markdown fences:
{
  "work_life_balance": {"score": <0-100 int>, "insight": "<one concise sentence>"},
  "job_satisfaction": {"score": <0-100 int>, "insight": "<one concise sentence>"},
  "stress_anxiety": {"score": <0-100 int>, "insight": "<one concise sentence>"},
  "self_confidence": {"score": <0-100 int>, "insight": "<one concise sentence>"},
  "empathy": {"score": <0-100 int>, "insight": "<one concise sentence>"},
  "frustration_disengagement": {"score": <0-100 int>, "insight": "<one concise sentence>"},
  "growth_mindset": {"score": <0-100 int>, "insight": "<one concise sentence>"},
  "psychological_safety": {"score": <0-100 int>, "insight": "<one concise sentence>"}
}

Messages to analyse:
---
{messages}
---"""


# ── LLM helpers ─────────────────────────────────────────────────────────────

def _build_client(ai_provider: str, api_key: str):
    if ai_provider == 'claude':
        from anthropic import Anthropic
        return Anthropic(api_key=api_key)
    from openai import OpenAI
    return OpenAI(api_key=api_key)


def _call_llm(prompt: str, client, ai_provider: str, ai_model: str, max_tokens: int = 1024) -> str:
    if ai_provider == 'claude':
        response = client.messages.create(
            model=ai_model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    response = client.chat.completions.create(
        model=ai_model,
        temperature=0.2,
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content.strip()


def _extract_json(raw: str) -> dict:
    match = re.search(r'\{[\s\S]*\}', raw)
    if not match:
        raise ValueError(f"No JSON object in LLM response: {raw[:200]}")
    return json.loads(match.group())


# ── Per-user scoring ─────────────────────────────────────────────────────────

def _score_user(messages: list, client, ai_provider: str, ai_model: str) -> dict | None:
    """Score a single user's messages. Returns {dim: int} or None on failure."""
    if not messages:
        return None
    combined = '\n'.join(f'- {m}' for m in messages)
    if len(combined) > _USER_MAX_CHARS:
        combined = combined[:_USER_MAX_CHARS] + '\n[...truncated]'
    try:
        raw = _call_llm(
            _USER_PROMPT.replace('{messages}', combined),
            client, ai_provider, ai_model, max_tokens=128,
        )
        data = _extract_json(raw)
        return {d: max(0, min(100, int(data.get(d, 0)))) for d in _DIMS}
    except Exception:
        return None


# ── Personal (single-user) analysis ──────────────────────────────────────────

def analyze_user_sentiment(user_id: str) -> dict | None:
    """
    Scores one user's own messages and writes second-person insights for their
    personal insights dashboard. Saves scores to sentiment_score (so the trend
    builds up run by run) and insights to user_sentiment. Returns the result,
    or None if there is too little to read or the LLM call fails.
    """
    ai_provider = os.environ.get('AI_PROVIDER', 'openai').lower()
    api_key = os.environ.get('CLAUDE_API_KEY' if ai_provider == 'claude' else 'OPENAI_API_KEY')
    if not api_key:
        print("Personal sentiment: no API key configured.")
        return None
    ai_model = os.environ.get('AI_MODEL', 'claude-sonnet-4-5' if ai_provider == 'claude' else 'gpt-4o')

    messages = get_user_messages(user_id)
    if len(messages) < _MIN_SELF_MESSAGES:
        return None

    combined = '\n'.join(f'- {m}' for m in messages)
    if len(combined) > _SELF_MAX_CHARS:
        combined = combined[:_SELF_MAX_CHARS] + '\n[...truncated]'

    client = _build_client(ai_provider, api_key)
    raw = None
    try:
        raw = _call_llm(_SELF_PROMPT.replace('{messages}', combined), client, ai_provider, ai_model)
        result = _extract_json(raw)
    except Exception as e:
        print(f"Personal sentiment analysis failed for {user_id}: {e}")
        if raw:
            print(f"  Raw snippet: {raw[:300]}")
        return None

    scores = {}
    for dim in _DIMS:
        entry = result.get(dim)
        if isinstance(entry, dict) and entry.get('score') is not None:
            try:
                scores[dim] = max(0, min(100, int(entry['score'])))
            except (TypeError, ValueError):
                continue
    if not scores:
        return None

    run_ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    insert_user_sentiment_scores(user_id, scores, run_ts)

    result['messages_analyzed'] = len(messages)
    upsert_user_sentiment(user_id, result)
    return result


# ── Main analysis ────────────────────────────────────────────────────────────

def analyze_org_sentiment(org_slug: str) -> dict | None:
    """
    Runs sentiment analysis for the org, processing only messages newer than
    the stored cursor. Saves per-user scores to sentiment_score and org-level
    insights to org_sentiment. Returns the full analysis result or None.
    """
    ai_provider = os.environ.get('AI_PROVIDER', 'openai').lower()
    api_key = os.environ.get('CLAUDE_API_KEY' if ai_provider == 'claude' else 'OPENAI_API_KEY')
    if not api_key:
        print("Sentiment job: no API key configured.")
        return None
    ai_model = os.environ.get('AI_MODEL', 'claude-sonnet-4-5' if ai_provider == 'claude' else 'gpt-4o')

    last_id = get_sentiment_cursor(org_slug)
    max_id = get_org_max_message_id(org_slug)

    if not max_id or max_id <= last_id:
        print(f"  No new messages for {org_slug} since last analysis.")
        return None

    # New messages for org-level LLM insight generation
    new_messages = get_org_user_messages_after(org_slug, last_id, limit=1000)
    if not new_messages:
        return None

    client = _build_client(ai_provider, api_key)

    # ── Step 1: org-level analysis on NEW messages → insights ───────────────
    combined = '\n'.join(f'- {m}' for m in new_messages)
    if len(combined) > _MAX_CHARS:
        combined = combined[:_MAX_CHARS] + '\n[...truncated]'

    raw = None
    try:
        raw = _call_llm(_ORG_PROMPT.replace('{messages}', combined), client, ai_provider, ai_model)
        result = _extract_json(raw)
        result['messages_analyzed'] = len(new_messages)
    except Exception as e:
        print(f"Org-level sentiment analysis failed for {org_slug}: {e}")
        if raw:
            print(f"  Raw snippet: {raw[:300]}")
        return None

    # ── Step 2: per-user scoring for users with new messages ────────────────
    active_user_ids = get_org_users_with_new_messages(org_slug, last_id)
    user_map = get_org_messages_by_user(org_slug)

    # Evenly sample if the active set exceeds the cap
    if len(active_user_ids) > _MAX_USERS:
        step = len(active_user_ids) / _MAX_USERS
        active_user_ids = [active_user_ids[int(i * step)] for i in range(_MAX_USERS)]

    run_ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    user_scores: list[dict] = []

    for uid in active_user_ids:
        scores = _score_user(user_map.get(uid, []), client, ai_provider, ai_model)
        if scores:
            insert_user_sentiment_scores(uid, scores, run_ts)
            user_scores.append(scores)

    # Compute bands from this run's scored users for the insight result
    if user_scores:
        n = len(user_scores)
        for dim in _DIMS:
            if isinstance(result.get(dim), dict):
                result[dim]['bands'] = {
                    'very_high':  round(sum(1 for s in user_scores if s.get(dim, 0) > 75)          / n * 100),
                    'moderate':   round(sum(1 for s in user_scores if 50 < s.get(dim, 0) <= 75)    / n * 100),
                    'low':        round(sum(1 for s in user_scores if 25 < s.get(dim, 0) <= 50)    / n * 100),
                    'negligible': round(sum(1 for s in user_scores if s.get(dim, 0) <= 25)         / n * 100),
                }

    result['users_analyzed'] = len(user_map)

    # Advance cursor so next run skips already-processed messages
    update_sentiment_cursor(org_slug, max_id)

    return result


# ── Job entry point ───────────────────────────────────────────────────────────

def run_sentiment_job():
    print("Running daily sentiment analysis job...")
    slugs = get_all_org_slugs()
    print(f"Found {len(slugs)} organisation(s) to process.")
    for slug in slugs:
        print(f"  Analysing: {slug}")
        result = analyze_org_sentiment(slug)
        if result:
            upsert_org_sentiment(slug, result)
            print(
                f"  Saved sentiment for {slug} "
                f"({result.get('messages_analyzed', 0)} new messages, "
                f"{result.get('users_analyzed', 0)} users)"
            )
        else:
            print(f"  Skipped {slug} — no new messages or analysis failed.")
    print("Sentiment job complete.")


if __name__ == '__main__':
    from dotenv import load_dotenv
    load_dotenv()
    run_sentiment_job()
