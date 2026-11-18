/*
  "Recommended Programmes" card on the corporate dashboard: the coaching,
  mentoring, workshops and training the org should offer, drawn from its
  sentiment report and theme cloud (see org_recommendations_job.py).

  The report is cached server-side and only rebuilt when a newer sentiment run
  or theme cloud exists, so a GET can take a while on the day's first view —
  the card shows a skeleton until it answers.

    const recs = NexaOrgRecommendations.create({
        mount:   document.getElementById('org-recommendations'),
        headers: getAuthHeaders,          // () => fetch headers
        orgSlug: () => slug | null,       // optional; left out, the server uses the caller's org
    });
    recs.load();                          // again after the sentiment is recalculated
*/

const NexaOrgRecommendations = (() => {
    const MAX_RECS = 6;     // cards shown, 3 per row
    const TYPES = {
        coaching:  { label: 'Coaching',  plural: 'Coaching' },
        mentoring: { label: 'Mentoring', plural: 'Mentoring' },
        workshop:  { label: 'Workshop',  plural: 'Workshops' },
        training:  { label: 'Training',  plural: 'Training' },
    };

    const ICONS = {
        coaching:  '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
        mentoring: '<path d="M12 2l3 6 6 .9-4.5 4.4 1 6.2L12 16.6 6.5 19.5l1-6.2L3 8.9 9 8z"/>',
        workshop:  '<rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/>',
        training:  '<path d="M22 10L12 5 2 10l10 5 10-5z"/><path d="M6 12v5c3 2 9 2 12 0v-5"/>',
    };

    function esc(str) {
        return String(str ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    function icon(type, size = 14) {
        return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[type] || ''}</svg>`;
    }

    function band(score) {
        return score < 35 ? 'low' : score < 65 ? 'mid' : 'high';
    }

    function addressHtml(a) {
        if (a.kind === 'dimension') {
            // Scores are stored 0-100; the dashboard speaks in tenths.
            const s = typeof a.score === 'number' ? ` <b>${(a.score / 10).toFixed(1)}</b>` : '';
            return `<span class="orec-tag orec-tag-dim orec-band-${band(a.score ?? 50)}" title="Sentiment dimension">${esc(a.label)}${s}</span>`;
        }
        return `<span class="orec-tag orec-tag-theme" title="Frequent theme">#${esc(a.label)}</span>`;
    }

    function itemHtml(r, readOnly) {
        let cta;
        const t = TYPES[r.type] || { label: r.type };
        return `<article class="orec-item orec-${esc(r.type)}" data-type="${esc(r.type)}">
            <div class="orec-item-top">
                <span class="orec-type">${icon(r.type, 13)}${esc(t.label)}</span>
            </div>
            <h4 class="orec-title">${esc(r.title)}</h4>
            <div class="orec-tags"><span class="orec-k">Addresses</span>${(r.addresses || []).map(addressHtml).join('')}</div>
            ${(cta = ctaHtml(r, false, readOnly)) ? `<div class="orec-cta">${cta}</div>` : ''}
        </article>`;
    }

    const CHECK = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="20 6 9 17 4 12"/></svg>';

    // readOnly: Platform Admin — the WeAce side sees whether the org has asked,
    // but can't ask on its behalf.
    function ctaHtml(r, justNow, readOnly) {
        if (readOnly) {
            return r.interested
                ? `<span class="orec-interested">${CHECK}Interest requested</span>
                   <span class="orec-cta-note">The organisation asked WeAce to reach out about this.</span>`
                : '';
        }
        if (r.interested) {
            return `<span class="orec-interested">${CHECK}Interest registered</span>
                <span class="orec-cta-note">${justNow ? 'Thanks — a' : 'A'} WeAce representative will reach out to you${justNow ? ' shortly' : ''}.</span>`;
        }
        return `<button type="button" class="orec-interest-btn" data-title="${esc(r.title)}">I'm interested</button>
            <span class="orec-cta-note"></span>`;
    }

    const SKELETON = `<div class="orec-grid">${'<div class="orec-item orec-skel"><div class="skeleton" style="width:40%"></div><div class="skeleton" style="width:80%;margin-top:14px"></div><div class="skeleton" style="width:95%;margin-top:10px"></div><div class="skeleton" style="width:70%;margin-top:6px"></div></div>'.repeat(3)}</div>`;

    function create({ mount, headers, orgSlug, readOnly = false,
                      noOrgText = 'Select an organisation to see its recommended programmes.' }) {
        mount.innerHTML = `
            <div class="heat-card orec-card">
                <div class="heat-card-header">
                    <div class="heat-title-wrap">
                        <span class="heat-num"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18h6M10 22h4"/><path d="M12 2a7 7 0 0 0-4 12.7c.6.5 1 1.3 1 2.3h6c0-1 .4-1.8 1-2.3A7 7 0 0 0 12 2z"/></svg></span>
                        <h3>Recommended Programmes</h3>
                    </div>
                    <div class="orec-filter" role="tablist" aria-label="Filter by programme type"></div>
                </div>
                <div class="orec-body" aria-live="polite"></div>
                <div class="combo-footer">
                    <span class="combo-footer-hint">Based on Team Sentiment and Frequent Themes — programme types to offer, not specific providers.</span>
                    <span class="section-meta orec-meta-line"></span>
                    <button class="refresh-btn orec-refresh">Regenerate</button>
                </div>
            </div>`;
        const body = mount.querySelector('.orec-body');
        const filter = mount.querySelector('.orec-filter');
        const metaEl = mount.querySelector('.orec-meta-line');
        const btn = mount.querySelector('.orec-refresh');
        let active = 'all';
        let req = 0;
        let shownSlug;   // org whose cards are on screen; a different one starts from a skeleton

        function url(path) {
            const slug = orgSlug && orgSlug();
            return path + (slug ? '?org_slug=' + encodeURIComponent(slug) : '');
        }

        function applyFilter() {
            filter.querySelectorAll('button').forEach(b => {
                const on = b.dataset.type === active;
                b.classList.toggle('active', on);
                b.setAttribute('aria-selected', on ? 'true' : 'false');
            });
            body.querySelectorAll('.orec-item[data-type]').forEach(el => {
                el.hidden = active !== 'all' && el.dataset.type !== active;
            });
        }

        function render(data) {
            const recs = ((data && data.recommendations) || []).slice(0, MAX_RECS);
            if (!recs.length) {
                filter.innerHTML = '';
                metaEl.textContent = '';
                body.innerHTML = '<div class="sentiment-no-data">Not enough insight yet to recommend programmes — they appear once Team Sentiment or Frequent Themes have data.</div>';
                return;
            }
            const counts = {};
            recs.forEach(r => { counts[r.type] = (counts[r.type] || 0) + 1; });
            if (active !== 'all' && !counts[active]) active = 'all';
            filter.innerHTML = [`<button type="button" class="combo-switch-btn" data-type="all" role="tab">All <span>${recs.length}</span></button>`]
                .concat(Object.keys(TYPES).filter(t => counts[t]).map(t =>
                    `<button type="button" class="combo-switch-btn" data-type="${t}" role="tab">${TYPES[t].plural} <span>${counts[t]}</span></button>`))
                .join('');
            body.innerHTML = `<div class="orec-grid">${recs.map(r => itemHtml(r, readOnly)).join('')}</div>`;
            if (data.calculated_at) {
                const at = new Date(data.calculated_at);
                metaEl.textContent = 'Updated ' + at.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' });
            }
            applyFilter();
        }

        filter.addEventListener('click', e => {
            const b = e.target.closest('button[data-type]');
            if (!b) return;
            active = b.dataset.type;
            applyFilter();
        });

        body.addEventListener('click', async e => {
            const b = e.target.closest('.orec-interest-btn');
            if (!b || b.disabled) return;
            const cta = b.closest('.orec-cta');
            const note = cta.querySelector('.orec-cta-note');
            b.disabled = true;
            b.textContent = 'Sending…';
            try {
                const res = await fetch(url('/api/org-recommendations/interest'), {
                    method: 'POST', credentials: 'omit',
                    headers: Object.assign({ 'Content-Type': 'application/json' }, headers()),
                    body: JSON.stringify({ title: b.dataset.title }),
                });
                const data = await res.json().catch(() => ({}));
                if (!res.ok) throw new Error(data.error || 'Could not send. Please try again.');
                cta.innerHTML = ctaHtml({ interested: true }, true);
                cta.classList.add('is-done');
            } catch (err) {
                b.disabled = false;
                b.textContent = "I'm interested";
                note.textContent = err.message;
                note.classList.add('is-error');
            }
        });

        async function fetchInto(path, opts, busyLabel) {
            const mine = ++req;
            const slug = orgSlug ? orgSlug() : undefined;
            if (slug === null) {   // "All organisations" picked
                shownSlug = null;
                filter.innerHTML = '';
                metaEl.textContent = '';
                btn.disabled = true;
                body.innerHTML = `<div class="sentiment-no-data">${esc(noOrgText)}</div>`;
                return;
            }
            btn.disabled = true;
            btn.textContent = busyLabel;
            const grid = slug === shownSlug && body.querySelector('.orec-grid');
            shownSlug = slug;
            if (grid) grid.classList.add('is-stale');
            else body.innerHTML = SKELETON + '<div class="wc-note">Matching programmes to your team\'s challenges…</div>';
            try {
                const res = await fetch(url(path), Object.assign({ headers: headers(), credentials: 'omit' }, opts));
                const data = await res.json().catch(() => ({}));
                if (mine !== req) return;
                if (!res.ok) throw new Error(data.error || 'Request failed');
                render(data);
            } catch (err) {
                if (mine !== req) return;
                const grid = body.querySelector('.orec-grid.is-stale');
                if (grid) {
                    grid.classList.remove('is-stale');
                    metaEl.textContent = err.message || 'Could not refresh.';
                } else {
                    body.innerHTML = `<div class="sentiment-no-data">${esc(err.message || 'Could not load recommendations.')}</div>`;
                }
            } finally {
                if (mine === req) {
                    btn.disabled = false;
                    btn.textContent = 'Regenerate';
                }
            }
        }

        return {
            load: () => fetchInto('/api/org-recommendations', {}, 'Loading…'),
            refresh: () => fetchInto('/api/org-recommendations/refresh', { method: 'POST' }, 'Regenerating…'),
            bindRefresh() { btn.addEventListener('click', () => this.refresh()); },
        };
    }

    return {
        create(opts) {
            const api = create(opts);
            api.bindRefresh();
            return api;
        },
    };
})();
