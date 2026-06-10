import os
import uuid
import logging
import requests as http_requests
from flask import Flask, request, jsonify, render_template, session, redirect
from dotenv import load_dotenv
from ai_coach import SYSTEM_INSTRUCTION
from database import (
    init_db,
    create_chat_session,
    save_message,
    has_previous_sessions,
    get_session_highlights,
    get_user_history,
    get_org_analytics,
    get_org_filter_options,
    upsert_org_sentiment,
    get_org_sentiment_data,
    upsert_user_login,
    get_user_nexa_access,
    get_user_access_settings,
    get_user_profile_context,
    set_user_nexa_access,
    get_all_users_admin,
    disable_inactive_users,
    set_user_access_till,
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
    scheduler.add_job(
        lambda: print(f"Nexa expiry: disabled {disable_inactive_users(30)} user(s)."),
        'cron', hour=3, minute=0, id='nexa_access_expiry',
    )
    scheduler.start()
    atexit.register(lambda: scheduler.shutdown())
    print("Scheduler started: sentiment at 02:00 UTC, Nexa access expiry at 03:00 UTC.")
    return scheduler


if os.environ.get('ENABLE_SCHEDULER', 'true').lower() == 'true':
    # In Flask debug mode the reloader spawns a child process; only start scheduler there.
    if not app.debug or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        try:
            _scheduler = _start_scheduler()
        except Exception as e:
            print(f"Scheduler failed to start: {e}")


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


@app.route('/')
def index():
    return render_template('index.html', weace_api_url=WEACE_API_URL)


@app.route('/me')
def me():
    if 'user_id' not in session:
        return jsonify({'logged_in': False})
    name = session['user_name']
    initials = ''.join(w[0].upper() for w in name.split()[:2])
    return jsonify({
        'logged_in': True,
        'user_name': name,
        'initials': initials,
        'profile_image': session.get('profile_image', ''),
        'role': session.get('role', 'user'),
        'nexa_access': session.get('nexa_access', True),
        'access_last_date': session.get('access_last_date'),
    })


@app.route('/session', methods=['POST'])
def create_session():
    """
    Called by the frontend after authentication. Receives accessToken + refreshToken,
    verifies them by fetching the user profile from the we-ace API, then sets up the
    DB chat session and Flask cookie.
    """
    ip = request.remote_addr
    data = request.json or {}
    access_token = (data.get('accessToken') or '').strip()
    refresh_token = (data.get('refreshToken') or '').strip()
    client_user_id = (data.get('userId') or '').strip()

    logger.info("[create_session] request from ip=%s has_access_token=%s has_refresh_token=%s client_user_id=%s",
                ip, bool(access_token), bool(refresh_token), client_user_id or 'none')

    if not access_token:
        logger.warning("[create_session] rejected: missing accessToken from ip=%s", ip)
        return jsonify({'error': 'accessToken is required'}), 400

    # Verify token and fetch user details from profile API
    logger.info("[create_session] fetching profile from we-ace API")
    try:
        profile_resp = http_requests.get(
            f'{WEACE_API_URL}/api/v1/personal-info/{client_user_id}',
            headers={'Authorization': f'Bearer {access_token}'},
            timeout=10,
        )
        logger.info("[create_session] profile API responded with status=%s", profile_resp.status_code)

        if not profile_resp.ok:
            logger.warning("[create_session] profile API rejected token: status=%s ip=%s",
                           profile_resp.status_code, ip)
            return jsonify({'error': 'Invalid or expired access token'}), 401
        profile_data = profile_resp.json()
    except Exception as e:
        logger.error("[create_session] profile API request failed: %s", e)
        return jsonify({'error': f'Profile fetch failed: {e}'}), 502

    # Parse profile response — user data is nested under 'data'
    profile = profile_data.get('data') or {}

    raw_roles = profile.get('roles') or []
    roles_arr = raw_roles if isinstance(raw_roles, list) else [raw_roles]
    role_slugs = [r.get('slug', '') for r in roles_arr if isinstance(r, dict)]

    # Fallback: use roles forwarded from the frontend auth response
    if not role_slugs:
        client_roles = data.get('roles') or []
        if isinstance(client_roles, list):
            role_slugs = [r.get('slug', '') for r in client_roles if isinstance(r, dict)]

    if 'weace_super_admin' in role_slugs:
        role = 'weace_super_admin'
    elif 'corporate_super_admin' in role_slugs:
        role = 'corporate_super_admin'
    else:
        role = 'user'

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

        # Super admins always have Nexa access; regular users are controlled by the flag
        if role in ('weace_super_admin', 'corporate_super_admin'):
            nexa_access = True
            access_last_date = None
            upsert_user_login(
                user_id,
                first_name=first, last_name=last, email=email,
                org_id=org_slug, org_name=org_name, cohort_id=cohort_id,
                cohort_name=cohort_name, country=country,
                level_name=level_name, gender=gender,
                functional_areas=functional_areas, industry_types=industry_types,
            )
        else:
            settings = upsert_user_login(
                user_id,
                first_name=first, last_name=last, email=email,
                org_id=org_slug, org_name=org_name, cohort_id=cohort_id,
                cohort_name=cohort_name, country=country,
                level_name=level_name, gender=gender,
                functional_areas=functional_areas, industry_types=industry_types,
            )
            nexa_access = settings['nexa_access']
            access_last_date = settings['access_last_date']
        session['nexa_access'] = nexa_access
        session['access_last_date'] = access_last_date

        logger.info("[create_session] session established: user_id=%s user_name=%r nexa_access=%s",
                    user_id, user_name, nexa_access)

        initials = ''.join(w[0].upper() for w in user_name.split()[:2])
        recent_messages = [
            {'role': r['role'], 'content': r['content']}
            for r in get_user_history(user_id, 15)
        ]
        return jsonify({
            'user_id': user_id,
            'user_name': user_name,
            'returning': returning,
            'initials': initials,
            'profile_image': profile_image,
            'role': role,
            'nexa_access': nexa_access,
            'access_last_date': access_last_date,
            'recent_messages': recent_messages,
        })
    except Exception as e:
        logger.exception("[create_session] unexpected error for user_id=%s: %s", user_id, e)
        return jsonify({'error': str(e)}), 500


@app.route('/new-session', methods=['POST'])
def new_session():
    """
    Called on every page load when the user is already logged in.
    Discards the previous in-memory history and starts a fresh chat session,
    injecting session highlights from past conversations as AI context.
    """
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    # Free the old in-memory history
    old_uuid = session.get('session_uuid')
    if old_uuid and old_uuid in sessions_cache:
        del sessions_cache[old_uuid]

    user_id = session['user_id']
    user_name = session['user_name']
    email = session.get('email', '')

    try:
        returning = has_previous_sessions(user_id)
        highlights = get_session_highlights(user_id) if returning else []

        session_uuid = str(uuid.uuid4())
        create_chat_session(session_uuid, user_id, user_name, email,
                            session.get('org_name'), session.get('org_slug'), session.get('cohort_id'))

        profile_context = session.get('profile_context') or {}
        prompt = _build_personalized_prompt(user_name, highlights, profile_context)
        sessions_cache[session_uuid] = [{"role": "system", "content": prompt}]
        session['session_uuid'] = session_uuid

        role = session.get('role', 'user')
        if role in ('weace_super_admin', 'corporate_super_admin'):
            nexa_access = True
            access_last_date = None
        else:
            settings = get_user_access_settings(user_id)
            nexa_access = settings['nexa_access']
            access_last_date = settings['access_last_date']
        session['nexa_access'] = nexa_access
        session['access_last_date'] = access_last_date

        initials = ''.join(w[0].upper() for w in user_name.split()[:2])
        recent_messages = [
            {'role': r['role'], 'content': r['content']}
            for r in get_user_history(user_id, 10)
        ]
        return jsonify({
            'user_name': user_name,
            'returning': returning,
            'initials': initials,
            'profile_image': session.get('profile_image', ''),
            'role': role,
            'nexa_access': nexa_access,
            'access_last_date': access_last_date,
            'recent_messages': recent_messages,
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


@app.route('/logout', methods=['POST'])
def logout():
    session_uuid = session.get('session_uuid')
    if session_uuid and session_uuid in sessions_cache:
        del sessions_cache[session_uuid]
    session.clear()
    return jsonify({'ok': True})


@app.route('/chat', methods=['POST'])
def chat():
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    if not session.get('nexa_access', False):
        return jsonify({'error': 'nexa_access_denied'}), 403
    if not client:
        return jsonify({'error': 'AI Coach is not initialised. Check your API key.'}), 500

    data = request.json or {}
    user_message = (data.get('message') or '').strip()
    if not user_message:
        return jsonify({'error': 'Message is required'}), 400

    user_id = session['user_id']
    user_name = session['user_name']
    session_uuid = session['session_uuid']

    conversation_history = _get_or_rebuild_history(
        session_uuid, user_id, user_name, session.get('profile_context')
    )

    try:
        conversation_history.append({"role": "user", "content": user_message})
        save_message(session_uuid, user_id, 'user', user_message)

        if AI_PROVIDER == "claude":
            system_content = next(
                (m["content"] for m in conversation_history if m["role"] == "system"),
                SYSTEM_INSTRUCTION,
            )
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
                messages=conversation_history,
                temperature=0.7,
            )
            reply = response.choices[0].message.content

        conversation_history.append({"role": "assistant", "content": reply})
        save_message(session_uuid, user_id, 'assistant', reply)
        return jsonify({'response': reply})

    except Exception as e:
        if conversation_history and conversation_history[-1]["role"] == "user":
            conversation_history.pop()
        return jsonify({'error': str(e)}), 500


@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect('/')
    if session.get('role') != 'corporate_super_admin':
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
def cohorts():
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    if session.get('role') != 'corporate_super_admin':
        return jsonify({'error': 'Forbidden'}), 403
    org_slug = session.get('org_slug', '')
    access_token = session.get('access_token', '')
    if not org_slug:
        return jsonify({'error': 'No organisation associated with this account'}), 400
    try:
        resp = http_requests.get(
            f'{WEACE_API_URL}/api/v1/coaching/cohort',
            params={'organizationId': org_slug},
            headers={'Authorization': f'Bearer {access_token}'},
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
def analytics():
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    if session.get('role') != 'corporate_super_admin':
        return jsonify({'error': 'Forbidden'}), 403
    org_slug = session.get('org_slug', '')
    if not org_slug:
        return jsonify({'error': 'No organisation associated with this account'}), 400
    date_from   = request.args.get('date_from',    '').strip() or None
    date_to     = request.args.get('date_to',      '').strip() or None
    gender      = request.args.get('gender',       '').strip() or None
    level_name  = request.args.get('level_name',   '').strip() or None
    cohort_name = request.args.get('cohort_name',  '').strip() or None
    try:
        data = get_org_analytics(org_slug, date_from=date_from, date_to=date_to,
                                 gender=gender, level_name=level_name, cohort_name=cohort_name)
        data['org_name'] = session.get('org_name', '')
        return jsonify(data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/filters')
def dashboard_filters():
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    if session.get('role') != 'corporate_super_admin':
        return jsonify({'error': 'Forbidden'}), 403
    org_slug = session.get('org_slug', '')
    if not org_slug:
        return jsonify({'error': 'No organisation associated with this account'}), 400
    try:
        data = get_org_filter_options(org_slug)
        return jsonify(data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/sentiment')
def sentiment():
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    if session.get('role') != 'corporate_super_admin':
        return jsonify({'error': 'Forbidden'}), 403
    org_slug = session.get('org_slug', '')
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
def sentiment_refresh():
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    if session.get('role') != 'corporate_super_admin':
        return jsonify({'error': 'Forbidden'}), 403
    org_slug = session.get('org_slug', '')
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
    if session.get('role') != 'weace_super_admin':
        return redirect('/')
    name = session['user_name']
    initials = ''.join(w[0].upper() for w in name.split()[:2])
    return render_template('admin.html',
        user_name=name,
        initials=initials,
        profile_image=session.get('profile_image', ''),
    )


@app.route('/api/admin/users')
def admin_users():
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    if session.get('role') != 'weace_super_admin':
        return jsonify({'error': 'Forbidden'}), 403
    try:
        users = get_all_users_admin()
        return jsonify({'users': users, 'total': len(users)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/admin/users/<user_id>/access_till', methods=['PUT'])
def admin_set_access_till(user_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    if session.get('role') != 'weace_super_admin':
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


@app.route('/api/admin/users/<user_id>/nexa_access', methods=['PUT'])
def admin_set_nexa_access(user_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    if session.get('role') != 'weace_super_admin':
        return jsonify({'error': 'Forbidden'}), 403
    data = request.json or {}
    value = bool(data.get('nexa_access', False))
    try:
        set_user_nexa_access(user_id, value)
        return jsonify({'ok': True, 'nexa_access': value})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    app.run(debug=True, port=5000)
