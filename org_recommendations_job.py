"""
Recommended development programmes for one organisation — coaching, mentoring,
workshops and training an HR team could put its people through — built from
what the org's other insights already say:

  - the org sentiment report (scores per dimension plus the de-identified
    insight and patterns behind each, see sentiment_job._org_detail), and
  - the latest theme cloud (what 3+ people keep raising, see word_cloud_job).

Neither input carries message text, so nothing here can surface what one
person said. One LLM call per build; the report is cached per org and rebuilt
only when a newer sentiment run or theme cloud lands (`based_on`).
"""
import os

from database import (
    get_org_sentiment_data, get_latest_theme_cloud,
    get_org_recommendations, upsert_org_recommendations,
    _SENTIMENT_DIMS, _SENTIMENT_TITLES, _SENTIMENT_SCALES,
)

REC_TYPES = ('coaching', 'mentoring', 'workshop', 'training')
REC_PRIORITIES = ('high', 'medium', 'low')
_MAX_RECS = 6
_MAX_THEMES = 12

_PROMPT = """You are an organisational development advisor. An HR leader wants to know which development \
programmes their employees should be offered, based on what the company's AI work coach (Nexa) has picked up \
across many employees' coaching conversations.

You are given two de-identified summaries. Nothing in them identifies a person, and your answer must not try to.

1. SENTIMENT — each dimension is scored 0-100, where HIGHER IS ALWAYS THE HEALTHIER READING \
(0-34 needs attention, 35-64 developing, 65-100 strong). Low scores are where people need support.
{sentiment}

2. FREQUENT THEMES — topics at least 3 different people raised, most raised first \
(people = how many different people, messages = how many messages).
{themes}

Recommend {count} programmes the organisation should run. Rules — every one is mandatory:
- Each is one of: "coaching" (1:1 or small-group with a professional coach), "mentoring" (pairing with \
experienced colleagues), "workshop" (interactive, cohort-based, a few hours to a day), "training" \
(structured skills course or curriculum).
- Every recommendation must be grounded in the data above. "addresses" lists the EXACT dimension titles \
and/or theme names (copied verbatim from above) it responds to — at least one.
- Prioritise the weakest dimensions and the most-raised themes. Where a theme and a low dimension point at \
the same underlying challenge, address them with one programme rather than two.
- Use a sensible mix of the four types; never recommend the same type for every item unless the data truly \
calls for it.
- Recommend programme TYPES and topics, never a named vendor, product, course provider or person.
- "audience" names a broad group (e.g. "People managers", "New joiners", "All employees", \
"High-potential individual contributors") — never a team, project or anything that singles people out.
- Write for an HR leader: plain, specific, no jargon, no filler.

Reply with ONLY this JSON, no prose around it:
{{
  "overview": "<one or two sentences on the main development need across the organisation, max 45 words>",
  "recommendations": [
    {{
      "type": "coaching|mentoring|workshop|training",
      "title": "<programme name, max 8 words>",
      "why": "<one sentence linking it to what the data shows, max 35 words>",
      "what": "<one sentence on what the programme covers, max 35 words>",
      "audience": "<who should take it, max 6 words>",
      "format": "<delivery and duration, e.g. '6 x 1-hour sessions over 3 months', max 10 words>",
      "outcome": "<the change HR should look for afterwards, max 20 words>",
      "priority": "high|medium|low",
      "addresses": ["<exact dimension title or theme name>"]
    }}
  ]
}}"""


def _inputs(org_slug: str):
    """(sentiment dims, themes, based_on) from what's stored for the org."""
    sentiment = get_org_sentiment_data(org_slug) or {}
    dims = []
    for key in _SENTIMENT_DIMS:
        entry = sentiment.get(key)
        if not isinstance(entry, dict) or entry.get('score') is None:
            continue
        dims.append({
            'key': key,
            'title': _SENTIMENT_TITLES.get(key, key),
            'score': entry['score'],
            'insight': entry.get('insight') or '',
            'patterns': entry.get('patterns') or [],
            'scale': _SENTIMENT_SCALES.get(key, ('', '')),
        })
    cloud = get_latest_theme_cloud(org_slug) or {}
    themes = (cloud.get('themes') or [])[:_MAX_THEMES]
    based_on = f"s:{sentiment.get('calculated_at') or ''}|t:{cloud.get('calculated_at') or ''}"
    return dims, themes, based_on


def _band(score: int) -> str:
    return 'needs attention' if score < 35 else 'developing' if score < 65 else 'strong'


def _sentiment_block(dims: list) -> str:
    if not dims:
        return '(no sentiment scores yet)'
    lines = []
    for d in sorted(dims, key=lambda d: d['score']):
        low, high = d['scale']
        line = f"- {d['title']}: {d['score']}/100 ({_band(d['score'])}; 0 = {low}, 100 = {high})"
        if d['insight']:
            line += f"\n  Insight: {d['insight']}"
        for p in d['patterns'][:2]:
            line += f"\n  Pattern: {p}"
        lines.append(line)
    return '\n'.join(lines)


def _themes_block(themes: list) -> str:
    if not themes:
        return '(no shared themes yet)'
    return '\n'.join(f"- {t.get('text')}: {t.get('people', 0)} people, {t.get('weight', 0)} messages"
                     for t in themes if t.get('text'))


def _clean(result: dict, dims: list, themes: list) -> dict | None:
    """Keep only well-formed items whose `addresses` name a real input."""
    known = {d['title'].lower(): ('dimension', d['title'], d['score']) for d in dims}
    for t in themes:
        if t.get('text'):
            known.setdefault(t['text'].lower(), ('theme', t['text'], None))

    recs = []
    for it in (result.get('recommendations') or []):
        if not isinstance(it, dict) or not str(it.get('title') or '').strip():
            continue
        rtype = str(it.get('type') or '').strip().lower()
        if rtype not in REC_TYPES:
            continue
        addresses, seen = [], set()
        for a in it.get('addresses') or []:
            hit = known.get(str(a or '').strip().lower())
            if hit and hit[1] not in seen:
                seen.add(hit[1])
                kind, label, score = hit
                addresses.append({'kind': kind, 'label': label, **({'score': score} if score is not None else {})})
        if not addresses:
            continue   # not grounded in this org's data
        priority = str(it.get('priority') or '').strip().lower()
        recs.append({
            'type': rtype,
            'title': str(it['title']).strip()[:90],
            'why': str(it.get('why') or '')[:300],
            'what': str(it.get('what') or '')[:300],
            'audience': str(it.get('audience') or '')[:60],
            'format': str(it.get('format') or '')[:90],
            'outcome': str(it.get('outcome') or '')[:200],
            'priority': priority if priority in REC_PRIORITIES else 'medium',
            'addresses': addresses,
        })
        if len(recs) >= _MAX_RECS:
            break
    if not recs:
        return None
    order = {p: i for i, p in enumerate(REC_PRIORITIES)}
    recs.sort(key=lambda r: order[r['priority']])
    return {'overview': str(result.get('overview') or '')[:360], 'recommendations': recs}


def generate_org_recommendations(org_slug: str) -> dict | None:
    """Builds, caches and returns the report. None when there is nothing to
    build from yet or the model call fails."""
    from sentiment_job import _build_client, _call_llm, _extract_json

    dims, themes, based_on = _inputs(org_slug)
    if not dims and not themes:
        return None
    ai_provider = os.environ.get('AI_PROVIDER', 'claude').lower()
    api_key = os.environ.get('CLAUDE_API_KEY' if ai_provider == 'claude' else 'OPENAI_API_KEY')
    if not api_key:
        print("Org recommendations: no API key configured.")
        return None
    ai_model = os.environ.get('AI_MODEL', 'claude-sonnet-4-6' if ai_provider == 'claude' else 'gpt-4o')

    prompt = _PROMPT.format(sentiment=_sentiment_block(dims), themes=_themes_block(themes),
                            count=f"4-{_MAX_RECS}")
    raw = None
    try:
        raw = _call_llm(prompt, _build_client(ai_provider, api_key), ai_provider, ai_model,
                        max_tokens=3000)
        report = _clean(_extract_json(raw), dims, themes)
    except Exception as e:
        print(f"Org recommendations failed for {org_slug}: {e}")
        if raw:
            print(f"  Raw snippet: {raw[:300]}")
        return None
    if not report:
        return None
    upsert_org_recommendations(org_slug, report, based_on)
    return get_org_recommendations(org_slug)


def get_or_build(org_slug: str, force: bool = False) -> dict | None:
    """The cached report, rebuilt first when forced or when the sentiment run
    or theme cloud behind it has since been replaced. A failed rebuild falls
    back to the older report rather than nothing."""
    cached = get_org_recommendations(org_slug)
    if cached and not force:
        _, _, based_on = _inputs(org_slug)
        if cached.get('based_on') == based_on:
            return cached
    return generate_org_recommendations(org_slug) or cached


if __name__ == '__main__':
    import json
    import sys
    for slug in sys.argv[1:]:
        print(json.dumps(get_or_build(slug, force=True), indent=2))
