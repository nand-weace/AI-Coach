import os
import re
import time
import uuid
import logging
import threading
import requests as http_requests
from functools import wraps
from flask import Flask, request, jsonify, render_template, session, redirect, g
from dotenv import load_dotenv
from ai_coach import SYSTEM_INSTRUCTION
from prompt import COACHING_MODE, MENTORING_MODE
from database import (
    init_db,
    create_chat_session,
    save_message,
    has_previous_sessions,
    is_resumable_session,
    get_session_highlights,
    get_user_history,
    get_user_history_before,
    get_recent_user_messages,
    set_message_feedback,
    get_message_feedback,
    get_org_custom_content,
    upsert_org_custom_content,
    get_org_analytics,
    get_org_filter_options,
    upsert_org_sentiment,
    get_org_sentiment_data,
    get_user_sentiment,
    get_user_sentiment_data,
    get_user_insight_report,
    get_user_stats,
    upsert_user_login,
    get_user_access_settings,
    get_user_profile_context,
    get_user_identity,
    get_all_users_admin,
    get_all_orgs,
    get_user_language,
    set_user_language,
    get_user_mode,
    set_user_mode,
    get_user_additional_details,
    set_user_additional_details,
    get_user_linkedin,
    set_user_linkedin_url,
    save_user_linkedin_profile,
)
import linkedin

# Languages Nexa can coach in, as code -> (display label, name used in the prompt).
# Keep in sync with the picker in static/script.js.
SUPPORTED_LANGUAGES = {
    'English': ('English', 'English'),
    'Hindi': ('हिन्दी (Hindi)', 'Hindi'),
    'Marathi': ('मराठी (Marathi)', 'Marathi'),
    'Bengali': ('বাংলা (Bengali)', 'Bengali'),
    'Tamil': ('தமிழ் (Tamil)', 'Tamil'),
    'Telugu': ('తెలుగు (Telugu)', 'Telugu'),
    'Kannada': ('ಕನ್ನಡ (Kannada)', 'Kannada'),
    'Malayalam': ('മലയാളം (Malayalam)', 'Malayalam'),
    'Gujarati': ('ગુજરાતી (Gujarati)', 'Gujarati'),
    'Spanish': ('Español (Spanish)', 'Spanish'),
    'French': ('Français (French)', 'French'),
    'German': ('Deutsch (German)', 'German'),
    'Portuguese': ('Português (Portuguese)', 'Portuguese'),
    'Arabic': ('العربية (Arabic)', 'Arabic'),
    'Chinese': ('中文 (Chinese)', 'Chinese (Simplified)'),
    'Japanese': ('日本語 (Japanese)', 'Japanese'),
}
DEFAULT_LANGUAGE = 'English'

# How Nexa shows up: 'coaching' draws the answer out of the user, 'mentoring'
# gives direct advice from experience. Toggled from the chat window; the prompt
# text for each lives in prompt.py. Keep in sync with the toggle in index.html.
MODES = {
    'coaching': {'label': 'Coaching', 'directive': COACHING_MODE},
    'mentoring': {'label': 'Mentoring', 'directive': MENTORING_MODE},
}
DEFAULT_MODE = 'coaching'


def _elapsed(since: float) -> str:
    """Human-readable duration since a time.perf_counter() mark, for logging."""
    return f"{(time.perf_counter() - since) * 1000:.0f}ms"


def _language_list() -> list[dict]:
    """Language options for the API — code plus the label shown in the picker."""
    return [{'code': code, 'label': label} for code, (label, _) in SUPPORTED_LANGUAGES.items()]


def _serialize_messages(user_id: str, rows: list) -> list[dict]:
    """Shape history rows for the client, including any thumbs the user already
    left on Nexa's replies so restored messages render in the rated state."""
    ratings = get_message_feedback(
        user_id, [r['id'] for r in rows if r['role'] == 'assistant']
    )
    return [
        {'id': r['id'], 'role': r['role'], 'content': r['content'],
         'language': r.get('language') or DEFAULT_LANGUAGE,
         'created_at': r['created_at'].isoformat() if r.get('created_at') else None,
         'feedback': ratings.get(r['id'], 0)}
        for r in rows
    ]


def _language_directive(language: str | None) -> str:
    """System-prompt addendum instructing Nexa to reply in the chosen language."""
    if not language or language == DEFAULT_LANGUAGE:
        return ''
    label = SUPPORTED_LANGUAGES.get(language, (language, language))[1]
    return (
        f"\n\n---\nLANGUAGE:\n"
        f"Write every reply in {label}, including the suggested follow-ups inside the "
        f"[[SUGGESTIONS]] block. Keep the same warm, informal coaching tone in {label}. "
        f"Do not translate or alter the [[SUGGESTIONS]] tags themselves. "
        f"If the user writes in another language, still reply in {label} unless they ask "
        f"you to switch.\n---"
    )


def _normalize_mode(mode: str | None) -> str:
    """Coerce whatever the client sent into a supported mode."""
    mode = (mode or '').strip().lower()
    return mode if mode in MODES else DEFAULT_MODE


def _mode_directive(mode: str | None) -> str:
    """System-prompt addendum setting Nexa to coach or mentor."""
    return f"\n\n---{MODES[_normalize_mode(mode)]['directive'].rstrip()}\n---"


# Short-lived cache of per-org custom content so /chat doesn't hit the DB on every
# message. Corporate admins editing content see it reflected within the TTL window.
_ORG_CONTENT_TTL = 60  # seconds
_org_content_cache: dict = {}  # org_slug -> (expires_at, content)


def _get_org_content_cached(org_slug: str | None) -> str | None:
    if not org_slug:
        return None
    now = time.time()
    cached = _org_content_cache.get(org_slug)
    if cached and cached[0] > now:
        return cached[1]
    try:
        content = get_org_custom_content(org_slug)
    except Exception as e:
        logger.error("[org_content] fetch failed for org=%s: %s", org_slug, e)
        return cached[1] if cached else None
    _org_content_cache[org_slug] = (now + _ORG_CONTENT_TTL, content)
    return content


def _org_content_directive(org_slug: str | None) -> str:
    """System-prompt addendum carrying the organisation's own reference material."""
    content = _get_org_content_cached(org_slug)
    if not content or not content.strip():
        return ''
    return (
        f"\n\n---\nORGANISATION KNOWLEDGE:\n"
        f"The following is organisation-specific information, policies, and context "
        f"provided by this user's company. Draw on it when it's relevant to the user's "
        f"question — reflect their company's policies, programmes, and terminology. "
        f"If it isn't relevant to what they're asking, ignore it. Never dump this "
        f"content verbatim or announce that you were given it; weave it in naturally.\n\n"
        f"{content.strip()}\n---"
    )

# Same idea for the per-user notes an admin maintains: cached briefly so /chat
# stays a single AI call, and edits land within the TTL window.
_USER_DETAILS_TTL = 60  # seconds
_user_details_cache: dict = {}  # user_id -> (expires_at, details)


def _get_user_details_cached(user_id: str | None) -> str | None:
    if not user_id:
        return None
    now = time.time()
    cached = _user_details_cache.get(user_id)
    if cached and cached[0] > now:
        return cached[1]
    try:
        details = get_user_additional_details(user_id)
    except Exception as e:
        logger.error("[user_details] fetch failed for user_id=%s: %s", user_id, e)
        return cached[1] if cached else None
    _user_details_cache[user_id] = (now + _USER_DETAILS_TTL, details)
    return details


def _user_details_directive(user_id: str | None) -> str:
    """System-prompt addendum carrying the admin-authored notes about this user.

    Read from the database rather than the client-supplied profile context, so the
    text can only come from an admin.
    """
    details = _get_user_details_cached(user_id)
    if not details or not details.strip():
        return ''
    return (
        f"\n\n---\nADDITIONAL DETAILS ABOUT THIS USER:\n"
        f"Background on this user recorded by their organisation. Treat it as true "
        f"and let it shape how you coach them — their situation, strengths, goals, "
        f"and constraints. Never read it back verbatim or mention that you were given "
        f"it; weave it in naturally.\n\n"
        f"{details.strip()}\n---"
    )


load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'dev-secret-change-in-prod')


@app.template_global()
def asset(filename: str) -> str:
    """A /static URL stamped with the file's mtime.

    Flask serves static files with a 12-hour max-age, so without this a deployed
    JS or CSS fix keeps missing anyone whose browser already has the old copy —
    a hard refresh is not something users know to do. The stamp changes when the
    file does, which is exactly when the cache should be bypassed.
    """
    path = os.path.join(app.static_folder, filename)
    try:
        return f"/static/{filename}?v={int(os.path.getmtime(path))}"
    except OSError:
        return f"/static/{filename}"

WEACE_API_URL = os.environ.get("WEACE_API_URL", "https://api.we-ace.com")

AI_PROVIDER = os.environ.get("AI_PROVIDER", "claude").lower()
DEFAULT_MODELS = {"openai": "gpt-4o", "claude": "claude-sonnet-4-6"}
AI_MODEL = os.environ.get("AI_MODEL", DEFAULT_MODELS.get(AI_PROVIDER, "claude-sonnet-4-6"))

api_key = os.environ.get("CLAUDE_API_KEY" if AI_PROVIDER == "claude" else "OPENAI_API_KEY")
client = None

if api_key:
    try:
        if AI_PROVIDER == "claude":
            from anthropic import Anthropic
            client = Anthropic(api_key=api_key)
        else:
            from openai import OpenAI
            client = OpenAI(api_key=api_key)
        print(f"AI Provider: {AI_PROVIDER.upper()} | Model: {AI_MODEL}")
    except Exception as e:
        print(f"Failed to initialize AI client: {e}")
else:
    print(f"Warning: API key not set for provider '{AI_PROVIDER}'.")

# Claude has no browsing of its own. Web access comes from these Anthropic-hosted
# server tools — declaring them is what lets Nexa answer from current information
# instead of stopping at its training cutoff. They execute on Anthropic's side, so
# there's no tool loop to run here. Requires Sonnet 4.6+ / Opus 4.6+.
WEB_TOOLS = [
    {"type": "web_search_20260209", "name": "web_search"},
    {"type": "web_fetch_20260209", "name": "web_fetch"},
]

# A search-heavy turn can exhaust the server-side tool loop and come back paused
# rather than finished. Re-sending the conversation with the paused turn appended
# picks it back up; the cap stops a pathological turn from looping forever.
MAX_PAUSE_RESUMES = 3


def _reply_text(response) -> str:
    """Pull the assistant's prose out of a response.

    With tools enabled, content interleaves server_tool_use and tool-result
    blocks, so the first block is often not text — and the reply can arrive as
    several text blocks either side of a search.
    """
    parts = [b.text.strip() for b in response.content
             if b.type == "text" and b.text.strip()]
    return "\n\n".join(parts)


def _claude_reply(system_content: str, messages: list, max_tokens: int) -> str:
    """Call Claude with web search available, resuming if the turn pauses."""
    convo = list(messages)
    response = None
    for _ in range(MAX_PAUSE_RESUMES + 1):
        response = client.messages.create(
            model=AI_MODEL,
            max_tokens=max_tokens,
            system=system_content,
            messages=convo,
            tools=WEB_TOOLS,
        )
        if response.stop_reason != "pause_turn":
            break
        convo.append({"role": "assistant", "content": response.content})
    return _reply_text(response)


# Cached system prompt per session_uuid, as a one-element history list. The
# conversation turns themselves are read from the DB per request — see
# _get_or_rebuild_history.
sessions_cache: dict[str, list] = {}

# How many past messages a /chat turn replays, counted across all of the user's
# sessions rather than just the current one. Sonnet's window is far larger than
# this; the cap is here so a user with years of history doesn't make every turn
# slow and expensive.
MAX_CONTEXT_MESSAGES = 400

# In-memory user state keyed by weace access_token
auth_tokens: dict[str, dict] = {}


def _get_bearer_token() -> str | None:
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        return auth[7:].strip() or None
    return None


def _has_role(role_val, *slugs) -> bool:
    """Check whether role_val (array of role objects or legacy string) contains any of the given slugs."""
    if isinstance(role_val, list):
        return any(r.get('slug', '') in slugs for r in role_val if isinstance(r, dict))
    return role_val in slugs


def require_weace_token(f):
    """
    Decorator that reads the Bearer token from the Authorization header,
    verifies it against the we-ace verify-token API, then sets:
      g.access_token — the verified token
      g.token_data   — parsed JSON from the verify-token response
      g.user         — cached user data from auth_tokens (None until /session is called)
    Returns 401 if the token is missing or invalid, 502 if unreachable.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        token = _get_bearer_token()
        if not token:
            return jsonify({'error': 'Authorization: Bearer token required'}), 401
        try:
            resp = http_requests.get(
                f'{WEACE_API_URL}/api/v1/users/profile',
                headers={'accept': '*/*', 'Authorization': f'Bearer {token}'},
                timeout=10,
            )
            if not resp.ok:
                logger.warning("[auth] token rejected by verify-token API: status=%s", resp.status_code)
                return jsonify({'error': 'Invalid or expired token'}), 401
        except Exception as e:
            logger.error("[auth] verify-token API unreachable: %s", e)
            return jsonify({'error': 'Token verification service unavailable'}), 502
        g.access_token = token
        try:
            g.token_data = resp.json()
        except Exception:
            g.token_data = {}
        user_data = g.token_data.get('data') or {}
        raw_roles = user_data.get('roles') or []
        roles_arr = raw_roles if isinstance(raw_roles, list) else [raw_roles]
        g.user = {
            'user_id': user_data.get('_id'),
            'user_name': user_data.get('username'),
            'email': user_data.get('email'),
            'role': roles_arr,
            'first_name': user_data.get('firstName'),
            'last_name': user_data.get('lastName'),
            'org_id': user_data.get('parentId'),
        }
        return f(*args, **kwargs)
    return decorated


try:
    init_db()
    print("Database initialized.")
except Exception as e:
    print(f"Database initialization failed: {e}")


def _start_scheduler():
    from apscheduler.schedulers.background import BackgroundScheduler
    import atexit
    from sentiment_job import run_sentiment_job
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(run_sentiment_job, 'cron', hour=2, minute=0, id='sentiment_daily')
    # Sweeps up users who have a LinkedIn URL but no scraped profile — see
    # linkedin_job.py. Offset off the hour so it never starts alongside the
    # sentiment run.
    scheduler.add_job(_run_linkedin_backfill, 'cron', hour='*/4', minute=15,
                      id='linkedin_backfill', max_instances=1, coalesce=True)
    scheduler.start()
    atexit.register(lambda: scheduler.shutdown())
    print("Scheduler started: sentiment at 02:00 UTC, LinkedIn backfill every 4h.")
    return scheduler


def _run_linkedin_backfill(trigger: str = 'scheduled'):
    """Run the backfill, telling it which users the app is already scraping.

    Skipping `_linkedin_inflight` keeps the job from paying Mindcase a second
    time for a scrape a login or an admin refresh already kicked off.
    """
    from linkedin_job import run_linkedin_backfill_job
    result = run_linkedin_backfill_job(
        skip_user_ids=set(_linkedin_inflight),
        trigger=trigger,
    )
    # Enriched users must not keep serving the pre-scrape (empty) cache entry.
    _linkedin_cache.clear()
    return result


if os.environ.get('ENABLE_SCHEDULER', 'true').lower() == 'true':
    # In Flask debug mode the reloader spawns a child process; only start scheduler there.
    if not app.debug or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        try:
            _scheduler = _start_scheduler()
        except Exception as e:
            print(f"Scheduler failed to start: {e}")


def _fetch_nexa_access(token: str) -> bool:
    """Nexa access from the WeAce user-config API — the source of truth.

    Access requires both flags: nexaCoachAccessCohort (cohort is entitled) and
    settingNexaCoachAccess (the user's own setting). Falls back to no access if
    the API is unreachable, so entitlement is never granted on a failed call.
    """
    try:
        resp = http_requests.get(
            f'{WEACE_API_URL}/api/v1/static-data/user-config',
            headers={'accept': '*/*', 'Authorization': f'Bearer {token}'},
            timeout=10,
        )
        if not resp.ok:
            logger.warning("[nexa] user-config API returned status=%s", resp.status_code)
            return False
        cfg = (resp.json() or {}).get('data') or {}
        return bool(cfg.get('nexaCoachAccessCohort')) and bool(cfg.get('settingNexaCoachAccess'))
    except Exception as e:
        logger.error("[nexa] user-config API unreachable: %s", e)
        return False


# Heading the profile block is written under — also how /chat tells whether the
# cached system prompt already carries the profile.
_PROFILE_HEADER = 'USER PROFILE FOR'


def _profile_block(user_name: str, profile_context: dict = None) -> str:
    """Who this person is — role, level, background — as a system-prompt block.

    Empty string when the profile carries nothing worth telling the model.
    """
    if not profile_context:
        return ''
    parts = []
    role = (profile_context.get('current_role') or '').strip()
    company = (profile_context.get('current_company') or '').strip()
    if role and company:
        parts.append(f"- Current Role: {role} at {company}")
    elif role:
        parts.append(f"- Current Role: {role}")
    if profile_context.get('level_name'):
        parts.append(f"- Experience Level: {profile_context['level_name']}")
    if profile_context.get('gender'):
        parts.append(f"- Gender: {profile_context['gender']}")
    if profile_context.get('country'):
        parts.append(f"- Country: {profile_context['country']}")
    functional_areas = profile_context.get('functional_areas') or []
    if isinstance(functional_areas, list) and functional_areas:
        parts.append(f"- Functional Background: {', '.join(functional_areas)}")
    industry_types = profile_context.get('industry_types') or []
    if isinstance(industry_types, list) and industry_types:
        parts.append(f"- Industry Context: {', '.join(industry_types)}")
    if not parts:
        return ''
    return (
        f"\n\n---\n{_PROFILE_HEADER} {user_name.upper()}:\n"
        + "\n".join(parts)
        + "\n---\n"
        "Use this profile to personalise your coaching — tailor advice to their role, "
        "industry, and background. Do not read out this profile to the user unless asked."
    )


# The profile changes rarely and /chat needs it on every message, so cache it the
# same way as the org content and admin notes rather than hitting the DB per turn.
_PROFILE_TTL = 300  # seconds
_profile_cache: dict = {}  # user_id -> (expires_at, profile_context)


def _get_profile_context_cached(user_id: str | None) -> dict:
    if not user_id:
        return {}
    now = time.time()
    cached = _profile_cache.get(user_id)
    if cached and cached[0] > now:
        return cached[1]
    try:
        ctx = get_user_profile_context(user_id) or {}
    except Exception as e:
        logger.error("[profile] fetch failed for user_id=%s: %s", user_id, e)
        return cached[1] if cached else {}
    _profile_cache[user_id] = (now + _PROFILE_TTL, ctx)
    return ctx


def _profile_directive(user_id: str | None, user_name: str, profile_context: dict = None) -> str:
    """Profile block for a single request, from whatever context is at hand.

    Falls back to the stored profile when the caller has none, so the model knows
    who it's talking to even if the client sent nothing.
    """
    return _profile_block(user_name, profile_context or _get_profile_context_cached(user_id))


# --- LinkedIn enrichment -------------------------------------------------------
#
# The WeAce profile tells us who someone is on paper; their LinkedIn tells us the
# arc — how they got here, what they've built, what they're publicly wrestling
# with. Scraped once per user via Mindcase (see linkedin.py), stored in
# user_settings, and folded into both the welcome message and every chat turn.

# Heading the LinkedIn block is written under — also how /chat tells whether the
# cached system prompt already carries it.
_LINKEDIN_HEADER = 'LINKEDIN BACKGROUND FOR'

_LINKEDIN_URL_RE = re.compile(r'https?://[^\s"\'<>]*linkedin\.com/in/[^\s"\'<>,]+', re.I)

# A brand-new user's first welcome message is generated seconds after login,
# normally before their first scrape has finished — so that one opener goes out
# without LinkedIn context, and every session after it has it. Set this to a
# number of seconds to hold session creation until the scrape lands and make even
# the first opener LinkedIn-aware. 0 (the default) never delays login.
LINKEDIN_WELCOME_WAIT = int(os.getenv('LINKEDIN_WELCOME_WAIT', '0'))


def _extract_linkedin_url(profile: dict) -> str | None:
    """Find the user's LinkedIn URL anywhere in the WeAce personal-info payload.

    The field name isn't guaranteed across profile shapes, so rather than pinning
    to one key this walks the payload for the first personal LinkedIn URL it
    finds. Company pages (/company/...) are ignored by the pattern.
    """
    found = []

    def walk(node, depth=0):
        if found or depth > 6:
            return
        if isinstance(node, str):
            match = _LINKEDIN_URL_RE.search(node)
            if match:
                found.append(match.group(0).rstrip('/'))
        elif isinstance(node, dict):
            for value in node.values():
                walk(value, depth + 1)
        elif isinstance(node, list):
            for value in node[:50]:
                walk(value, depth + 1)

    walk(profile or {})
    return found[0] if found else None


def _scrape_linkedin(user_id: str, url: str):
    """Scrape a profile and store it. Blocking — always call this on a
    background thread."""
    logger.info("[linkedin] scrape starting for user_id=%s url=%s", user_id, url)
    t0 = time.perf_counter()
    try:
        profile = linkedin.fetch_profile(url)
        saved = save_user_linkedin_profile(user_id, profile)
        logger.info("[linkedin] scrape %s for user_id=%s in %s",
                    'ok' if saved else 'empty', user_id, _elapsed(t0))
    except Exception as e:
        logger.error("[linkedin] scrape failed for user_id=%s: %s", user_id, e)
    finally:
        _linkedin_cache.pop(user_id, None)
        _linkedin_inflight.discard(user_id)


# Guards against two concurrent logins both paying for the same scrape.
_linkedin_inflight: set = set()
_linkedin_inflight_lock = threading.Lock()


def _kickoff_linkedin_scrape(user_id: str, url: str, force: bool = False):
    """Start a background scrape when this user has no LinkedIn data yet.

    Data is fetched once and then reused — an admin can force a re-scrape from
    the admin panel. Returns the running thread, or None if nothing was started.

    A stored profile is the only record that a scrape ever ran, so a URL that
    never resolves is retried on each login rather than being given up on.
    """
    if not user_id or not url or not linkedin.is_configured():
        return None
    try:
        stored = get_user_linkedin(user_id)
    except Exception as e:
        logger.error("[linkedin] could not read stored data for user_id=%s: %s", user_id, e)
        return None
    if not force and stored.get('profile'):
        return None

    with _linkedin_inflight_lock:
        if user_id in _linkedin_inflight:
            return None
        _linkedin_inflight.add(user_id)
    thread = threading.Thread(target=_scrape_linkedin, args=(user_id, url),
                              name=f'linkedin-{user_id[:8]}', daemon=True)
    thread.start()
    return thread


# Scraped once and rarely changing, but /chat needs it every turn — cache it the
# same way as the profile and admin notes.
_LINKEDIN_TTL = 300  # seconds
_linkedin_cache: dict = {}  # user_id -> (expires_at, data)


def _get_linkedin_cached(user_id: str | None) -> dict:
    if not user_id:
        return {}
    now = time.time()
    cached = _linkedin_cache.get(user_id)
    if cached and cached[0] > now:
        return cached[1]
    try:
        data = get_user_linkedin(user_id) or {}
    except Exception as e:
        logger.error("[linkedin] fetch failed for user_id=%s: %s", user_id, e)
        return cached[1] if cached else {}
    _linkedin_cache[user_id] = (now + _LINKEDIN_TTL, data)
    return data


def _linkedin_block(user_name: str, data: dict) -> str:
    """Scraped LinkedIn profile as a system-prompt block.

    Empty string until a scrape has actually landed something.
    """
    if not data:
        return ''
    summary = linkedin.profile_summary(data.get('profile') or {})
    if not summary:
        return ''

    block = f"\n\n---\n{_LINKEDIN_HEADER} {user_name.upper()}:\n"
    block += "Public LinkedIn data for this person.\n"
    block += f"\n{summary}\n"
    block += (
        "---\n"
        "Use this to coach them as someone whose career you already understand — "
        "their trajectory, the scale they operate at, the transitions they've made, "
        "and what they're publicly focused on right now. Ground your advice in it. "
        "Never recite it back, list it out, flatter them about it, or say you looked "
        "them up; it's public information you simply know. If they contradict it, "
        "believe them over this — it may be out of date."
    )
    return block


def _linkedin_directive(user_id: str | None, user_name: str) -> str:
    """LinkedIn block for a single request, read from storage."""
    return _linkedin_block(user_name, _get_linkedin_cached(user_id))


def _build_personalized_prompt(user_name: str, highlights: list, profile_context: dict = None,
                               user_id: str = None) -> str:
    prompt = SYSTEM_INSTRUCTION + _profile_block(user_name, profile_context) \
        + _linkedin_directive(user_id, user_name)

    if not highlights:
        return prompt
    lines = []
    for h in highlights:
        line = f"• [{h['date']}] Topic: \"{h['topic']}\""
        if h.get('takeaway'):
            line += f"\n  Coaching note: \"{h['takeaway']}\""
        lines.append(line)
    history_text = "\n".join(lines)
    prompt += (
        f"\n\n---\nPAST COACHING HIGHLIGHTS FOR {user_name.upper()}:\n"
        f"{history_text}\n---\n"
        "This is a new session. Use the highlights above to personalise your coaching — "
        "build on past insights, reference earlier themes naturally, and avoid starting from scratch. "
        "Do not explicitly tell the user you have access to previous session records unless asked."
    )
    return prompt


def _system_prompt_for(session_uuid: str, user_id: str, user_name: str,
                       profile_context: dict = None) -> str:
    """The session's system prompt, built once and cached per session_uuid."""
    cached = sessions_cache.get(session_uuid)
    if cached:
        return cached[0]["content"]
    highlights = get_session_highlights(user_id)
    ctx = profile_context or _get_profile_context_cached(user_id)
    prompt = _build_personalized_prompt(user_name, highlights, ctx, user_id)
    if session_uuid:
        sessions_cache[session_uuid] = [{"role": "system", "content": prompt}]
    return prompt


def _get_or_rebuild_history(session_uuid: str, user_id: str, user_name: str,
                            profile_context: dict = None) -> list:
    """System prompt plus the user's whole coaching history, not just this session.

    Nexa is a continuing relationship, so every past conversation is replayed —
    the user can pick up something from weeks ago without repeating it. Read from
    the DB on every request: the in-memory cache can't be the source of truth,
    because we run several gunicorn workers, each with its own copy, so
    consecutive messages land in different processes and no single worker ever
    holds the whole conversation. Replaying from the DB is also what carries the
    context through a restart or a deploy.
    """
    history = [{"role": "system", "content":
                _system_prompt_for(session_uuid, user_id, user_name, profile_context)}]

    rows = get_recent_user_messages(user_id, MAX_CONTEXT_MESSAGES)
    turns = [{"role": r["role"], "content": r["content"]} for r in rows
             if r["role"] in ("user", "assistant") and (r["content"] or "").strip()]
    # The API requires the first message to be from the user, so drop a leading
    # assistant turn — a session's welcome message, or whatever the window
    # happened to start on.
    while turns and turns[0]["role"] == "assistant":
        turns.pop(0)
    return history + turns


def _generate_welcome(user_name: str, highlights: list, profile_context: dict = None,
                      returning: bool = False, language: str = None,
                      user_id: str = None, mode: str = None):
    """Generate a personalised opening message for a new chat session.

    When the user has past coaching history, the message warmly references an
    earlier theme and nudges them to pick it back up; otherwise it's a warm
    first-time greeting. Returns (message, suggestions). Falls back to a static
    greeting if the AI is unavailable or errors out.
    """
    if returning:
        fallback_msg = (
            f"Welcome back, {user_name}. Ready to continue your leadership journey? "
            "What's on your mind today?"
        )
    else:
        fallback_msg = (
            f"Welcome to your Executive Leadership Coaching Session, {user_name}. "
            "I'm Nexa, here to help you navigate complex professional challenges, "
            "enhance your leadership skills, and drive strategic impact. "
            "What would you like to focus on today?"
        )

    if client is None:
        return fallback_msg, []

    system_content = _build_personalized_prompt(user_name, highlights, profile_context, user_id)
    details = _get_user_details_cached(user_id)
    has_linkedin = bool(_linkedin_directive(user_id, user_name))
    system_content += _user_details_directive(user_id) + _mode_directive(mode) \
        + _language_directive(language)
    if returning and highlights:
        instruction = (
            f"[SESSION START] Greet {user_name} to open a new coaching session. "
            "Warmly reference a relevant theme from their past coaching highlights and "
            "invite them to continue it or start something new — without stating that you "
            "have access to past session records. Keep it to 2-3 warm, concise sentences. "
            "Then include 2-3 tappable starter nudges in the [[SUGGESTIONS]] block, phrased "
            "from the user's point of view, that pick up past themes or open new ground."
        )
    else:
        instruction = (
            f"[SESSION START] Greet {user_name} to open their first coaching session. "
            "Briefly introduce yourself as Nexa and what you help with, in 2-3 warm, concise "
            "sentences. Then include 2-3 tappable starter nudges in the [[SUGGESTIONS]] block, "
            "phrased from the user's point of view, to help them begin."
        )

    # Their LinkedIn is usually the richest thing we have on a first-time user —
    # it's what turns a generic hello into an opener that lands.
    if has_linkedin:
        instruction += (
            " You have this person's public LinkedIn background in context. Use it to make "
            "this opener specific to them. Anchor on one concrete thing from their career — "
            "the scale of what they run now, a transition they've made, the shift between "
            "their last role and this one, or something they've recently posted about — and "
            "open on that. Do not summarise their career, list their roles or achievements "
            "back at them, compliment their CV, or hint that you looked them up. Make the "
            "suggested nudges specific to where they actually are in their career, not "
            "generic coaching topics."
        )

    # When an admin has recorded background on this user, the opening line should
    # land as "this coach already knows me" rather than a generic hello.
    if details and details.strip():
        instruction += (
            " You have detailed background on this person under ADDITIONAL DETAILS ABOUT "
            "THIS USER — use it to make this opener land. Anchor on one specific, telling "
            "thing from it (a current mandate, a recent move, a tension or ambition that "
            "stands out) and speak to it directly, the way a coach who has done their "
            "homework would. Show you understand where they are right now; do not recite "
            "their background, list facts back at them, flatter them, or say you were "
            "briefed or given any information. Make the suggested nudges specific to their "
            "actual situation, not generic coaching topics."
        )

    try:
        if AI_PROVIDER == "claude":
            response = client.messages.create(
                model=AI_MODEL,
                max_tokens=400,
                system=system_content,
                messages=[{"role": "user", "content": instruction}],
            )
            reply = response.content[0].text
        else:
            response = client.chat.completions.create(
                model=AI_MODEL,
                messages=[
                    {"role": "system", "content": system_content},
                    {"role": "user", "content": instruction},
                ],
                temperature=0.7,
            )
            reply = response.choices[0].message.content
        message, suggestions = _extract_suggestions(reply)
        return (message or fallback_msg), suggestions
    except Exception as e:
        logger.warning("[welcome] generation failed for user %s: %s", user_name, e)
        return fallback_msg, []


@app.route('/')
def index():
    return render_template('index.html', weace_api_url=WEACE_API_URL, active_tab='talk')


@app.route('/me')
@require_weace_token
def me():
    if not g.user:
        return jsonify({'error': 'Session not initialised — call /session first'}), 401
    name = g.user['user_name']
    initials = ''.join(w[0].upper() for w in name.split()[:2])
    # Nexa access is resolved from the user-config API at login and cached per token.
    token_state = auth_tokens.get(g.access_token) or {}
    return jsonify({
        'logged_in': True,
        'user_name': name,
        'initials': initials,
        'profile_image': g.user.get('profile_image', ''),
        'role': g.user.get('role', 'user'),
        'nexa_access': token_state.get('nexa_access', False),
        'access_last_date': token_state.get('access_last_date'),
    })


@app.route('/session', methods=['POST'])
@require_weace_token
def create_session():
    """
    Called by the frontend after authentication. Receives accessToken via
    Authorization: Bearer header (verified by @require_weace_token) + refreshToken in body,
    fetches the user profile from the we-ace API, then sets up the
    DB chat session and returns a session_token for subsequent Bearer auth.
    """
    started = time.perf_counter()
    ip = request.remote_addr
    data = request.json or {}
    access_token = g.access_token          # set by @require_weace_token
    refresh_token = (data.get('refreshToken') or '').strip()
    client_user_id = g.user.get('user_id')

    logger.info("[create_session] request from ip=%s ua=%r has_refresh_token=%s "
                "client_user_id=%s client_session_uuid=%s client_roles=%s",
                ip, request.headers.get('User-Agent', '')[:120], bool(refresh_token),
                client_user_id or 'none', (data.get('sessionUuid') or 'none'),
                len(data.get('roles') or []))

    # @require_weace_token already fetched /api/v1/users/profile to verify the
    # token — reuse that response instead of asking for it a second time.
    client_user_id = (client_user_id or '').strip()
    logger.info("[create_session] profile resolved from verified token: user_id=%s",
                client_user_id or 'MISSING')
    if not client_user_id:
        logger.warning("[create_session] verified token carried no user id ip=%s", ip)
        return jsonify({'error': 'Failed to fetch user profile'}), 401
    try:
        t0 = time.perf_counter()
        profile_resp = http_requests.get(
            f'{WEACE_API_URL}/api/v1/personal-info/{client_user_id}',
            headers={'Authorization': f'Bearer {access_token}'},
            timeout=10,
        )
        logger.info("[create_session] personal-info API responded with status=%s in %s",
                    profile_resp.status_code, _elapsed(t0))
        if not profile_resp.ok:
            logger.warning("[create_session] personal-info API error: status=%s ip=%s body=%r",
                           profile_resp.status_code, ip, profile_resp.text[:300])
            return jsonify({'error': 'Failed to fetch user profile'}), profile_resp.status_code
        profile_data = profile_resp.json()
    except Exception as e:
        logger.error("[create_session] personal-info API request failed after %s: %s", _elapsed(t0), e)
        return jsonify({'error': f'Profile fetch failed: {e}'}), 502

    # Parse profile response — user data is nested under 'data'
    profile = profile_data.get('data') or {}

    raw_roles = profile.get('roles') or []
    roles_arr = raw_roles if isinstance(raw_roles, list) else [raw_roles]

    # Fallback: use roles forwarded from the frontend auth response
    if not roles_arr:
        client_roles = data.get('roles') or []
        if isinstance(client_roles, list):
            roles_arr = [r for r in client_roles if isinstance(r, dict)]
        logger.info("[create_session] profile carried no roles — using %s role(s) forwarded by the client",
                    len(roles_arr))

    role = roles_arr

    user_id = client_user_id
    first = (profile.get('firstName') or '').strip()
    last = (profile.get('lastName') or '').strip()
    email = (profile.get('email') or '').strip()
    user_name = f"{first} {last}".strip() or email.split('@')[0]
    profile_image = (profile.get('profileImage') or '').strip()
    org_name = (profile.get('organizationName') or '').strip()
    org_slug = (profile.get('organizationId') or '').strip()
    cohort_id = (profile.get('cohortId') or '').strip() or None
    cohort_name = (profile.get('cohortName') or '').strip() or None
    country = (profile.get('countryName') or '').strip() or None
    level_name = (profile.get('levelName') or '').strip() or None
    candidate_profile = profile.get('candidateProfile') or {}
    gender = (candidate_profile.get('gender') or '').strip() or None
    functional_areas = profile.get('functionalAreaNames') or []
    industry_types = profile.get('industryTypeNames') or []
    employment = profile.get('employment') or {}
    current_role = (employment.get('role') or '').strip() or None
    current_company = (employment.get('company') or '').strip() or None

    logger.info("[create_session] parsed profile: user_id=%s email=%s role=%s org_slug=%s",
                user_id or 'MISSING', email or 'MISSING', role, org_slug or 'none')
    logger.info("[create_session] profile detail: user_name=%r org_name=%r cohort=%s/%s level=%s "
                "country=%s gender=%s functional_areas=%s industry_types=%s has_profile_image=%s",
                user_name, org_name or 'none', cohort_id or 'none', cohort_name or 'none',
                level_name or 'none', country or 'none', gender or 'none',
                len(functional_areas), len(industry_types), bool(profile_image))

    if not user_id:
        logger.warning("[create_session] could not determine user_id from profile data ip=%s", ip)
        return jsonify({'error': 'Could not determine user ID from profile'}), 400

    profile_context = {
        'current_role': current_role,
        'current_company': current_company,
        'level_name': level_name,
        'gender': gender,
        'country': country,
        'functional_areas': functional_areas,
        'industry_types': industry_types,
    }

    # Public LinkedIn background, scraped once per user and then reused from the
    # database. Kicked off here — as early as the user id is known — so it has the
    # longest possible head start on the welcome message.
    linkedin_thread = None
    try:
        linkedin_url = _extract_linkedin_url(profile)
        logger.info("[linkedin] user_id=%s linkedin_url=%s", user_id, linkedin_url or 'none')
        if linkedin_url and set_user_linkedin_url(user_id, linkedin_url):
            _linkedin_cache.pop(user_id, None)  # URL changed — drop the stale block
        linkedin_thread = _kickoff_linkedin_scrape(user_id, linkedin_url)
        if linkedin_thread:
            logger.info("[linkedin] background scrape started for user_id=%s", user_id)
    except Exception as e:
        logger.error("[linkedin] setup failed for user_id=%s: %s", user_id, e)

    try:
        t0 = time.perf_counter()
        returning = has_previous_sessions(user_id)
        highlights = get_session_highlights(user_id) if returning else []
        logger.info("[create_session] history lookup: returning=%s highlights=%s in %s",
                    returning, len(highlights), _elapsed(t0))

        # Tab switches resume through /session/resume, so the client normally
        # sends no sessionUuid here and this opens a new conversation. Honoured
        # when it is sent, for older clients and direct API callers.
        client_session_uuid = (data.get('sessionUuid') or '').strip()
        resumed = is_resumable_session(client_session_uuid, user_id)

        if resumed:
            session_uuid = client_session_uuid
            logger.info("[create_session] resuming chat session: session_uuid=%s user_id=%s",
                        session_uuid, user_id)
        else:
            if client_session_uuid:
                logger.info("[create_session] client session_uuid=%s not resumable (wrong user, "
                            "unknown, or older than the resume window) — starting a new session",
                            client_session_uuid)
            session_uuid = str(uuid.uuid4())
            create_chat_session(session_uuid, user_id, user_name, email, org_name, org_slug, cohort_id)
            logger.info("[create_session] chat session created: session_uuid=%s user_id=%s returning=%s",
                        session_uuid, user_id, returning)

        # Optionally hold here so a first-ever session still opens with LinkedIn
        # context, at the cost of a slower login. Off unless configured.
        if linkedin_thread and LINKEDIN_WELCOME_WAIT > 0:
            t0 = time.perf_counter()
            linkedin_thread.join(timeout=LINKEDIN_WELCOME_WAIT)
            logger.info("[linkedin] waited %s for the scrape (finished=%s)",
                        _elapsed(t0), not linkedin_thread.is_alive())

        # Only the system prompt is cached here — a resumed session's turns are
        # replayed from the DB on each /chat request.
        if session_uuid not in sessions_cache:
            prompt = _build_personalized_prompt(user_name, highlights, profile_context, user_id)
            sessions_cache[session_uuid] = [{"role": "system", "content": prompt}]
            logger.info("[create_session] seeded system prompt: session_uuid=%s prompt_chars=%s "
                        "cached_sessions=%s", session_uuid, len(prompt), len(sessions_cache))
        else:
            logger.info("[create_session] reusing cached system prompt: session_uuid=%s",
                        session_uuid)

        language = get_user_language(user_id) or DEFAULT_LANGUAGE
        mode = get_user_mode(user_id) or DEFAULT_MODE
        logger.info("[create_session] language for user_id=%s: %s (mode=%s)",
                    user_id, language, mode)
        if resumed:
            welcome_message, welcome_suggestions = '', []
            logger.info("[create_session] resumed session — skipping welcome generation")
        else:
            t0 = time.perf_counter()
            welcome_message, welcome_suggestions = _generate_welcome(
                user_name, highlights, profile_context, returning, language, user_id, mode)
            logger.info("[create_session] welcome generated in %s: chars=%s suggestions=%s",
                        _elapsed(t0), len(welcome_message or ''), len(welcome_suggestions or []))

        # Evict any stale auth_tokens entry for this user (token rotation)
        stale = [t for t, d in auth_tokens.items() if d.get('user_id') == user_id]
        for t in stale:
            auth_tokens.pop(t, None)
        if stale:
            logger.info("[create_session] evicted %s stale token entr%s for user_id=%s "
                        "(%s tokens cached)", len(stale), 'y' if len(stale) == 1 else 'ies',
                        user_id, len(auth_tokens))

        # Keep Flask session only for server-rendered page routes (/dashboard, /admin)
        session['user_id'] = user_id
        session['user_name'] = user_name
        session['email'] = email
        session['profile_image'] = profile_image
        session['access_token'] = access_token
        session['refresh_token'] = refresh_token
        session['session_uuid'] = session_uuid
        session['role'] = role
        session['org_name'] = org_name
        session['org_slug'] = org_slug
        session['cohort_id'] = cohort_id
        session['profile_context'] = profile_context

        # Super admins always have Nexa access; everyone else is decided by the
        # user-config API at login and cached for the admin dashboard.
        is_super_admin = _has_role(role, 'weace_super_admin', 'corporate_super_admin')
        t0 = time.perf_counter()
        nexa_access = True if is_super_admin else _fetch_nexa_access(g.access_token)
        logger.info("[create_session] nexa_access=%s (source=%s) resolved in %s",
                    nexa_access, 'super_admin_role' if is_super_admin else 'user-config API',
                    _elapsed(t0))
        t0 = time.perf_counter()
        settings = upsert_user_login(
            user_id,
            first_name=first, last_name=last, email=email,
            org_id=org_slug, org_name=org_name, cohort_id=cohort_id,
            cohort_name=cohort_name, country=country,
            level_name=level_name, gender=gender,
            functional_areas=functional_areas, industry_types=industry_types,
            nexa_access=nexa_access,
        )
        access_last_date = settings['access_last_date']
        logger.info("[create_session] user_settings upserted in %s: access_last_date=%s",
                    _elapsed(t0), access_last_date)

        auth_tokens[g.access_token] = {
            'user_id': user_id,
            'user_name': user_name,
            'email': email,
            'profile_image': profile_image,
            'access_token': access_token,
            'refresh_token': refresh_token,
            'session_uuid': session_uuid,
            'role': role,
            'org_name': org_name,
            'org_id': org_slug,
            'cohort_id': cohort_id,
            'profile_context': profile_context,
            'nexa_access': nexa_access,
            'access_last_date': access_last_date,
        }

        logger.info("[create_session] session established: user_id=%s user_name=%r nexa_access=%s",
                    user_id, user_name, nexa_access)

        initials = ''.join(w[0].upper() for w in user_name.split()[:2])
        t0 = time.perf_counter()
        recent_messages = _serialize_messages(user_id, get_user_history(user_id, 15))
        logger.info("[create_session] replaying %s recent message(s) loaded in %s",
                    len(recent_messages), _elapsed(t0))
        logger.info("[create_session] responding to user_id=%s: session_uuid=%s resumed=%s "
                    "returning=%s nexa_access=%s language=%s messages=%s suggestions=%s total=%s",
                    user_id, session_uuid, resumed, returning, nexa_access, language,
                    len(recent_messages), len(welcome_suggestions or []), _elapsed(started))
        return jsonify({
            'user_id': user_id,
            'user_name': user_name,
            'session_uuid': session_uuid,
            'returning': returning,
            'resumed': resumed,
            'initials': initials,
            'profile_image': profile_image,
            'role': role,
            'nexa_access': nexa_access,
            'access_last_date': access_last_date,
            'recent_messages': recent_messages,
            'welcome_message': welcome_message,
            'welcome_suggestions': welcome_suggestions,
            'language': language,
            'mode': mode,
            'profile_context': profile_context,
            'org_name': org_name,
            'org_id': org_slug,
            'cohort_id': cohort_id,
        })
    except Exception as e:
        logger.exception("[create_session] unexpected error for user_id=%s after %s: %s",
                         user_id, _elapsed(started), e)
        return jsonify({'error': str(e)}), 500


@app.route('/session/resume', methods=['POST'])
@require_weace_token
def resume_session():
    """
    Fast path for coming back to the chat page from another tab (Sentiment
    Analysis, Admin, …). Given the chat session the client already had, it
    re-establishes server state and replays the conversation without the full
    /session work — no profile/personal-info API round trips, no new session
    row, no fresh welcome message.

    Returns 409 when there is nothing to resume, so the client can fall back to
    the normal login chain.
    """
    started = time.perf_counter()
    data = request.json or {}
    client_session_uuid = (data.get('sessionUuid') or '').strip()
    refresh_token = (data.get('refreshToken') or '').strip()
    user_id = (g.user.get('user_id') or '').strip()

    if not client_session_uuid or not user_id:
        return jsonify({'error': 'Nothing to resume'}), 409

    try:
        if not is_resumable_session(client_session_uuid, user_id):
            logger.info("[resume_session] session_uuid=%s not resumable for user_id=%s "
                        "— client should run the full login chain",
                        client_session_uuid, user_id)
            return jsonify({'error': 'Session not resumable'}), 409

        session_uuid = client_session_uuid
        # The token cache usually still holds everything /session resolved. It's
        # cold after a token rotation or a server restart — rebuild from what was
        # stored at login instead of calling the profile APIs again.
        cached = auth_tokens.get(g.access_token) or {}
        if cached:
            user_name = cached['user_name']
            email = cached.get('email', '')
            profile_image = cached.get('profile_image', '')
            org_name = cached.get('org_name', '')
            org_slug = cached.get('org_id', '')
            cohort_id = cached.get('cohort_id')
            profile_context = cached.get('profile_context') or {}
            role = cached.get('role') or g.user.get('role') or []
        else:
            identity = get_user_identity(user_id)
            name = ' '.join(p for p in [(identity.get('first_name') or '').strip(),
                                        (identity.get('last_name') or '').strip()] if p)
            email = (identity.get('email') or g.user.get('email') or '').strip()
            user_name = name or (g.user.get('user_name') or email.split('@')[0])
            profile_image = ((g.token_data.get('data') or {}).get('profileImage') or '').strip()
            org_name = identity.get('org_name') or ''
            org_slug = identity.get('org_id') or ''
            cohort_id = identity.get('cohort_id')
            profile_context = get_user_profile_context(user_id)
            role = g.user.get('role') or []
            logger.info("[resume_session] token cache cold — rebuilt identity for user_id=%s from DB",
                        user_id)

        # Warm the system prompt for this session; the conversation itself is
        # replayed from the DB on each /chat turn.
        _system_prompt_for(session_uuid, user_id, user_name, profile_context)

        if _has_role(role, 'weace_super_admin', 'corporate_super_admin'):
            nexa_access, access_last_date = True, cached.get('access_last_date')
        elif cached:
            nexa_access = cached.get('nexa_access', False)
            access_last_date = cached.get('access_last_date')
        else:
            settings = get_user_access_settings(user_id)
            nexa_access = settings['nexa_access']
            access_last_date = settings['access_last_date']

        language = get_user_language(user_id) or DEFAULT_LANGUAGE
        mode = get_user_mode(user_id) or DEFAULT_MODE

        # Keep Flask session (server-rendered pages) and the token cache (/chat)
        # pointing at this same conversation.
        session['user_id'] = user_id
        session['user_name'] = user_name
        session['email'] = email
        session['profile_image'] = profile_image
        session['access_token'] = g.access_token
        session['refresh_token'] = refresh_token or session.get('refresh_token', '')
        session['session_uuid'] = session_uuid
        session['role'] = role
        session['org_name'] = org_name
        session['org_slug'] = org_slug
        session['cohort_id'] = cohort_id
        session['profile_context'] = profile_context

        auth_tokens[g.access_token] = {
            'user_id': user_id,
            'user_name': user_name,
            'email': email,
            'profile_image': profile_image,
            'access_token': g.access_token,
            'refresh_token': refresh_token or (cached.get('refresh_token') or ''),
            'session_uuid': session_uuid,
            'role': role,
            'org_name': org_name,
            'org_id': org_slug,
            'cohort_id': cohort_id,
            'profile_context': profile_context,
            'nexa_access': nexa_access,
            'access_last_date': access_last_date,
        }

        initials = ''.join(w[0].upper() for w in user_name.split()[:2])
        recent_messages = _serialize_messages(user_id, get_user_history(user_id, 15))
        logger.info("[resume_session] resumed session_uuid=%s user_id=%s messages=%s in %s",
                    session_uuid, user_id, len(recent_messages), _elapsed(started))
        return jsonify({
            'user_id': user_id,
            'user_name': user_name,
            'session_uuid': session_uuid,
            'returning': True,
            'resumed': True,
            'initials': initials,
            'profile_image': profile_image,
            'role': role,
            'nexa_access': nexa_access,
            'access_last_date': access_last_date,
            'recent_messages': recent_messages,
            'welcome_message': '',
            'welcome_suggestions': [],
            'language': language,
            'mode': mode,
            'profile_context': profile_context,
            'org_name': org_name,
            'org_id': org_slug,
            'cohort_id': cohort_id,
        })
    except Exception as e:
        logger.exception("[resume_session] failed for user_id=%s after %s: %s",
                         user_id, _elapsed(started), e)
        return jsonify({'error': str(e)}), 500


@app.route('/new-session', methods=['POST'])
@require_weace_token
def new_session():
    """
    Discards the previous in-memory history and starts a fresh chat session,
    injecting session highlights from past conversations as AI context.
    """
    if not g.user:
        return jsonify({'error': 'Session not initialised — call /session first'}), 401

    old_uuid = g.user.get('session_uuid')
    if old_uuid and old_uuid in sessions_cache:
        del sessions_cache[old_uuid]

    user_id = g.user['user_id']
    user_name = g.user['user_name']
    email = g.user.get('email', '')

    try:
        returning = has_previous_sessions(user_id)
        highlights = get_session_highlights(user_id) if returning else []

        session_uuid = str(uuid.uuid4())
        create_chat_session(session_uuid, user_id, user_name, email,
                            g.user.get('org_name'), g.user.get('org_slug'), g.user.get('cohort_id'))

        profile_context = g.user.get('profile_context') or {}
        prompt = _build_personalized_prompt(user_name, highlights, profile_context)
        sessions_cache[session_uuid] = [{"role": "system", "content": prompt}]

        language = get_user_language(user_id) or DEFAULT_LANGUAGE
        mode = get_user_mode(user_id) or DEFAULT_MODE
        welcome_message, welcome_suggestions = _generate_welcome(
            user_name, highlights, profile_context, returning, language, user_id, mode)

        role = g.user.get('role', [])
        if _has_role(role, 'weace_super_admin', 'corporate_super_admin'):
            nexa_access = True
            access_last_date = None
        else:
            settings = get_user_access_settings(user_id)
            # Resolved at login by the user-config API; cached per token, then in the DB.
            token_state = auth_tokens.get(g.access_token) or {}
            nexa_access = token_state.get('nexa_access', settings['nexa_access'])
            access_last_date = settings['access_last_date']

        auth_tokens[g.access_token]['session_uuid'] = session_uuid
        auth_tokens[g.access_token]['nexa_access'] = nexa_access
        auth_tokens[g.access_token]['access_last_date'] = access_last_date

        initials = ''.join(w[0].upper() for w in user_name.split()[:2])
        recent_messages = _serialize_messages(user_id, get_user_history(user_id, 10))
        return jsonify({
            'user_name': user_name,
            'returning': returning,
            'initials': initials,
            'profile_image': g.user.get('profile_image', ''),
            'role': role,
            'nexa_access': nexa_access,
            'access_last_date': access_last_date,
            'recent_messages': recent_messages,
            'welcome_message': welcome_message,
            'welcome_suggestions': welcome_suggestions,
            'language': language,
            'mode': mode,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500



@app.route('/auth')
def auto_login():
    """
    Deep-link entry point for external platforms.
    URL: /auth?access_token=<token>&refresh_token=<token>
    If only refresh_token is provided, exchanges it for a new token pair via the auth API,
    then redirects back so the frontend can pick up the fresh tokens normally.
    """
    refresh_token = request.args.get('refresh_token', '').strip()

    if not refresh_token:
        return redirect('/')
    else:
        try:
            resp = http_requests.post(
                f'{WEACE_API_URL}/api/v1/auth/refresh',
                json={'refreshToken': refresh_token},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            new_access = (data.get('accessToken') or data.get('access_token') or '').strip()
            new_refresh = (data.get('refreshToken') or data.get('refresh_token') or '').strip()
            if new_access:
                return redirect(f'/auth?access_token={new_access}&refresh_token={new_refresh}')
        except Exception:
            pass
        return redirect('/')

    return render_template('index.html', weace_api_url=WEACE_API_URL, active_tab='talk')


@app.route('/history', methods=['GET'])
@require_weace_token
def chat_history():
    """Page older chat messages for infinite scroll. Returns up to 20 messages
    older than `before_id` (oldest-first) and whether more history remains."""
    if not g.user:
        return jsonify({'error': 'Session not initialised — call /session first'}), 401

    user_id = g.user['user_id']
    try:
        before_id = int(request.args.get('before_id'))
    except (TypeError, ValueError):
        return jsonify({'error': 'A numeric before_id is required'}), 400

    PAGE = 20
    rows = get_user_history_before(user_id, before_id, PAGE + 1)
    has_more = len(rows) > PAGE
    messages = _serialize_messages(user_id, rows[-PAGE:])
    return jsonify({'messages': messages, 'has_more': has_more})


@app.route('/feedback', methods=['POST'])
@require_weace_token
def message_feedback():
    """Thumbs up/down on one of Nexa's replies. `rating` is 1 (up), -1 (down),
    or 0 to undo. Users can only rate their own assistant messages."""
    if not g.user:
        return jsonify({'error': 'Session not initialised — call /session first'}), 401

    data = request.json or {}
    try:
        message_id = int(data.get('message_id'))
        rating = int(data.get('rating'))
    except (TypeError, ValueError):
        return jsonify({'error': 'A numeric message_id and rating are required'}), 400
    if rating not in (-1, 0, 1):
        return jsonify({'error': 'rating must be 1, -1, or 0'}), 400

    try:
        if not set_message_feedback(message_id, g.user['user_id'], rating):
            return jsonify({'error': 'Message not found'}), 404
        return jsonify({'ok': True, 'message_id': message_id, 'rating': rating})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/org-content', methods=['GET', 'POST'])
@require_weace_token
def org_content():
    """Read or update the corporate-specific content Nexa draws on for this org.
    Restricted to corporate super admins, scoped to their own organisation."""
    if not g.user:
        return jsonify({'error': 'Session not initialised — call /session first'}), 401
    if not _has_role(g.user.get('role'), 'corporate_super_admin', 'weace_super_admin'):
        return jsonify({'error': 'Forbidden'}), 403

    org_slug = (g.user.get('org_id') or '').strip() or None
    if not org_slug:
        return jsonify({'error': 'No organisation associated with this account'}), 400

    if request.method == 'GET':
        return jsonify({'content': get_org_custom_content(org_slug) or ''})

    content = ((request.json or {}).get('content') or '').strip()
    if len(content) > 20000:
        return jsonify({'error': 'Content is too long (20,000 character limit)'}), 400
    upsert_org_custom_content(org_slug, content, g.user.get('user_id'))
    _org_content_cache.pop(org_slug, None)  # reflect the change on the next message
    logger.info("[org_content] org=%s updated by user_id=%s (%d chars)",
                org_slug, g.user.get('user_id'), len(content))
    return jsonify({'ok': True, 'content': content})


@app.route('/languages', methods=['GET'])
def list_languages():
    """List all languages Nexa can coach in. Public — no session required."""
    return jsonify({
        'languages': _language_list(),
        'default': DEFAULT_LANGUAGE,
    })


@app.route('/language', methods=['GET', 'POST'])
@require_weace_token
def user_language():
    """Read or update the language Nexa replies in for the logged-in user."""
    if not g.user:
        return jsonify({'error': 'Session not initialised — call /session first'}), 401

    user_id = g.user['user_id']
    if request.method == 'GET':
        return jsonify({
            'language': get_user_language(user_id) or DEFAULT_LANGUAGE,
            'languages': _language_list(),
        })

    language = ((request.json or {}).get('language') or '').strip()
    if language not in SUPPORTED_LANGUAGES:
        return jsonify({'error': 'Unsupported language'}), 400
    set_user_language(user_id, language)
    logger.info("[language] user_id=%s set language=%s", user_id, language)
    return jsonify({'ok': True, 'language': language})


@app.route('/mode', methods=['GET', 'POST'])
@require_weace_token
def user_mode():
    """Read or update whether Nexa coaches or mentors the logged-in user."""
    if not g.user:
        return jsonify({'error': 'Session not initialised — call /session first'}), 401

    user_id = g.user['user_id']
    if request.method == 'GET':
        return jsonify({
            'mode': get_user_mode(user_id) or DEFAULT_MODE,
            'modes': [{'code': code, 'label': m['label']} for code, m in MODES.items()],
        })

    mode = ((request.json or {}).get('mode') or '').strip().lower()
    if mode not in MODES:
        return jsonify({'error': 'Unsupported mode'}), 400
    set_user_mode(user_id, mode)
    logger.info("[mode] user_id=%s set mode=%s", user_id, mode)
    return jsonify({'ok': True, 'mode': mode})


@app.route('/logout', methods=['POST'])
def logout():
    token = _get_bearer_token()
    if token and token in auth_tokens:
        old_uuid = auth_tokens[token].get('session_uuid')
        if old_uuid and old_uuid in sessions_cache:
            del sessions_cache[old_uuid]
        del auth_tokens[token]
    session.clear()
    return jsonify({'ok': True})


_SUGGESTIONS_RE = re.compile(
    r'\[\[\s*SUGGESTIONS\s*\]\](.*?)\[\[\s*/\s*SUGGESTIONS\s*\]\]',
    re.DOTALL | re.IGNORECASE,
)


def _extract_suggestions(text):
    """Split the model reply into (clean_text, suggestions).

    The model may append a [[SUGGESTIONS]]...[[/SUGGESTIONS]] block of tappable
    follow-ups. Strip it from the displayed text and return the parsed list.
    """
    if not text:
        return text, []
    match = _SUGGESTIONS_RE.search(text)
    if not match:
        return text.strip(), []

    clean = (text[:match.start()] + text[match.end():]).strip()
    suggestions = []
    for line in match.group(1).splitlines():
        line = line.strip().lstrip('-*•').strip()
        if line:
            suggestions.append(line)
    return clean, suggestions[:3]


@app.route('/chat', methods=['POST'])
@require_weace_token
def chat():
    if not g.user:
        return jsonify({'error': 'Session not initialised — call /session first'}), 401

    data = request.json or {}
    user_message = (data.get('message') or '').strip()
    if not user_message:
        return jsonify({'error': 'Message is required'}), 400

    user_id = g.user['user_id']
    user_name = g.user['user_name']
    session_uuid = data.get('session_uuid') or ''
    # Client-supplied profile first, then whatever /session resolved at login.
    profile_context = data.get('profile_context') or g.user.get('profile_context') or {}

    conversation_history = _get_or_rebuild_history(
        session_uuid, user_id, user_name, profile_context
    )

    # The cached system prompt is language-neutral; the directive is applied per
    # request so a mid-session language switch takes effect immediately.
    language = (data.get('language') or '').strip()
    if language not in SUPPORTED_LANGUAGES:
        language = get_user_language(user_id) or DEFAULT_LANGUAGE

    # Same for the coaching/mentoring toggle — read per request so flipping it
    # mid-conversation changes the very next reply.
    mode = (data.get('mode') or '').strip().lower()
    if mode not in MODES:
        mode = get_user_mode(user_id) or DEFAULT_MODE

    try:
        conversation_history.append({"role": "user", "content": user_message})
        save_message(session_uuid, user_id, 'user', user_message, language=language)

        system_content = next(
            (m["content"] for m in conversation_history if m["role"] == "system"),
            SYSTEM_INSTRUCTION,
        )
        # The welcome message is always generated with the user's profile in
        # context; every reply should be too. The cached prompt normally carries
        # it from session setup — add it here when it doesn't (prompt seeded
        # before the profile resolved, or an older cached session).
        if _PROFILE_HEADER not in system_content:
            system_content += _profile_directive(user_id, user_name, profile_context)

        # Same for LinkedIn. The scrape runs in the background after login, so on
        # a user's first session it usually lands *after* the prompt was cached —
        # this is what folds it in from the next message onward.
        if _LINKEDIN_HEADER not in system_content:
            system_content += _linkedin_directive(user_id, user_name)

        system_content += _user_details_directive(user_id) \
          + _org_content_directive(g.user.get('org_id')) \
          + _mode_directive(mode) \
          + _language_directive(language)

        if AI_PROVIDER == "claude":
            api_messages = [m for m in conversation_history if m["role"] != "system"]
            # Search results land in context before the reply is written, so this
            # needs more headroom than a no-tools turn.
            reply = _claude_reply(system_content, api_messages, max_tokens=2048)
        else:
            response = client.chat.completions.create(
                model=AI_MODEL,
                messages=[{"role": "system", "content": system_content}]
                         + [m for m in conversation_history if m["role"] != "system"],
                temperature=0.7,
            )
            reply = response.choices[0].message.content

        reply, suggestions = _extract_suggestions(reply)

        message_id = save_message(session_uuid, user_id, 'assistant', reply, language=language)
        return jsonify({'response': reply, 'suggestions': suggestions,
                        'message_id': message_id})

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect('/')
    if not _has_role(session.get('role'), 'corporate_super_admin'):
        return redirect('/')
    name = session['user_name']
    initials = ''.join(w[0].upper() for w in name.split()[:2])
    return render_template('dashboard.html',
        user_name=name,
        initials=initials,
        profile_image=session.get('profile_image', ''),
        org_name=session.get('org_name', 'Organisation'),
        org_slug=session.get('org_slug', ''),
        refresh_token=session.get('refresh_token', ''),
        # shared header (_header.html)
        active_tab='sentiment',
        show_org_tabs=True,
        show_admin=_has_role(session.get('role'), 'weace_super_admin'),
    )


@app.route('/api/cohorts')
@require_weace_token
def cohorts():
    if not g.user:
        return jsonify({'error': 'Session not initialised — call /session first'}), 401
    org_slug = g.user.get('org_id', '').strip() or None
    if not org_slug:
        return jsonify({'error': 'No organisation associated with this account'}), 400
    try:
        resp = http_requests.get(
            f'{WEACE_API_URL}/api/v1/coaching/cohort',
            params={'organizationId': org_slug},
            headers={'Authorization': f'Bearer {g.access_token}'},
            timeout=10,
        )
        if not resp.ok:
            return jsonify({'error': 'Failed to fetch cohorts', 'status': resp.status_code}), resp.status_code
        payload = resp.json()
        cohorts_list = payload.get('data')
        return jsonify({'cohorts': cohorts_list})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/analytics')
@require_weace_token
def analytics():
    if not g.user:
        return jsonify({'error': 'Session not initialised — call /session first'}), 401
    if not _has_role(g.user.get('role'), 'corporate_super_admin', 'weace_super_admin'):
        return jsonify({'error': 'Forbidden'}), 403
    org_slug = g.user.get('org_id', '').strip() or None
    if not org_slug:
        return jsonify({'error': 'No organisation associated with this account'}), 400
    date_from     = request.args.get('date_from',      '').strip() or None
    date_to       = request.args.get('date_to',        '').strip() or None
    gender        = request.args.get('gender',         '').strip() or None
    level_name    = request.args.get('level_name',     '').strip() or None
    cohort_name   = request.args.get('cohort_name',    '').strip() or None
    industry_type = request.args.get('industry_type',  '').strip() or None
    req_org_id   = org_slug
    req_org_name = request.args.get('org_name',    '').strip() or None
    try:
        data = get_org_analytics(org_slug, date_from=date_from, date_to=date_to,
                                 gender=gender, level_name=level_name, cohort_name=cohort_name,
                                 industry_type=industry_type)
        data['org_name'] = req_org_name or g.user.get('org_name', '')
        data['org_id']   = req_org_id   or org_slug
        return jsonify(data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/filters')
@require_weace_token
def dashboard_filters():
    if not g.user:
        return jsonify({'error': 'Session not initialised — call /session first'}), 401
    org_slug = g.user.get('org_id', '').strip() or None
    if not org_slug:
        return jsonify({'error': 'No organisation associated with this account'}), 400
    try:
        data = get_org_filter_options(org_slug)
        return jsonify(data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/sentiment')
@require_weace_token
def sentiment():
    if not g.user:
        return jsonify({'error': 'Session not initialised — call /session first'}), 401
    if not _has_role(g.user.get('role'), 'corporate_super_admin', 'weace_super_admin'):
        return jsonify({'error': 'Forbidden'}), 403
    # weace_super_admin can view any organisation (or all) via org_slug param;
    # corporate admins are always scoped to their own org.
    if _has_role(g.user.get('role'), 'weace_super_admin'):
        req_org = request.args.get('org_slug', '').strip()
        org_slug = None if req_org in ('', 'all', '__all__') else req_org
    else:
        org_slug = g.user.get('org_id', '').strip() or None
        if not org_slug:
            return jsonify({'error': 'No organisation associated with this account'}), 400
    date_from   = request.args.get('date_from',    '').strip() or None
    date_to     = request.args.get('date_to',      '').strip() or None
    gender      = request.args.get('gender',       '').strip() or None
    level_name  = request.args.get('level_name',   '').strip() or None
    cohort_name = request.args.get('cohort_name',  '').strip() or None
    try:
        data = get_org_sentiment_data(org_slug, date_from=date_from, date_to=date_to,
                                      gender=gender, level_name=level_name, cohort_name=cohort_name)
        return jsonify(data or {})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/sentiment/refresh', methods=['POST'])
@require_weace_token
def sentiment_refresh():
    if not g.user:
        return jsonify({'error': 'Session not initialised — call /session first'}), 401
    if not _has_role(g.user.get('role'), 'corporate_super_admin', 'weace_super_admin'):
        return jsonify({'error': 'Forbidden'}), 403
    # weace_super_admin recalculates a specific organisation via org_slug param;
    # aggregate ("all") recalculation is not supported — a single org must be chosen.
    if _has_role(g.user.get('role'), 'weace_super_admin'):
        req_org = (request.args.get('org_slug', '').strip()
                   or (request.json or {}).get('org_slug', '').strip())
        if not req_org or req_org in ('all', '__all__'):
            return jsonify({'error': 'Select a specific organisation to recalculate.'}), 400
        org_slug = req_org
    else:
        org_slug = g.user.get('org_id', '')
        if not org_slug:
            return jsonify({'error': 'No organisation associated with this account'}), 400
    try:
        from sentiment_job import analyze_org_sentiment
        result = analyze_org_sentiment(org_slug)
        if result:
            upsert_org_sentiment(org_slug, result)
            data = get_org_sentiment_data(org_slug)
            return jsonify({'ok': True, 'data': data or result})
        return jsonify({'ok': False, 'error': 'No new messages to analyse or analysis failed'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/my-insights')
def my_insights():
    """Personal insights dashboard — every Nexa user sees their own sentiment."""
    if 'user_id' not in session:
        return redirect('/')
    name = session['user_name']
    initials = ''.join(w[0].upper() for w in name.split()[:2])
    return render_template('my_insights.html',
        user_name=name,
        initials=initials,
        profile_image=session.get('profile_image', ''),
        org_name=session.get('org_name', 'Organisation'),
        refresh_token=session.get('refresh_token', ''),
        # shared header (_header.html)
        active_tab='my-insights',
        show_org_tabs=_has_role(session.get('role'), 'corporate_super_admin'),
        show_admin=_has_role(session.get('role'), 'weace_super_admin'),
    )


@app.route('/api/my-analytics')
@require_weace_token
def my_analytics():
    """The signed-in user's own session/message counts."""
    if not g.user:
        return jsonify({'error': 'Session not initialised — call /session first'}), 401
    try:
        return jsonify(get_user_stats(g.user['user_id']))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/my-sentiment')
@require_weace_token
def my_sentiment():
    """The signed-in user's own sentiment scores, trend, and insights.

    Generates the first report on demand so a user who has chatted enough sees
    something on their first visit rather than an empty dashboard.
    """
    if not g.user:
        return jsonify({'error': 'Session not initialised — call /session first'}), 401
    user_id = g.user['user_id']
    date_from = request.args.get('date_from', '').strip() or None
    date_to   = request.args.get('date_to',   '').strip() or None
    try:
        if get_user_sentiment(user_id) is None:
            from sentiment_job import analyze_user_sentiment
            analyze_user_sentiment(user_id)
        data = get_user_sentiment_data(user_id, date_from=date_from, date_to=date_to)
        return jsonify(data or {})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/my-sentiment/refresh', methods=['POST'])
@require_weace_token
def my_sentiment_refresh():
    if not g.user:
        return jsonify({'error': 'Session not initialised — call /session first'}), 401
    user_id = g.user['user_id']
    try:
        from sentiment_job import analyze_user_sentiment
        result = analyze_user_sentiment(user_id)
        if result:
            data = get_user_sentiment_data(user_id)
            return jsonify({'ok': True, 'data': data or result})
        return jsonify({'ok': False,
                        'error': 'Not enough conversation to analyse yet — keep chatting with Nexa.'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/my-themes')
@require_weace_token
def my_themes():
    """Topics the signed-in user keeps returning to across their sessions.

    Generated on first request so the section is populated on a first visit,
    then served from cache until the user asks for a refresh.
    """
    if not g.user:
        return jsonify({'error': 'Session not initialised — call /session first'}), 401
    user_id = g.user['user_id']
    try:
        report = get_user_insight_report(user_id, 'recurring_themes')
        if report is None:
            from sentiment_job import detect_recurring_themes
            report = detect_recurring_themes(user_id)
        return jsonify(report or {'themes': []})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/my-themes/refresh', methods=['POST'])
@require_weace_token
def my_themes_refresh():
    if not g.user:
        return jsonify({'error': 'Session not initialised — call /session first'}), 401
    try:
        from sentiment_job import detect_recurring_themes, ReportUnavailable
        try:
            report = detect_recurring_themes(g.user['user_id'])
        except ReportUnavailable as e:
            return jsonify({'ok': False, 'error': str(e)}), 503
        if report is not None:
            return jsonify({'ok': True, 'data': report})
        return jsonify({'ok': False,
                        'error': 'Not enough conversation yet to spot recurring themes — keep chatting with Nexa.'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/my-growth')
@require_weace_token
def my_growth():
    """The signed-in user's GROW-aligned Growth Snapshot.

    Same contract as /api/my-themes: generated on first request so the section is
    populated on a first visit, then served from cache until a refresh.
    """
    if not g.user:
        return jsonify({'error': 'Session not initialised — call /session first'}), 401
    user_id = g.user['user_id']
    try:
        report = get_user_insight_report(user_id, 'growth_snapshot')
        if report is None:
            from sentiment_job import generate_growth_snapshot
            report = generate_growth_snapshot(user_id)
        return jsonify(report or {'strengths': [], 'growth_areas': [],
                                  'opportunities': [], 'patterns': []})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/my-growth/refresh', methods=['POST'])
@require_weace_token
def my_growth_refresh():
    if not g.user:
        return jsonify({'error': 'Session not initialised — call /session first'}), 401
    try:
        from sentiment_job import generate_growth_snapshot, ReportUnavailable
        try:
            report = generate_growth_snapshot(g.user['user_id'])
        except ReportUnavailable as e:
            return jsonify({'ok': False, 'error': str(e)}), 503
        if report is not None:
            return jsonify({'ok': True, 'data': report})
        return jsonify({'ok': False,
                        'error': 'Not enough conversation yet to build a snapshot — keep chatting with Nexa.'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/my-digest')
@require_weace_token
def my_digest():
    """A reflection digest over the signed-in user's last seven days."""
    if not g.user:
        return jsonify({'error': 'Session not initialised — call /session first'}), 401
    user_id = g.user['user_id']
    try:
        report = get_user_insight_report(user_id, 'weekly_digest')
        if report is None:
            from sentiment_job import generate_weekly_digest
            report = generate_weekly_digest(user_id)
        return jsonify(report or {})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/my-digest/refresh', methods=['POST'])
@require_weace_token
def my_digest_refresh():
    if not g.user:
        return jsonify({'error': 'Session not initialised — call /session first'}), 401
    try:
        from sentiment_job import generate_weekly_digest, ReportUnavailable
        try:
            report = generate_weekly_digest(g.user['user_id'])
        except ReportUnavailable as e:
            return jsonify({'ok': False, 'error': str(e)}), 503
        if report is not None:
            return jsonify({'ok': True, 'data': report})
        return jsonify({'ok': False,
                        'error': 'Too few messages this week to write a digest — chat with Nexa and try again.'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# Recommendation runs, keyed by user_id. Building a list means one web search per
# resource, which takes far longer than a request should sit open — so the work
# happens on a background thread and the page polls for the result.
_recs_jobs: dict[str, dict] = {}
_recs_jobs_lock = threading.Lock()
_RECS_JOB_STALE_SECS = 300   # a thread that outlives this is assumed dead


def _recs_job_running(user_id: str) -> bool:
    with _recs_jobs_lock:
        job = _recs_jobs.get(user_id)
        if not job or job.get('status') != 'running':
            return False
        if time.time() - job.get('started', 0) > _RECS_JOB_STALE_SECS:
            _recs_jobs.pop(user_id, None)
            return False
        return True


def _run_recs_job(user_id: str):
    from sentiment_job import generate_recommendations, ReportUnavailable
    try:
        report = generate_recommendations(user_id)
        status = 'done' if report else 'empty'
        error = None if report else 'Not enough conversation yet to recommend anything — keep chatting with Nexa.'
    except ReportUnavailable as e:
        status, error = 'empty', str(e)
        print(f"Recommendations unavailable for {user_id}: {e}")
    except Exception as e:
        status, error = 'error', str(e)
        print(f"Recommendation job failed for {user_id}: {e}")
    with _recs_jobs_lock:
        _recs_jobs[user_id] = {'status': status, 'error': error, 'started': time.time()}


@app.route('/api/my-recommendations')
@require_weace_token
def my_recommendations():
    """Articles, books, videos, and lectures picked from the user's own topics.

    Cache-only, unlike the other insight endpoints: this report runs web searches,
    so generating it on a page load would leave the section spinning for as long
    as the searches take. The user asks for it with the button instead.
    """
    if not g.user:
        return jsonify({'error': 'Session not initialised — call /session first'}), 401
    user_id = g.user['user_id']
    try:
        report = get_user_insight_report(user_id, 'recommendations') or {'recommendations': []}
        if _recs_job_running(user_id):
            report['status'] = 'running'
        else:
            with _recs_jobs_lock:
                job = _recs_jobs.get(user_id) or {}
            report['status'] = job.get('status') or 'idle'
            if job.get('error'):
                report['error'] = job['error']
        return jsonify(report)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/my-recommendations/refresh', methods=['POST'])
@require_weace_token
def my_recommendations_refresh():
    """Kick off a run and return straight away — the page polls the GET above."""
    if not g.user:
        return jsonify({'error': 'Session not initialised — call /session first'}), 401
    user_id = g.user['user_id']
    if _recs_job_running(user_id):
        return jsonify({'ok': True, 'status': 'running'})
    with _recs_jobs_lock:
        _recs_jobs[user_id] = {'status': 'running', 'error': None, 'started': time.time()}
    threading.Thread(target=_run_recs_job, args=(user_id,), daemon=True).start()
    return jsonify({'ok': True, 'status': 'running'})


@app.route('/admin')
def admin():
    if 'user_id' not in session:
        return redirect('/')
    if not _has_role(session.get('role'), 'weace_super_admin'):
        return redirect('/')
    name = session['user_name']
    initials = ''.join(w[0].upper() for w in name.split()[:2])
    return render_template('admin.html',
        user_name=name,
        initials=initials,
        profile_image=session.get('profile_image', ''),
    )


@app.route('/api/admin/users')
@require_weace_token
def admin_users():
    if not g.user:
        return jsonify({'error': 'Session not initialised — call /session first'}), 401
    if not _has_role(g.user.get('role'), 'corporate_super_admin', 'weace_super_admin'):
        return jsonify({'error': 'Forbidden'}), 403
    # weace_super_admin can scope to one organisation (or all) via org_slug param.
    org_slug = None
    if _has_role(g.user.get('role'), 'weace_super_admin'):
        req_org = request.args.get('org_slug', '').strip()
        org_slug = None if req_org in ('', 'all', '__all__') else req_org
    try:
        users = get_all_users_admin(org_slug)
        return jsonify({'users': users, 'total': len(users)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _authorise_admin_target(target_id: str):
    """Check the calling admin may act on `target_id`.

    Returns (identity, None) when allowed, or (None, error_response) to return.
    Corporate super admins are limited to users in their own organisation.
    """
    identity = get_user_identity(target_id)
    if not identity:
        return None, (jsonify({'error': 'User not found'}), 404)
    if not _has_role(g.user.get('role'), 'weace_super_admin'):
        admin_org = (g.user.get('org_id') or '').strip()
        if not admin_org or (identity.get('org_id') or '').strip() != admin_org:
            return None, (jsonify({'error': 'Forbidden'}), 403)
    return identity, None


def _linkedin_admin_state(user_id: str) -> dict:
    """What the admin panel shows about a user's LinkedIn enrichment: the URL,
    whether a profile has landed, and enough of it to confirm it's the right
    person."""
    data = _get_linkedin_cached(user_id)
    profile = data.get('profile') or {}
    if user_id in _linkedin_inflight:
        status = 'pending'
    else:
        status = 'ok' if profile else None
    return {
        'configured': linkedin.is_configured(),
        'url': data.get('url'),
        'status': status,
        'headline': (profile.get('headline') or '').strip() or None,
        'name': ' '.join(p for p in [(profile.get('firstName') or '').strip(),
                                     (profile.get('lastName') or '').strip()] if p) or None,
    }


@app.route('/api/admin/user-linkedin/refresh', methods=['POST'])
@require_weace_token
def admin_refresh_linkedin():
    """Force a re-scrape of one user's LinkedIn data.

    LinkedIn is otherwise scraped exactly once per user, so this is how an admin
    picks up a job change or retries a scrape that came back empty.
    """
    if not g.user:
        return jsonify({'error': 'Session not initialised — call /session first'}), 401
    if not _has_role(g.user.get('role'), 'corporate_super_admin', 'weace_super_admin'):
        return jsonify({'error': 'Forbidden'}), 403

    body = request.json or {}
    target_id = (body.get('user_id') or '').strip()
    if not target_id:
        return jsonify({'error': 'user_id is required'}), 400

    identity, error = _authorise_admin_target(target_id)
    if error:
        return error

    if not linkedin.is_configured():
        return jsonify({'error': 'LinkedIn enrichment is not configured (MINDCASE_API_KEY)'}), 503

    # An admin can also correct the URL here, for users whose WeAce profile
    # doesn't carry one or carries the wrong one.
    url = (body.get('linkedin_url') or '').strip()
    if url:
        if not _LINKEDIN_URL_RE.match(url):
            return jsonify({'error': 'Not a valid LinkedIn profile URL'}), 400
        set_user_linkedin_url(target_id, url)
        _linkedin_cache.pop(target_id, None)
    else:
        url = (_get_linkedin_cached(target_id) or {}).get('url')
    if not url:
        return jsonify({'error': 'No LinkedIn URL on file for this user'}), 400

    started = _kickoff_linkedin_scrape(target_id, url, force=True)
    logger.info("[linkedin] admin user_id=%s triggered a refresh for user_id=%s (started=%s)",
                g.user.get('user_id'), target_id, bool(started))
    if not started:
        return jsonify({'error': 'A scrape is already running for this user'}), 409
    return jsonify({'ok': True, 'user_id': target_id, 'linkedin_url': url, 'status': 'pending'})


@app.route('/api/admin/linkedin-backfill', methods=['GET'])
@require_weace_token
def admin_linkedin_backfill_status():
    """Backlog size and last-run result for the LinkedIn enrichment job."""
    if not g.user:
        return jsonify({'error': 'Session not initialised — call /session first'}), 401
    if not _has_role(g.user.get('role'), 'weace_super_admin'):
        return jsonify({'error': 'Forbidden'}), 403
    try:
        from linkedin_job import job_status
        return jsonify(job_status())
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/admin/linkedin-backfill/run', methods=['POST'])
@require_weace_token
def admin_linkedin_backfill_run():
    """Run the LinkedIn backfill now, instead of waiting for the 4-hourly run.

    weace_super_admin only — it works across every organisation. Runs on a
    background thread and returns immediately; the panel polls the GET above for
    the result.
    """
    if not g.user:
        return jsonify({'error': 'Session not initialised — call /session first'}), 401
    if not _has_role(g.user.get('role'), 'weace_super_admin'):
        return jsonify({'error': 'Forbidden'}), 403

    from linkedin_job import is_running, job_status

    if not linkedin.is_configured():
        return jsonify({'error': 'LinkedIn enrichment is not configured (MINDCASE_API_KEY)'}), 503
    if is_running():
        return jsonify({'error': 'A backfill is already running'}), 409

    threading.Thread(
        target=_run_linkedin_backfill,
        kwargs={'trigger': 'manual'},
        name='linkedin-backfill', daemon=True,
    ).start()

    logger.info("[linkedin-job] manual run triggered by user_id=%s", g.user.get('user_id'))
    return jsonify({'ok': True, 'started': True, **job_status(), 'running': True})


@app.route('/api/admin/user-details', methods=['GET', 'POST'])
@require_weace_token
def admin_user_details():
    """Read or update the free-text 'additional details' notes for one user.

    Admin-only: the notes are fed into Nexa's system prompt, so users never write
    their own. Corporate super admins are limited to users in their organisation.
    """
    if not g.user:
        return jsonify({'error': 'Session not initialised — call /session first'}), 401
    if not _has_role(g.user.get('role'), 'corporate_super_admin', 'weace_super_admin'):
        return jsonify({'error': 'Forbidden'}), 403

    if request.method == 'GET':
        target_id = (request.args.get('user_id') or '').strip()
    else:
        target_id = ((request.json or {}).get('user_id') or '').strip()
    if not target_id:
        return jsonify({'error': 'user_id is required'}), 400

    identity, error = _authorise_admin_target(target_id)
    if error:
        return error

    if request.method == 'GET':
        return jsonify({'user_id': target_id,
                        'additional_details': get_user_additional_details(target_id) or '',
                        'linkedin': _linkedin_admin_state(target_id)})

    details = ((request.json or {}).get('additional_details') or '').strip()
    if len(details) > 20000:
        return jsonify({'error': 'Details are too long (20,000 character limit)'}), 400
    set_user_additional_details(target_id, details)
    _user_details_cache.pop(target_id, None)  # reflect the change on the next message
    logger.info("[user_details] user_id=%s updated by admin user_id=%s (%d chars)",
                target_id, g.user.get('user_id'), len(details))
    return jsonify({'ok': True, 'user_id': target_id, 'additional_details': details})


@app.route('/api/admin/orgs')
@require_weace_token
def admin_orgs():
    if not g.user:
        return jsonify({'error': 'Session not initialised — call /session first'}), 401
    if not _has_role(g.user.get('role'), 'weace_super_admin'):
        return jsonify({'error': 'Forbidden'}), 403
    try:
        return jsonify({'orgs': get_all_orgs()})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _first_num(row: dict, *keys):
    """First numeric value among candidate keys (WeAce API casing varies)."""
    for k in keys:
        v = row.get(k)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            return v
        if isinstance(v, str) and v.strip().lstrip('-').isdigit():
            return int(v.strip())
    return None


def _nexa_rows(payload):
    """Pulls the user list out of a WeAce /users response, whatever it's wrapped in."""
    node = payload.get('data', payload) if isinstance(payload, dict) else payload
    if isinstance(node, list):
        return node, (payload if isinstance(payload, dict) else {})
    if isinstance(node, dict):
        for key in ('users', 'items', 'records', 'results', 'data', 'docs'):
            if isinstance(node.get(key), list):
                return node[key], node
    return [], (node if isinstance(node, dict) else {})


@app.route('/api/admin/nexa_counts')
@require_weace_token
def admin_nexa_counts():
    """Aggregate Nexa next/used counts for an organisation, from the WeAce users API."""
    if not g.user:
        return jsonify({'error': 'Session not initialised — call /session first'}), 401
    if not _has_role(g.user.get('role'), 'corporate_super_admin', 'weace_super_admin'):
        return jsonify({'error': 'Forbidden'}), 403

    if _has_role(g.user.get('role'), 'weace_super_admin'):
        req_org = request.args.get('org_slug', '').strip()
        org_id = None if req_org in ('', 'all', '__all__') else req_org
    else:
        org_id = g.user.get('org_id', '').strip() or None

    PAGE_SIZE, MAX_PAGES = 100, 50
    next_total = used_total = 0
    user_total = 0
    page = 1
    try:
        while page <= MAX_PAGES:
            params = {'page': page, 'limit': PAGE_SIZE, 'nexa': 'true', 'roleId': '7,10'}
            if org_id:
                params['organizationId'] = org_id
            resp = http_requests.get(
                f'{WEACE_API_URL}/api/v1/users',
                params=params,
                headers={'Authorization': f'Bearer {g.access_token}'},
                timeout=20,
            )
            if not resp.ok:
                return jsonify({'error': 'Failed to fetch Nexa counts',
                                'status': resp.status_code}), resp.status_code
            rows, container = _nexa_rows(resp.json())

            # If the API already aggregates for us, trust that and stop paging.
            agg_next = _first_num(container, 'totalNextCount', 'totalNexaCount', 'nextCount')
            agg_used = _first_num(container, 'totalUsedCount', 'usedCount')
            if agg_next is not None and agg_used is not None:
                return jsonify({
                    'org_id': org_id,
                    'user_count': _first_num(container, 'total', 'totalCount', 'totalUsers') or len(rows),
                    'next_count': agg_next,
                    'used_count': agg_used,
                })

            for r in rows:
                if not isinstance(r, dict):
                    continue
                nexa = r.get('nexa') if isinstance(r.get('nexa'), dict) else r
                next_total += _first_num(nexa, 'nextCount', 'nexaCount', 'next_count',
                                         'totalCount', 'sessionCount') or 0
                used_total += _first_num(nexa, 'usedCount', 'used_count', 'usedSessions') or 0
            user_total += len(rows)
            if len(rows) < PAGE_SIZE:
                break
            page += 1

        return jsonify({
            'org_id': org_id,
            'user_count': user_total,
            'next_count': next_total,
            'used_count': used_total,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    app.run(debug=True, port=5000)
