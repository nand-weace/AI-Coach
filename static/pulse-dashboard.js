/*
  Pulse Dashboard — the anonymised org-wide view of Daily Pulse. Shared by the
  corporate super admin's /pulse page and Platform Admin's Dashboard, so both
  draw exactly the same numbers and mood mix.

  Data comes from /api/pulse/aggregate, which never returns note text — counts,
  and moods are the whole of what an admin can see.

    const pulse = NexaPulseDashboard.create({
        mount:      document.getElementById('pulse-dashboard'),
        headers:    getAuthHeaders,           // () => fetch headers
        orgSlug:    () => slug | null,        // admin only; null = no single org picked
        noOrgText:  'Select an organisation…',// shown while orgSlug() is null
        lockedText: 'Not on Nexa Pro…',       // shown on a 403 instead of the API's upgrade pitch
        days:       30,                       // window shown (7, 30 or 90); default 30
    });
    pulse.load();                             // again whenever the org changes
*/

const NexaPulseDashboard = (() => {
    const PULSE_MOOD_LABELS = { 1: 'Terrible', 2: 'Poor', 3: 'Okay', 4: 'Good', 5: 'Great' };

    function escHtml(str) {
        return String(str ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    function renderMoodMix(counts, total) {
        const rows = [5, 4, 3, 2, 1].map(m => {
            const n = counts[String(m)] || 0;
            const pct = total ? Math.round((n / total) * 100) : 0;
            return `<div class="pulse-mood-row">
                        <span class="pulse-mood-name">${PULSE_MOOD_LABELS[m]}</span>
                        <div class="pulse-mood-track"><div class="pulse-mood-fill" style="width:${pct}%"></div></div>
                        <span class="pulse-mood-pct">${pct}%</span>
                    </div>`;
        }).join('');
        return `<div class="pulse-mood-bars">${rows}</div>`;
    }

    function renderPulse(body, data) {
        const avg = data.avg_mood !== null && data.avg_mood !== undefined ? data.avg_mood.toFixed(1) : '—';
        const nearest = data.avg_mood ? PULSE_MOOD_LABELS[Math.round(data.avg_mood)] : '';

        // One card in two columns: headline numbers and the mood mix.
        // Stacks on narrow screens.
        body.innerHTML = `
            <div class="pulse-compact">
                <div class="pulse-pane pulse-stats">
                    <div class="pulse-stat">
                        <div class="pulse-stat-label">Responses</div>
                        <div class="pulse-stat-value">${data.responses}</div>
                        <div class="pulse-stat-sub">last ${data.days} days</div>
                    </div>
                    <div class="pulse-stat">
                        <div class="pulse-stat-label">Average mood</div>
                        <div class="pulse-stat-value">${avg}<span class="pulse-stat-of"> / 5</span></div>
                        <div class="pulse-stat-sub">${nearest ? 'around &ldquo;' + nearest + '&rdquo;' : '&nbsp;'}</div>
                    </div>
                </div>
                <div class="pulse-pane">
                    <h4>Mood mix</h4>
                    <div class="pulse-pane-sub">Share of responses at each mood</div>
                    ${renderMoodMix(data.mood_counts || {}, data.responses)}
                </div>
            </div>`;
    }

    function create({ mount, headers, orgSlug, noOrgText, lockedText, days = 30 }) {
        mount.innerHTML = `
            <div class="pulse-body">
                <div class="pulse-empty"><div class="skeleton" style="width:200px; margin:0 auto;"></div></div>
            </div>
            <div class="pulse-privacy">
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>
                </svg>
                <span>Anonymous: check-ins aren't linked to who sent them, and notes are never shown.</span>
            </div>`;
        const body = mount.querySelector('.pulse-body');
        let req = 0;   // drops responses a newer load has overtaken

        async function load() {
            const mine = ++req;
            const slug = orgSlug ? orgSlug() : null;
            if (orgSlug && !slug) {
                body.innerHTML = `<div class="pulse-empty">${escHtml(noOrgText || 'Select an organisation to see its pulse.')}</div>`;
                return;
            }
            body.innerHTML = '<div class="pulse-empty"><div class="skeleton" style="width:200px; margin:0 auto;"></div></div>';
            const params = new URLSearchParams({ days: String(days) });
            if (slug) params.set('org_slug', slug);
            try {
                const res = await fetch('/api/pulse/aggregate?' + params, { headers: headers(), credentials: 'omit' });
                const data = await res.json().catch(() => ({}));
                if (mine !== req) return;
                if (!res.ok) {
                    const msg = res.status === 403 && lockedText ? lockedText : (data.error || 'Could not load the pulse.');
                    body.innerHTML = `<div class="pulse-empty">${escHtml(msg)}</div>`;
                    return;
                }
                renderPulse(body, data);
            } catch (_) {
                if (mine !== req) return;
                body.innerHTML = '<div class="pulse-empty">Request failed. Please try again.</div>';
            }
        }

        return { load };
    }

    return { create };
})();
