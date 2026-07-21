// Language picker — keep in sync with SUPPORTED_LANGUAGES in app.py
const LANGUAGES = [
    { code: 'English',    label: 'English' },
    { code: 'Hindi',      label: 'हिन्दी (Hindi)' },
    { code: 'Marathi',    label: 'मराठी (Marathi)' },
    { code: 'Bengali',    label: 'বাংলা (Bengali)' },
    { code: 'Tamil',      label: 'தமிழ் (Tamil)' },
    { code: 'Telugu',     label: 'తెలుగు (Telugu)' },
    { code: 'Kannada',    label: 'ಕನ್ನಡ (Kannada)' },
    { code: 'Malayalam',  label: 'മലയാളം (Malayalam)' },
    { code: 'Gujarati',   label: 'ગુજરાતી (Gujarati)' },
    { code: 'Spanish',    label: 'Español (Spanish)' },
    { code: 'French',     label: 'Français (French)' },
    { code: 'German',     label: 'Deutsch (German)' },
    { code: 'Portuguese', label: 'Português (Portuguese)' },
    { code: 'Arabic',     label: 'العربية (Arabic)' },
    { code: 'Chinese',    label: '中文 (Chinese)' },
    { code: 'Japanese',   label: '日本語 (Japanese)' },
];

document.addEventListener('DOMContentLoaded', async () => {
    const chatForm = document.getElementById('chat-form');
    const userInput = document.getElementById('user-input');
    const chatContainer = document.getElementById('chat-container');

    if (typeof marked !== 'undefined') {
        marked.setOptions({ breaks: true, gfm: true });
    }

    // role can be an array of role objects [{slug: '...'}] or a legacy string
    function hasRole(roleVal, ...slugs) {
        if (Array.isArray(roleVal))
            return roleVal.some(r => r && typeof r === 'object' && slugs.includes(r.slug));
        return slugs.includes(roleVal);
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

    // ── Build session — server verifies tokens and fetches profile ────
    async function buildSession(accessToken, refreshToken, profileRoles) {
        const userId = localStorage.getItem('we_ace_user_id') || '';
        const payload = { refreshToken };
        if (userId) payload.userId = userId;
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
        if (data.user_id) localStorage.setItem('we_ace_user_id', data.user_id);
        if (data.user_name) localStorage.setItem('we_ace_user_name', data.user_name);
        if (data.session_uuid) localStorage.setItem('we_ace_session_uuid', data.session_uuid);
        if (data.profile_context) localStorage.setItem('we_ace_profile_context', JSON.stringify(data.profile_context));
        if (data.org_id) localStorage.setItem('we_ace_org_id', data.org_id);
        if (data.org_name) localStorage.setItem('we_ace_org_name', data.org_name);
        if (data.cohort_id) localStorage.setItem('we_ace_cohort_id', data.cohort_id);
        if (data.language) localStorage.setItem('we_ace_language', data.language);
        return data;
    }

    // ── Auth Step 1a: ?refresh_token= in URL — exchange → store → profile → session
    const _qp = new URLSearchParams(window.location.search);
    const _urlRefreshToken = _qp.get('refresh_token');
    const _extToken = _qp.get('access_token');
    let _authed = false;

    // Any stored/incoming credential means we're about to run the slow auth
    // chain — show the stepper instead of a bare spinner.
    if (_urlRefreshToken || _extToken ||
        localStorage.getItem('we_ace_refresh_token') ||
        localStorage.getItem('we_ace_access_token')) {
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
                        showApp(_sd.user_name, _sd.initials, _sd.profile_image, _sd.returning,
                                _sd.role, _sd.nexa_access, _sd.access_last_date, _sd.remaining_days, _sd.recent_messages || [],
                        _sd.welcome_message, _sd.welcome_suggestions);
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
                showApp(_sd.user_name, _sd.initials, _sd.profile_image, _sd.returning,
                        _sd.role, _sd.nexa_access, _sd.access_last_date, _sd.remaining_days, _sd.recent_messages || [],
                        _sd.welcome_message, _sd.welcome_suggestions);
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
                            showApp(sd.user_name, sd.initials, sd.profile_image, sd.returning,
                                    sd.role, sd.nexa_access, sd.access_last_date, sd.remaining_days, sd.recent_messages || [],
                                    sd.welcome_message, sd.welcome_suggestions);
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
                    showApp(sd.user_name, sd.initials, sd.profile_image, sd.returning,
                            sd.role, sd.nexa_access, sd.access_last_date, sd.remaining_days, sd.recent_messages || [],
                            sd.welcome_message, sd.welcome_suggestions);
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
        try {
            const data = await buildSession(authData.accessToken, authData.refreshToken, authData.profileDetails?.roles);
            if (!data) {
                errorEl.textContent = 'Session error. Please try again.';
                resetBtn();
                return;
            }
            showApp(data.user_name, data.initials, data.profile_image, data.returning,
                    data.role, data.nexa_access, data.access_last_date, data.remaining_days, data.recent_messages || [],
                    data.welcome_message, data.welcome_suggestions);
        } catch (err) {
            errorEl.textContent = 'Connection error. Please try again.';
            resetBtn();
        }
    });

    // User-chip dropdown toggle
    const chipWrap = document.getElementById('user-chip-wrap');
    const chipTrigger = document.getElementById('user-chip');
    if (chipWrap && chipTrigger) {
        chipTrigger.addEventListener('click', e => {
            e.stopPropagation();
            chipWrap.classList.toggle('open');
        });
        document.addEventListener('click', e => {
            if (!chipWrap.contains(e.target)) {
                chipWrap.classList.remove('open');
                document.getElementById('language-menu-wrap').classList.remove('open');
            }
        });
    }

    // Weace Coaching
    document.getElementById('btn-weace-coaching').addEventListener('click', () => {
        const rt = localStorage.getItem('we_ace_refresh_token') || '';
        window.open('https://we-ace.com/app/?refresh_token=' + encodeURIComponent(rt), '_blank');
        if (chipWrap) chipWrap.classList.remove('open');
    });


    function getLanguage() {
        return localStorage.getItem('we_ace_language') || 'English';
    }

    // Looks elements up on each call — showApp can run before this block is reached.
    function renderLanguageMenu() {
        const langSubmenu = document.getElementById('language-submenu');
        const langCurrent = document.getElementById('language-current');
        if (!langSubmenu || !langCurrent) return;
        const active = getLanguage();
        langCurrent.textContent = (LANGUAGES.find(l => l.code === active) || LANGUAGES[0])
            .label.replace(/\s*\(.*\)$/, '');
        langSubmenu.innerHTML = '';
        LANGUAGES.forEach(({ code, label }) => {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'chip-submenu-option' + (code === active ? ' selected' : '');
            btn.textContent = label;
            btn.addEventListener('click', e => {
                e.stopPropagation();
                setLanguage(code);
            });
            langSubmenu.appendChild(btn);
        });
    }

    async function setLanguage(code) {
        const previous = getLanguage();
        localStorage.setItem('we_ace_language', code);
        renderLanguageMenu();
        document.getElementById('language-menu-wrap').classList.remove('open');
        if (chipWrap) chipWrap.classList.remove('open');
        if (code === previous) return;
        try {
            const accessToken = localStorage.getItem('we_ace_access_token') || '';
            await fetch('/language', {
                method: 'POST',
                credentials: 'omit',
                headers: {
                    'Content-Type': 'application/json',
                    ...(accessToken ? { 'Authorization': `Bearer ${accessToken}` } : {}),
                },
                body: JSON.stringify({ language: code }),
            });
        } catch (err) {
            // Preference still applies locally — it's sent with every /chat request.
        }
    }

    document.getElementById('btn-language').addEventListener('click', e => {
        e.stopPropagation();
        document.getElementById('language-menu-wrap').classList.toggle('open');
    });
    renderLanguageMenu();

    // Edit Profile
    document.getElementById('btn-edit-profile').addEventListener('click', () => {
        const rt = localStorage.getItem('we_ace_refresh_token') || '';
        window.open('https://we-ace.com/app/employee/edit/profile?refresh_token=' + encodeURIComponent(rt), '_blank');
        if (chipWrap) chipWrap.classList.remove('open');
    });

    // Logout
    document.getElementById('logout-button').addEventListener('click', async () => {
        const accessToken = localStorage.getItem('we_ace_access_token') || '';
        await fetch('/logout', {
            method: 'POST',
            headers: accessToken ? { 'Authorization': `Bearer ${accessToken}` } : {},
        });
        clearTokens();
        location.reload();
    });

    // Auto-resize textarea
    userInput.addEventListener('input', () => {
        userInput.style.height = 'auto';
        userInput.style.height = userInput.scrollHeight + 'px';
        userInput.style.overflowY = userInput.scrollHeight > 160 ? 'auto' : 'hidden';
    });

    // Enter submits; Shift+Enter inserts newline
    userInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            chatForm.dispatchEvent(new Event('submit', { cancelable: true }));
        }
    });

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
                }),
            });
            const data = await res.json();
            removeElement(typingId);

            if (res.status === 401) {
                location.reload();
            } else if (res.ok) {
                addMessage(data.response, 'assistant');
                renderSuggestions(data.suggestions);
            } else {
                addMessage(`Error: ${data.error}`, 'assistant');
            }
        } catch (err) {
            removeElement(typingId);
            addMessage(`Connection error: ${err.message}`, 'assistant');
        }
    });

    function showApp(userName, initials, profileImage, returning, role, nexaAccess, accessLastDate, remainingDays, recentMessages, welcomeMessage, welcomeSuggestions) {
        loginProgress.stop();
        document.getElementById('login-overlay').style.display = 'none';
        const appEl = document.getElementById('app-container');
        appEl.style.opacity = '0';
        appEl.style.display = 'flex';
        appEl.offsetHeight; // force reflow
        appEl.style.transition = 'opacity 0.22s ease';
        appEl.style.opacity = '1';
        document.getElementById('user-display').textContent = userName;
        renderLanguageMenu();   // reflect the preference restored by /session
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

        const dashboardLink = document.getElementById('dashboard-link');
        if (dashboardLink) {
            dashboardLink.style.display = hasRole(role, 'corporate_super_admin') ? 'flex' : 'none';
        }

        const adminLink = document.getElementById('admin-link');
        if (adminLink) {
            adminLink.style.display = hasRole(role, 'weace_super_admin') ? 'flex' : 'none';
        }

        const navDivider = document.getElementById('chip-nav-divider');
        if (navDivider) {
            const hasNavItem = hasRole(role, 'corporate_super_admin', 'weace_super_admin');
            navDivider.style.display = hasNavItem ? 'block' : 'none';
        }

        const wCoachBtn = document.getElementById('btn-weace-coaching');
        if (wCoachBtn) {
            const blocked = !nexaAccess || (remainingDays !== null && remainingDays !== undefined && remainingDays < 7);
            wCoachBtn.disabled = blocked;
            if (blocked) wCoachBtn.title = 'Access required to use Weace Coaching';
        }

        const welcomeEl = document.getElementById('welcome-text');
        if (welcomeMessage && welcomeMessage.trim()) {
            welcomeEl.textContent = welcomeMessage.trim();
        } else if (returning) {
            welcomeEl.textContent = `Welcome back, ${userName}. Ready to continue your leadership journey? What's on your mind today?`;
        } else {
            welcomeEl.textContent = `Welcome to your Executive Leadership Coaching Session, ${userName}. I'm Nexa, here to help you navigate complex professional challenges, enhance your leadership skills, and drive strategic impact. What would you like to focus on today?`;
        }

        if (recentMessages && recentMessages.length > 0) {
            const divider = document.createElement('div');
            divider.className = 'history-divider';
            divider.innerHTML = '<span>Previous conversation</span>';
            chatContainer.appendChild(divider);
            recentMessages.forEach(msg => addMessage(msg.content, msg.role));
            // Welcome bubble is static markup at the top of the container; move it
            // below the replayed history so it opens the new session rather than
            // sitting above the fold once we scroll to the bottom.
            const welcomeWrapper = welcomeEl.closest('.message-wrapper');
            if (welcomeWrapper) chatContainer.appendChild(welcomeWrapper);
            scrollToBottom();
        }

        // Nudge the user to start the engagement with tappable starter prompts
        if (nexaAccess && Array.isArray(welcomeSuggestions) && welcomeSuggestions.length > 0) {
            renderSuggestions(welcomeSuggestions);
        }

        if (!nexaAccess) {
            showNexaAccessDenied();
        } else if (remainingDays !== null && remainingDays !== undefined) {
            const expiryNotice = document.getElementById('access-expiry-notice');
            const expiryText = document.getElementById('access-expiry-text');
            if (remainingDays <= 0) {
                showNexaAccessDenied();
            } else if (remainingDays <= 7) {
                expiryText.textContent = `Your access expires in ${remainingDays} day${remainingDays === 1 ? '' : 's'}.`;
                expiryNotice.style.display = 'block';
            }
        }
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
    }

    function makeInitialsSpan(initials) {
        const span = document.createElement('span');
        span.textContent = initials || 'U';
        return span;
    }

    function addMessage(text, sender) {
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

        wrapper.appendChild(avatar);
        wrapper.appendChild(messageDiv);
        chatContainer.appendChild(wrapper);
        scrollToBottom();
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
});
