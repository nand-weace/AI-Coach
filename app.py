import os
import re
import time
import uuid
import logging
from datetime import date
import requests as http_requests
from functools import wraps
from flask import Flask, request, jsonify, render_template, session, redirect, g
from dotenv import load_dotenv
from ai_coach import SYSTEM_INSTRUCTION
from database import (
    init_db,
    create_chat_session,
    save_message,
    has_previous_sessions,
    get_session_highlights,
    get_user_history,
    get_user_history_before,
    get_org_custom_content,
    upsert_org_custom_content,
    get_org_analytics,
    get_org_filter_options,
    upsert_org_sentiment,
    get_org_sentiment_data,
    upsert_user_login,
    get_user_access_settings,
    get_user_profile_context,
    get_all_users_admin,
    get_all_orgs,
    set_user_access_till,
    get_user_language,
    set_user_language,
)

# Languages Nexa can coach in. Keep in sync with the picker in templates/index.html.
SUPPORTED_LANGUAGES = {
    'English': 'English',
    'Hindi': 'Hindi',
    'Marathi': 'Marathi',
    'Bengali': 'Bengali',
    'Tamil': 'Tamil',
    'Telugu': 'Telugu',
    'Kannada': 'Kannada',
    'Malayalam': 'Malayalam',
    'Gujarati': 'Gujarati',
    'Spanish': 'Spanish',
    'French': 'French',
    'German': 'German',
    'Portuguese': 'Portuguese',
    'Arabic': 'Arabic',
    'Chinese': 'Chinese (Simplified)',
    'Japanese': 'Japanese',
}
DEFAULT_LANGUAGE = 'English'


def _language_directive(language: str | None) -> str:
    """System-prompt addendum instructing Nexa to reply in the chosen language."""
    if not language or language == DEFAULT_LANGUAGE:
        return ''
    label = SUPPORTED_LANGUAGES.get(language, language)
    return (
        f"\n\n---\nLANGUAGE:\n"
        f"Write every reply in {label}, including the suggested follow-ups inside the "
        f"[[SUGGESTIONS]] block. Keep the same warm, informal coaching tone in {label}. "
        f"Do not translate or alter the [[SUGGESTIONS]] tags themselves. "
        f"If the user writes in another language, still reply in {label} unless they ask "
        f"you to switch.\n---"
    )


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

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'dev-secret-change-in-prod')

WEACE_API_URL = os.environ.get("WEACE_API_URL", "https://api.we-ace.com")

AI_PROVIDER = os.environ.get("AI_PROVIDER", "openai").lower()
DEFAULT_MODELS = {"openai": "gpt-4o", "claude": "claude-sonnet-4-6"}
AI_MODEL = os.environ.get("AI_MODEL", DEFAULT_MODELS.get(AI_PROVIDER, "gpt-4o"))

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

# In-memory conversation history keyed by session_uuid
sessions_cache: dict[str, list] = {}

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
    scheduler.start()
    atexit.register(lambda: scheduler.shutdown())
    print("Scheduler started: sentiment at 02:00 UTC.")
    return scheduler


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


def _build_personalized_prompt(user_name: str, highlights: list, profile_context: dict = None) -> str:
    prompt = SYSTEM_INSTRUCTION

    if profile_context:
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
        if parts:
            prompt += (
                f"\n\n---\nUSER PROFILE FOR {user_name.upper()}:\n"
                + "\n".join(parts)
                + "\n---\n"
                "Use this profile to personalise your coaching — tailor advice to their role, "
                "industry, and background. Do not read out this profile to the user unless asked."
            )

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


def _get_or_rebuild_history(session_uuid: str, user_id: str, user_name: str,
                            profile_context: dict = None) -> list:
    if session_uuid in sessions_cache:
        return sessions_cache[session_uuid]
    highlights = get_session_highlights(user_id)
    ctx = profile_context or get_user_profile_context(user_id)
    prompt = _build_personalized_prompt(user_name, highlights, ctx)
    history = [{"role": "system", "content": prompt}]
    sessions_cache[session_uuid] = history
    return history


def _generate_welcome(user_name: str, highlights: list, profile_context: dict = None,
                      returning: bool = False, language: str = None):
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

    system_content = _build_personalized_prompt(user_name, highlights, profile_context)
    system_content += _language_directive(language)
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
    return render_template('index.html', weace_api_url=WEACE_API_URL)


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
    ip = request.remote_addr
    data = request.json or {}
    access_token = g.access_token          # set by @require_weace_token
    refresh_token = (data.get('refreshToken') or '').strip()
    client_user_id = g.user.get('user_id')

    logger.info("[create_session] request from ip=%s has_refresh_token=%s client_user_id=%s",
                ip, bool(refresh_token), client_user_id or 'none')

    # Fetch full user details from profile API
    logger.info("[create_session] fetching profile from we-ace API")
    try:
        user_resp = http_requests.get(
            f'{WEACE_API_URL}/api/v1/users/profile',
            headers={'Authorization': f'Bearer {access_token}'},
            timeout=10,
        )
        logger.info("[create_session] profile API responded with status=%s", user_resp.status_code)
        if not user_resp.ok:
            logger.warning("[create_session] profile API error: status=%s ip=%s",
                           user_resp.status_code, ip)
            return jsonify({'error': 'Failed to fetch user profile'}), user_resp.status_code
        client_user_id = (user_resp.json().get('data', {}).get('_id') or '').strip()
    except Exception as e:
        logger.error("[create_session] profile API request failed: %s", e)
        return jsonify({'error': f'Profile fetch failed: {e}'}), 502
    try:
        profile_resp = http_requests.get(
            f'{WEACE_API_URL}/api/v1/personal-info/{client_user_id}',
            headers={'Authorization': f'Bearer {access_token}'},
            timeout=10,
        )
        logger.info("[create_session] profile API responded with status=%s", profile_resp.status_code)
        if not profile_resp.ok:
            logger.warning("[create_session] profile API error: status=%s ip=%s",
                           profile_resp.status_code, ip)
            return jsonify({'error': 'Failed to fetch user profile'}), profile_resp.status_code
        profile_data = profile_resp.json()
    except Exception as e:
        logger.error("[create_session] profile API request failed: %s", e)
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

    try:
        returning = has_previous_sessions(user_id)
        highlights = get_session_highlights(user_id) if returning else []

        session_uuid = str(uuid.uuid4())
        create_chat_session(session_uuid, user_id, user_name, email, org_name, org_slug, cohort_id)
        logger.info("[create_session] chat session created: session_uuid=%s user_id=%s returning=%s",
                    session_uuid, user_id, returning)

        prompt = _build_personalized_prompt(user_name, highlights, profile_context)
        sessions_cache[session_uuid] = [{"role": "system", "content": prompt}]

        language = get_user_language(user_id) or DEFAULT_LANGUAGE
        welcome_message, welcome_suggestions = _generate_welcome(
            user_name, highlights, profile_context, returning, language)

        # Evict any stale auth_tokens entry for this user (token rotation)
        stale = [t for t, d in auth_tokens.items() if d.get('user_id') == user_id]
        for t in stale:
            auth_tokens.pop(t, None)

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
        nexa_access = (True if _has_role(role, 'weace_super_admin', 'corporate_super_admin')
                       else _fetch_nexa_access(g.access_token))
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
        if access_last_date:
            remaining_days = max(0, (date.fromisoformat(access_last_date.split()[0]) - date.today()).days)
        else:
            remaining_days = None

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
            'remaining_days': remaining_days,
        }

        logger.info("[create_session] session established: user_id=%s user_name=%r nexa_access=%s",
                    user_id, user_name, nexa_access)

        initials = ''.join(w[0].upper() for w in user_name.split()[:2])
        recent_messages = [
            {'id': r['id'], 'role': r['role'], 'content': r['content'],
             'created_at': r['created_at'].isoformat() if r.get('created_at') else None}
            for r in get_user_history(user_id, 15)
        ]
        return jsonify({
            'user_id': user_id,
            'user_name': user_name,
            'session_uuid': session_uuid,
            'returning': returning,
            'initials': initials,
            'profile_image': profile_image,
            'role': role,
            'nexa_access': nexa_access,
            'access_last_date': access_last_date,
            'remaining_days': remaining_days,
            'recent_messages': recent_messages,
            'welcome_message': welcome_message,
            'welcome_suggestions': welcome_suggestions,
            'language': language,
            'profile_context': profile_context,
            'org_name': org_name,
            'org_id': org_slug,
            'cohort_id': cohort_id,
        })
    except Exception as e:
        logger.exception("[create_session] unexpected error for user_id=%s: %s", user_id, e)
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
        welcome_message, welcome_suggestions = _generate_welcome(
            user_name, highlights, profile_context, returning, language)

        role = g.user.get('role', [])
        if _has_role(role, 'weace_super_admin', 'corporate_super_admin'):
            nexa_access = True
            access_last_date = None
            remaining_days = None
        else:
            settings = get_user_access_settings(user_id)
            # Resolved at login by the user-config API; cached per token, then in the DB.
            token_state = auth_tokens.get(g.access_token) or {}
            nexa_access = token_state.get('nexa_access', settings['nexa_access'])
            access_last_date = settings['access_last_date']
            if access_last_date:
                remaining_days = max(0, (date.fromisoformat(access_last_date.split()[0]) - date.today()).days)
            else:
                remaining_days = None

        auth_tokens[g.access_token]['session_uuid'] = session_uuid
        auth_tokens[g.access_token]['nexa_access'] = nexa_access
        auth_tokens[g.access_token]['access_last_date'] = access_last_date
        auth_tokens[g.access_token]['remaining_days'] = remaining_days

        initials = ''.join(w[0].upper() for w in user_name.split()[:2])
        recent_messages = [
            {'id': r['id'], 'role': r['role'], 'content': r['content'],
             'created_at': r['created_at'].isoformat() if r.get('created_at') else None}
            for r in get_user_history(user_id, 10)
        ]
        return jsonify({
            'user_name': user_name,
            'returning': returning,
            'initials': initials,
            'profile_image': g.user.get('profile_image', ''),
            'role': role,
            'nexa_access': nexa_access,
            'access_last_date': access_last_date,
            'remaining_days': remaining_days,
            'recent_messages': recent_messages,
            'welcome_message': welcome_message,
            'welcome_suggestions': welcome_suggestions,
            'language': language,
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

    return render_template('index.html', weace_api_url=WEACE_API_URL)


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
    messages = [
        {'id': r['id'], 'role': r['role'], 'content': r['content'],
         'created_at': r['created_at'].isoformat() if r.get('created_at') else None}
        for r in rows[-PAGE:]
    ]
    return jsonify({'messages': messages, 'has_more': has_more})


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
        'languages': [{'code': c, 'label': l} for c, l in SUPPORTED_LANGUAGES.items()],
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
            'languages': [{'code': c, 'label': l} for c, l in SUPPORTED_LANGUAGES.items()],
        })

    language = ((request.json or {}).get('language') or '').strip()
    if language not in SUPPORTED_LANGUAGES:
        return jsonify({'error': 'Unsupported language'}), 400
    set_user_language(user_id, language)
    logger.info("[language] user_id=%s set language=%s", user_id, language)
    return jsonify({'ok': True, 'language': language})


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
    profile_context = data.get('profile_context') or {}
    
    conversation_history = _get_or_rebuild_history(
        session_uuid, user_id, user_name, profile_context
    )

    # The cached system prompt is language-neutral; the directive is applied per
    # request so a mid-session language switch takes effect immediately.
    language = (data.get('language') or '').strip()
    if language not in SUPPORTED_LANGUAGES:
        language = get_user_language(user_id) or DEFAULT_LANGUAGE

    try:
        conversation_history.append({"role": "user", "content": user_message})
        save_message(session_uuid, user_id, 'user', user_message)

        system_content = next(
            (m["content"] for m in conversation_history if m["role"] == "system"),
            SYSTEM_INSTRUCTION,
        ) + _org_content_directive(g.user.get('org_id')) + _language_directive(language)

        if AI_PROVIDER == "claude":
            api_messages = [m for m in conversation_history if m["role"] != "system"]
            response = client.messages.create(
                model=AI_MODEL,
                max_tokens=1024,
                system=system_content,
                messages=api_messages,
            )
            reply = response.content[0].text
        else:
            response = client.chat.completions.create(
                model=AI_MODEL,
                messages=[{"role": "system", "content": system_content}]
                         + [m for m in conversation_history if m["role"] != "system"],
                temperature=0.7,
            )
            reply = response.choices[0].message.content

        reply, suggestions = _extract_suggestions(reply)

        conversation_history.append({"role": "assistant", "content": reply})
        save_message(session_uuid, user_id, 'assistant', reply)
        return jsonify({'response': reply, 'suggestions': suggestions})

    except Exception as e:
        if conversation_history and conversation_history[-1]["role"] == "user":
            conversation_history.pop()
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


@app.route('/api/admin/users/<user_id>/access_till', methods=['PUT'])
@require_weace_token
def admin_set_access_till(user_id):
    if not g.user:
        return jsonify({'error': 'Session not initialised — call /session first'}), 401
    if not _has_role(g.user.get('role'), 'corporate_super_admin', 'weace_super_admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.json or {}
    access_till = (data.get('access_till') or '').strip()
    if not access_till:
        return jsonify({'error': 'access_till date is required'}), 400
    try:
        set_user_access_till(user_id, access_till)
        return jsonify({'ok': True, 'access_till': access_till})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    app.run(debug=True, port=5000)
