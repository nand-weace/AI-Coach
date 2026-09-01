// Thumb icons for the message feedback row. Module scope on purpose: replaying
// history calls into this from the auth chain, before the rest of the
// DOMContentLoaded body has run.
const THUMB_UP_PATH = 'M2 20h2.5V10H2v10zM22 11c0-1.1-.9-2-2-2h-5.6l.9-4.1v-.3c0-.4-.2-.8-.4-1.1L13.8 2 7.6 8.2c-.4.4-.6.9-.6 1.4V19c0 1.1.9 2 2 2h8.5c.8 0 1.5-.5 1.8-1.2l2.6-6.1c.1-.2.1-.5.1-.7v-2z';
const THUMB_DOWN_PATH = 'M22 4h-2.5v10H22V4zM2 13c0 1.1.9 2 2 2h5.6l-.9 4.1v.3c0 .4.2.8.4 1.1l1.1 1.1 6.2-6.2c.4-.4.6-.9.6-1.4V5c0-1.1-.9-2-2-2H6.5c-.8 0-1.5.5-1.8 1.2l-2.6 6.1c-.1.2-.1.5-.1.7v2z';

document.addEventListener('DOMContentLoaded', async () => {
    const chatForm = document.getElementById('chat-form');
    const userInput = document.getElementById('user-input');
    const chatContainer = document.getElementById('chat-container');

    // ── Infinite scroll state (older history paged in as the user scrolls up) ──
    let oldestMessageId = null;   // id of the topmost loaded message
    let hasMoreHistory = false;   // whether older messages may still exist
    let loadingHistory = false;   // guards against overlapping fetches
    let historyAnchor = null;     // node above which older messages are inserted

    // ── Voice state ──────────────────────────────────────────────────────────
    // Declared up here, not down with the voice code, for the same reason as the
    // thumb icons above: showApp() runs from inside the auth chain and speaks
    // the welcome message, long before the bottom of this file has executed.
    // Locales are shared — dictation listens in the coaching language, and Nexa
    // replies in it.
    const SPEECH_LOCALES = {
        English: 'en-US', Hindi: 'hi-IN', Marathi: 'mr-IN', Bengali: 'bn-IN',
        Tamil: 'ta-IN', Telugu: 'te-IN', Kannada: 'kn-IN', Malayalam: 'ml-IN',
        Gujarati: 'gu-IN', Spanish: 'es-ES', French: 'fr-FR', German: 'de-DE',
        Portuguese: 'pt-BR', Arabic: 'ar-SA', Chinese: 'zh-CN', Japanese: 'ja-JP',
    };
    const ttsSupported = 'speechSynthesis' in window && 'SpeechSynthesisUtterance' in window;
    let autoSpeak = ttsSupported && localStorage.getItem('we_ace_autospeak') === '1';
    let speakingButton = null;    // the play button currently lit up, if any
    // Chrome fills the voice list asynchronously — read it now and refresh when
    // it lands, so a reply that arrives early still gets the right accent.
    let voices = ttsSupported ? window.speechSynthesis.getVoices() : [];
    if (ttsSupported) {
        window.speechSynthesis.addEventListener('voiceschanged', () => {
            voices = window.speechSynthesis.getVoices();
        });
    }

    // Nexa is voiced female. The Web Speech API exposes no gender field, so the
    // only signal is the voice name: either an explicit label ("Google UK
    // English Female") or a known female voice shipped by the platform. Names
    // below cover macOS/iOS (Samantha, Karen…), Windows/Edge (Zira, Aria…),
    // Android/Chrome OS and the common Indian-language voices.
    const FEMALE_VOICE_NAMES = [
        'samantha', 'karen', 'moira', 'tessa', 'fiona', 'victoria', 'allison',
        'ava', 'susan', 'vicki', 'kathy', 'nicky', 'serena', 'veena',
        'lekha', 'kanya', 'zira', 'aria', 'jenny', 'michelle', 'hazel',
        'linda', 'heera', 'kalpana', 'swara', 'neerja', 'catherine', 'eva',
        'amelie', 'anna', 'alice', 'monica', 'paulina', 'luciana', 'joana',
        'ting-ting', 'sin-ji', 'kyoko', 'yuna', 'mariska', 'zuzana', 'milena',
        // Chrome's unlabelled default for US English is a female voice; without
        // it a US listener gets pushed onto "Google UK English Female" instead.
        'google us english',
    ];

    // Voice quality varies enormously within one browser. The compact/legacy
    // engines are the buzzy, robotic ones; the enhanced, neural and network
    // voices sound close to a real person. Nothing in the API reports this
    // either, so again it comes down to the name — plus `localService`, which
    // is false for Google's (much better) network voices.
    const GOOD_VOICE_HINTS = [
        ['premium', 45], ['enhanced', 45], ['neural', 45], ['natural', 40],
        ['siri', 35], ['online', 25], ['wavenet', 45], ['google', 15],
    ];
    // macOS ships a set of joke voices that are unusable for coaching.
    const NOVELTY_VOICES = [
        'albert', 'bad news', 'bahh', 'bells', 'boing', 'bubbles', 'cellos',
        'deranged', 'good news', 'jester', 'organ', 'superstar', 'trinoids',
        'whisper', 'wobble', 'zarvox', 'hysterical', 'pipe organ', 'ralph',
    ];

    const SPEAKER_ICON =
        '<svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true" fill="none" ' +
        'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">' +
        '<polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/>' +
        '<path d="M15.54 8.46a5 5 0 0 1 0 7.07"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/></svg>';

    if (typeof marked !== 'undefined') {
        marked.setOptions({ breaks: true, gfm: true });
    }

    // ── Token persistence helpers ────────────────────────────────────────────
    function storeTokens(accessToken, refreshToken) {
        if (accessToken) localStorage.setItem('we_ace_access_token', accessToken);
        if (refreshToken) localStorage.setItem('we_ace_refresh_token', refreshToken);
    }
    function clearTokens() {
        localStorage.removeItem('we_ace_access_token');
        localStorage.removeItem('we_ace_refresh_token');
        localStorage.removeItem('we_ace_user_id');
        localStorage.removeItem('we_ace_user_name');
        localStorage.removeItem('we_ace_session_uuid');
        localStorage.removeItem('we_ace_profile_context');
        localStorage.removeItem('we_ace_profile_roles');
        localStorage.removeItem('we_ace_session_token');
        localStorage.removeItem('we_ace_org_id');
        localStorage.removeItem('we_ace_org_name');
        localStorage.removeItem('we_ace_cohort_id');
        localStorage.removeItem('we_ace_language');
        localStorage.removeItem('we_ace_mode');
    }
    function storeProfileRoles(roles) {
        if (roles && Array.isArray(roles) && roles.length > 0)
            localStorage.setItem('we_ace_profile_roles', JSON.stringify(roles));
    }
    function getStoredRoles() {
        try { return JSON.parse(localStorage.getItem('we_ace_profile_roles') || 'null'); } catch { return null; }
    }
    function extractAndStoreUserId(apiData) {
        const uid = apiData?.profileDetails?._id;
        if (uid) localStorage.setItem('we_ace_user_id', uid);
    }

    // ── Login progress stepper ──────────────────────────────────────────────
    // /session does several slow things server-side (profile API, personal-info
    // API, history lookup, LLM welcome generation). We can't stream those, so we
    // walk the user through the stages on a timer and hold on the last one until
    // the request actually resolves.
    const LOGIN_STEPS = [
        { label: 'Signing you in',              hold: 1200 },
        { label: 'Verifying your credentials',  hold: 1800 },
        { label: 'Fetching your profile',       hold: 2600 },
        { label: 'Analysing your past sessions', hold: 3500 },
        { label: 'Personalising your coaching space', hold: 0 },
    ];

    const loginProgress = (() => {
        let timer = null;
        let index = 0;

        const CHECK_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
            'stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round">' +
            '<polyline points="20 6 9 17 4 12"/></svg>';

        function render() {
            const list = document.getElementById('login-steps');
            if (!list) return;
            // Build once, then only restate — rebuilding would replay the
            // entry animation for every row on each advance.
            if (list.childElementCount !== LOGIN_STEPS.length) {
                list.innerHTML = '';
                LOGIN_STEPS.forEach((step, i) => {
                    const li = document.createElement('li');
                    li.className = 'login-step';
                    li.style.setProperty('--step-delay', (i * 70) + 'ms');
                    li.innerHTML =
                        '<span class="login-step-marker"><span class="login-step-dot"></span></span>' +
                        '<span class="login-step-label">' + step.label + '</span>';
                    list.appendChild(li);
                });
            }

            Array.from(list.children).forEach((li, i) => {
                const state = i < index ? 'done' : (i === index ? 'active' : 'pending');
                if (li.dataset.state === state) return;
                li.dataset.state = state;
                li.className = 'login-step ' + state;
                li.firstElementChild.innerHTML = state === 'done'
                    ? CHECK_SVG
                    : '<span class="login-step-dot"></span>';
            });

            // Rail + bar fill: sit halfway into the running step.
            const frac = (index + 0.5) / LOGIN_STEPS.length;
            list.style.setProperty('--rail-fill', frac.toFixed(3));
            const fill = document.getElementById('login-progress-fill');
            if (fill) fill.style.width = (frac * 100).toFixed(1) + '%';
        }

        function schedule() {
            const hold = LOGIN_STEPS[index].hold;
            if (!hold) return;                               // last step: hold here
            timer = setTimeout(() => {
                index = Math.min(index + 1, LOGIN_STEPS.length - 1);
                render();
                schedule();
            }, hold);
        }

        return {
            start(from = 0) {
                this.stop();
                index = from;
                const loader = document.getElementById('login-loader');
                const status = document.getElementById('login-status');
                const form = document.getElementById('login-form');
                if (form) form.style.display = 'none';
                if (loader) loader.style.display = 'flex';
                if (status) status.textContent = 'Setting up Nexa for you';
                render();
                schedule();
            },
            // Jump straight to a step (e.g. auth returned — login is confirmed).
            advanceTo(i) {
                if (i <= index) return;
                clearTimeout(timer);
                index = Math.min(i, LOGIN_STEPS.length - 1);
                render();
                schedule();
            },
            stop() {
                clearTimeout(timer);
                timer = null;
            },
        };
    })();

    // Keeps the browser's copy of the session in step with the server's.
    function persistSessionData(data) {
        if (data.user_id) localStorage.setItem('we_ace_user_id', data.user_id);
        if (data.user_name) localStorage.setItem('we_ace_user_name', data.user_name);
        if (data.session_uuid) localStorage.setItem('we_ace_session_uuid', data.session_uuid);
        if (data.profile_context) localStorage.setItem('we_ace_profile_context', JSON.stringify(data.profile_context));
        if (data.org_id) localStorage.setItem('we_ace_org_id', data.org_id);
        if (data.org_name) localStorage.setItem('we_ace_org_name', data.org_name);
        if (data.cohort_id) localStorage.setItem('we_ace_cohort_id', data.cohort_id);
        if (data.language) localStorage.setItem('we_ace_language', data.language);
        if (data.mode) localStorage.setItem('we_ace_mode', data.mode);
        if (data.role) storeProfileRoles(data.role);
    }

    // Did this page load come from clicking a tab inside the app? header.js
    // leaves a marker in sessionStorage (per browser tab, survives the
    // navigation) just before it hands over. Read once and clear, so only the
    // load that the click caused counts — a later refresh sees nothing and
    // starts a new session.
    function consumeTabNavIntent() {
        let stamp = null;
        try {
            stamp = sessionStorage.getItem('we_ace_tab_nav');
            sessionStorage.removeItem('we_ace_tab_nav');
        } catch (_) { return false; }
        if (!stamp) return false;
        // Belt and braces: a reload is never a tab switch, whatever is stored.
        const nav = performance.getEntriesByType('navigation')[0];
        if (nav && nav.type === 'reload') return false;
        return Date.now() - Number(stamp) < 5 * 60 * 1000;
    }

    // ── Resume — fast path back from another tab (dashboard, admin) ─────────
    // Carries on with the chat session this browser already had: no token
    // refresh, no profile fetch, no new greeting. Returns null when there's
    // nothing to resume, and the normal login chain takes over.
    async function resumeSession() {
        const accessToken = localStorage.getItem('we_ace_access_token') || '';
        const sessionUuid = localStorage.getItem('we_ace_session_uuid') || '';
        if (!accessToken || !sessionUuid) return null;
        try {
            const res = await fetch('/session/resume', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'Authorization': `Bearer ${accessToken}`,
                },
                body: JSON.stringify({
                    sessionUuid,
                    refreshToken: localStorage.getItem('we_ace_refresh_token') || '',
                }),
            });
            if (!res.ok) return null;      // 409 = not resumable, 401 = token stale
            const data = await res.json();
            persistSessionData(data);
            return data;
        } catch (_) {
            return null;
        }
    }

    // ── Build session — server verifies tokens and fetches profile ────
    // /session is expensive (profile API, history, LLM welcome) and opens a new
    // conversation, so a page load gets exactly one attempt. The steps below
    // fall through to each other; without this flag a failed attempt in Step 2
    // would be retried by Step 3 with an older token, hitting /session twice.
    let _sessionAttempted = false;
    async function buildSession(accessToken, refreshToken, profileRoles) {
        if (_sessionAttempted) return null;
        _sessionAttempted = true;
        const userId = localStorage.getItem('we_ace_user_id') || '';
        const payload = { refreshToken };
        if (userId) payload.userId = userId;
        // No sessionUuid on purpose: reaching /session means a refresh, a fresh
        // visit or a new login, and each of those opens a new conversation.
        // Carrying one on is /session/resume's job.
        if (profileRoles && Array.isArray(profileRoles) && profileRoles.length > 0) {
            payload.roles = profileRoles;
        }
        loginProgress.advanceTo(2);   // profile + history + welcome happen inside /session
        const res = await fetch('/session', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${accessToken}`,
            },
            body: JSON.stringify(payload),
        });
        if (!res.ok) return null;
        const data = await res.json();
        console.log('Session established:', data);
        persistSessionData(data);
        return data;
    }

    // ── Coaching / mentoring toggle ─────────────────────────────────────────
    // Coaching is non-directive — Nexa draws the answer out of the user.
    // Mentoring is directive — Nexa gives advice from experience. The choice is
    // stored per user and sent with every message, so flipping it mid-session
    // changes the very next reply rather than waiting for a new conversation.
    const COACH_MODES = {
        coaching: {
            hint: 'Nexa Coach probes, reflects, and helps you discover your own solution',
            note: 'Switched to Coaching — Nexa will probe and reflect rather than giving direct advice',
        },
        mentoring: {
            hint: 'Nexa Mentor brings lived experience and direct advice when you need it.',
            note: 'Switched to Mentoring — Nexa brings lived experience and direct advice when you need it.',
        },
    };
    const modeToggle = document.getElementById('mode-toggle');
    const modeHint = document.getElementById('mode-hint');

    function currentMode() {
        const stored = localStorage.getItem('we_ace_mode');
        return COACH_MODES[stored] ? stored : 'coaching';
    }

    function renderMode(mode) {
        if (!modeToggle) return;
        modeToggle.dataset.mode = mode;
        modeToggle.querySelectorAll('.mode-option').forEach(btn => {
            const on = btn.dataset.mode === mode;
            btn.classList.toggle('is-active', on);
            btn.setAttribute('aria-checked', on ? 'true' : 'false');
        });
        if (modeHint) modeHint.textContent = COACH_MODES[mode].hint;
    }

    // Marks the point in the transcript where the user changed their mind, so
    // the shift in Nexa's tone doesn't read as the AI going off the rails.
    function addModeSwitchNote(mode) {
        const note = document.createElement('div');
        note.className = 'mode-switch-note';
        note.textContent = COACH_MODES[mode].note;
        chatContainer.appendChild(note);
        scrollToBottom();
    }

    async function setMode(mode) {
        if (!COACH_MODES[mode] || mode === currentMode()) return;
        localStorage.setItem('we_ace_mode', mode);
        renderMode(mode);
        addModeSwitchNote(mode);
        try {
            const accessToken = localStorage.getItem('we_ace_access_token') || '';
            await fetch('/mode', {
                method: 'POST',
                credentials: 'omit',
                headers: {
                    'Content-Type': 'application/json',
                    ...(accessToken ? { 'Authorization': `Bearer ${accessToken}` } : {}),
                },
                body: JSON.stringify({ mode }),
            });
        } catch (_) {
            // Saving the preference failed, but the mode still rides along with
            // every /chat call — this session behaves as chosen regardless.
        }
    }

    if (modeToggle) {
        modeToggle.addEventListener('click', (e) => {
            const btn = e.target.closest('.mode-option');
            if (btn) setMode(btn.dataset.mode);
        });
        renderMode(currentMode());
    }

    // ── Auth Step 1a: ?refresh_token= in URL — exchange → store → profile → session
    const _qp = new URLSearchParams(window.location.search);
    const _urlRefreshToken = _qp.get('refresh_token');
    const _extToken = _qp.get('access_token');
    let _authed = false;

    // ── Auth Step 0: resume the chat session this browser was already in ────
    // Only when the user got here by clicking a tab (Talk ⇄ Sentiment Analysis
    // ⇄ Admin) — those are full page loads, and without this every hop would
    // re-login and open a fresh conversation. A refresh, a fresh visit, or
    // tokens in the URL (a platform entry, possibly as a different user) all
    // fall through to the normal chain and start a new session.
    if (!_urlRefreshToken && !_extToken && consumeTabNavIntent()) {
        const _resumeStatus = document.getElementById('login-status');
        if (_resumeStatus) _resumeStatus.textContent = 'Picking up where you left off';
        const _rd = await resumeSession();
        if (_rd) {
            showApp(_rd);
            _authed = true;
        }
    }

    // Any stored/incoming credential means we're about to run the slow auth
    // chain — show the stepper instead of a bare spinner.
    if (!_authed && (_urlRefreshToken || _extToken ||
        localStorage.getItem('we_ace_refresh_token') ||
        localStorage.getItem('we_ace_access_token'))) {
        loginProgress.start(1);   // credentials already in hand
    }

    if (_urlRefreshToken && !_extToken) {
        history.replaceState({}, '', '/'); // strip token from URL immediately
        try {
            const _rrRes = await fetch(`${window.WEACE_API_URL}/api/v1/auth/refresh`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ refreshToken: _urlRefreshToken }),
            });
            if (_rrRes.ok) {
                const _rrData = await _rrRes.json();
                const _newAccess = (_rrData.accessToken || _rrData.access_token || '').trim();
                const _newRefresh = (_rrData.refreshToken || _rrData.refresh_token || _urlRefreshToken).trim();
                if (_newAccess) {
                    extractAndStoreUserId(_rrData);
                    storeTokens(_newAccess, _newRefresh);
                    const _roles = _rrData.profileDetails?.roles || getStoredRoles();
                    const _sd = await buildSession(_newAccess, _newRefresh, _roles);
                    if (_sd) {
                        showApp(_sd);
                        _authed = true;
                    }
                }
            } else if (_rrRes.status === 401 || _rrRes.status === 403) {
                clearTokens();
                await fetch('/logout', { method: 'POST' }).catch(() => {});
                showLoginForm();
                return;
            }
        } catch (_) {}
    }

    // ── Auth Step 1b: ?access_token= in URL (external platform redirect) ─────
    if (!_authed && _extToken) {
        history.replaceState({}, '', '/'); // strip tokens from URL immediately
        const _extRefresh = _qp.get('refresh_token') || '';
        try {
            const _sd = await buildSession(_extToken, _extRefresh, getStoredRoles());
            if (_sd) {
                storeTokens(_extToken, _extRefresh);
                showApp(_sd);
                _authed = true;
            }
        } catch (_) {}
    }

    // ── Auth Step 2: localStorage refresh token (primary persistent auth) ────
    if (!_authed) {
        const storedRefresh = localStorage.getItem('we_ace_refresh_token');
        if (storedRefresh) {
            try {
                const refreshRes = await fetch(`${window.WEACE_API_URL}/api/v1/auth/refresh`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ refreshToken: storedRefresh }),
                });
                if (refreshRes.ok) {
                    const refreshData = await refreshRes.json();
                    extractAndStoreUserId(refreshData);
                    const newAccess = (refreshData.accessToken || refreshData.access_token || '').trim();
                    const newRefresh = (refreshData.refreshToken || refreshData.refresh_token || storedRefresh).trim();
                    if (newAccess) {
                        const roles = refreshData.profileDetails?.roles || getStoredRoles();
                        const sd = await buildSession(newAccess, newRefresh, roles);
                        if (sd) {
                            storeTokens(newAccess, newRefresh);
                            showApp(sd);
                            _authed = true;
                        }
                    }
                } else if (refreshRes.status === 401 || refreshRes.status === 403) {
                    // Token revoked or expired — fully log out and stop
                    clearTokens();
                    await fetch('/logout', { method: 'POST' }).catch(() => {});
                    showLoginForm();
                    return;
                } else {
                    // Other error (network hiccup, 5xx) — clear tokens, fall through
                    clearTokens();
                }
            } catch (_) {}
        }
    }

    // ── Auth Step 3: stored access token fallback (page reload) ─────────────
    if (!_authed) {
        const storedAccess = localStorage.getItem('we_ace_access_token');
        const storedRefresh = localStorage.getItem('we_ace_refresh_token');
        if (storedAccess) {
            try {
                const sd = await buildSession(storedAccess, storedRefresh || '', getStoredRoles());
                if (sd) {
                    showApp(sd);
                    _authed = true;
                }
            } catch (_) {}
        }
    }

    if (!_authed) {
        showLoginForm();
    }

    // Password visibility toggle
    document.getElementById('toggle-password').addEventListener('click', () => {
        const input = document.getElementById('login-password');
        const eyeOn = document.getElementById('eye-icon');
        const eyeOff = document.getElementById('eye-off-icon');
        if (input.type === 'password') {
            input.type = 'text';
            eyeOn.style.display = 'none';
            eyeOff.style.display = 'inline';
        } else {
            input.type = 'password';
            eyeOn.style.display = 'inline';
            eyeOff.style.display = 'none';
        }
    });

    // Login
    document.getElementById('login-form').addEventListener('submit', async (e) => {
        e.preventDefault();
        const email = document.getElementById('login-email').value.trim();
        const password = document.getElementById('login-password').value;
        const errorEl = document.getElementById('login-error');
        const btn = document.getElementById('login-button');

        const resetBtn = () => {
            loginProgress.stop();
            showLoginForm();
            btn.disabled = false;
            btn.innerHTML = 'Sign In';
        };
        btn.disabled = true;
        btn.innerHTML = '<span class="btn-spinner"></span>Signing in…';
        errorEl.textContent = '';
        loginProgress.start(0);

        // Step 1: authenticate directly against the we-ace auth API
        const AUTH_URL_MAP = {
            'admin@wit.com': `${window.WEACE_API_URL}/api/v1/admin-auth/login`,
        };
        const authUrl = AUTH_URL_MAP[email] || `${window.WEACE_API_URL}/api/v1/auth/login`;

        let authData;
        try {
            const authRes = await fetch(authUrl, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ email, password }),
            });
            authData = await authRes.json();
            if (!authRes.ok) {
                errorEl.textContent = authData.message || authData.error || 'Invalid email or password';
                resetBtn();
                return;
            }
        } catch (err) {
            errorEl.textContent = 'Authentication service unavailable. Please try again.';
            resetBtn();
            return;
        }

        // Store tokens and user ID from login response immediately
        loginProgress.advanceTo(1);   // credentials accepted
        storeTokens(authData.accessToken, authData.refreshToken);
        extractAndStoreUserId(authData);
        storeProfileRoles(authData.profileDetails?.roles);

        // Step 2: establish our backend session — server fetches and verifies the profile
        // A manual sign-in is a fresh start: it gets its own attempt even if the
        // page-load chain already spent one and fell through to the login form.
        _sessionAttempted = false;
        try {
            const data = await buildSession(authData.accessToken, authData.refreshToken, authData.profileDetails?.roles);
            if (!data) {
                errorEl.textContent = 'Session error. Please try again.';
                resetBtn();
                return;
            }
            showApp(data);
        } catch (err) {
            errorEl.textContent = 'Connection error. Please try again.';
            resetBtn();
        }
    });

    // Header — tabs, language picker, chip dropdown, Corporate Knowledge modal
    // all live in header.js, shared with the dashboard.
    NexaHeader.init({
        onTalk: () => userInput.focus(),          // already on the chat page
        onLogout: () => { clearTokens(); location.reload(); },
    });

    // Auto-resize textarea
    function autoResizeInput() {
        userInput.style.height = 'auto';
        userInput.style.height = userInput.scrollHeight + 'px';
        userInput.style.overflowY = userInput.scrollHeight > 160 ? 'auto' : 'hidden';
    }
    userInput.addEventListener('input', autoResizeInput);

    // Enter submits; Shift+Enter inserts newline
    userInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            chatForm.dispatchEvent(new Event('submit', { cancelable: true }));
        }
    });

    // ── Voice input (browser-native speech recognition) ──────────────────────
    // Dictation runs in the coaching language (SPEECH_LOCALES, declared at the
    // top), so speaking Hindi/Tamil/etc. is transcribed correctly instead of
    // being forced through en-US.
    const micButton = document.getElementById('mic-button');
    const SpeechRecognitionCtor = window.SpeechRecognition || window.webkitSpeechRecognition;
    let recognition = null;      // active recogniser, null when idle
    let dictationBase = '';      // text already in the box when dictation started
    const DEFAULT_PLACEHOLDER = userInput.placeholder;

    // Hands-free: three seconds of silence after something has been said ends
    // dictation and sends the message, so speaking to Nexa needs no tap.
    const AUTO_SEND_SILENCE_MS = 3000;
    let silenceTimer = null;
    let autoSendOnEnd = false;

    function clearSilenceTimer() {
        clearTimeout(silenceTimer);
        silenceTimer = null;
    }

    function armSilenceTimer() {
        clearSilenceTimer();
        silenceTimer = setTimeout(() => {
            silenceTimer = null;
            // nothing transcribed yet — keep the mic open rather than sending air
            if (!recognition || !userInput.value.trim()) return;
            autoSendOnEnd = true;
            recognition.stop();   // onend submits once the mic is actually closed
        }, AUTO_SEND_SILENCE_MS);
    }

    function showVoiceHint(text) {
        let hint = document.getElementById('voice-hint');
        if (!hint) {
            hint = document.createElement('div');
            hint.id = 'voice-hint';
            hint.className = 'voice-hint';
            chatForm.parentNode.insertBefore(hint, chatForm);
        }
        hint.textContent = text;
        clearTimeout(hint._timer);
        hint._timer = setTimeout(() => hint.remove(), 5000);
    }

    function stopDictation() {
        // a deliberate stop never auto-sends — the user keeps the draft
        clearSilenceTimer();
        autoSendOnEnd = false;
        if (recognition) {
            recognition.stop();   // onend does the UI cleanup
        }
    }

    function startDictation() {
        if (userInput.disabled || recognition) return;
        stopSpeaking();   // don't let Nexa talk over the mic she's opening

        const language = localStorage.getItem('we_ace_language') || 'English';
        const rec = new SpeechRecognitionCtor();
        rec.lang = SPEECH_LOCALES[language] || 'en-US';
        rec.continuous = true;      // keep listening through natural pauses
        rec.interimResults = true;  // stream words into the box as they're spoken

        // Anything already typed is kept; speech is appended after it.
        dictationBase = userInput.value.trim();

        rec.onstart = () => {
            micButton.classList.add('recording');
            micButton.setAttribute('aria-pressed', 'true');
            micButton.title = 'Stop listening';
            userInput.classList.add('dictating');
            userInput.placeholder = 'Listening… speak now';
        };

        rec.onresult = (event) => {
            let finalText = '';
            let interimText = '';
            for (let i = event.resultIndex; i < event.results.length; i++) {
                const chunk = event.results[i][0].transcript;
                if (event.results[i].isFinal) finalText += chunk;
                else interimText += chunk;
            }
            if (finalText) {
                dictationBase = (dictationBase ? dictationBase + ' ' : '') + finalText.trim();
            }
            const parts = [dictationBase, interimText.trim()].filter(Boolean);
            userInput.value = parts.join(' ');
            autoResizeInput();
            armSilenceTimer();   // every word heard restarts the 3s countdown
        };

        rec.onerror = (event) => {
            clearSilenceTimer();
            autoSendOnEnd = false;
            if (event.error === 'not-allowed' || event.error === 'service-not-allowed') {
                showVoiceHint('Microphone access is blocked. Enable it in your browser settings to use voice.');
            } else if (event.error === 'no-speech') {
                showVoiceHint("Didn't catch that — tap the mic and try again.");
            } else if (event.error !== 'aborted') {
                showVoiceHint('Voice input stopped unexpectedly. Please try again.');
            }
        };

        rec.onend = () => {
            clearSilenceTimer();
            recognition = null;
            micButton.classList.remove('recording');
            micButton.setAttribute('aria-pressed', 'false');
            micButton.title = 'Speak your message';
            userInput.classList.remove('dictating');
            userInput.placeholder = DEFAULT_PLACEHOLDER;
            userInput.focus();

            if (autoSendOnEnd) {
                autoSendOnEnd = false;
                if (!userInput.disabled && userInput.value.trim()) {
                    chatForm.dispatchEvent(new Event('submit', { cancelable: true }));
                }
            }
        };

        try {
            rec.start();
            recognition = rec;
        } catch (err) {
            console.error('[voice] start failed:', err);
            showVoiceHint('Could not start voice input. Please try again.');
        }
    }

    // Typing while the mic is open also counts as activity, so an edit in
    // progress is never cut off mid-word by the auto-send.
    userInput.addEventListener('input', () => {
        if (recognition && silenceTimer) armSilenceTimer();
    });

    if (SpeechRecognitionCtor && micButton) {
        micButton.style.display = 'flex';
        micButton.addEventListener('click', () => {
            if (recognition) stopDictation();
            else startDictation();
        });
    }

    // ── Voice output (browser-native speech synthesis) ───────────────────────
    // Nexa reads her replies aloud through the browser's own TTS engine — no
    // API, no cost. The toggle beside the mic turns it on for every reply and
    // is remembered per browser; each reply also carries its own play button
    // for listening to just that one. State and constants (ttsSupported,
    // autoSpeak, voices, the voice-name tables, SPEAKER_ICON) live at the top of
    // this file: showApp builds and speaks the welcome message from inside the
    // auth chain, long before this point is reached, and a `const` read before
    // its declaration throws rather than reading as undefined.
    const speakToggle = document.getElementById('speak-toggle');

    function speechLocale() {
        const language = localStorage.getItem('we_ace_language') || 'English';
        return SPEECH_LOCALES[language] || 'en-US';
    }

    function isFemaleVoice(voice) {
        const name = (voice.name || '').toLowerCase();
        if (/\bfemale\b/.test(name)) return true;
        if (/\bmale\b/.test(name)) return false;   // "…Male" must never match below
        return FEMALE_VOICE_NAMES.some(n => name.includes(n));
    }

    function isNoveltyVoice(voice) {
        const name = (voice.name || '').toLowerCase();
        return NOVELTY_VOICES.some(n => name.includes(n));
    }

    function voiceQuality(voice) {
        const name = (voice.name || '').toLowerCase();
        let score = 0;
        GOOD_VOICE_HINTS.forEach(([hint, points]) => {
            if (name.includes(hint)) score += points;
        });
        if (name.includes('compact')) score -= 40;   // macOS low-bitrate variants
        if (name.includes('espeak')) score -= 60;    // the classic robot
        if (name.includes('desktop')) score -= 25;   // legacy Microsoft SAPI voices
        if (voice.localService === false) score += 20;
        return score;
    }

    // Best voice for the coaching language, ranked on language match first,
    // then female, then audio quality. Language outranks everything — a male or
    // plainer voice in the right language reads far better than a polished one
    // in the wrong one. Returns null when nothing matches, which leaves the
    // browser to pick its own default.
    function pickVoice(locale) {
        if (!voices.length) return null;
        const target = locale.toLowerCase();
        const base = target.split('-')[0];
        const langOf = v => (v.lang || '').replace('_', '-').toLowerCase();

        const scored = voices.map(v => {
            if (isNoveltyVoice(v)) return null;      // never, in any language
            const lang = langOf(v);
            let score;
            if (lang === target) score = 1000;
            else if (lang === base || lang.startsWith(base + '-')) score = 500;
            else return null;                        // wrong language — never used
            if (isFemaleVoice(v)) score += 100;
            return { voice: v, score: score + voiceQuality(v) };
        }).filter(Boolean);

        if (!scored.length) return null;
        scored.sort((a, b) => b.score - a.score);
        return scored[0].voice;
    }

    // Replies are markdown, and markdown read literally sounds like punctuation
    // soup — keep the words, drop the syntax.
    function speakableText(text) {
        return String(text || '')
            .replace(/```[\s\S]*?```/g, ' ')
            .replace(/`([^`]*)`/g, '$1')
            .replace(/!\[[^\]]*\]\([^)]*\)/g, ' ')
            .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
            .replace(/^\s{0,3}#{1,6}\s+/gm, '')
            .replace(/^\s{0,3}>\s?/gm, '')
            .replace(/^\s*[-*+]\s+/gm, '')
            .replace(/\*\*|__|~~/g, '')
            .replace(/(^|\s)[*_]([^*_]+)[*_](?=\s|$|[.,!?])/g, '$1$2')
            .replace(/\|/g, ' ')
            .replace(/\s+/g, ' ')
            .trim();
    }

    // Chrome cuts an utterance off after roughly 15 seconds, so anything long
    // is queued as sentence-sized pieces rather than one blob. A single
    // sentence over the limit is left whole — splitting mid-clause sounds worse.
    //
    // Every boundary is an audible seam: the engine re-primes, which is heard as
    // a click or a beat of silence. So the limit is set as high as the 15-second
    // ceiling allows (~320 characters at normal speaking pace) and short replies
    // stay a single utterance with no seams at all.
    function chunkForSpeech(text, limit = 320) {
        const sentences = text.match(/[^.!?…]+[.!?…]*\s*/g) || [text];
        const chunks = [];
        let current = '';
        sentences.forEach(sentence => {
            if (current && (current + sentence).length > limit) {
                chunks.push(current.trim());
                current = '';
            }
            current += sentence;
        });
        if (current.trim()) chunks.push(current.trim());
        return chunks;
    }

    function markSpeaking(button) {
        speakingButton = button || null;
        if (!button) return;
        button.classList.add('speaking');
        button.setAttribute('aria-pressed', 'true');
        button.title = 'Stop reading';
    }

    function clearSpeaking() {
        if (speakingButton) {
            speakingButton.classList.remove('speaking');
            speakingButton.setAttribute('aria-pressed', 'false');
            speakingButton.title = 'Read this aloud';
        }
        speakingButton = null;
    }

    function stopSpeaking() {
        if (!ttsSupported) return;
        window.speechSynthesis.cancel();
        clearSpeaking();
    }

    // Speak `text`, replacing anything already being read. `button` is the play
    // button to light up while it runs (optional).
    function speak(text, button) {
        if (!ttsSupported) return;
        const content = speakableText(text);
        stopSpeaking();
        if (!content) return;

        const locale = speechLocale();
        const voice = pickVoice(locale);
        const chunks = chunkForSpeech(content);
        markSpeaking(button);

        chunks.forEach((chunk, i) => {
            const utterance = new SpeechSynthesisUtterance(chunk);
            utterance.lang = locale;
            if (voice) utterance.voice = voice;
            // Left at the engine's natural settings on purpose. Off-default rate
            // and pitch are resampled by the local voices, and that resampling
            // is where the buzzy, metallic edge comes from.
            utterance.rate = 1;
            utterance.pitch = 1;
            utterance.volume = 1;
            if (i === chunks.length - 1) utterance.onend = clearSpeaking;
            utterance.onerror = clearSpeaking;
            window.speechSynthesis.speak(utterance);
        });
    }

    // Play button shown alongside the thumbs on a Nexa reply.
    function makeSpeakButton(text) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'feedback-btn speak-btn';
        btn.title = 'Read this aloud';
        btn.setAttribute('aria-label', 'Read this aloud');
        btn.setAttribute('aria-pressed', 'false');
        btn.innerHTML = SPEAKER_ICON;
        btn.addEventListener('click', () => {
            if (speakingButton === btn) stopSpeaking();
            else speak(text, btn);
        });
        return btn;
    }

    function renderSpeakToggle() {
        if (!speakToggle) return;
        speakToggle.classList.toggle('is-on', autoSpeak);
        speakToggle.setAttribute('aria-pressed', autoSpeak ? 'true' : 'false');
        const label = autoSpeak ? 'Nexa reads replies aloud — tap to mute'
                                : 'Have Nexa read replies aloud';
        speakToggle.title = label;
        speakToggle.setAttribute('aria-label', label);
        const on = speakToggle.querySelector('.speaker-on');
        const off = speakToggle.querySelector('.speaker-off');
        if (on) on.style.display = autoSpeak ? '' : 'none';
        if (off) off.style.display = autoSpeak ? 'none' : '';
    }

    if (ttsSupported && speakToggle) {
        speakToggle.style.display = 'flex';
        renderSpeakToggle();
        speakToggle.addEventListener('click', () => {
            autoSpeak = !autoSpeak;
            localStorage.setItem('we_ace_autospeak', autoSpeak ? '1' : '0');
            renderSpeakToggle();
            if (autoSpeak) showVoiceHint('Nexa will read her replies aloud.');
            else stopSpeaking();
        });
    }

    // Speech keeps playing after the tab is left behind in some browsers.
    window.addEventListener('pagehide', stopSpeaking);

    // Retries only network-level failures (e.g. "Failed to fetch"), not HTTP error responses
    async function fetchWithRetry(url, options, retries = 4, delayMs = 800) {
        for (let attempt = 0; ; attempt++) {
            try {
                return await fetch(url, options);
            } catch (err) {
                if (attempt >= retries) throw err;
                await new Promise((r) => setTimeout(r, delayMs * (attempt + 1)));
            }
        }
    }

    // Chat
    chatForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        stopDictation();  // sending ends the dictation turn
        stopSpeaking();   // and cuts off whatever Nexa was still reading
        const message = userInput.value.trim();
        if (!message) return;

        clearSuggestions();
        addMessage(message, 'user');
        userInput.value = '';
        userInput.style.height = 'auto';
        userInput.style.overflowY = 'hidden';

        const typingId = showTypingIndicator();

        try {
            const accessToken = localStorage.getItem('we_ace_access_token') || '';
            const res = await fetchWithRetry('/chat', {
                method: 'POST',
                credentials: 'omit',
                headers: {
                    'Content-Type': 'application/json',
                    ...(accessToken ? { 'Authorization': `Bearer ${accessToken}` } : {}),
                },
                body: JSON.stringify({
                    message,
                    user_id: localStorage.getItem('we_ace_user_id') || '',
                    user_name: localStorage.getItem('we_ace_user_name') || '',
                    session_uuid: localStorage.getItem('we_ace_session_uuid') || '',
                    profile_context: JSON.parse(localStorage.getItem('we_ace_profile_context') || 'null'),
                    language: localStorage.getItem('we_ace_language') || 'English',
                    mode: currentMode(),
                }),
            });
            const data = await res.json();
            removeElement(typingId);

            if (res.status === 401) {
                location.reload();
            } else if (res.ok) {
                const replyEl = addMessage(data.response, 'assistant', null,
                    { messageId: data.message_id });
                if (autoSpeak) speak(data.response, replyEl._speakButton);
                renderSuggestions(data.suggestions);
            } else {
                addMessage(`Error: ${data.error}`, 'assistant');
            }
        } catch (err) {
            removeElement(typingId);
            addMessage(`Connection error: ${err.message}`, 'assistant');
        }
    });

    function showApp(sessionData) {
        const {
            user_name: userName,
            initials,
            profile_image: profileImage,
            returning,
            role,
            nexa_access: nexaAccess,
            welcome_message: welcomeMessage,
            welcome_suggestions: welcomeSuggestions,
            // Same chat session as before the page load (came back from another
            // page) — pick up where the conversation left off.
            resumed,
        } = sessionData;
        const recentMessages = sessionData.recent_messages || [];

        // A WeAce super admin manages the platform rather than using the
        // coaching product, so hand them straight to /admin instead of painting
        // the chat UI. This is the fresh-login path — the server does the same
        // on '/' once it has a session cookie to read the role from.
        // replace(), not href: Back must not bounce them into the chat page.
        if (NexaHeader.hasRole(role, 'weace_super_admin')) {
            window.location.replace('/admin');
            return;
        }

        loginProgress.stop();
        document.getElementById('login-overlay').style.display = 'none';
        const appEl = document.getElementById('app-container');
        appEl.style.opacity = '0';
        appEl.style.display = 'flex';
        appEl.offsetHeight; // force reflow
        appEl.style.transition = 'opacity 0.22s ease';
        appEl.style.opacity = '1';
        document.getElementById('user-display').textContent = userName;
        NexaHeader.renderLanguageMenu();   // reflect the preference restored by /session
        renderMode(currentMode());         // ditto for the coaching/mentoring toggle
        window._userInitials = initials;
        window._profileImage = profileImage || '';

        // Header avatar
        const headerAvatar = document.getElementById('header-avatar');
        if (profileImage) {
            const img = document.createElement('img');
            img.src = profileImage;
            img.alt = userName;
            img.className = 'avatar-img';
            img.onerror = () => { img.replaceWith(makeInitialsSpan(initials)); };
            headerAvatar.appendChild(img);
        } else {
            headerAvatar.appendChild(makeInitialsSpan(initials));
        }

        // Settles the tab bar for this user's roles — reveals Nexa Insights /
        // Knowledge Base, or collapses the bar to Admin for a WeAce super admin.
        NexaHeader.applyRoles(role);
        NexaHeader.setNexaAccess(nexaAccess);

        const welcomeEl = document.getElementById('welcome-text');
        const welcomeWrapper = welcomeEl.closest('.message-wrapper');

        // Resuming: no fresh greeting and no "previous conversation" break —
        // the replayed messages are this same, still-running conversation.
        if (resumed) {
            if (welcomeWrapper) welcomeWrapper.remove();
        } else if (welcomeMessage && welcomeMessage.trim()) {
            welcomeEl.textContent = welcomeMessage.trim();
        } else if (returning) {
            welcomeEl.textContent = `Welcome back, ${userName}. Ready to continue your leadership journey? What's on your mind today?`;
        } else {
            welcomeEl.textContent = `Welcome to your Executive Leadership Coaching Session, ${userName}. I'm Nexa, here to help you navigate complex professional challenges, enhance your leadership skills, and drive strategic impact. What would you like to focus on today?`;
        }

        // The opener gets the same play button as any other reply, and is read
        // aloud straight away when auto-speak is on. Browsers that require a
        // user gesture before audio simply stay silent until the first reply.
        const welcomeBody = welcomeEl.parentNode;
        if (!resumed && ttsSupported && welcomeBody &&
                welcomeBody.classList.contains('message-body')) {
            const actions = document.createElement('div');
            actions.className = 'message-feedback';
            const speakBtn = makeSpeakButton(welcomeEl.textContent);
            actions.appendChild(speakBtn);
            welcomeBody.appendChild(actions);
            if (autoSpeak) speak(welcomeEl.textContent, speakBtn);
        }

        if (recentMessages.length > 0) {
            let anchor = null;
            if (!resumed) {
                anchor = document.createElement('div');
                anchor.className = 'history-divider';
                anchor.innerHTML = '<span>Previous conversation</span>';
                chatContainer.appendChild(anchor);
            }
            recentMessages.forEach(msg => addMessage(msg.content, msg.role, msg.created_at,
                { messageId: msg.id, feedback: msg.feedback }));

            // Enable infinite scroll: remember the oldest loaded id so scrolling
            // up can page in earlier messages, inserted above this anchor.
            const ids = recentMessages.map(m => m.id).filter(Number.isFinite);
            if (ids.length) {
                oldestMessageId = Math.min(...ids);
                hasMoreHistory = true;
                historyAnchor = anchor || chatContainer.firstElementChild;
            }
            // Welcome bubble is static markup at the top of the container; move it
            // below the replayed history so it opens the new session rather than
            // sitting above the fold once we scroll to the bottom.
            if (welcomeWrapper && !resumed) chatContainer.appendChild(welcomeWrapper);
            scrollToBottom();
        }

        // Nudge the user to start the engagement with tappable starter prompts
        if (nexaAccess && Array.isArray(welcomeSuggestions) && welcomeSuggestions.length > 0) {
            renderSuggestions(welcomeSuggestions);
        }

        if (!nexaAccess) {
            showNexaAccessDenied();
        }

        consumeFocusIntent();
    }

    // ── Focus session — arrived from a Growth Area on My Insights ─────────────
    // The insights page parks {label, message} in sessionStorage and sends the
    // browser here. Read it once (so a refresh doesn't re-ask the same thing)
    // and open the conversation on that area instead of a blank prompt.
    function consumeFocusIntent() {
        let raw = null;
        try {
            raw = sessionStorage.getItem('we_ace_focus_session');
            sessionStorage.removeItem('we_ace_focus_session');
        } catch (_) { return; }
        if (!raw) return;

        let focus;
        try { focus = JSON.parse(raw); } catch (_) { return; }
        if (!focus || !focus.message || userInput.disabled) return;

        // After the greeting has painted, so the opener reads as the reply to it.
        setTimeout(() => sendMessage(focus.message), 350);
    }

    function showLoginForm() {
        loginProgress.stop();
        document.getElementById('login-loader').style.display = 'none';
        document.getElementById('login-form').style.display = 'block';
        document.getElementById('login-error').style.display = 'block';
    }

    function showNexaAccessDenied() {
        const expiryNotice = document.getElementById('access-expiry-notice');
        const expiryText = document.getElementById('access-expiry-text');
        expiryText.textContent = 'Your access has been expired, please contact your corporate Admin.';
        expiryNotice.style.display = 'block';
        const input = document.getElementById('user-input');
        input.disabled = true;
        input.placeholder = 'Access expired';
        document.getElementById('send-button').disabled = true;
        const mic = document.getElementById('mic-button');
        if (mic) mic.disabled = true;
        const modeRow = document.querySelector('.mode-row');
        if (modeRow) modeRow.style.display = 'none';
    }

    function makeInitialsSpan(initials) {
        const span = document.createElement('span');
        span.textContent = initials || 'U';
        return span;
    }

    // Format a message timestamp as e.g. "24 Jul, 3:45 PM" (today shows time only).
    function formatMessageTime(value) {
        const d = value ? new Date(value) : new Date();
        if (isNaN(d.getTime())) return '';
        const time = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
        const now = new Date();
        const sameDay = d.toDateString() === now.toDateString();
        if (sameDay) return time;
        const date = d.toLocaleDateString([], { day: 'numeric', month: 'short' });
        return `${date}, ${time}`;
    }


    function makeThumbButton(rating, active) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'feedback-btn' + (active ? ' active' : '');
        btn.dataset.rating = String(rating);
        const label = rating === 1 ? 'Good response' : 'Bad response';
        btn.title = label;
        btn.setAttribute('aria-label', label);
        btn.setAttribute('aria-pressed', active ? 'true' : 'false');
        btn.innerHTML =
            `<svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true">` +
            `<path d="${rating === 1 ? THUMB_UP_PATH : THUMB_DOWN_PATH}"/></svg>`;
        return btn;
    }

    // Thumbs up/down row shown under each of Nexa's replies. `current` is the
    // rating already stored for this message (1, -1, or 0 for none).
    function buildFeedbackRow(messageId, current) {
        const row = document.createElement('div');
        row.className = 'message-feedback';
        let rating = current || 0;

        [1, -1].forEach(value => {
            const btn = makeThumbButton(value, rating === value);
            btn.addEventListener('click', async () => {
                // Tapping the active thumb clears the rating.
                const next = rating === value ? 0 : value;
                const previous = rating;
                rating = next;
                paint();
                const saved = await submitFeedback(messageId, next);
                if (!saved) {
                    rating = previous;
                    paint();
                } else if (next !== 0) {
                    row.classList.add('thanks');
                }
            });
            row.appendChild(btn);
        });

        const note = document.createElement('span');
        note.className = 'feedback-thanks';
        note.textContent = 'Thanks for the feedback';
        row.appendChild(note);

        function paint() {
            row.querySelectorAll('.feedback-btn').forEach(b => {
                const on = Number(b.dataset.rating) === rating;
                b.classList.toggle('active', on);
                b.setAttribute('aria-pressed', on ? 'true' : 'false');
            });
            if (!rating) row.classList.remove('thanks');
        }

        if (rating) row.classList.add('thanks');
        return row;
    }

    // Persist a rating. Returns false so the caller can roll the UI back.
    async function submitFeedback(messageId, rating) {
        try {
            const accessToken = localStorage.getItem('we_ace_access_token') || '';
            const res = await fetch('/feedback', {
                method: 'POST',
                credentials: 'omit',
                headers: {
                    'Content-Type': 'application/json',
                    ...(accessToken ? { 'Authorization': `Bearer ${accessToken}` } : {}),
                },
                body: JSON.stringify({ message_id: messageId, rating }),
            });
            return res.ok;
        } catch (err) {
            return false;
        }
    }

    function buildMessage(text, sender, timestamp, opts = {}) {
        const wrapper = document.createElement('div');
        wrapper.className = `message-wrapper ${sender}`;

        const avatar = document.createElement('div');
        avatar.className = `avatar ${sender}-avatar`;

        if (sender === 'user' && window._profileImage) {
            const img = document.createElement('img');
            img.src = window._profileImage;
            img.alt = window._userInitials || 'U';
            img.className = 'avatar-img';
            img.onerror = () => { avatar.textContent = window._userInitials || 'U'; };
            avatar.appendChild(img);
        } else {
            avatar.textContent = sender === 'user' ? (window._userInitials || 'U') : 'NX';
        }

        const messageDiv = document.createElement('div');
        messageDiv.className = `message ${sender}-message`;

        if (sender === 'assistant' && typeof marked !== 'undefined') {
            messageDiv.innerHTML = marked.parse(text);
        } else {
            messageDiv.textContent = text;
        }

        const timeEl = document.createElement('div');
        timeEl.className = 'message-time';
        timeEl.textContent = formatMessageTime(timestamp);

        const bubble = document.createElement('div');
        bubble.className = 'message-body';
        bubble.appendChild(messageDiv);
        bubble.appendChild(timeEl);

        // Only stored replies can be rated — transient errors carry no id, but
        // they still get a play button, so the actions row may hold either.
        if (sender === 'assistant') {
            const rateable = Number.isFinite(opts.messageId);
            let actions = null;
            if (rateable) {
                actions = buildFeedbackRow(opts.messageId, opts.feedback || 0);
            } else if (ttsSupported) {
                actions = document.createElement('div');
                actions.className = 'message-feedback';
            }
            if (actions && ttsSupported) {
                const speakBtn = makeSpeakButton(text);
                actions.insertBefore(speakBtn, actions.firstChild);
                wrapper._speakButton = speakBtn;
            }
            if (actions) bubble.appendChild(actions);
        }

        wrapper.appendChild(avatar);
        wrapper.appendChild(bubble);
        return wrapper;
    }

    function addMessage(text, sender, timestamp, opts) {
        const el = buildMessage(text, sender, timestamp, opts);
        chatContainer.appendChild(el);
        scrollToBottom();
        return el;
    }

    // Insert an older message above `referenceNode` (for infinite scroll).
    function prependMessage(text, sender, referenceNode, timestamp, opts) {
        const el = buildMessage(text, sender, timestamp, opts);
        chatContainer.insertBefore(el, referenceNode || chatContainer.firstChild);
        return el;
    }

    // Remove any suggestion bubbles currently on screen
    function clearSuggestions() {
        const existing = document.getElementById('suggestion-row');
        if (existing) existing.remove();
    }

    // Render tappable follow-up nudges below the latest assistant reply
    function renderSuggestions(suggestions) {
        clearSuggestions();
        if (!Array.isArray(suggestions) || suggestions.length === 0) return;
        if (userInput.disabled) return; // access expired — no interaction

        const row = document.createElement('div');
        row.className = 'suggestion-row';
        row.id = 'suggestion-row';

        suggestions.forEach(text => {
            const label = (text || '').trim();
            if (!label) return;
            const chip = document.createElement('button');
            chip.type = 'button';
            chip.className = 'suggestion-chip';
            chip.textContent = label;
            chip.addEventListener('click', () => sendMessage(label));
            row.appendChild(chip);
        });

        if (!row.childElementCount) return;
        chatContainer.appendChild(row);
        scrollToBottom();
    }

    // Send a message programmatically (used by suggestion chips)
    function sendMessage(text) {
        if (userInput.disabled) return;
        userInput.value = text;
        chatForm.dispatchEvent(new Event('submit', { cancelable: true }));
    }

    function showTypingIndicator() {
        const id = 'typing-' + Date.now();
        const wrapper = document.createElement('div');
        wrapper.className = 'message-wrapper assistant';
        wrapper.id = id;

        const avatar = document.createElement('div');
        avatar.className = 'avatar assistant-avatar';
        avatar.textContent = 'NX';

        const messageDiv = document.createElement('div');
        messageDiv.className = 'message assistant-message';

        const typingDiv = document.createElement('div');
        typingDiv.className = 'typing-indicator';
        typingDiv.innerHTML = '<div class="typing-dot"></div><div class="typing-dot"></div><div class="typing-dot"></div>';

        messageDiv.appendChild(typingDiv);
        wrapper.appendChild(avatar);
        wrapper.appendChild(messageDiv);
        chatContainer.appendChild(wrapper);
        scrollToBottom();
        return id;
    }

    function removeElement(id) {
        const el = document.getElementById(id);
        if (el) el.remove();
    }

    function scrollToBottom() {
        chatContainer.scrollTop = chatContainer.scrollHeight;
    }

    // Fetch the previous 20 messages and prepend them, preserving scroll position.
    async function loadOlderMessages() {
        if (loadingHistory || !hasMoreHistory || oldestMessageId == null) return;
        loadingHistory = true;

        const loader = document.createElement('div');
        loader.className = 'history-loader';
        loader.textContent = 'Loading earlier messages…';
        chatContainer.insertBefore(loader, chatContainer.firstChild);

        try {
            const accessToken = localStorage.getItem('we_ace_access_token') || '';
            const res = await fetch(`/history?before_id=${oldestMessageId}`, {
                headers: accessToken ? { 'Authorization': `Bearer ${accessToken}` } : {},
            });
            const data = await res.json();
            if (!res.ok) throw new Error(data.error || 'Failed to load history');

            // Anchor scroll so the viewport stays on the same message after prepending.
            const prevHeight = chatContainer.scrollHeight;
            const prevTop = chatContainer.scrollTop;

            // Insert oldest-first above the current anchor; the batch's topmost
            // element becomes the anchor so the next (older) batch lands above it.
            const anchor = historyAnchor || chatContainer.firstChild;
            let newTop = null;
            (data.messages || []).forEach(msg => {
                const el = prependMessage(msg.content, msg.role, anchor, msg.created_at,
                    { messageId: msg.id, feedback: msg.feedback });
                if (!newTop) newTop = el;
            });
            if (newTop) historyAnchor = newTop;

            if (data.messages && data.messages.length) {
                oldestMessageId = data.messages[0].id;
            }
            hasMoreHistory = !!data.has_more;

            chatContainer.scrollTop = prevTop + (chatContainer.scrollHeight - prevHeight);
        } catch (err) {
            console.error('[history] load failed:', err);
        } finally {
            loader.remove();
            loadingHistory = false;
        }
    }

    // Trigger a page load when the user scrolls near the top of the chat.
    chatContainer.addEventListener('scroll', () => {
        if (chatContainer.scrollTop <= 60) loadOlderMessages();
    });
});
