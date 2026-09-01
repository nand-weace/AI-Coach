/*
  Shared line-chart helpers for the admin pages.
  Platform Admin plots messages and active users over time; Cost Analysis plots
  Anthropic spend. Both draw the same card, axis and hover tooltip, so the
  drawing code lives here rather than being copied into each template.

  Values are formatted by the caller: `fmt` for the tooltip and `fmtAxis` for
  the y-axis, both defaulting to a whole count.
*/

const _usageMeta = {};

function usageLabel(period, granularity, long) {
    const d = new Date(period + 'T00:00:00');
    const opts = long ? { day: 'numeric', month: 'short', year: 'numeric' }
                      : { day: 'numeric', month: 'short' };
    const txt = d.toLocaleDateString('en-GB', opts);
    return granularity === 'week' ? `Week of ${txt}` : txt;
}

// Axis ceiling as four equal, whole-number steps, so every gridline label is
// a round count with only a little headroom above the peak.
function niceCeil(v) {
    const raw = Math.max(v, 1) / 4;
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const norm = raw / mag;
    const tier = [1, 1.25, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10].find(t => norm <= t + 1e-9) || 10;
    return Math.max(1, Math.round(tier * mag)) * 4;
}

function usageCard(title, inner) {
    return `<div class="combo-chart-card">
        <div class="combo-chart-header">
            <div class="heat-title-wrap">
                <span class="heat-num"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg></span>
                <h3>${title}</h3>
            </div>
        </div>
        ${inner}
    </div>`;
}

/**
 * One line chart over `series`, drawn into `wrapId`.
 *   key      — the field of each bucket to plot
 *   color    — line/fill colour
 *   rows     — [[swatch, label, key], …] shown in the hover tooltip
 *   fmt      — value formatter for the tooltip (defaults to a whole count)
 *   fmtAxis  — value formatter for the y-axis labels (defaults to `fmt`)
 */
function renderUsageChart({ wrapId, title, series, granularity, key, color, rows, fmt, fmtAxis }) {
    fmt = fmt || (v => Math.round(v || 0).toLocaleString('en-GB'));
    fmtAxis = fmtAxis || fmt;
    const wrap = document.getElementById(wrapId);
    if (!wrap) return;

    if (series.length < 2) {
        wrap.innerHTML = usageCard(title, '<div class="usage-empty">Not enough activity yet to plot a trend.</div>');
        return;
    }

    const n = series.length;
    const W = 560, H = 300, padL = 42, padR = 14, padT = 16, padB = 32;
    const iW = W - padL - padR, iH = H - padT - padB;
    const maxVal = niceCeil(Math.max(...series.map(p => p[key]), 1));
    const toX = i => padL + (i / (n - 1)) * iW;
    const toY = v => padT + iH - (Math.max(0, v) / maxVal) * iH;

    const gridLines = [0, 0.25, 0.5, 0.75, 1].map(f => {
        const v = maxVal * f;
        const y = toY(v).toFixed(1);
        return `<line x1="${padL}" y1="${y}" x2="${padL + iW}" y2="${y}" stroke="rgba(45, 32, 90, 0.08)" stroke-width="1"/>
                <text x="${padL - 8}" y="${y}" text-anchor="end" dominant-baseline="middle" font-size="10" fill="#7f78a0">${fmtAxis(v)}</text>`;
    }).join('');

    // Spaced back from the most recent bucket, so the latest point is always
    // labelled and no label is crowded against it.
    const step = Math.max(1, Math.ceil(n / 6));
    const xLabels = series.map((p, i) => {
        if ((n - 1 - i) % step !== 0) return '';
        const d = new Date(p.period + 'T00:00:00');
        const lbl = d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
        // The end labels sit on the plot edges, so anchor them inward or
        // they run off the card.
        const anchor = i === n - 1 ? 'end' : i === 0 ? 'start' : 'middle';
        return `<text x="${toX(i).toFixed(1)}" y="${H - 10}" text-anchor="${anchor}" font-size="10" fill="#7f78a0">${lbl}</text>`;
    }).join('');

    const pts  = series.map((p, i) => `${toX(i).toFixed(1)},${toY(p[key]).toFixed(1)}`).join(' ');
    const area = `${padL},${(padT + iH).toFixed(1)} ${pts} ${(padL + iW).toFixed(1)},${(padT + iH).toFixed(1)}`;
    const last = series[n - 1];
    const gradId = wrapId + '-fill';

    const svg = `<svg id="${wrapId}-svg" viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;display:block;cursor:crosshair;">
        <defs>
            <linearGradient id="${gradId}" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stop-color="${color}" stop-opacity="0.22"/>
                <stop offset="100%" stop-color="${color}" stop-opacity="0"/>
            </linearGradient>
        </defs>
        ${gridLines}
        <polygon points="${area}" fill="url(#${gradId})"/>
        <polyline points="${pts}" fill="none" stroke="${color}" stroke-width="2.2" stroke-linejoin="round" stroke-linecap="round"/>
        <circle cx="${toX(n - 1).toFixed(1)}" cy="${toY(last[key]).toFixed(1)}" r="4" fill="${color}"/>
        ${xLabels}
        <g id="${wrapId}-hover" style="pointer-events:none;"></g>
        <rect id="${wrapId}-hit" x="${padL}" y="${padT}" width="${iW}" height="${iH}" fill="transparent"/>
    </svg>`;

    wrap.innerHTML = usageCard(title,
        `<div class="combo-chart-body">${svg}<div class="combo-tooltip" id="${wrapId}-tooltip"></div></div>`);

    _usageMeta[wrapId] = { series, granularity, key, color, rows, fmt, n, W, padT, padL, iW, iH, maxVal };

    const svgEl   = document.getElementById(`${wrapId}-svg`);
    const hitRect = document.getElementById(`${wrapId}-hit`);
    const hoverG  = document.getElementById(`${wrapId}-hover`);
    const tooltip = document.getElementById(`${wrapId}-tooltip`);
    const body    = svgEl.closest('.combo-chart-body');

    hitRect.addEventListener('mousemove', e => {
        const m = _usageMeta[wrapId];
        const svgRect = svgEl.getBoundingClientRect();
        const svgX = (e.clientX - svgRect.left) * (m.W / svgRect.width);
        const xi = Math.round(Math.max(0, Math.min(m.n - 1, (svgX - m.padL) / m.iW * (m.n - 1))));
        const p  = m.series[xi];
        const cx = (m.padL + (xi / (m.n - 1)) * m.iW).toFixed(1);
        const cy = (m.padT + m.iH - (Math.max(0, p[m.key]) / m.maxVal) * m.iH).toFixed(1);

        hoverG.innerHTML = `<line x1="${cx}" y1="${m.padT}" x2="${cx}" y2="${m.padT + m.iH}"
                stroke="rgba(0,0,0,0.18)" stroke-width="1" stroke-dasharray="4,3"/>
            <circle cx="${cx}" cy="${cy}" r="5" fill="${m.color}" stroke="white" stroke-width="2"/>`;

        const rowHtml = m.rows.map(([c, label, field]) => `<div class="combo-tooltip-row">
                <span class="combo-tooltip-swatch" style="background:${c}"></span>
                <span class="combo-tooltip-label">${label}</span>
                <span class="combo-tooltip-val">${m.fmt(p[field])}</span>
            </div>`).join('');
        // The last bucket is today (or this week) and is still filling — say so,
        // otherwise its dip reads as a drop in usage.
        const partial = xi === m.n - 1 ? ' · in progress' : '';
        tooltip.innerHTML = `<div class="combo-tooltip-date">${usageLabel(p.period, m.granularity, true)}${partial}</div>${rowHtml}`;

        const bodyRect = body.getBoundingClientRect();
        let tx = e.clientX - bodyRect.left + 14;
        const ty = e.clientY - bodyRect.top - 10;
        if (tx + 210 > bodyRect.width) tx = e.clientX - bodyRect.left - 210;
        tooltip.style.left = tx + 'px';
        tooltip.style.top  = ty + 'px';
        tooltip.style.display = 'block';
    });

    hitRect.addEventListener('mouseleave', () => {
        hoverG.innerHTML = '';
        tooltip.style.display = 'none';
    });
}
