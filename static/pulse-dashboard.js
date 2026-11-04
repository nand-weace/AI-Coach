/*
  Pulse Dashboard — the anonymised org-wide view of Daily Pulse. Shared by the
  corporate super admin's /pulse page and Platform Admin's Dashboard, so both
  draw exactly the same tiles, charts and mood mix.

  Data comes from /api/pulse/aggregate, which never returns note text — counts,
  moods and their trend over time are the whole of what an admin can see.

    const pulse = NexaPulseDashboard.create({
        mount:      document.getElementById('pulse-dashboard'),
        headers:    getAuthHeaders,           // () => fetch headers
        orgSlug:    () => slug | null,        // admin only; null = no single org picked
        noOrgText:  'Select an organisation…',// shown while orgSlug() is null
        lockedText: 'Not on Nexa Pro…',       // shown on a 403 instead of the API's upgrade pitch
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

    // ── Over time ────────────────────────────────────────────────────────────
    // The API's trend only has rows for days someone responded, so the window
    // is laid out day by day here: a quiet day is a real zero on the
    // submissions chart and a gap (not a dip) on the rating line.
    function pulseDaysInWindow(days) {
        const out = [];
        const today = new Date();
        today.setHours(0, 0, 0, 0);
        for (let i = days; i >= 0; i--) {
            const d = new Date(today);
            d.setDate(today.getDate() - i);
            const iso = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
            out.push(iso);
        }
        return out;
    }

    const fmtDay = (iso, opts) => new Date(iso + 'T00:00:00').toLocaleDateString('en-GB', opts || { day: 'numeric', month: 'short' });

    // Shared frame for both charts: x is the day index across the window.
    function pulseChartFrame(n) {
        const W = 520, H = 200, padL = 30, padR = 10, padT = 12, padB = 26;
        const iW = W - padL - padR, iH = H - padT - padB;
        const slot = iW / n;
        return { W, H, padL, padR, padT, padB, iW, iH, slot,
                 cx: i => padL + slot * (i + 0.5) };
    }

    function pulseXLabels(f, dates) {
        const n = dates.length;
        const step = Math.max(1, Math.ceil(n / 6));
        return dates.map((d, i) => (i % step === 0 || i === n - 1)
            ? `<text class="axis-text" x="${f.cx(i).toFixed(1)}" y="${f.H - 6}" text-anchor="middle">${fmtDay(d)}</text>`
            : '').join('');
    }

    function renderSubmissionsChart(dates, byDate) {
        const f = pulseChartFrame(dates.length);
        const counts = dates.map(d => (byDate[d] || {}).responses || 0);
        const max = Math.max(1, ...counts);
        // Whole-number ticks — these are people, not fractions of them.
        const stepV = Math.max(1, Math.ceil(max / 4));
        const top = stepV * Math.ceil(max / stepV);
        const toY = v => f.padT + f.iH - (v / top) * f.iH;
        const ticks = [];
        for (let v = 0; v <= top; v += stepV) ticks.push(v);
        const grid = ticks.map(v => `<line class="grid-line" x1="${f.padL}" x2="${f.W - f.padR}" y1="${toY(v).toFixed(1)}" y2="${toY(v).toFixed(1)}"/>
            <text class="axis-text" x="${f.padL - 6}" y="${toY(v).toFixed(1)}" text-anchor="end" dominant-baseline="middle">${v}</text>`).join('');
        // 2px surface gap between neighbours; top corners rounded, base square.
        const bw = Math.max(1, f.slot - 2);
        const bars = counts.map((c, i) => {
            const x = f.cx(i) - bw / 2;
            const hit = `<rect class="hit" data-i="${i}" x="${(f.padL + f.slot * i).toFixed(1)}" y="${f.padT}" width="${f.slot.toFixed(1)}" height="${f.iH}"/>`;
            if (!c) return hit;
            const y = toY(c), h = f.padT + f.iH - y;
            const r = Math.min(4, bw / 2, h);
            const path = `M${x.toFixed(1)},${(y + h).toFixed(1)} V${(y + r).toFixed(1)} Q${x.toFixed(1)},${y.toFixed(1)} ${(x + r).toFixed(1)},${y.toFixed(1)}`
                       + ` H${(x + bw - r).toFixed(1)} Q${(x + bw).toFixed(1)},${y.toFixed(1)} ${(x + bw).toFixed(1)},${(y + r).toFixed(1)} V${(y + h).toFixed(1)} Z`;
            return hit + `<path class="bar" d="${path}"/>`;
        }).join('');
        return `<div class="pulse-chart" data-chart="submissions">
            <svg viewBox="0 0 ${f.W} ${f.H}" role="img" aria-label="Pulse submissions per day">${grid}${bars}${pulseXLabels(f, dates)}</svg>
            <div class="pulse-tip"></div>
        </div>`;
    }

    function renderRatingChart(dates, byDate) {
        const f = pulseChartFrame(dates.length);
        const toY = v => f.padT + f.iH - ((v - 1) / 4) * f.iH;
        const grid = [1, 2, 3, 4, 5].map(v => `<line class="grid-line" x1="${f.padL}" x2="${f.W - f.padR}" y1="${toY(v).toFixed(1)}" y2="${toY(v).toFixed(1)}"/>
            <text class="axis-text" x="${f.padL - 6}" y="${toY(v).toFixed(1)}" text-anchor="end" dominant-baseline="middle">${v}</text>`).join('');
        const pts = dates.map((d, i) => byDate[d] ? { i, v: byDate[d].avg_mood } : null).filter(Boolean);
        // Consecutive responding days join up; a day with no responses breaks
        // the line rather than being drawn through as if it had been measured.
        const segs = [];
        pts.forEach(p => {
            const last = segs[segs.length - 1];
            if (last && p.i === last[last.length - 1].i + 1) last.push(p);
            else segs.push([p]);
        });
        const lines = segs.filter(sg => sg.length > 1).map(sg =>
            `<polyline points="${sg.map(p => `${f.cx(p.i).toFixed(1)},${toY(p.v).toFixed(1)}`).join(' ')}" fill="none" stroke="var(--primary-color)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`).join('');
        const dots = pts.map(p =>
            `<circle cx="${f.cx(p.i).toFixed(1)}" cy="${toY(p.v).toFixed(1)}" r="4" fill="var(--primary-color)" stroke="#fff" stroke-width="2"/>`).join('');
        const hits = dates.map((d, i) =>
            `<rect class="hit" data-i="${i}" x="${(f.padL + f.slot * i).toFixed(1)}" y="${f.padT}" width="${f.slot.toFixed(1)}" height="${f.iH}"/>`).join('');
        return `<div class="pulse-chart" data-chart="rating">
            <svg viewBox="0 0 ${f.W} ${f.H}" role="img" aria-label="Average pulse rating per day, 1 to 5">${grid}<g class="hover-g"></g>${lines}${dots}${pulseXLabels(f, dates)}${hits}</svg>
            <div class="pulse-tip"></div>
        </div>`;
    }

    // One tooltip per chart, positioned over the hovered day.
    function wirePulseCharts(root, dates, byDate) {
        root.querySelectorAll('.pulse-chart').forEach(chart => {
            const kind = chart.dataset.chart;
            const svg = chart.querySelector('svg');
            const tip = chart.querySelector('.pulse-tip');
            const f = pulseChartFrame(dates.length);
            const hoverG = chart.querySelector('.hover-g');
            chart.querySelectorAll('.hit').forEach(hit => {
                hit.addEventListener('mouseenter', () => {
                    const i = +hit.dataset.i;
                    const row = byDate[dates[i]];
                    const val = kind === 'submissions'
                        ? `${row ? row.responses : 0} submission${row && row.responses === 1 ? '' : 's'}`
                        : (row ? `${row.avg_mood.toFixed(1)} / 5 avg · ${row.responses} response${row.responses === 1 ? '' : 's'}` : 'No responses');
                    tip.innerHTML = `<div class="pulse-tip-date">${fmtDay(dates[i], { weekday: 'short', day: 'numeric', month: 'short' })}</div><div class="pulse-tip-val">${val}</div>`;
                    if (hoverG) hoverG.innerHTML = `<line class="hover-line" x1="${f.cx(i).toFixed(1)}" x2="${f.cx(i).toFixed(1)}" y1="${f.padT}" y2="${f.padT + f.iH}"/>`;
                    const scale = svg.getBoundingClientRect().width / f.W;
                    tip.style.display = 'block';
                    const x = f.cx(i) * scale;
                    const left = Math.min(Math.max(0, x - tip.offsetWidth / 2), chart.clientWidth - tip.offsetWidth);
                    tip.style.left = left + 'px';
                    tip.style.top = (f.padT * scale - tip.offsetHeight - 4) + 'px';
                });
                hit.addEventListener('mouseleave', () => {
                    tip.style.display = 'none';
                    if (hoverG) hoverG.innerHTML = '';
                });
            });
        });
    }

    function renderPulse(body, data) {
        const avg = data.avg_mood !== null && data.avg_mood !== undefined ? data.avg_mood.toFixed(1) : '—';
        const nearest = data.avg_mood ? PULSE_MOOD_LABELS[Math.round(data.avg_mood)] : '';
        const dates = pulseDaysInWindow(data.days);
        const byDate = Object.fromEntries((data.trend || []).map(t => [t.date, t]));

        body.innerHTML = `
            <div class="pulse-tiles">
                <div class="pulse-tile">
                    <div class="pulse-tile-label">Responses</div>
                    <div class="pulse-tile-value">${data.responses}</div>
                    <div class="pulse-tile-sub">in the last ${data.days} days</div>
                </div>
                <div class="pulse-tile">
                    <div class="pulse-tile-label">Average mood</div>
                    <div class="pulse-tile-value">${avg}<span style="font-size:0.9rem; font-weight:500; color:var(--text-muted);"> / 5</span></div>
                    <div class="pulse-tile-sub">${nearest ? 'around &ldquo;' + nearest + '&rdquo;' : ''}</div>
                </div>
            </div>
            <div class="pulse-time-grid">
                <div class="pulse-card">
                    <h4>Submissions over time</h4>
                    <div class="pulse-card-sub">Pulse check-ins per day</div>
                    ${renderSubmissionsChart(dates, byDate)}
                </div>
                <div class="pulse-card">
                    <h4>Average rating over time</h4>
                    <div class="pulse-card-sub">Mean mood per day, 1 (Terrible) to 5 (Great)</div>
                    ${renderRatingChart(dates, byDate)}
                </div>
            </div>
            <div class="pulse-card">
                <h4>How the days are landing</h4>
                <div class="pulse-card-sub">Share of responses at each mood</div>
                ${renderMoodMix(data.mood_counts || {}, data.responses)}
            </div>`;
        wirePulseCharts(body, dates, byDate);
    }

    function create({ mount, headers, orgSlug, noOrgText, lockedText }) {
        mount.innerHTML = `
            <div class="pulse-toolbar">
                <div class="pulse-window" role="group" aria-label="Pulse window">
                    <button type="button" data-days="7">7d</button>
                    <button type="button" data-days="30" class="active">30d</button>
                    <button type="button" data-days="90">90d</button>
                </div>
            </div>
            <div class="pulse-body">
                <div class="pulse-empty"><div class="skeleton" style="width:200px; margin:0 auto;"></div></div>
            </div>
            <div class="pulse-privacy">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>
                </svg>
                <span>Check-ins are stored with no link to the person who sent them, and no note is
                      ever shown here.</span>
            </div>`;
        const body = mount.querySelector('.pulse-body');
        const buttons = mount.querySelectorAll('.pulse-window button');
        let days = 30;
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

        buttons.forEach(btn => {
            btn.addEventListener('click', () => {
                days = parseInt(btn.dataset.days, 10);
                buttons.forEach(b => b.classList.toggle('active', b === btn));
                load();
            });
        });

        return { load };
    }

    return { create };
})();
