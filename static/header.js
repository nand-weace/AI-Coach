// Shared side-panel behaviour — chat page and dashboard both load this.
// Wires the tabs, language picker, Corporate Knowledge modal, user-chip
// dropdown and the mobile off-canvas drawer. Pages hook back in via
// window.NexaHeader.

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

    // Every credential and preference the app persists — keep in sync with
    // clearTokens() in script.js. Logout has to drop all of it: clearing only
    // the server session leaves a live refresh token behind, and the very next
    // page load signs straight back in, which reads as "logout didn't work".
    const STORED_KEYS = [
        'we_ace_access_token', 'we_ace_refresh_token', 'we_ace_user_id',
        'we_ace_user_name', 'we_ace_session_uuid', 'we_ace_profile_context',
        'we_ace_profile_roles', 'we_ace_session_token', 'we_ace_org_id',
        'we_ace_org_name', 'we_ace_cohort_id', 'we_ace_language', 'we_ace_mode',
    ];
    function clearStoredCredentials() {
        try { STORED_KEYS.forEach(k => localStorage.removeItem(k)); } catch (_) {}
        try { sessionStorage.removeItem('we_ace_tab_nav'); } catch (_) {}
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

    // ── My Insights sub-nav ─────────────────────────────────────────────────
    // The reports are addressed by URL hash, so the highlight follows the hash
    // rather than a click: it also has to be right after a back/forward or a
    // tab switched from inside the page itself. 'growth' is the report
    // my_insights.html opens on when the hash is empty.
    const DEFAULT_SUBTAB = 'growth';

    function syncSubnav() {
        const subs = document.querySelectorAll('.app-subtab');
        if (!subs.length) return;
        const onPage = window.location.pathname.replace(/\/+$/, '') === '/my-insights';
        const current = onPage ? (window.location.hash.slice(1) || DEFAULT_SUBTAB) : null;
        subs.forEach(a => a.classList.toggle('active', a.dataset.subtab === current));
    }

    // ── Off-canvas panel (under 900px) ──────────────────────────────────────
    function setDrawer(open) {
        const panel = $('app-sidebar');
        const scrim = $('sidebar-scrim');
        const toggle = $('sidebar-toggle');
        if (panel) panel.classList.toggle('open', open);
        if (scrim) scrim.classList.toggle('open', open);
        if (toggle) toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
        if (!open) closeMenus();
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

        const toggleBtn = $('sidebar-toggle');
        if (toggleBtn) {
            toggleBtn.addEventListener('click', e => {
                e.stopPropagation();
                const panel = $('app-sidebar');
                setDrawer(!(panel && panel.classList.contains('open')));
            });
        }
        const scrim = $('sidebar-scrim');
        if (scrim) scrim.addEventListener('click', () => setDrawer(false));

        document.addEventListener('click', e => {
            if (chipWrap && chipWrap.contains(e.target)) return;
            if (langWrap && langWrap.contains(e.target)) return;
            closeMenus();
        });
        document.addEventListener('keydown', e => {
            if (e.key === 'Escape') { closeMenus(); setDrawer(false); }
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

        // Any nav choice dismisses the drawer on mobile. Language is the one
        // exception — its flyout lives inside the panel, so closing it there
        // would hide the options being picked from.
        document.querySelectorAll('.app-tab, .app-subtab').forEach(tab => {
            if (tab.id === 'btn-language') return;
            tab.addEventListener('click', () => setDrawer(false));
        });

        // A sub-nav link either navigates to /my-insights (carry the running
        // conversation over, as the tabs do) or, when already there, only moves
        // the hash — no reload, so the highlight is updated here as well as on
        // hashchange.
        document.querySelectorAll('.app-subtab').forEach(link => {
            link.addEventListener('click', () => {
                markTabNav();
                requestAnimationFrame(syncSubnav);
            });
        });
        window.addEventListener('hashchange', syncSubnav);
        syncSubnav();

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

        const logoutBtn = $('logout-button');
        logoutBtn.addEventListener('click', async () => {
            if (logoutBtn.disabled) return;   // one logout per click, not one per impatient click
            logoutBtn.disabled = true;
            // Server session first — it needs the token that's about to go.
            // A failed request must not strand the user signed in locally, so
            // the credentials are dropped either way.
            try {
                await fetch('/logout', { method: 'POST', headers: authHeaders() });
            } catch (_) {}
            clearStoredCredentials();
            if (opts.onLogout) opts.onLogout();
            else window.location.href = '/';
        });

        initCorpContent();
        renderLanguageMenu();
    }

    // Chat page calls this once /session reports the user's roles. Mirrors the
    // server-rendered gating in _header.html — keep the two in step.
    function applyRoles(role) {
        const isOrgAdmin = hasRole(role, 'corporate_super_admin');
        const isWeaceAdmin = hasRole(role, 'weace_super_admin');

        // '' drops the inline override so each element falls back to its own
        // stylesheet display — flex for a tab, block for the language wrapper.
        const show = (id, on) => {
            const el = $(id);
            if (el) el.style.display = on ? '' : 'none';
        };

        // A WeAce super admin runs the platform rather than using the coaching
        // product, so the bar collapses to Admin — Chat, Language, My Insights
        // and Knowledge Base are not theirs to see.
        show('tab-talk', !isWeaceAdmin);
        show('language-menu-wrap', !isWeaceAdmin);
        show('my-insights-link', !isWeaceAdmin);
        show('my-insights-subnav', !isWeaceAdmin);
        show('btn-corp-content', isOrgAdmin && !isWeaceAdmin);
        show('dashboard-link', isOrgAdmin);
        show('tab-admin', isWeaceAdmin);
    }

    function setNexaAccess(allowed) {
        const btn = $('btn-weace-coaching');
        if (!btn) return;
        btn.disabled = !allowed;
        if (!allowed) btn.title = 'Access required to use Weace Coaching';
    }

    return { init, applyRoles, setNexaAccess, renderLanguageMenu, getLanguage, hasRole,
             closeMenus, setDrawer, syncSubnav };
})();

window.NexaHeader = NexaHeader;
