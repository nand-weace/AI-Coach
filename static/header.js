// Shared app-header behaviour — chat page and dashboard both load this.
// Wires the tabs, language picker, Corporate Knowledge modal and user-chip
// dropdown. Pages hook back in via window.NexaHeader.

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

const NexaHeader = (() => {
    const $ = id => document.getElementById(id);

    function accessToken() {
        return localStorage.getItem('we_ace_access_token') || '';
    }
    function authHeaders(extra) {
        const t = accessToken();
        return Object.assign(t ? { 'Authorization': `Bearer ${t}` } : {}, extra || {});
    }
    // Chat page keeps the refresh token in localStorage; the dashboard is
    // server-rendered and carries it on the dropdown.
    function refreshToken() {
        const el = $('chip-dropdown');
        return (el && el.dataset.refreshToken) || localStorage.getItem('we_ace_refresh_token') || '';
    }

    // Marks the next page load as an in-app tab switch. sessionStorage is
    // per browser tab and survives the navigation; script.js reads it once.
    function markTabNav() {
        try { sessionStorage.setItem('we_ace_tab_nav', String(Date.now())); } catch (_) {}
    }

    function closeMenus() {
        const chip = $('user-chip-wrap');
        const lang = $('language-menu-wrap');
        if (chip) chip.classList.remove('open');
        if (lang) {
            lang.classList.remove('open');
            const btn = $('btn-language');
            if (btn) btn.setAttribute('aria-expanded', 'false');
        }
    }

    // role can be an array of role objects [{slug: '...'}] or a legacy string
    function hasRole(roleVal, ...slugs) {
        if (Array.isArray(roleVal))
            return roleVal.some(r => r && typeof r === 'object' && slugs.includes(r.slug));
        return slugs.includes(roleVal);
    }

    // ── Language ────────────────────────────────────────────────────────────
    function getLanguage() {
        return localStorage.getItem('we_ace_language') || 'English';
    }

    function renderLanguageMenu() {
        const menu = $('language-submenu');
        const current = $('language-current');
        if (!menu || !current) return;
        const active = getLanguage();
        current.textContent = (LANGUAGES.find(l => l.code === active) || LANGUAGES[0])
            .label.replace(/\s*\(.*\)$/, '');
        menu.innerHTML = '';
        LANGUAGES.forEach(({ code, label }) => {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'chip-submenu-option' + (code === active ? ' selected' : '');
            btn.textContent = label;
            btn.addEventListener('click', e => {
                e.stopPropagation();
                setLanguage(code);
            });
            menu.appendChild(btn);
        });
    }

    async function setLanguage(code) {
        const previous = getLanguage();
        localStorage.setItem('we_ace_language', code);
        renderLanguageMenu();
        closeMenus();
        if (code === previous) return;
        try {
            await fetch('/language', {
                method: 'POST',
                credentials: 'omit',
                headers: authHeaders({ 'Content-Type': 'application/json' }),
                body: JSON.stringify({ language: code }),
            });
        } catch (err) {
            // Preference still applies locally — it's sent with every /chat request.
        }
    }

    // ── Corporate Knowledge modal ───────────────────────────────────────────
    function initCorpContent() {
        const openBtn = $('btn-corp-content');
        const overlay = $('corp-content-overlay');
        if (!openBtn || !overlay) return;
        const input = $('corp-content-input');
        const status = $('corp-content-status');
        const saveBtn = $('corp-content-save');

        function setStatus(msg, kind) {
            status.textContent = msg || '';
            status.className = 'corp-modal-status' + (kind ? ' ' + kind : '');
        }
        function closeModal() { overlay.style.display = 'none'; }

        async function openModal() {
            closeMenus();
            input.value = '';
            setStatus('Loading…');
            overlay.style.display = 'flex';
            input.focus();
            try {
                const res = await fetch('/api/org-content', { headers: authHeaders() });
                const data = await res.json();
                if (!res.ok) throw new Error(data.error || 'Failed to load');
                input.value = data.content || '';
                setStatus('');
            } catch (err) {
                setStatus(err.message || 'Failed to load', 'error');
            }
        }

        async function saveContent() {
            saveBtn.disabled = true;
            setStatus('Saving…');
            try {
                const res = await fetch('/api/org-content', {
                    method: 'POST',
                    headers: authHeaders({ 'Content-Type': 'application/json' }),
                    body: JSON.stringify({ content: input.value }),
                });
                const data = await res.json();
                if (!res.ok) throw new Error(data.error || 'Failed to save');
                setStatus('Saved', 'success');
                setTimeout(closeModal, 700);
            } catch (err) {
                setStatus(err.message || 'Failed to save', 'error');
            } finally {
                saveBtn.disabled = false;
            }
        }

        openBtn.addEventListener('click', openModal);
        saveBtn.addEventListener('click', saveContent);
        $('corp-content-cancel').addEventListener('click', closeModal);
        $('corp-content-close').addEventListener('click', closeModal);
        overlay.addEventListener('click', e => { if (e.target === overlay) closeModal(); });
    }

    // ── Wiring ──────────────────────────────────────────────────────────────
    function init(opts = {}) {
        const chipWrap = $('user-chip-wrap');
        const chipTrigger = $('user-chip');
        const langWrap = $('language-menu-wrap');

        if (chipWrap && chipTrigger) {
            chipTrigger.addEventListener('click', e => {
                e.stopPropagation();
                const open = !chipWrap.classList.contains('open');
                closeMenus();
                chipWrap.classList.toggle('open', open);
            });
        }

        if (langWrap) {
            $('btn-language').addEventListener('click', e => {
                e.stopPropagation();
                const open = !langWrap.classList.contains('open');
                closeMenus();
                langWrap.classList.toggle('open', open);
                e.currentTarget.setAttribute('aria-expanded', open ? 'true' : 'false');
            });
        }

        document.addEventListener('click', e => {
            if (chipWrap && chipWrap.contains(e.target)) return;
            if (langWrap && langWrap.contains(e.target)) return;
            closeMenus();
        });
        document.addEventListener('keydown', e => {
            if (e.key === 'Escape') closeMenus();
        });

        // Tabs that navigate: mark them active straight away so the header
        // reflects the click while the next page loads, and leave a marker so
        // the chat page knows this was a tab switch and carries the running
        // conversation over instead of starting a new session. Anything else
        // (refresh, fresh visit, new login) leaves no marker.
        document.querySelectorAll('.app-tab[href], .logo-home').forEach(tab => {
            tab.addEventListener('click', () => {
                markTabNav();
                document.querySelectorAll('.app-tab').forEach(t => t.classList.remove('active'));
                if (tab.classList.contains('app-tab')) tab.classList.add('active');
            });
        });

        const adminLink = $('admin-link');
        if (adminLink) adminLink.addEventListener('click', markTabNav);

        // The chat page has no separate "Talk" destination when already there.
        const talkTab = $('tab-talk');
        if (talkTab && opts.onTalk) {
            talkTab.addEventListener('click', e => {
                e.preventDefault();
                closeMenus();
                opts.onTalk();
            });
        }

        $('btn-weace-coaching').addEventListener('click', () => {
            window.open('https://we-ace.com/app/?refresh_token=' + encodeURIComponent(refreshToken()), '_blank');
            closeMenus();
        });

        const editProfile = $('btn-edit-profile');
        if (editProfile) {
            editProfile.addEventListener('click', () => {
                window.open('https://we-ace.com/app/employee/edit/profile?refresh_token='
                    + encodeURIComponent(refreshToken()), '_blank');
                closeMenus();
            });
        }

        $('logout-button').addEventListener('click', async () => {
            try { sessionStorage.removeItem('we_ace_tab_nav'); } catch (_) {}
            await fetch('/logout', { method: 'POST', headers: authHeaders() });
            if (opts.onLogout) opts.onLogout();
            else window.location.href = '/';
        });

        initCorpContent();
        renderLanguageMenu();
    }

    // Chat page calls this once /session reports the user's roles.
    function applyRoles(role) {
        const isOrgAdmin = hasRole(role, 'corporate_super_admin');
        const isWeaceAdmin = hasRole(role, 'weace_super_admin');

        const dash = $('dashboard-link');
        if (dash) dash.style.display = isOrgAdmin ? 'flex' : 'none';

        const corp = $('btn-corp-content');
        if (corp) corp.style.display = (isOrgAdmin || isWeaceAdmin) ? 'flex' : 'none';

        const admin = $('admin-link');
        if (admin) admin.style.display = isWeaceAdmin ? 'flex' : 'none';

        // Divider only earns its place when Admin sits above it
        const divider = $('chip-nav-divider');
        if (divider) divider.style.display = isWeaceAdmin ? 'block' : 'none';
    }

    function setNexaAccess(allowed) {
        const btn = $('btn-weace-coaching');
        if (!btn) return;
        btn.disabled = !allowed;
        if (!allowed) btn.title = 'Access required to use Weace Coaching';
    }

    return { init, applyRoles, setNexaAccess, renderLanguageMenu, getLanguage, hasRole, closeMenus };
})();

window.NexaHeader = NexaHeader;
