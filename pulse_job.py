"""Daily Pulse aggregation.

Turns a pool of anonymous notes into the only two things an admin ever sees of
them: a word cloud and a written summary. Both are built here, server-side, from
`get_pulse_note_texts` — the note strings themselves never leave this module or
app.py's aggregate endpoint.

Two safeguards do the anonymising work:

  * the k-anonymity floor (`PULSE_MIN_RESPONSES`) — below it the caller shows
    nothing at all, not a smaller word cloud;
  * the document-frequency floor (`_MIN_WORD_DOCS`) — a word only reaches the
    cloud if it appears in several different notes, so a phrase unique to one
    person cannot surface as a tag.
"""

import json
import os
import re
from collections import Counter

from database import get_pulse_note_texts, upsert_pulse_insight

# Below this many responses in the window, an org's pulse stays sealed. Shared
# with app.py, which gates the whole aggregate response on it.
PULSE_MIN_RESPONSES = int(os.environ.get('PULSE_MIN_RESPONSES', '5'))

# A word must appear in at least this many *different* notes to be a theme
# rather than one person's turn of phrase.
_MIN_WORD_DOCS = 3
_MAX_CLOUD_WORDS = 60
_MIN_WORD_LEN = 3

# Enough notes to write a summary from. Lower than PULSE_MIN_RESPONSES would be
# pointless — the caller has already refused below that.
_MIN_INSIGHT_NOTES = 5
_INSIGHT_MAX_CHARS = 60000

_STOPWORDS = set("""
a about above after again against all am an and any are aren't as at be because been
before being below between both but by can cannot could couldn't did didn't do does
doesn't doing don't down during each few for from further had hadn't has hasn't have
haven't having he her here hers herself him himself his how i i'm i've if in into is
isn't it it's its itself just let's me more most mustn't my myself no nor not of off on
once only or other ought our ours ourselves out over own same shan't she should
shouldn't so some such than that the their theirs them themselves then there these they
this those through to too under until up very was wasn't we were weren't what when
where which while who whom why with won't would wouldn't you your yours yourself
yourselves
also get got getting really quite lot lots much many thing things stuff bit way ways
today day days week weeks month feel feels feeling felt like liked im ive dont cant
been will can may might one two three still even yet ok okay
""".split())


def _tokenise(text: str) -> list:
    """Words worth counting: letters and internal apostrophes, lowercased.

    Numbers, emails and @handles are dropped outright — they identify far more
    readily than they inform.
    """
    text = re.sub(r'\S+@\S+', ' ', text)
    words = re.findall(r"[a-z][a-z']{%d,}" % (_MIN_WORD_LEN - 1), text.lower())
    return [w.strip("'") for w in words if w.strip("'") not in _STOPWORDS]


def build_word_cloud(notes: list) -> list:
    """[{text, weight, notes}] for the notes given, rarest words already gone.

    `weight` is total mentions and drives the type size; `notes` is how many
    different people's notes the word came from, which is what the floor is
    applied to.
    """
    total = Counter()
    docs = Counter()
    for note in notes:
        tokens = _tokenise(note or '')
        if not tokens:
            continue
        total.update(tokens)
        docs.update(set(tokens))

    cloud = [{'text': word, 'weight': count, 'notes': docs[word]}
             for word, count in total.items() if docs[word] >= _MIN_WORD_DOCS]
    cloud.sort(key=lambda w: (-w['weight'], w['text']))
    return cloud[:_MAX_CLOUD_WORDS]


_PULSE_PROMPT = """You are summarising anonymous daily pulse-check notes from employees at one organisation.

These notes were given on the explicit promise that no individual is ever identifiable from what you write. That promise governs everything below.

Rules — every one of them is mandatory:
- NEVER quote or paraphrase any single note. Write only about patterns.
- NEVER include a name, a role, a team, a project name, a location, or any other detail that could point at one person.
- Only report a theme if it appears across at least THREE distinct notes. Drop anything rarer, however striking it is.
- Write about "some people", "a recurring thread", "several notes" — never "one person said".
- If the notes are too thin or too scattered to support any theme, return an empty themes list rather than inventing one.

Reply with ONLY this JSON, no prose around it:
{
  "mood_summary": "<2-3 sentences on the overall mood across the whole set, addressed to a leader>",
  "themes": [
    {
      "title": "<3-5 words>",
      "summary": "<one or two sentences on the pattern, aggregate language only>",
      "sentiment": "positive" | "negative" | "mixed",
      "prevalence": "widespread" | "common" | "emerging"
    }
  ],
  "watch_outs": ["<a risk the pattern suggests, max 18 words>"],
  "recommendations": ["<a concrete action for leadership, max 18 words>"]
}

Give at most 5 themes, 3 watch-outs and 3 recommendations.

The notes:
---
{notes}
---"""


def analyze_org_pulse(org_slug: str, days: int = 30) -> dict | None:
    """Write (and cache) the anonymised pulse summary for one org.

    Returns None when there is too little to read or the model call fails — the
    caller shows the word cloud on its own in that case rather than an error.
    """
    from sentiment_job import _build_client, _call_llm, _extract_json

    notes = get_pulse_note_texts(org_slug, days=days)
    if len(notes) < _MIN_INSIGHT_NOTES:
        return None

    ai_provider = os.environ.get('AI_PROVIDER', 'claude').lower()
    api_key = os.environ.get('CLAUDE_API_KEY' if ai_provider == 'claude' else 'OPENAI_API_KEY')
    if not api_key:
        print("Pulse insight: no API key configured.")
        return None
    ai_model = os.environ.get('AI_MODEL', 'claude-sonnet-4-6' if ai_provider == 'claude' else 'gpt-4o')

    combined = '\n'.join(f'- {n.strip()}' for n in notes if (n or '').strip())
    if len(combined) > _INSIGHT_MAX_CHARS:
        combined = combined[:_INSIGHT_MAX_CHARS] + '\n[...truncated]'

    client = _build_client(ai_provider, api_key)
    try:
        raw = _call_llm(_PULSE_PROMPT.replace('{notes}', combined), client,
                        ai_provider, ai_model, max_tokens=1500)
        result = _extract_json(raw)
    except Exception as e:
        print(f"Pulse insight failed for {org_slug}: {e}")
        return None

    insight = {
        'mood_summary': (result.get('mood_summary') or '').strip(),
        'themes': [t for t in (result.get('themes') or []) if isinstance(t, dict)][:5],
        'watch_outs': [str(w) for w in (result.get('watch_outs') or [])][:3],
        'recommendations': [str(r) for r in (result.get('recommendations') or [])][:3],
    }
    upsert_pulse_insight(org_slug, days, insight, len(notes))
    return insight


if __name__ == '__main__':
    import sys
    from database import get_all_org_slugs, get_org_licence

    # Daily Pulse ships with Nexa Pro. A sweep over every org would spend AI
    # calls summarising notes nobody can read, so Regular orgs are skipped —
    # named explicitly on the command line too, since the licence is the gate.
    slugs = sys.argv[1:] or get_all_org_slugs()
    for slug in slugs:
        if get_org_licence(slug) != 'pro':
            print(f"Skipping {slug} — not on Nexa Pro.")
            continue
        print(f"Pulse insight for {slug}…")
        print(json.dumps(analyze_org_pulse(slug), indent=2))
