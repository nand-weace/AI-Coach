import json
import os
import re
from datetime import datetime, timezone

from database import (
    get_all_org_slugs,
    get_user_messages,
    get_user_dated_messages,
    get_user_insight_report,
    upsert_user_insight_report,
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

_THEMES_MAX_CHARS = 24_000  # recurring-theme prompt cap
_MIN_THEME_MESSAGES = 8     # a theme needs a few mentions before it recurs
_DIGEST_MAX_CHARS = 20_000  # weekly digest prompt cap
_DIGEST_DAYS = 7            # window the weekly digest reads
_MIN_DIGEST_MESSAGES = 3    # below this the week has nothing to reflect on

# Recommendations are the most expensive report on the page (web search + a long
# prompt), so the input is deliberately small: the cached recurring themes if we
# have them, otherwise a trimmed sample of recent messages.
_RECS_MAX_CHARS = 3_000     # recommendation prompt cap
_RECS_MSG_SAMPLE = 40       # messages read when there are no cached themes
_RECS_MSG_CHARS = 220       # per-message truncation in that fallback
_MIN_RECS_MESSAGES = 5      # below this we cannot tell what to recommend
_RECS_COUNT = 6             # resources to ask for per run
# Searches run one after another and dominate the wall-clock time, so the cap is
# below the resource count on purpose: the model spends them on the resources
# whose links it is least sure of, and the rest fall back to a search URL.
_RECS_SEARCHES = 4          # hard cap on web searches per run
_RECS_TYPES = ('article', 'book', 'video', 'lecture')

# ── Prompts ──────────────────────────────────────────────────────────────────

# Framing for the personal reports. Without it the request arrives as a bare
# "analyse this person's messages", which reads like profiling someone who isn't
# in the room — and the model sometimes declines rather than answer. It is the
# user's own data, requested by them, shown only to them, so say so.
_PERSONAL_SYSTEM = """You are supporting an executive coaching product. The messages you are given were \
written by ONE person during their own AI coaching sessions, and this analysis was requested by that same \
person about themselves. It is shown only to them, in their private insights dashboard.

Write to them in the second person ("you"). This is reflective coaching feedback on their own words — \
supportive and specific, never a clinical or diagnostic assessment, and never a judgement of them as a person.

If their messages touch on distress, health, or personal difficulty, do not diagnose, alarm, or speculate: \
describe the topic plainly and gently in the language they used themselves.

Always answer with the JSON object the user turn asks for, and nothing else. If some part of the material \
does not fit the requested shape, leave that field empty rather than replying with prose."""

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

# Recurring theme detection — what the same person keeps coming back to across
# sessions. Dated lines let the model judge whether a theme is rising or fading.
_THEMES_PROMPT = """You are an executive coach reviewing one leader's own messages from their AI coaching sessions. \
Each line is prefixed with the date it was written.

Identify the topics this person KEEPS RETURNING TO across different dates — recurring themes, not one-off remarks. \
A theme only counts if it appears on at least two separate dates. Return at most 6, most significant first. \
Ignore pleasantries, greetings, and administrative chatter.

For each theme:
- label: 2-4 words, plain language (e.g. "Delegating to the team")
- summary: one second-person sentence on how this shows up in their words
- mentions: how many messages touch this theme (integer)
- first_seen / last_seen: the earliest and latest dates it appears, as YYYY-MM-DD
- momentum: "rising" if it is showing up more lately, "steady" if evenly spread, "fading" if it has quietened down
- sentiment: "positive", "mixed", or "challenging" — how they sound when writing about it
- prompt: one short coaching question they could take into their next session

Write to the person ("you"), constructive and specific. Never diagnose.

Return ONLY a valid JSON object with no markdown fences:
{
  "themes": [
    {"label": "<2-4 words>", "summary": "<one sentence>", "mentions": <int>,
     "first_seen": "YYYY-MM-DD", "last_seen": "YYYY-MM-DD",
     "momentum": "rising|steady|fading", "sentiment": "positive|mixed|challenging",
     "prompt": "<one coaching question>"}
  ],
  "overview": "<one sentence tying the themes together>"
}

If nothing genuinely recurs, return {"themes": [], "overview": "<one sentence saying so>"}.

Messages to analyse:
---
{messages}
---"""

# Weekly reflection digest — a short read-back of the last seven days.
_DIGEST_PROMPT = """You are an executive coach writing a short weekly reflection digest for ONE leader, \
based only on the messages they wrote during their AI coaching sessions this week. \
Each line is prefixed with the date it was written.

Write everything in the second person ("you"), warm but direct, and grounded in what they actually said. \
Never invent events they did not mention. Keep every sentence short.

Return ONLY a valid JSON object with no markdown fences:
{
  "headline": "<6-10 words capturing the week>",
  "summary": "<2-3 sentences on what occupied you this week>",
  "wins": ["<up to 3 short bullets on progress or good moments>"],
  "challenges": ["<up to 3 short bullets on what weighed on you>"],
  "patterns": ["<up to 3 short bullets on habits or language patterns worth noticing>"],
  "questions": ["<2-3 reflection questions to sit with>"],
  "focus": "<one sentence suggesting where to put attention next week>"
}

Use an empty array for any section the week gives you nothing for. Do not pad.

This week's messages:
---
{messages}
---"""

# Reading & watching recommendations. Two variants: the search version is used
# when web search is available (Claude), so links can be checked rather than
# recalled; the offline version asks for well-known works only and leaves the
# link to be built as a search URL on our side.
_RECS_SEARCH_PROMPT = """You are an executive coach curating a reading and watching list for ONE leader, \
from the coaching topics below.

Recommend {count} resources — at least one each of: "article" (a substantial article, essay, or research), \
"book" (a well-regarded book), "video" (a YouTube video or talk), "lecture" (a talk, university lecture, \
or course session). Prefer well-known, high-quality sources.

Links: you have at most {searches} web searches — fewer than the number of resources, so spend them on the \
ones whose URL you are least sure of, and batch several resources into one search where you can. Never invent \
or pattern-guess a URL, and do not open or read the pages — the search result link is enough. Leave "url" as \
"" for anything you did not find a link for; do not spend a search confirming something you already have.

Per resource: title (real title), author (author, speaker, or publication), type, \
description (ONE sentence written to them — "you" — on why it fits), topic (2-4 words), \
length (e.g. "12 min read", "18 min", "288 pages", else ""), url (the link you found, or "").

Return ONLY a valid JSON object with no markdown fences:
{
  "recommendations": [
    {"title": "", "author": "", "type": "article|book|video|lecture",
     "description": "", "topic": "", "length": "", "url": ""}
  ],
  "overview": "<one sentence on why this list, addressed to them>"
}

Their coaching topics:
---
{messages}
---"""

_RECS_OFFLINE_PROMPT = """You are an executive coach curating a reading and watching list for ONE leader, \
from the coaching topics below.

Recommend {count} resources — at least one each of: "article" (a substantial article, essay, or research), \
"book" (a well-regarded book), "video" (a YouTube video or talk), "lecture" (a talk, university lecture, \
or course session).

Recommend ONLY works you are confident genuinely exist and are well known — a real title by a real author. \
Do NOT output any URL: leave "url" as "". Accuracy of title and author matters more than novelty.

Per resource: title (real title), author (author, speaker, or publication), type, \
description (ONE sentence written to them — "you" — on why it fits), topic (2-4 words), \
length (e.g. "12 min read", "288 pages", else ""), url ("").

Return ONLY a valid JSON object with no markdown fences:
{
  "recommendations": [
    {"title": "", "author": "", "type": "article|book|video|lecture",
     "description": "", "topic": "", "length": "", "url": ""}
  ],
  "overview": "<one sentence on why this list, addressed to them>"
}

Their coaching topics:
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


class ReportUnavailable(Exception):
    """The model replied, but not with the report we asked for.

    Distinct from "too little data" — the user has said plenty, we just could not
    turn this particular run into a report, so the UI should say that rather than
    telling them to keep chatting.
    """


def _reply_text(response) -> str:
    """Pull the prose out of a Claude response.

    Not just content[0].text: a response can lead with a non-text block, and a
    declined turn can carry no text block at all.
    """
    if getattr(response, 'stop_reason', None) == 'refusal':
        raise ReportUnavailable(
            "Nexa could not write this report from your recent conversations. Try again later."
        )
    parts = [b.text.strip() for b in response.content
             if getattr(b, 'type', None) == 'text' and b.text.strip()]
    return '\n\n'.join(parts)


def _call_llm(prompt: str, client, ai_provider: str, ai_model: str, max_tokens: int = 1024,
              system: str = None) -> str:
    if ai_provider == 'claude':
        kwargs = {'system': system} if system else {}
        response = client.messages.create(
            model=ai_model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
            **kwargs,
        )
        return _reply_text(response)
    messages = ([{"role": "system", "content": system}] if system else []) \
        + [{"role": "user", "content": prompt}]
    response = client.chat.completions.create(
        model=ai_model,
        temperature=0.2,
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
        messages=messages,
    )
    return response.choices[0].message.content.strip()


# Claude has no browsing of its own — this Anthropic-hosted server tool is what
# lets a recommendation carry a link that actually resolves today rather than one
# recalled from training. Search only: web_fetch would pull whole pages into the
# prompt, and a search result already carries the link and title we need.
# max_uses stops a thorough turn from running twenty searches.
_WEB_TOOLS = [
    {"type": "web_search_20260209", "name": "web_search", "max_uses": _RECS_SEARCHES},
]
_MAX_PAUSE_RESUMES = 2


def _call_llm_websearch(prompt: str, client, ai_model: str, max_tokens: int = 4096,
                        system: str = None) -> str:
    """Claude-only: same as _call_llm but with web search, resuming paused turns.

    A search-heavy turn can exhaust the server-side tool loop and come back
    paused; re-sending with the paused turn appended picks it back up.
    """
    convo = [{"role": "user", "content": prompt}]
    kwargs = {'system': system} if system else {}
    response = None
    for _ in range(_MAX_PAUSE_RESUMES + 1):
        response = client.messages.create(
            model=ai_model,
            max_tokens=max_tokens,
            messages=convo,
            tools=_WEB_TOOLS,
            **kwargs,
        )

        if response.stop_reason != 'pause_turn':
            break
        convo.append({"role": "assistant", "content": response.content})
    # With tools on, content interleaves tool-use blocks — the prose can arrive
    # as several text blocks either side of a search.
    return _reply_text(response)


def _extract_json(raw: str) -> dict:
    match = re.search(r'\{[\s\S]*\}', raw)
    if not match:
        # Prose where JSON should be means the model answered something other
        # than what we asked — usually a declined or deferred turn. That is a
        # different problem from bad data, so it gets its own exception. The
        # model's actual words go to the log, not to the user.
        print(f"  Expected JSON, got prose: {raw[:300].strip()!r}")
        raise ReportUnavailable(
            "Nexa returned an empty response. Try again in a moment." if not raw.strip()
            else "Nexa could not write this report from your recent conversations. Try again in a moment."
        )
    try:
        return json.loads(match.group())
    except json.JSONDecodeError:
        # A tool-using turn often wraps the JSON in prose that itself contains
        # braces, so the outermost span is not the object. Fall back to the
        # first brace that opens something parseable.
        decoder = json.JSONDecoder()
        for i, ch in enumerate(raw):
            if ch != '{':
                continue
            try:
                obj, _ = decoder.raw_decode(raw[i:])
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                return obj
        raise


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
    ai_provider = os.environ.get('AI_PROVIDER', 'claude').lower()
    api_key = os.environ.get('CLAUDE_API_KEY' if ai_provider == 'claude' else 'OPENAI_API_KEY')
    if not api_key:
        print("Personal sentiment: no API key configured.")
        return None
    ai_model = os.environ.get('AI_MODEL', 'claude-sonnet-4-6' if ai_provider == 'claude' else 'gpt-4o')

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


# ── Personal narrative reports ───────────────────────────────────────────────

def _llm_config():
    """(client, provider, model) for the configured provider, or None if unset."""
    ai_provider = os.environ.get('AI_PROVIDER', 'claude').lower()
    api_key = os.environ.get('CLAUDE_API_KEY' if ai_provider == 'claude' else 'OPENAI_API_KEY')
    if not api_key:
        return None
    ai_model = os.environ.get('AI_MODEL', 'claude-sonnet-4-6' if ai_provider == 'claude' else 'gpt-4o')
    return _build_client(ai_provider, api_key), ai_provider, ai_model


def _dated_block(messages: list, max_chars: int) -> str:
    combined = '\n'.join(f"[{m['date']}] {m['content']}" for m in messages)
    if len(combined) > max_chars:
        # keep the most recent tail — recency matters more for both reports
        combined = '[...earlier messages truncated]\n' + combined[-max_chars:]
    return combined


def detect_recurring_themes(user_id: str) -> dict | None:
    """
    Finds the topics one user keeps returning to across sessions and caches the
    result. Returns the report, or None if there is too little to read or the
    LLM call fails.
    """
    cfg = _llm_config()
    if not cfg:
        print("Recurring themes: no API key configured.")
        return None
    client, ai_provider, ai_model = cfg

    messages = get_user_dated_messages(user_id, limit=300)
    if len(messages) < _MIN_THEME_MESSAGES:
        return None

    raw = None
    try:
        raw = _call_llm(
            _THEMES_PROMPT.replace('{messages}', _dated_block(messages, _THEMES_MAX_CHARS)),
            client, ai_provider, ai_model, max_tokens=2048, system=_PERSONAL_SYSTEM,
        )
        result = _extract_json(raw)
    except ReportUnavailable:
        print(f"Recurring theme detection unavailable for {user_id}")
        raise
    except Exception as e:
        print(f"Recurring theme detection failed for {user_id}: {e}")
        if raw:
            print(f"  Raw snippet: {raw[:300]}")
        return None

    themes = result.get('themes')
    if not isinstance(themes, list):
        return None

    cleaned = []
    for t in themes[:6]:
        if not isinstance(t, dict) or not t.get('label'):
            continue
        try:
            mentions = max(0, int(t.get('mentions') or 0))
        except (TypeError, ValueError):
            mentions = 0
        momentum = str(t.get('momentum', 'steady')).lower()
        sentiment = str(t.get('sentiment', 'mixed')).lower()
        cleaned.append({
            'label':      str(t['label'])[:80],
            'summary':    str(t.get('summary', ''))[:400],
            'mentions':   mentions,
            'first_seen': str(t.get('first_seen', ''))[:10],
            'last_seen':  str(t.get('last_seen', ''))[:10],
            'momentum':   momentum if momentum in ('rising', 'steady', 'fading') else 'steady',
            'sentiment':  sentiment if sentiment in ('positive', 'mixed', 'challenging') else 'mixed',
            'prompt':     str(t.get('prompt', ''))[:300],
        })

    report = {
        'themes': cleaned,
        'overview': str(result.get('overview', ''))[:500],
        'messages_analyzed': len(messages),
    }
    upsert_user_insight_report(user_id, 'recurring_themes', report)
    return report


def generate_weekly_digest(user_id: str) -> dict | None:
    """
    Writes a reflection digest over the user's last seven days of messages and
    caches it. Returns the report, or None if the week is too quiet to read.
    """
    cfg = _llm_config()
    if not cfg:
        print("Weekly digest: no API key configured.")
        return None
    client, ai_provider, ai_model = cfg

    messages = get_user_dated_messages(user_id, limit=200, since_days=_DIGEST_DAYS)
    if len(messages) < _MIN_DIGEST_MESSAGES:
        return None

    raw = None
    try:
        raw = _call_llm(
            _DIGEST_PROMPT.replace('{messages}', _dated_block(messages, _DIGEST_MAX_CHARS)),
            client, ai_provider, ai_model, max_tokens=2048, system=_PERSONAL_SYSTEM,
        )
        result = _extract_json(raw)
    except ReportUnavailable:
        print(f"Weekly digest unavailable for {user_id}")
        raise
    except Exception as e:
        print(f"Weekly digest failed for {user_id}: {e}")
        if raw:
            print(f"  Raw snippet: {raw[:300]}")
        return None

    def _bullets(key, cap=3):
        vals = result.get(key)
        if not isinstance(vals, list):
            return []
        return [str(v)[:300] for v in vals if str(v).strip()][:cap]

    report = {
        'headline':   str(result.get('headline', ''))[:160],
        'summary':    str(result.get('summary', ''))[:800],
        'wins':       _bullets('wins'),
        'challenges': _bullets('challenges'),
        'patterns':   _bullets('patterns'),
        'questions':  _bullets('questions'),
        'focus':      str(result.get('focus', ''))[:400],
        'messages_analyzed': len(messages),
        'period_from': messages[0]['date'],
        'period_to':   messages[-1]['date'],
    }
    if not report['summary'] and not report['headline']:
        return None
    upsert_user_insight_report(user_id, 'weekly_digest', report)
    return report


def _fallback_url(item: dict) -> str:
    """A search link for a resource whose real URL we could not verify.

    Better than dropping the card (the recommendation itself is still useful)
    and far better than shipping a guessed URL that 404s.
    """
    from urllib.parse import quote_plus
    query = ' '.join(p for p in (item.get('title', ''), item.get('author', '')) if p).strip()
    if not query:
        return ''
    if item.get('type') in ('video', 'lecture'):
        return f"https://www.youtube.com/results?search_query={quote_plus(query)}"
    return f"https://www.google.com/search?q={quote_plus(query)}"


def _recs_input(user_id: str) -> tuple[str, int]:
    """
    The smallest input that still says what to recommend, as (text, messages_read).

    Recurring themes are already a distilled read of the same history, so when
    that report is cached we send a few hundred characters instead of the whole
    message log. Only a user who has never run theme detection pays for the
    message sample.
    """
    themes_report = get_user_insight_report(user_id, 'recurring_themes') or {}
    themes = themes_report.get('themes') or []
    if themes:
        lines = [f"- {t.get('label', '')}: {t.get('summary', '')}".strip(' :')
                 for t in themes[:6] if t.get('label')]
        if lines:
            return '\n'.join(lines)[:_RECS_MAX_CHARS], themes_report.get('messages_analyzed', 0)

    messages = get_user_dated_messages(user_id, limit=_RECS_MSG_SAMPLE)
    if len(messages) < _MIN_RECS_MESSAGES:
        return '', 0
    lines = [f"- {m['content'][:_RECS_MSG_CHARS]}" for m in messages]
    return '\n'.join(lines)[-_RECS_MAX_CHARS:], len(messages)


def generate_recommendations(user_id: str) -> dict | None:
    """
    Curates articles, books, videos, and lectures for one user from the topics
    in their own coaching messages, and caches the result. Uses web search when
    the provider supports it so links resolve; otherwise falls back to a search
    URL built from the title. Returns the report, or None if there is too little
    to read or the LLM call fails.
    """

    cfg = _llm_config()
    if not cfg:
        print("Recommendations: no API key configured.")
        return None
    client, ai_provider, ai_model = cfg

    block, analyzed = _recs_input(user_id)
    if not block:
        return None

    can_search = ai_provider == 'claude'
    prompt = (_RECS_SEARCH_PROMPT if can_search else _RECS_OFFLINE_PROMPT) \
        .replace('{count}', str(_RECS_COUNT)) \
        .replace('{searches}', str(_RECS_SEARCHES)) \
        .replace('{messages}', block)

    raw = None
    try:
        if can_search:
            raw = _call_llm_websearch(prompt, client, ai_model, max_tokens=3072,
                                      system=_PERSONAL_SYSTEM)
        else:
            raw = _call_llm(prompt, client, ai_provider, ai_model, max_tokens=2048,
                            system=_PERSONAL_SYSTEM)
        result = _extract_json(raw)
    except ReportUnavailable:
        print(f"Recommendations unavailable for {user_id}")
        raise
    except Exception as e:
        print(f"Recommendations failed for {user_id}: {e}")
        if raw:
            print(f"  Raw snippet: {raw[:300]}")
        return None

    items = result.get('recommendations')
    if not isinstance(items, list):
        return None

    cleaned = []
    for it in items[:12]:
        if not isinstance(it, dict) or not it.get('title'):
            continue
        rtype = str(it.get('type', '')).lower().strip()
        if rtype not in _RECS_TYPES:
            rtype = 'article'
        url = str(it.get('url', '') or '').strip()
        entry = {
            'title':       str(it['title'])[:160],
            'author':      str(it.get('author', ''))[:120],
            'type':        rtype,
            'description': str(it.get('description', ''))[:600],
            'topic':       str(it.get('topic', ''))[:80],
            'length':      str(it.get('length', ''))[:40],
        }
        if url.startswith('http://') or url.startswith('https://'):
            entry['url'] = url[:500]
            entry['verified'] = bool(can_search)
        else:
            entry['url'] = _fallback_url(entry)
            entry['verified'] = False
        cleaned.append(entry)

    if not cleaned:
        return None

    report = {
        'recommendations': cleaned,
        'overview': str(result.get('overview', ''))[:500],
        'messages_analyzed': analyzed,
        'web_searched': can_search,
    }
    upsert_user_insight_report(user_id, 'recommendations', report)
    return report


# ── Main analysis ────────────────────────────────────────────────────────────

def analyze_org_sentiment(org_slug: str) -> dict | None:
    """
    Runs sentiment analysis for the org, processing only messages newer than
    the stored cursor. Saves per-user scores to sentiment_score and org-level
    insights to org_sentiment. Returns the full analysis result or None.
    """
    ai_provider = os.environ.get('AI_PROVIDER', 'claude').lower()
    api_key = os.environ.get('CLAUDE_API_KEY' if ai_provider == 'claude' else 'OPENAI_API_KEY')
    if not api_key:
        print("Sentiment job: no API key configured.")
        return None
    ai_model = os.environ.get('AI_MODEL', 'claude-sonnet-4-6' if ai_provider == 'claude' else 'gpt-4o')

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
