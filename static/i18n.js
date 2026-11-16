// Static UI text in the user's language.
//
// Fixed text (labels, buttons, headings, empty states) is translated from the
// dictionaries in static/i18n/<code>.json, never by a model call. Each one maps
// the English text to its translation, so the English stays readable in the
// markup and anything missing from a dictionary simply shows in English.
//
//   Markup:  <span data-i18n>Edit Profile</span>          text
//            <div data-i18n-html>Need <b>3</b> …</div>    trusted static HTML
//            <x data-i18n-title="…" data-i18n-aria="…" data-i18n-placeholder="…">
//   Script:  NexaI18n.t('{n} messages', { n: 4 })
//
// Loaded by _header.html, before header.js and every page script. The language
// is the one header.js keeps in localStorage; when the user picks another,
// header.js fires 'nexa:languagechange', the dictionary loads, the page's
// marked-up text is swapped, and 'nexa:i18nchange' fires so pages can re-render
// whatever they build in script.
const NexaI18n = (() => {
    // Language code -> dictionary URL (versioned), written by _header.html.
    const FILES = window.NEXA_I18N_FILES || {};
    const LOCALES = {
        English: 'en-GB', Hindi: 'hi-IN', Arabic: 'ar-u-nu-latn', Spanish: 'es-ES',
        Tamil: 'ta-IN', French: 'fr-FR', German: 'de-DE',
    };
    const HTML_LANG = {
        English: 'en', Hindi: 'hi', Arabic: 'ar', Spanish: 'es',
        Tamil: 'ta', French: 'fr', German: 'de',
    };
    const CACHE_PREFIX = 'we_ace_i18n:';

    let language = 'English';
    let dict = {};

    function storedLanguage() {
        let code = '';
        try { code = localStorage.getItem('we_ace_language') || ''; } catch (_) {}
        return LOCALES[code] ? code : 'English';
    }

    // Keys and markup are compared with whitespace collapsed, so indentation
    // in a template never stops a match.
    const norm = s => String(s == null ? '' : s).replace(/\s+/g, ' ').trim();

    function t(src, vars) {
        const key = norm(src);
        let out = (language !== 'English' && dict[key]) || key;
        if (vars) out = out.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? String(vars[k]) : m));
        return out;
    }

    function apply(root) {
        root = root || document;
        root.querySelectorAll('[data-i18n]').forEach(el => {
            if (el.dataset.i18nSrc == null) el.dataset.i18nSrc = norm(el.textContent);
            el.textContent = t(el.dataset.i18nSrc);
        });
        root.querySelectorAll('[data-i18n-html]').forEach(el => {
            if (el.dataset.i18nSrc == null) el.dataset.i18nSrc = norm(el.innerHTML);
            el.innerHTML = t(el.dataset.i18nSrc);
        });
        [['i18nTitle', 'title'], ['i18nAria', 'aria-label'], ['i18nPlaceholder', 'placeholder']]
            .forEach(([data, attr]) => {
                const sel = '[data-' + data.replace(/[A-Z]/g, c => '-' + c.toLowerCase()) + ']';
                root.querySelectorAll(sel).forEach(el => el.setAttribute(attr, t(el.dataset[data])));
            });
        document.documentElement.lang = HTML_LANG[language] || 'en';
    }

    // A dictionary is cached per versioned URL, so a page load in the same
    // language paints translated straight away instead of flashing English
    // while it downloads; a new deploy changes the URL and misses the cache.
    function cached(url) {
        try {
            const raw = localStorage.getItem(CACHE_PREFIX + url);
            return raw ? JSON.parse(raw) : null;
        } catch (_) { return null; }
    }
    function remember(code, url, data) {
        try {
            Object.keys(localStorage)
                .filter(k => k.startsWith(CACHE_PREFIX) && k !== CACHE_PREFIX + url
                             && k.includes('/' + (HTML_LANG[code] || '') + '.json'))
                .forEach(k => localStorage.removeItem(k));
            localStorage.setItem(CACHE_PREFIX + url, JSON.stringify(data));
        } catch (_) {}
    }

    async function loadDict(code) {
        const url = FILES[code];
        if (code === 'English' || !url) return {};
        const hit = cached(url);
        if (hit) return hit;
        try {
            const res = await fetch(url, { credentials: 'omit' });
            if (!res.ok) return {};
            const data = await res.json();
            remember(code, url, data);
            return data;
        } catch (_) { return {}; }
    }

    const domReady = new Promise(resolve => {
        if (document.readyState !== 'loading') resolve();
        else document.addEventListener('DOMContentLoaded', resolve, { once: true });
    });

    let switchId = 0;
    async function use(code) {
        const id = ++switchId;
        const next = await loadDict(code);
        if (id !== switchId) return;   // overtaken by a later pick
        language = code;
        dict = next;
        await domReady;
        apply(document);
        window.dispatchEvent(new CustomEvent('nexa:i18nchange', { detail: { language } }));
    }

    // Initial language: from a cached dictionary this is set synchronously,
    // so page scripts that render before `ready` resolves are already right.
    (function init() {
        const code = storedLanguage();
        const hit = code !== 'English' && FILES[code] ? cached(FILES[code]) : null;
        if (hit) { language = code; dict = hit; }
    })();
    const ready = (async () => {
        const code = storedLanguage();
        if (code !== language || (code !== 'English' && !Object.keys(dict).length)) {
            dict = await loadDict(code);
            language = code;
        }
        await domReady;
        apply(document);
    })();

    window.addEventListener('nexa:languagechange', () => use(storedLanguage()));

    return {
        t, apply, ready,
        get language() { return language; },
        // For toLocaleDateString — dates follow the UI language.
        get locale() { return LOCALES[language] || 'en-GB'; },
    };
})();

window.NexaI18n = NexaI18n;
