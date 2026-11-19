"""
The best-fit coach for one coachee, picked by the AI from their organisation's
coach list (the Coaches page, /api/coaches/match).

What the model is told about the coachee, most specific first:

  - their stored profile (role, level, functional areas, industry),
  - their LinkedIn summary, when it has been scraped,
  - the topics they keep returning to in Nexa (the recurring-themes report),
    or, before that report exists, a short sample of their latest messages.

Coaches are described by name, current role and company, plus a trimmed
summary of their aboutMyself and linkedinJson (see coach_background) — never
their email. Coaches without a role and company are left out of the prompt.
One LLM call per build. The whole page result (match + the coaches to show)
is saved per coachee in `coach_matches` and served from there — no coach-service
or model call — until it is a week old (a day when there was no match), the
user's language changes, or they press Refresh. An automatic rebuild skips the
model when nothing it reads has changed (`based_on`). A coachee with no context
at all gets no match rather than a guess.
"""
import hashlib
import json
import os
from datetime import datetime, timedelta

from database import (
    get_user_profile_context, get_user_linkedin, get_user_insight_report,
    get_user_dated_messages, get_user_language,
    get_coach_match, upsert_coach_match,
)

_MAX_THEMES = 8
_MSG_SAMPLE = 30          # latest messages used when there is no themes report
_MSG_MAX_CHARS = 3000
_LINKEDIN_MAX_CHARS = 1500
_REASON_MAX = 320
_FOCUS_MAX = 3
_RANK_MAX = 8            # the page shows at most 10 coaches, in this order
_PROMPT_VERSION = 3      # bump to rebuild cached matches after a prompt change

_PROMPT = """You help an employee choose a coach from the coaches their company offers.

ABOUT THE EMPLOYEE
{context}

AVAILABLE COACHES (each starts with [id] name — role at company, then their own background where known)
{coaches}

Pick the ONE coach whose background best fits what this employee is working on: their role and level, \
their function and industry, and above all the challenges and goals they keep bringing up. Prefer a coach \
whose own experience (career history, what they say they coach on, their skills) speaks to those challenges.

Then write the reason for the employee, addressed to them as "you", in {language}. Keep it to one or two \
warm, specific sentences linking what they are working on to the coach's background. Do not quote their \
messages, mention scores, or bring up anything sensitive (health, family, conflicts with named people). \
Only state facts about the coach that appear in their entry above.

Also give up to {focus_max} short focus areas (2-4 words each, in {language}) the coaching could cover.

Finally, rank up to {rank_max} coaches from the list by how well they fit this employee, best first \
(starting with the coach you picked).

Answer with JSON only:
{{"coach_id": "<id from the list>", "reason": "...", "focus": ["...", "..."], "ranked": ["<id>", "<id>", "..."]}}"""


def _profile_lines(ctx: dict) -> list:
    lines = []
    if ctx.get('level_name'):
        lines.append(f"Experience level: {ctx['level_name']}")
    for key, label in (('functional_areas', 'Functional background'), ('industry_types', 'Industry')):
        val = ctx.get(key)
        if isinstance(val, list) and val:
            lines.append(f"{label}: {', '.join(str(v) for v in val)}")
    if ctx.get('country'):
        lines.append(f"Country: {ctx['country']}")
    return lines


def _context(user_id: str) -> tuple[str, list]:
    """(prompt text about the coachee, fingerprint parts). Empty text when
    there is nothing to match on."""
    from linkedin import profile_summary

    sections, parts = [], []

    ctx = get_user_profile_context(user_id) or {}
    prof = _profile_lines(ctx)
    if prof:
        sections.append('Profile:\n' + '\n'.join(f'- {l}' for l in prof))
        parts.append(prof)

    li = (get_user_linkedin(user_id) or {}).get('profile') or {}
    li_text = profile_summary(li)[:_LINKEDIN_MAX_CHARS] if li else ''
    if li_text:
        sections.append('LinkedIn:\n' + li_text)
        parts.append(hashlib.sha1(li_text.encode()).hexdigest())

    themes_report = get_user_insight_report(user_id, 'recurring_themes') or {}
    themes = [t for t in (themes_report.get('themes') or []) if isinstance(t, dict) and t.get('label')]
    if themes:
        lines = [f"- {t['label']}: {t.get('summary', '')}".rstrip(' :') for t in themes[:_MAX_THEMES]]
        sections.append('Topics they keep raising with their AI coach:\n' + '\n'.join(lines))
        parts.append(themes_report.get('calculated_at'))
    else:
        msgs = get_user_dated_messages(user_id, limit=_MSG_SAMPLE)
        if msgs:
            text, used = '', []
            for m in reversed(msgs):  # newest first until the budget runs out
                line = '- ' + ' '.join(str(m['content']).split())[:300]
                if len(text) + len(line) > _MSG_MAX_CHARS:
                    break
                used.append(line)
                text += line
            sections.append('What they have recently asked their AI coach about (latest first):\n'
                            + '\n'.join(used))
            # Re-match at most once a day while it runs on raw messages.
            parts.append(msgs[-1]['date'])

    return '\n\n'.join(sections), parts


_COACH_ABOUT_MAX = 400       # per coach, from aboutMyself
_COACH_LINKEDIN_MAX = 600    # per coach, from linkedinJson


def _as_profile(value) -> dict:
    """linkedinJson as a dict, whether it came as an object, a JSON string, a
    one-item list or wrapped in {data: ...}. {} when it holds nothing usable."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return {}
    if isinstance(value, list):
        value = next((v for v in value if isinstance(v, dict)), {})
    if not isinstance(value, dict):
        return {}
    inner = value.get('data')
    if isinstance(inner, dict) and inner and not any(
            k in value for k in ('headline', 'experience', 'about', 'summary')):
        value = inner
    return value


def coach_background(row: dict) -> tuple[str, str]:
    """(about, linkedin) for one coach row of the corporate-coaches API, each
    trimmed to a short prompt-sized summary. Used only for matching — never
    sent to the browser."""
    about = ' '.join(str(row.get('aboutMyself') or '').split())[:_COACH_ABOUT_MAX]
    linkedin = ''
    profile = _as_profile(row.get('linkedinJson'))
    if profile:
        from linkedin import profile_summary
        try:
            text = profile_summary(profile, max_experience=4, max_education=1)
        except Exception:
            text = ''
        # The name is already on the coach's line; drop it here.
        lines = [l for l in text.splitlines() if not l.startswith('- Name:')]
        linkedin = '\n'.join(lines)[:_COACH_LINKEDIN_MAX].strip()
    return about, linkedin


def _has_profile(coach: dict) -> bool:
    """Whether a coach has enough filled in to be matched on. A coach with no
    role or company gives the model nothing but a name, so they are left out
    of the prompt (and its token cost); the page still lists them."""
    return bool((coach.get('role') or '').strip() and (coach.get('company') or '').strip())


def _coaches_block(coaches: list) -> str:
    blocks = []
    for c in coaches:
        lines = [f"[{c['id']}] {c['name']} — {c['role']} at {c['company']}"]
        if c.get('about'):
            lines.append(f"  About (in their words): {c['about']}")
        if c.get('linkedin'):
            lines.append('  LinkedIn:\n' + '\n'.join('    ' + l for l in c['linkedin'].splitlines()))
        blocks.append('\n'.join(lines))
    return '\n\n'.join(blocks)


def _fingerprint(coaches: list, parts: list, language: str) -> str:
    key = json.dumps({
        'coaches': sorted(
            [c['id'], c.get('role', ''), c.get('company', ''),
             hashlib.sha1((c.get('about', '') + '\0' + c.get('linkedin', '')).encode()).hexdigest()]
            for c in coaches),
        'ctx': parts,
        'lang': language,
        'v': _PROMPT_VERSION,
    }, sort_keys=True, default=str)
    return hashlib.sha256(key.encode()).hexdigest()


def _clean(result: dict, coaches: list) -> dict | None:
    ids = {c['id'] for c in coaches}
    coach_id = str(result.get('coach_id') or '').strip()
    if coach_id not in ids:
        return None
    reason = ' '.join(str(result.get('reason') or '').split())[:_REASON_MAX]
    focus = [' '.join(str(f).split())[:40] for f in (result.get('focus') or []) if str(f).strip()]
    # Only real ids, once each, with the pick always first.
    ranked = [coach_id]
    for rid in result.get('ranked') or []:
        rid = str(rid).strip()
        if rid in ids and rid not in ranked:
            ranked.append(rid)
    return {'coach_id': coach_id, 'reason': reason, 'focus': focus[:_FOCUS_MAX],
            'ranked': ranked[:_RANK_MAX]}


# Coach fields the page shows — email and LinkedIn stay server-side.
PUBLIC_FIELDS = ('id', 'name', 'role', 'company', 'photo', 'about')
_MAX_SHOWN = 10                    # coaches on the page, best match included
_CACHE_TTL = timedelta(days=7)     # a saved result is rebuilt after this...
_CACHE_TTL_NO_MATCH = timedelta(days=1)  # ...or sooner when it found no match


def _ai_match(user_id: str, coaches: list, language: str, context: str) -> tuple[dict | None, bool]:
    """(match, failed). match is None with failed=False when there is nothing
    to match on; failed=True when the model call itself went wrong."""
    if not coaches or not context:
        return None, False
    from sentiment_job import _build_client, _call_llm, _extract_json
    ai_provider = os.environ.get('AI_PROVIDER', 'claude').lower()
    api_key = os.environ.get('CLAUDE_API_KEY' if ai_provider == 'claude' else 'OPENAI_API_KEY')
    if not api_key:
        print("Coach match: no API key configured.")
        return None, True
    ai_model = os.environ.get('AI_MODEL', 'claude-sonnet-4-6' if ai_provider == 'claude' else 'gpt-4o')
    prompt = _PROMPT.format(context=context, coaches=_coaches_block(coaches),
                            language=language, focus_max=_FOCUS_MAX, rank_max=_RANK_MAX)
    raw = None
    try:
        raw = _call_llm(prompt, _build_client(ai_provider, api_key), ai_provider, ai_model,
                        max_tokens=1000)
        match = _clean(_extract_json(raw), coaches)
    except Exception as e:
        print(f"Coach match failed for {user_id}: {e}")
        if raw:
            print(f"  Raw snippet: {raw[:300]}")
        return None, True
    return match, match is None   # an answer naming no real coach counts as a failure


def _shown(coaches: list, match: dict | None) -> list:
    """The coaches the page shows: the AI's ranking first, then the rest
    alphabetically, at most _MAX_SHOWN, with public fields only."""
    by_id = {c['id']: c for c in coaches}
    ranked = [by_id[i] for i in (match or {}).get('ranked', []) if i in by_id]
    rest = [c for c in coaches if c not in ranked]
    return [{k: c.get(k, '') for k in PUBLIC_FIELDS} for c in (ranked + rest)[:_MAX_SHOWN]]


def _result(data: dict, calculated_at, **extra) -> dict:
    return {'match': data.get('match'), 'coaches': data.get('coaches') or [],
            'calculated_at': str(calculated_at) if calculated_at else None, **extra}


def get_or_build(user_id: str, fetch_coaches, force: bool = False) -> dict:
    """{match, coaches, calculated_at[, refresh_failed]} for the Coaches page.

    The saved result is returned as is — no coach-service call, no model call —
    unless it is missing, older than its TTL, in another language, or `force`
    (the Refresh button) is set. A rebuild calls fetch_coaches() for the
    org's current list; the model is skipped on an automatic rebuild when
    neither the matchable coaches nor the coachee's context have changed.
    Raises when there is no saved result and the coach list can't be fetched.
    """
    from languages import prompt_name
    language = prompt_name(get_user_language(user_id))
    cached = get_coach_match(user_id)
    data = cached['data'] if cached and isinstance(cached.get('data'), dict) \
        and 'coaches' in cached['data'] else None

    if data and not force and data.get('language') == language:
        try:
            age = datetime.now() - datetime.fromisoformat(str(cached['calculated_at']))
        except (TypeError, ValueError):
            age = _CACHE_TTL
        if age < (_CACHE_TTL if data.get('match') else _CACHE_TTL_NO_MATCH):
            return _result(data, cached['calculated_at'])

    try:
        coaches = fetch_coaches()
    except Exception:
        if data:   # keep the page working on the last saved result
            return _result(data, cached['calculated_at'], refresh_failed=True)
        raise

    matchable = [c for c in coaches if _has_profile(c)]
    context, parts = _context(user_id)
    based_on = _fingerprint(matchable, parts, language)

    if data and not force and cached['based_on'] == based_on and data.get('language') == language:
        match = data.get('match')   # nothing the model reads has changed
    else:
        match, failed = _ai_match(user_id, matchable, language, context)
        if failed and data:
            return _result(data, cached['calculated_at'], refresh_failed=True)

    new = {'match': match, 'coaches': _shown(coaches, match), 'language': language}
    upsert_coach_match(user_id, based_on, new)
    return _result(new, datetime.now().replace(microsecond=0))
