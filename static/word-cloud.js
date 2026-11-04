/*
  Org word + theme cloud card, shared by the corporate dashboard and Platform
  Admin. The two clouds sit side by side: raw words on the left, the themes an
  LLM grouped the chats into on the right (see word_cloud_job.py).

  Shows one saved pair per day (the date picker steps back through earlier
  ones). Today's is built server-side on its first view or on Refresh; while
  that runs the card long-polls /api/word-cloud/status, which holds each
  request up to 20s and answers as soon as the run settles.

    const cloud = NexaWordCloud.create({
        mount:   document.getElementById('word-cloud'),
        headers: getAuthHeaders,          // () => fetch headers
        orgSlug: () => slug | null,       // admin only; null = "All" picked, nothing to show
                                          // (left out, the server uses the caller's own org)
    });
    cloud.load();                         // again whenever the org changes
*/

const NexaWordCloud = (() => {
    const POLL_WAIT_SECS = 20;
    const MAX_POLL_ERRORS = 3;

    function esc(str) {
        return String(str ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    function dayLabel(iso, today) {
        if (iso === today) return 'Today';
        const d = new Date(iso + 'T00:00:00');
        return d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' });
    }

    // Biggest words in the middle, smaller ones fanning out to either side,
    // so the wrapped rows read as a cloud rather than a ranked list.
    function cloudOrder(words) {
        const out = [];
        words.forEach((w, i) => { if (i % 2) out.push(w); else out.unshift(w); });
        return out;
    }

    // `range` is the font-size spread in rem above the 0.85rem floor; themes
    // run several words long, so they get a narrower one than single words.
    function cloudHtml(words, { range, unit }) {
        const max = Math.max(...words.map(w => w.weight));
        const min = Math.min(...words.map(w => w.weight));
        const span = Math.sqrt(max) - Math.sqrt(min) || 1;
        return cloudOrder(words).map(w => {
            const t = (Math.sqrt(w.weight) - Math.sqrt(min)) / span;   // 0 … 1
            const size = (0.85 + t * range).toFixed(2);
            const tier = t > 0.66 ? 1 : t > 0.33 ? 2 : t > 0.12 ? 3 : 4;
            // Shown on hover/focus by the CSS tooltip (word-cloud.css), and read
            // out as the label — a native title would double it up.
            const people = `${w.people} ${w.people === 1 ? 'person' : 'people'} talked about this`;
            const count = `${w.weight.toLocaleString('en-GB')} ${unit}`;
            return `<span class="wc-word wc-tier-${tier}" style="font-size:${size}rem" tabindex="0"
                data-tip="${esc(people)}" data-sub="${esc(count)}"
                aria-label="${esc(`${w.text}: ${people}, ${count}`)}">${esc(w.text)}</span>`;
        }).join('');
    }

    const SKELETON = '<div class="wc-empty"><div class="skeleton" style="width:160px; margin:0 auto;"></div></div>';

    function panelHtml(title, inner) {
        return `<section class="wc-panel">
            <div class="wc-panel-head"><h4>${title}</h4></div>
            ${inner}
        </section>`;
    }

    function create({ mount, headers, orgSlug }) {
        mount.innerHTML = `
            <div class="heat-card word-cloud-card">
                <div class="heat-card-header">
                    <div class="heat-title-wrap">
                        <span class="heat-num"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg></span>
                        <h3>What People Are Talking About</h3>
                    </div>
                    <select class="wc-date" aria-label="Cloud day" hidden></select>
                </div>
                <div class="wc-body" aria-live="polite"></div>
                <div class="combo-footer">
                    <span class="combo-footer-hint">Built from all Nexa chats. A word or theme only shows once 3+ people raise it.</span>
                    <span class="section-meta wc-meta"></span>
                    <button class="refresh-btn wc-refresh">Refresh</button>
                </div>
            </div>`;
        const body = mount.querySelector('.wc-body');
        const meta = mount.querySelector('.wc-meta');
        const btn = mount.querySelector('.wc-refresh');
        const picker = mount.querySelector('.wc-date');

        let req = 0;          // bumped by every load/refresh; older loops stop
        let viewDate = null;  // null = today

        function url(path, extra) {
            const params = new URLSearchParams(extra || {});
            const slug = orgSlug && orgSlug();
            if (slug) params.set('org_slug', slug);
            if (viewDate) params.set('date', viewDate);
            const qs = params.toString();
            return path + (qs ? '?' + qs : '');
        }

        async function call(path, opts, extra) {
            const res = await fetch(url(path, extra), Object.assign({ headers: headers() }, opts));
            const data = await res.json().catch(() => ({}));
            if (!res.ok && res.status !== 202) throw new Error(data.error || 'Request failed');
            return data;
        }

        function renderPicker(data) {
            const days = Array.from(new Set([data.today, ...(data.dates || [])])).sort().reverse();
            picker.innerHTML = days.map(d =>
                `<option value="${esc(d)}">${esc(dayLabel(d, data.today))}</option>`).join('');
            picker.value = data.date;
            picker.hidden = days.length < 2;
        }

        function render(data) {
            renderPicker(data);
            const words = data.words || [];
            const themes = data.themes;   // null: the theme pass hasn't run or couldn't
            const running = data.status === 'running';
            let note = '';
            if (running) note = words.length ? 'Updating with the latest conversations…' : '';
            else if (data.status === 'error') note = data.error || 'Could not build the word cloud.';

            if (words.length || running) {
                const stale = running ? ' is-stale' : '';
                const wordsInner = words.length
                    ? `<div class="wc-cloud${stale}">${cloudHtml(words, { range: 1.35, unit: 'mentions' })}</div>`
                    : SKELETON;
                let themesInner;
                if (themes && themes.length) {
                    themesInner = `<div class="wc-cloud${stale}">${cloudHtml(themes, { range: 0.95, unit: 'messages' })}</div>`;
                } else if (running) {
                    themesInner = SKELETON;
                } else if (themes) {
                    themesInner = '<div class="wc-empty">No theme is shared by 3+ people yet.</div>';
                } else {
                    themesInner = '<div class="wc-empty">Themes couldn\'t be worked out this time. Refresh to try again.</div>';
                }
                body.innerHTML = `<div class="wc-pair">
                        ${panelHtml('Frequent Words', wordsInner)}
                        ${panelHtml('Frequent Themes', themesInner)}
                    </div>`
                    + (running && !words.length
                        ? '<div class="wc-note">Building the clouds from your team\'s chats…</div>' : '')
                    + (note ? `<div class="wc-note${data.status === 'error' ? ' is-error' : ''}">${esc(note)}</div>` : '');
            } else if (data.status === 'error') {
                body.innerHTML = `<div class="sentiment-no-data">${esc(note)}</div>`;
            } else if (data.status === 'none') {
                body.innerHTML = '<div class="sentiment-no-data">No word cloud was saved for this day.</div>';
            } else {
                body.innerHTML = '<div class="sentiment-no-data">Not enough conversation yet — keep encouraging your team to chat with Nexa.</div>';
            }

            const parts = [];
            if (data.calculated_at) {
                const at = new Date(data.calculated_at);
                parts.push('Updated ' + at.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' }));
            }
            if (data.message_count) {
                parts.push(`${data.message_count.toLocaleString('en-GB')} messages · ${data.user_count} people`);
            }
            meta.textContent = parts.join(' · ');
            btn.disabled = running;
            btn.textContent = running ? 'Refreshing…' : 'Refresh';
        }

        function renderMessage(msg) {
            picker.hidden = true;
            meta.textContent = '';
            body.innerHTML = `<div class="sentiment-no-data">${esc(msg)}</div>`;
        }

        // Re-ask the long-poll endpoint until the run is no longer 'running'.
        // Each call returns within ~20s, so no single request can time out.
        async function poll(mine) {
            let errors = 0;
            while (mine === req) {
                try {
                    const data = await call('/api/word-cloud/status', {}, { wait: POLL_WAIT_SECS });
                    if (mine !== req) return;
                    errors = 0;
                    render(data);
                    if (data.status !== 'running') return;
                } catch (e) {
                    if (mine !== req) return;
                    if (++errors >= MAX_POLL_ERRORS) {
                        renderMessage('Lost track of the refresh. Reload the page to see the result.');
                        btn.disabled = false;
                        btn.textContent = 'Refresh';
                        return;
                    }
                    await new Promise(r => setTimeout(r, 2000 * errors));
                }
            }
        }

        async function load() {
            const mine = ++req;
            if (orgSlug && !orgSlug()) {
                btn.disabled = true;
                btn.title = 'Select a specific organisation to see its word cloud';
                return renderMessage('Select a specific organisation to see its word cloud.');
            }
            btn.title = '';
            body.innerHTML = '<div class="sentiment-no-data"><div class="skeleton" style="width:220px; margin:0 auto;"></div></div>';
            try {
                const data = await call('/api/word-cloud');
                if (mine !== req) return;
                render(data);
                if (data.status === 'running') poll(mine);
            } catch (e) {
                if (mine !== req) return;
                renderMessage(e.message || 'Failed to load the word cloud. Please refresh.');
                btn.disabled = false;
            }
        }

        async function refresh() {
            const mine = ++req;
            viewDate = null;
            btn.disabled = true;
            btn.textContent = 'Refreshing…';
            try {
                await call('/api/word-cloud/refresh', { method: 'POST' });
                if (mine === req) poll(mine);
            } catch (e) {
                if (mine !== req) return;
                renderMessage(e.message || 'Refresh failed. Please try again.');
                btn.disabled = false;
                btn.textContent = 'Refresh';
            }
        }

        btn.addEventListener('click', refresh);
        picker.addEventListener('change', () => {
            viewDate = picker.value === picker.options[0]?.value ? null : picker.value;
            load();
        });

        return {
            load() { viewDate = null; return load(); },
        };
    }

    return { create };
})();
