document.addEventListener('DOMContentLoaded', async () => {
    const chatForm = document.getElementById('chat-form');
    const userInput = document.getElementById('user-input');
    const chatContainer = document.getElementById('chat-container');

    if (typeof marked !== 'undefined') {
        marked.setOptions({ breaks: true, gfm: true });
    }

    // Token-based auth from external platform redirect (/auth?access_token=...&refresh_token=...)
    const _qp = new URLSearchParams(window.location.search);
    const _extToken = _qp.get('access_token');
    let _authed = false;

    if (_extToken) {
        history.replaceState({}, '', '/'); // strip tokens from URL immediately
        const _extRefresh = _qp.get('refresh_token') || '';
        try {
            const _pRes = await fetch('https://api.we-ace.com/api/v1/auth/profile', {
                headers: { 'Authorization': `Bearer ${_extToken}` },
            });
            if (_pRes.ok) {
                const _ad = await _pRes.json();
                const _pr = _ad.profileDetails || {};
                const _org = _ad.organisation || _ad.organization ||
                             _pr.organisation || _pr.organization || {};
                const _rr = _ad.roles || _ad.role || _pr.roles || _pr.role || [];
                const _ra = Array.isArray(_rr) ? _rr : [_rr];
                const _role = _ra.some(r => r && r.slug === 'weace_super_admin')
                    ? 'weace_super_admin'
                    : _ra.some(r => r && r.slug === 'corporate_super_admin')
                        ? 'corporate_super_admin' : 'user';
                const _sr = await fetch('/session', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        userId: _pr.userId || _pr._id,
                        firstName: _pr.firstName,
                        lastName: _pr.lastName,
                        email: _pr.email,
                        profileImage: _pr.profileImage,
                        accessToken: _extToken,
                        refreshToken: _extRefresh,
                        role: _role,
                        orgName: _org.name || _ad.orgName || _pr.orgName || '',
                        orgSlug: _ad.parentId || _pr.parentId || '',
                        cohortId: _ad.cohortId || _pr.cohortId || _org.cohortId || '',
                    }),
                });
                if (_sr.ok) {
                    const _sd = await _sr.json();
                    showApp(_sd.user_name, _sd.initials, _sd.profile_image, _sd.returning,
                            _sd.role, _sd.nexa_access, _sd.access_last_date);
                    _authed = true;
                }
            }
        } catch (_) {}
    }

    if (!_authed) {
        // Normal flow: resume existing session if cookie is present
        try {
            const meRes = await fetch('/me');
            const meData = await meRes.json();
            if (meData.logged_in) {
                const nsRes = await fetch('/new-session', { method: 'POST' });
                if (nsRes.ok) {
                    const nsData = await nsRes.json();
                    showApp(nsData.user_name, nsData.initials, nsData.profile_image, nsData.returning, nsData.role, nsData.nexa_access, nsData.access_last_date);
                    _authed = true;
                }
            }
        } catch (_) {}
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

        // Step 2: establish our backend session with the returned profile + tokens
        const profile = authData.profileDetails || {};
        const org = authData.organisation || authData.organization || profile.organisation || profile.organization || {};
        const rawRoles = authData.roles || authData.role || profile.roles || profile.role || [];
        const rolesArr = Array.isArray(rawRoles) ? rawRoles : [rawRoles];
        const role = rolesArr.some(r => r && r.slug === 'weace_super_admin')
            ? 'weace_super_admin'
            : rolesArr.some(r => r && r.slug === 'corporate_super_admin')
                ? 'corporate_super_admin'
                : 'user';
        const orgName = org.name || authData.orgName || profile.orgName || '';
        const orgSlug = authData.parentId || profile.parentId || '';
        const cohortId = authData.cohortId || profile.cohortId || org.cohortId || '';
        try {
            const res = await fetch('/session', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    userId: profile.userId || profile._id,
                    firstName: profile.firstName,
                    lastName: profile.lastName,
                    email: profile.email || email,
                    profileImage: profile.profileImage,
                    accessToken: authData.accessToken,
                    refreshToken: authData.refreshToken,
                    role,
                    orgName,
                    orgSlug,
                    cohortId,
                }),
            });
            const data = await res.json();
            if (!res.ok) {
                errorEl.textContent = data.error || 'Session error. Please try again.';
                resetBtn();
                return;
            }
            showApp(data.user_name, data.initials, data.profile_image, data.returning, data.role, data.nexa_access, data.access_last_date);
        } catch (err) {
            errorEl.textContent = 'Connection error. Please try again.';
            resetBtn();
        }
    });

    // Logout
    document.getElementById('logout-button').addEventListener('click', async () => {
        await fetch('/logout', { method: 'POST' });
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

    function showApp(userName, initials, profileImage, returning, role, nexaAccess, accessLastDate) {
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
            dashboardLink.style.display = role === 'corporate_super_admin' ? 'inline-block' : 'none';
        }

        const adminLink = document.getElementById('admin-link');
        if (adminLink) {
            adminLink.style.display = role === 'weace_super_admin' ? 'inline-block' : 'none';
        }

        const welcomeEl = document.getElementById('welcome-text');
        if (returning) {
            welcomeEl.textContent = `Welcome back, ${userName}. Ready to continue your leadership journey? What's on your mind today?`;
        } else {
            welcomeEl.textContent = `Welcome to your Executive Leadership Coaching Session, ${userName}. I'm Nexa, here to help you navigate complex professional challenges, enhance your leadership skills, and drive strategic impact. What would you like to focus on today?`;
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
