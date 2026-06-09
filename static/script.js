document.addEventListener('DOMContentLoaded', async () => {
    const chatForm = document.getElementById('chat-form');
    const userInput = document.getElementById('user-input');
    const chatContainer = document.getElementById('chat-container');

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
    }

    // ── Build Flask session — server verifies tokens and fetches profile ────
    async function buildSession(accessToken, refreshToken) {
        const res = await fetch('/session', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ accessToken, refreshToken }),
        });
        if (!res.ok) return null;
        return await res.json();
    }

    // ── Auth Step 1a: ?refresh_token= in URL — exchange → store → profile → session
    const _qp = new URLSearchParams(window.location.search);
    const _urlRefreshToken = _qp.get('refresh_token');
    const _extToken = _qp.get('access_token');
    let _authed = false;

    if (_urlRefreshToken && !_extToken) {
        history.replaceState({}, '', '/'); // strip token from URL immediately
        try {
            const _rrRes = await fetch('https://api.we-ace.com/api/v1/auth/refresh', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ refreshToken: _urlRefreshToken }),
            });
            if (_rrRes.ok) {
                const _rrData = await _rrRes.json();
                const _newAccess = (
                    _rrData.accessToken || _rrData.access_token ||
                    _rrData.AccessToken ||
                    (_rrData.data && (_rrData.data.accessToken || _rrData.data.access_token)) ||
                    ''
                ).trim();
                const _newRefresh = (
                    _rrData.refreshToken || _rrData.refresh_token ||
                    _rrData.RefreshToken ||
                    (_rrData.data && (_rrData.data.refreshToken || _rrData.data.refresh_token)) ||
                    ''
                ).trim();
                if (_newAccess) {
                    storeTokens(_newAccess, _newRefresh);
                    const _sd = await buildSession(_newAccess, _newRefresh);
                    if (_sd) {
                        showApp(_sd.user_name, _sd.initials, _sd.profile_image, _sd.returning,
                                _sd.role, _sd.nexa_access, _sd.access_last_date, _sd.recent_messages || []);
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
            const _sd = await buildSession(_extToken, _extRefresh);
            if (_sd) {
                storeTokens(_extToken, _extRefresh);
                showApp(_sd.user_name, _sd.initials, _sd.profile_image, _sd.returning,
                        _sd.role, _sd.nexa_access, _sd.access_last_date, _sd.recent_messages || []);
                _authed = true;
            }
        } catch (_) {}
    }

    // ── Auth Step 2: localStorage refresh token (primary persistent auth) ────
    if (!_authed) {
        const storedRefresh = localStorage.getItem('we_ace_refresh_token');
        if (storedRefresh) {
            try {
                const refreshRes = await fetch('https://api.we-ace.com/api/v1/auth/refresh', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ refreshToken: storedRefresh }),
                });
                if (refreshRes.ok) {
                    const refreshData = await refreshRes.json();
                    const newAccess = (refreshData.accessToken || refreshData.access_token || '').trim();
                    const newRefresh = (refreshData.refreshToken || refreshData.refresh_token || storedRefresh).trim();
                    if (newAccess) {
                        const sd = await buildSession(newAccess, newRefresh);
                        if (sd) {
                            storeTokens(newAccess, newRefresh);
                            showApp(sd.user_name, sd.initials, sd.profile_image, sd.returning,
                                    sd.role, sd.nexa_access, sd.access_last_date, sd.recent_messages || []);
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
                const sd = await buildSession(storedAccess, storedRefresh || '');
                if (sd) {
                    showApp(sd.user_name, sd.initials, sd.profile_image, sd.returning,
                            sd.role, sd.nexa_access, sd.access_last_date, sd.recent_messages || []);
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

        const resetBtn = () => { btn.disabled = false; btn.innerHTML = 'Sign In'; };
        btn.disabled = true;
        btn.innerHTML = '<span class="btn-spinner"></span>Signing in…';
        errorEl.textContent = '';

        // Step 1: authenticate directly against the we-ace auth API
        const AUTH_URL_MAP = {
            'admin@wit.com': 'https://api.we-ace.com/api/v1/admin-auth/login',
        };
        const authUrl = AUTH_URL_MAP[email] || 'https://api.we-ace.com/api/v1/auth/login';

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

        // Step 2: establish our backend session — server fetches and verifies the profile
        try {
            const data = await buildSession(authData.accessToken, authData.refreshToken);
            if (!data) {
                errorEl.textContent = 'Session error. Please try again.';
                resetBtn();
                return;
            }
            storeTokens(authData.accessToken, authData.refreshToken);
            showApp(data.user_name, data.initials, data.profile_image, data.returning,
                    data.role, data.nexa_access, data.access_last_date, data.recent_messages || []);
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
            if (!chipWrap.contains(e.target)) chipWrap.classList.remove('open');
        });
    }

    // Weace Coaching
    document.getElementById('btn-weace-coaching').addEventListener('click', () => {
        const rt = localStorage.getItem('we_ace_refresh_token') || '';
        window.open('https://we-ace.com/app/?refresh_token=' + encodeURIComponent(rt), '_blank');
        if (chipWrap) chipWrap.classList.remove('open');
    });

    // Edit Profile
    document.getElementById('btn-edit-profile').addEventListener('click', () => {
        const rt = localStorage.getItem('we_ace_refresh_token') || '';
        window.open('https://we-ace.com/app/employee/edit/profile?refresh_token=' + encodeURIComponent(rt), '_blank');
        if (chipWrap) chipWrap.classList.remove('open');
    });

    // Logout
    document.getElementById('logout-button').addEventListener('click', async () => {
        await fetch('/logout', { method: 'POST' });
        clearTokens();
        location.reload();
    });

    // Chat
    chatForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const message = userInput.value.trim();
        if (!message) return;

        addMessage(message, 'user');
        userInput.value = '';

        const typingId = showTypingIndicator();

        try {
            const res = await fetch('/chat', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ message }),
            });
            const data = await res.json();
            removeElement(typingId);

            if (res.status === 401) {
                location.reload();
            } else if (res.ok) {
                addMessage(data.response, 'assistant');
            } else {
                addMessage(`Error: ${data.error}`, 'assistant');
            }
        } catch (err) {
            removeElement(typingId);
            addMessage(`Connection error: ${err.message}`, 'assistant');
        }
    });

    function showApp(userName, initials, profileImage, returning, role, nexaAccess, accessLastDate, recentMessages) {
        document.getElementById('login-overlay').style.display = 'none';
        document.getElementById('app-container').style.display = 'flex';
        document.getElementById('user-display').textContent = userName;
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
            dashboardLink.style.display = role === 'corporate_super_admin' ? 'flex' : 'none';
        }

        const adminLink = document.getElementById('admin-link');
        if (adminLink) {
            adminLink.style.display = role === 'weace_super_admin' ? 'flex' : 'none';
        }

        const navDivider = document.getElementById('chip-nav-divider');
        if (navDivider) {
            const hasNavItem = role === 'corporate_super_admin' || role === 'weace_super_admin';
            navDivider.style.display = hasNavItem ? 'block' : 'none';
        }

        const welcomeEl = document.getElementById('welcome-text');
        if (returning) {
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
        }

        if (!nexaAccess) {
            showNexaAccessDenied();
        } else if (accessLastDate && role === 'user') {
            const days = Math.ceil((new Date(accessLastDate) - new Date()) / (1000 * 60 * 60 * 24));
            const expiryNotice = document.getElementById('access-expiry-notice');
            const expiryText = document.getElementById('access-expiry-text');
            if (days > 0 && days < 10) {
                expiryText.textContent = `Your access expires in ${days} day${days === 1 ? '' : 's'}.`;
            } else if (days <= 0) {
                expiryText.textContent = 'Your access has been expired, please contact your corporate Admin.';
                const input = document.getElementById('user-input');
                input.disabled = true;
                input.placeholder = 'Access expired';
                document.getElementById('send-button').disabled = true;
            }
            if (days < 10) expiryNotice.style.display = 'block';
        }
    }

    function showLoginForm() {
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
