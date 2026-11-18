"""
The best-fit coach for one coachee, picked by the AI from their organisation's
coach list (the Coaches page, /api/coaches/match).

What the model is told about the coachee, most specific first:

  - their stored profile (role, level, functional areas, industry),
  - their LinkedIn summary, when it has been scraped,
  - the topics they keep returning to in Nexa (the recurring-themes report),
    or, before that report exists, a short sample of their latest messages.

Coaches are described by name, current role and company only (no emails).
One LLM call per build. The result is cached per coachee and rebuilt only when
the coach list or the coachee's context changes (`based_on`). A coachee with no
context at all gets no match rather than a guess.
"""
import hashlib
import json
import os

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
_RANK_MAX = 9            # the page shows at most 10 coaches, in this order
_PROMPT_VERSION = 2       # bump to rebuild cached matches after a prompt change

_PROMPT = """You help an employee choose a coach from the coaches their company offers.

ABOUT THE EMPLOYEE
{context}

AVAILABLE COACHES (id | name | current role | company)
{coaches}

Pick the ONE coach whose background best fits what this employee is working on: their role and level, \
their function and industry, and above all the challenges and goals they keep bringing up. Prefer a coach \
whose own experience speaks to those challenges.

Then write the reason for the employee, addressed to them as "you", in {language}. Keep it to one or two \
warm, specific sentences linking what they are working on to the coach's background. Do not quote their \
messages, mention scores, or bring up anything sensitive (health, family, conflicts with named people). \
Do not invent facts about the coach beyond their role and company.

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


def _has_profile(coach: dict) -> bool:
    """Whether a coach has enough filled in to be matched on. A coach with no
    role or company gives the model nothing but a name, so they are left out
    of the prompt (and its token cost); the page still lists them."""
    return bool((coach.get('role') or '').strip() and (coach.get('company') or '').strip())


def _coaches_block(coaches: list) -> str:
    return '\n'.join(
        f"{c['id']} | {c['name']} | {c['role']} | {c['company']}"
        for c in coaches
    )


def _fingerprint(coaches: list, parts: list, language: str) -> str:
    key = json.dumps({
        'coaches': sorted([c['id'], c.get('role', ''), c.get('company', '')] for c in coaches),
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


def get_or_build(user_id: str, coaches: list) -> dict | None:
    """{coach_id, reason, focus, ranked} for this coachee, or None when no coach
    has a role and company filled in, nothing is known about the coachee yet,
    or the model call fails. Only coaches with a filled-in profile are matched."""
    coaches = [c for c in coaches or [] if _has_profile(c)]
    if not user_id or not coaches:
        return None
    from languages import prompt_name
    language = prompt_name(get_user_language(user_id))
    context, parts = _context(user_id)
    based_on = _fingerprint(coaches, parts, language)

    cached = get_coach_match(user_id)
    if cached and cached['based_on'] == based_on:
        return cached['match']
    if not context:
        upsert_coach_match(user_id, based_on, None)
        return None

    from sentiment_job import _build_client, _call_llm, _extract_json
    ai_provider = os.environ.get('AI_PROVIDER', 'claude').lower()
    api_key = os.environ.get('CLAUDE_API_KEY' if ai_provider == 'claude' else 'OPENAI_API_KEY')
    if not api_key:
        print("Coach match: no API key configured.")
        return cached['match'] if cached else None
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
        # Keep showing the previous pick (if it is still on the list) over nothing.
        old = cached['match'] if cached else None
        return old if old and old.get('coach_id') in {c['id'] for c in coaches} else None
    if match:
        upsert_coach_match(user_id, based_on, match)
    return match
