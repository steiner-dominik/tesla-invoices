// Tesla Invoices dashboard logic. Translations live in js/i18n.js (loaded
// first); this file provides rerenderAll() for language switches.
//
// NOTE: all URLs are RELATIVE (no leading "/") so they stay inside the
// Home Assistant ingress path prefix instead of escaping to the HA root.
const API_ANALYTICS = 'api/analytics';
const API_DOWNLOAD = 'api/download/';
const API_EMAIL = 'api/email/';
const API_SYNC = 'api/sync?month=all';
const API_FILES = 'api/files';
const API_RESCAN = 'api/files/rescan';
const API_SEND_SKIPPED = 'api/email/send-skipped';
const API_AUTH_START = 'api/auth/login/start';
const API_AUTH_COMPLETE = 'api/auth/login/complete';
const API_AUTH_TOKEN = 'api/auth/token';

// ---------- theme (Auto / Light / Dark) ----------

const THEME_KEY = 'tesla-invoices-theme';

function applyTheme(choice) {
    // "auto" removes the override so the prefers-color-scheme media
    // query decides — that is what Home Assistant (and the companion
    // app) report, so Auto follows the HA appearance.
    if (choice === 'light' || choice === 'dark') {
        document.documentElement.dataset.theme = choice;
    } else {
        choice = 'auto';
        delete document.documentElement.dataset.theme;
    }
    document.querySelectorAll('[data-theme-choice]').forEach((btn) => {
        btn.classList.toggle('active', btn.dataset.themeChoice === choice);
    });
}

document.querySelectorAll('[data-theme-choice]').forEach((btn) => {
    btn.addEventListener('click', () => {
        const choice = btn.dataset.themeChoice;
        try {
            if (choice === 'auto') localStorage.removeItem(THEME_KEY);
            else localStorage.setItem(THEME_KEY, choice);
        } catch (e) { /* private mode: theme just won't persist */ }
        applyTheme(choice);
    });
});
applyTheme((() => {
    try { return localStorage.getItem(THEME_KEY) || 'auto'; } catch (e) { return 'auto'; }
})());

// ---------- API + dialogs ----------

// Custom header on every mutating request: the backend rejects
// POST/DELETE without it, which blocks cross-site request forgery
// (HTML forms cannot set custom headers).
function apiFetch(url, options = {}) {
    options.headers = { 'X-Requested-With': 'tesla-invoices', ...(options.headers || {}) };
    return fetch(url, options);
}

// In-page replacement for prompt()/confirm()/alert(): the native
// dialogs are unreliable in embedded webviews (HA companion app),
// where they can silently return null and make buttons appear dead.
// Resolves to { value, checked } on OK, or null on cancel.
function showDialog({ title, message, okLabel = t('ok'), cancelLabel = t('cancel'), hideCancel = false,
                      input = null, checkbox = null, validate = null }) {
    const modal = document.getElementById('app-dialog');
    const inputEl = document.getElementById('dialog-input');
    const checkWrap = document.getElementById('dialog-check-wrap');
    const checkEl = document.getElementById('dialog-check');
    const errorEl = document.getElementById('dialog-error');
    const okBtn = document.getElementById('dialog-ok');
    const cancelBtn = document.getElementById('dialog-cancel');

    document.getElementById('dialog-title').textContent = title || '';
    document.getElementById('dialog-message').textContent = message || '';
    inputEl.hidden = !input;
    inputEl.value = input ? (input.value || '') : '';
    inputEl.placeholder = input ? (input.placeholder || '') : '';
    checkWrap.hidden = !checkbox;
    checkEl.checked = !!(checkbox && checkbox.checked);
    document.getElementById('dialog-check-label').textContent = checkbox ? checkbox.label : '';
    errorEl.hidden = true;
    okBtn.textContent = okLabel;
    cancelBtn.textContent = cancelLabel;
    cancelBtn.hidden = hideCancel;
    modal.hidden = false;
    if (input) inputEl.focus(); else okBtn.focus();

    return new Promise((resolve) => {
        function close(result) {
            modal.hidden = true;
            okBtn.removeEventListener('click', onOk);
            cancelBtn.removeEventListener('click', onCancel);
            modal.removeEventListener('click', onBackdrop);
            document.removeEventListener('keydown', onKey);
            resolve(result);
        }
        function onOk() {
            const value = inputEl.value.trim();
            if (validate) {
                const problem = validate(value);
                if (problem) {
                    errorEl.textContent = problem;
                    errorEl.hidden = false;
                    return;
                }
            }
            close({ value, checked: checkEl.checked });
        }
        function onCancel() { close(null); }
        function onBackdrop(e) { if (e.target === modal) close(null); }
        function onKey(e) {
            if (e.key === 'Escape') close(null);
            else if (e.key === 'Enter') onOk();
        }
        okBtn.addEventListener('click', onOk);
        cancelBtn.addEventListener('click', onCancel);
        modal.addEventListener('click', onBackdrop);
        document.addEventListener('keydown', onKey);
    });
}

function showError(message) {
    return showDialog({ title: t('error_title'), message, hideCancel: true });
}

const validEmail = (v) => v.includes('@') ? '' : t('invalid_email');

let allData = [];
let summary = {};
let syncState = {};
let filesLoaded = false;
const filters = { search: '', year: '', vehicle: '', type: '' };
const sort = { key: 'date', dir: -1 };

// ---------- helpers ----------

// Location shows the charging site only; subscriptions have none (the
// type badge already says "subscription", so no filler text there).
function rowLocation(item) {
    return item.site_name || '';
}

function rowVehicle(item) {
    return item.vehicle_name || item.vin || '';
}

function monthKey(dateStr) {
    return (dateStr || '').slice(0, 7); // YYYY-MM
}

// Numbers and month labels follow the UI language (de: 1.234,56)
function fmtMoney(value) {
    return (value || 0).toLocaleString(lang, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

// Per-line effective price per kWh (own currency); null when not applicable.
function pricePerKwh(item) {
    const kwh = item.energy_kwh || 0;
    const cost = item.total_cost || 0;
    if (item.type !== 'charging' || kwh <= 0 || cost <= 0) return null;
    return cost / kwh;
}

// All timestamps are shown as "YYYY-MM-DD HH:MM TZ" (24h clock) in
// the browser's time zone. Date-only values stay a plain date — no
// made-up midnight time.
function fmtTimestamp(value) {
    if (!value) return '-';
    const raw = String(value);
    if (!raw.includes('T') && !raw.includes(':')) return raw;
    // Exactly-midnight timestamps are date-only invoices
    // (subscriptions) — don't show a made-up "00:00" time.
    if (/T00:00:00(?:\.0+)?$/.test(raw)) return raw.slice(0, 10);
    const d = new Date(raw);
    if (isNaN(d)) return raw;
    const pad = (n) => String(n).padStart(2, '0');
    let tz = '';
    try {
        tz = new Intl.DateTimeFormat(undefined, { timeZoneName: 'short' })
            .formatToParts(d)
            .find((p) => p.type === 'timeZoneName').value;
    } catch (e) { /* very old browsers: omit the zone */ }
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate())
        + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes()) + (tz ? ' ' + tz : '');
}

function fmtBytes(n) {
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    return (n / (1024 * 1024)).toFixed(1) + ' MB';
}

function filteredData() {
    const q = filters.search.toLowerCase();
    return allData.filter((item) => {
        if (filters.year && !(item.date || '').startsWith(filters.year)) return false;
        if (filters.vehicle && rowVehicle(item) !== filters.vehicle) return false;
        if (filters.type && item.type !== filters.type) return false;
        if (q) {
            const haystack = [rowLocation(item), item.description, rowVehicle(item), item.type,
                item.filename, item.date].join(' ').toLowerCase();
            if (!haystack.includes(q)) return false;
        }
        return true;
    });
}

// Everything that holds dynamic, translated text — called by i18n.js
// after a language switch.
function rerenderAll() {
    populateFilterOptions();
    renderSyncState();
    renderEmailControls();
    renderAccountState();
    renderDownloadsBanner();
    applyFilters();
    if (filesLoaded) loadFiles();
}

// Both invoice types switched off is a legal "downloads paused" state;
// warn that syncs only verify the Tesla connection then.
function renderDownloadsBanner() {
    const paused = summary.charging_invoice_enabled === false
        && summary.subscription_invoice_enabled === false;
    document.getElementById('downloads-disabled-banner').hidden = !paused;
}

// ---------- tabs ----------

function filesTabVisible() {
    return !document.getElementById('tab-files').hidden;
}

document.querySelectorAll('.tab-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('.tab-btn').forEach((b) => b.classList.toggle('active', b === btn));
        for (const id of ['tab-dashboard', 'tab-files', 'tab-maintenance']) {
            document.getElementById(id).hidden = btn.dataset.tab !== id;
        }
        if (btn.dataset.tab === 'tab-files' && !filesLoaded) {
            filesLoaded = true;
            loadFiles();
        }
    });
});

// ---------- summary cards ----------

function renderCards(data) {
    const primary = summary.primary_currency || '';

    let costPrimary = 0;
    let kwh = 0;
    const currencies = new Set();
    const vehicles = new Set();
    for (const item of data) {
        const c = item.total_cost || 0;
        if (c) currencies.add(item.currency || '');
        if ((item.currency || '') === primary) costPrimary += c;
        if (item.type === 'charging') kwh += item.energy_kwh || 0;
        vehicles.add(rowVehicle(item));
    }

    document.getElementById('stat-count').textContent = data.length;
    document.getElementById('stat-kwh').textContent = kwh.toLocaleString(lang, { maximumFractionDigits: 1 }) + ' kWh';
    document.getElementById('stat-cost').textContent = fmtMoney(costPrimary) + ' ' + primary;

    const others = [...currencies].filter((c) => c && c !== primary);
    document.getElementById('stat-cost-note').textContent =
        others.length ? t('cost_note', { currencies: others.join(', ') }) : '';

    document.getElementById('stat-vehicles').textContent = [...vehicles].filter(Boolean).length;
}

// ---------- charts ----------

// First and last month across ALL currently filtered invoices — both
// charts use this as their x-axis range, so their timelines line up
// even when one kind of data (charging vs. subscription) starts later.
function monthRange(data) {
    let min = '', max = '';
    for (const item of data) {
        const key = monthKey(item.date);
        if (!key) continue;
        if (!min || key < min) min = key;
        if (!max || key > max) max = key;
    }
    return min ? [min, max] : null;
}

function monthlyBuckets(data, valueFn, range) {
    const buckets = new Map();
    for (const item of data) {
        const key = monthKey(item.date);
        if (!key) continue;
        buckets.set(key, (buckets.get(key) || 0) + valueFn(item));
    }
    // Fill the whole range (shared across both charts) with explicit
    // 0 entries so gap months show as 0 bars and the x-axis is
    // continuous. All months are kept (no cap), so the chart totals
    // always match the summary cards; use the year filter to zoom in.
    const sorted = [...buckets.keys()].sort();
    const [start, end] = range || [sorted[0], sorted[sorted.length - 1]];
    let keys = [];
    if (start) {
        const [startY, startM] = start.split('-').map(Number);
        const [endY, endM] = end.split('-').map(Number);
        for (let y = startY, m = startM; y < endY || (y === endY && m <= endM); m === 12 ? (y++, m = 1) : m++) {
            keys.push(y + '-' + String(m).padStart(2, '0'));
        }
    }
    return keys.map((k) => ({ month: k, value: buckets.get(k) || 0 }));
}

// Round an axis maximum up to a readable "nice" number.
function niceMax(value) {
    if (value <= 0) return 1;
    const pow = Math.pow(10, Math.floor(Math.log10(value)));
    const norm = value / pow;
    const nice = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10;
    return nice * pow;
}

function fmtAxis(value) {
    if (value >= 1000) return (value / 1000).toLocaleString(lang, { maximumFractionDigits: 1 }) + 'k';
    return value.toLocaleString(lang, { maximumFractionDigits: value < 10 ? 1 : 0 });
}

function monthLabel(key) {
    const [y, m] = key.split('-');
    return new Date(Number(y), Number(m) - 1, 1).toLocaleDateString(lang, { month: 'short', year: '2-digit' });
}

function renderBarChart(containerId, captionId, buckets, color, unit) {
    const container = document.getElementById(containerId);
    const caption = document.getElementById(captionId);
    container.replaceChildren();
    if (!buckets.length || buckets.every((b) => b.value === 0)) {
        caption.textContent = '';
        const empty = document.createElement('div');
        empty.className = 'chart-empty';
        empty.textContent = t('chart_empty');
        container.appendChild(empty);
        return;
    }

    const total = buckets.reduce((s, b) => s + b.value, 0);
    const fmtVal = (v) => v.toLocaleString(lang, { maximumFractionDigits: 2 }) + ' ' + unit;
    const defaultCaption = () => { caption.innerHTML = t('chart_total') + ' <b>' + fmtVal(total) + '</b>'; };
    defaultCaption();

    const W = 480, H = 190, padL = 38, padR = 6, padB = 24, padT = 10;
    const plotW = W - padL - padR;
    const plotH = H - padB - padT;
    const max = niceMax(Math.max(...buckets.map((b) => b.value)));
    const n = buckets.length;
    const slot = plotW / n;
    const barW = Math.max(4, Math.min(38, slot * 0.7));
    const svgNS = 'http://www.w3.org/2000/svg';
    const yFor = (v) => padT + plotH - (v / max) * plotH;

    const svg = document.createElementNS(svgNS, 'svg');
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`);

    // Y-axis gridlines + scale labels so magnitude is readable at a glance.
    const ticks = 4;
    for (let t = 0; t <= ticks; t++) {
        const v = (max / ticks) * t;
        const y = yFor(v);
        const line = document.createElementNS(svgNS, 'line');
        // Colors come from CSS classes so they follow the active theme
        line.setAttribute('class', t === 0 ? 'grid base' : 'grid');
        line.setAttribute('x1', padL);
        line.setAttribute('x2', W - padR);
        line.setAttribute('y1', y);
        line.setAttribute('y2', y);
        svg.appendChild(line);

        const label = document.createElementNS(svgNS, 'text');
        label.setAttribute('x', padL - 5);
        label.setAttribute('y', y + 3);
        label.setAttribute('text-anchor', 'end');
        label.setAttribute('font-size', '8.5');
        label.textContent = fmtAxis(v);
        svg.appendChild(label);
    }

    buckets.forEach((b, i) => {
        // Months with value 0 still get a thin stub bar, so it is
        // visible that the month exists but had nothing (and the
        // stub stays tappable for the caption).
        const h = Math.max((b.value / max) * plotH, 1.5);
        const x = padL + i * slot + (slot - barW) / 2;
        const y = padT + plotH - h;

        const rect = document.createElementNS(svgNS, 'rect');
        rect.setAttribute('class', b.value === 0 ? 'bar zero' : 'bar');
        rect.setAttribute('x', x);
        rect.setAttribute('y', y);
        rect.setAttribute('width', barW);
        rect.setAttribute('height', h);
        rect.setAttribute('rx', 2);
        // style.fill (not the fill attribute) so var(--…) resolves
        rect.style.fill = color;
        const title = document.createElementNS(svgNS, 'title');
        title.textContent = monthLabel(b.month) + ': ' + fmtVal(b.value);
        rect.appendChild(title);
        // Tap/hover updates the caption — works on mobile where :hover/title don't.
        const show = () => { caption.innerHTML = monthLabel(b.month) + ' · <b>' + fmtVal(b.value) + '</b>'; };
        rect.addEventListener('mouseenter', show);
        rect.addEventListener('mouseleave', defaultCaption);
        rect.addEventListener('click', show);
        svg.appendChild(rect);

        // x-axis label, thinned out when crowded
        const every = n > 16 ? 3 : n > 8 ? 2 : 1;
        if (i % every === 0) {
            const label = document.createElementNS(svgNS, 'text');
            label.setAttribute('x', x + barW / 2);
            label.setAttribute('y', H - padB + 12);
            label.setAttribute('text-anchor', 'middle');
            label.setAttribute('font-size', '8.5');
            const [yr, mo] = b.month.split('-');
            label.textContent = mo + '/' + yr.slice(2);
            svg.appendChild(label);
        }
    });

    container.appendChild(svg);
}

function renderCharts(data) {
    const primary = summary.primary_currency || '';
    const range = monthRange(data);
    renderBarChart(
        'chart-kwh', 'caption-kwh',
        monthlyBuckets(data.filter((d) => d.type === 'charging'), (d) => d.energy_kwh || 0, range),
        'var(--accent)', 'kWh'
    );
    document.getElementById('chart-cost-title').textContent =
        t('chart_cost') + (primary ? ' (' + primary + ')' : '');
    renderBarChart(
        'chart-cost', 'caption-cost',
        monthlyBuckets(data.filter((d) => (d.currency || '') === primary), (d) => d.total_cost || 0, range),
        'var(--green)', primary
    );
}

// ---------- table ----------

const sortAccessors = {
    date: (i) => i.date || '',
    vehicle: (i) => rowVehicle(i).toLowerCase(),
    type: (i) => i.type || '',
    location: (i) => rowLocation(i).toLowerCase(),
    energy: (i) => i.energy_kwh || 0,
    cost: (i) => i.total_cost || 0,
    perkwh: (i) => pricePerKwh(i) || 0,
};

function cell(text, className) {
    const td = document.createElement('td');
    if (className) td.className = className;
    td.textContent = text;
    return td;
}

function messageRow(text, isError) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 8;
    td.className = 'center' + (isError ? ' error' : '');
    td.textContent = text;
    tr.appendChild(td);
    return tr;
}

function renderTable(data) {
    const tbody = document.getElementById('invoice-table-body');
    tbody.replaceChildren();
    if (data.length === 0) {
        tbody.appendChild(messageRow(allData.length === 0
            ? (summary.token_configured === false ? t('no_token') : t('no_invoices'))
            : t('no_match')));
        return;
    }

    const accessor = sortAccessors[sort.key] || sortAccessors.date;
    const sorted = [...data].sort((a, b) => {
        const va = accessor(a), vb = accessor(b);
        return (va < vb ? -1 : va > vb ? 1 : 0) * sort.dir;
    });

    document.querySelectorAll('th.sortable').forEach((th) => {
        th.querySelector('.arrow').textContent = th.dataset.sort === sort.key ? (sort.dir > 0 ? '▲' : '▼') : '';
    });

    for (const item of sorted) {
        const tr = document.createElement('tr');
        const isCharging = item.type === 'charging';

        tr.appendChild(cell(fmtTimestamp(item.date)));
        tr.appendChild(cell(rowVehicle(item) || t('unknown')));

        const typeTd = document.createElement('td');
        const badge = document.createElement('span');
        badge.className = 'badge ' + (isCharging ? 'charging' : 'subscription');
        badge.textContent = item.type ? t('type_' + item.type) : '-';
        typeTd.appendChild(badge);
        tr.appendChild(typeTd);

        const loc = rowLocation(item) || '-';
        const locTd = cell(loc, 'loc');
        locTd.title = loc; // full text on hover, since the cell truncates
        tr.appendChild(locTd);

        tr.appendChild(cell(isCharging ? fmtMoney(item.energy_kwh) : '-', 'num'));
        tr.appendChild(cell(fmtMoney(item.total_cost) + ' ' + (item.currency || ''), 'num'));

        const rate = pricePerKwh(item);
        tr.appendChild(cell(rate !== null ? fmtMoney(rate) + ' ' + (item.currency || '') : '-', 'num'));

        const actionTd = document.createElement('td');
        actionTd.className = 'num';
        const actions = document.createElement('div');
        actions.className = 'row-actions';
        if (item.filename && item.filename.toLowerCase().endsWith('.pdf')) {
            const view = document.createElement('button');
            view.className = 'action';
            view.type = 'button';
            view.textContent = t('action_view');
            view.addEventListener('click', () => openPdf(item.filename));
            actions.appendChild(view);

            const dl = document.createElement('a');
            dl.className = 'action';
            dl.href = API_DOWNLOAD + encodeURIComponent(item.filename);
            dl.textContent = t('action_download');
            actions.appendChild(dl);

            if (summary.email_configured) {
                const mail = document.createElement('button');
                mail.className = 'action';
                mail.type = 'button';
                mail.textContent = item.email_sent ? t('action_resend') : t('action_email');
                mail.addEventListener('click', () => emailInvoice(item.filename, mail));
                actions.appendChild(mail);
            }
        }
        if (!actions.childElementCount) actions.textContent = '-';
        actionTd.appendChild(actions);
        tr.appendChild(actionTd);
        tbody.appendChild(tr);
    }
}

// ---------- in-app PDF viewer ----------

function openPdf(filename) {
    // Same-origin ingress URL in an iframe keeps the HA session, so the
    // mobile app does not bounce out to an external browser / re-login.
    const url = API_DOWNLOAD + encodeURIComponent(filename) + '?inline=true';
    document.getElementById('pdf-modal-title').textContent = filename;
    document.getElementById('pdf-modal-frame').src = url;
    document.getElementById('pdf-modal-dl').href = API_DOWNLOAD + encodeURIComponent(filename);
    document.getElementById('pdf-modal').hidden = false;
    document.body.style.overflow = 'hidden';
}

function closePdf() {
    document.getElementById('pdf-modal').hidden = true;
    document.getElementById('pdf-modal-frame').src = ''; // stop loading / free memory
    document.body.style.overflow = '';
}

document.getElementById('pdf-modal-close').addEventListener('click', closePdf);
document.getElementById('pdf-modal').addEventListener('click', (e) => {
    if (e.target.id === 'pdf-modal') closePdf(); // click on backdrop
});
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !document.getElementById('pdf-modal').hidden) closePdf();
});

async function emailInvoice(filename, btn) {
    // Recipient is editable per send; config's email.to is the default
    const result = await showDialog({
        title: t('dlg_email_title'),
        message: t('dlg_email_msg', { file: filename }),
        okLabel: t('btn_send'),
        input: { value: summary.email_default_to || '', placeholder: 'recipient@example.com' },
        validate: validEmail,
    });
    if (result === null) return; // cancelled
    btn.disabled = true;
    const original = btn.textContent;
    btn.textContent = t('sending');
    try {
        // Recipient in the body, not the query string (proxy logs)
        const response = await apiFetch(API_EMAIL + encodeURIComponent(filename), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ to: result.value }),
        });
        const body = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(body.detail || ('HTTP ' + response.status));
        btn.textContent = t('sent_check');
    } catch (error) {
        console.error('Failed to send email:', error);
        await showError(t('send_failed', { error: error.message }));
        btn.textContent = original;
        btn.disabled = false;
    }
}

// ---------- Tesla account login ----------

// Reflect the connection state in the setup banner and the
// Maintenance "Tesla account" section.
function renderAccountState() {
    const connected = !!summary.token_configured;
    const banner = document.getElementById('setup-banner');
    // The banner only nudges first-time users; once connected it is gone
    banner.hidden = connected;

    const statusEl = document.getElementById('account-status');
    statusEl.textContent = connected ? t('account_connected') : t('account_disconnected');
    statusEl.className = connected ? 'connected' : 'disconnected';
    document.getElementById('connect-btn').textContent =
        connected ? t('btn_reconnect') : t('btn_connect');
}

// Two ways in: the guided browser sign-in (default), or pasting a refresh
// token from a third-party tool when the browser flow is not workable.
let authMethod = 'browser';

function setAuthMethod(method) {
    authMethod = method;
    document.getElementById('auth-browser-pane').hidden = method !== 'browser';
    document.getElementById('auth-token-pane').hidden = method !== 'token';
    document.querySelectorAll('[data-auth-method]').forEach((btn) => {
        btn.classList.toggle('active', btn.dataset.authMethod === method);
    });
    document.getElementById('auth-error').hidden = true;
}

document.querySelectorAll('[data-auth-method]').forEach((btn) => {
    btn.addEventListener('click', () => setAuthMethod(btn.dataset.authMethod));
});

async function startAuth(statusEl) {
    if (statusEl) statusEl.textContent = '';

    const modal = document.getElementById('auth-modal');
    const input = document.getElementById('auth-input');
    const tokenInput = document.getElementById('auth-token-input');
    const errorEl = document.getElementById('auth-error');
    const completeBtn = document.getElementById('auth-complete');
    const openBtn = document.getElementById('auth-open-btn');

    input.value = '';
    tokenInput.value = '';
    errorEl.hidden = true;
    openBtn.removeAttribute('href');
    setAuthMethod('browser');
    modal.hidden = false;

    // Fetch the login URL in the background; the token pane works without it.
    try {
        const response = await apiFetch(API_AUTH_START, { method: 'POST' });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        openBtn.href = (await response.json()).url;
    } catch (error) {
        console.error('Failed to start login:', error);
        errorEl.textContent = t('auth_failed', { error: error.message });
        errorEl.hidden = false;
    }

    function close() {
        modal.hidden = true;
        completeBtn.removeEventListener('click', onComplete);
        document.getElementById('auth-cancel').removeEventListener('click', close);
        modal.removeEventListener('click', onBackdrop);
        document.removeEventListener('keydown', onKey);
    }
    function onBackdrop(e) { if (e.target === modal) close(); }
    function onKey(e) { if (e.key === 'Escape') close(); }

    async function onComplete() {
        const isToken = authMethod === 'token';
        const value = (isToken ? tokenInput : input).value.trim();
        if (!value) {
            errorEl.textContent = t(isToken ? 'auth_token_empty' : 'auth_no_code');
            errorEl.hidden = false;
            return;
        }
        completeBtn.disabled = true;
        completeBtn.textContent = t('auth_connecting');
        errorEl.hidden = true;
        try {
            const response = await apiFetch(isToken ? API_AUTH_TOKEN : API_AUTH_COMPLETE, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(isToken ? { refresh_token: value } : { callback_url: value }),
            });
            const body = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(body.detail || ('HTTP ' + response.status));
            close();
            if (statusEl) statusEl.textContent = t('auth_success');
            loadData(); // token_configured flips, a first sync is already running
        } catch (error) {
            console.error('Failed to complete login:', error);
            errorEl.textContent = t('auth_failed', { error: error.message });
            errorEl.hidden = false;
        } finally {
            completeBtn.disabled = false;
            completeBtn.textContent = t('btn_auth_complete');
        }
    }

    completeBtn.addEventListener('click', onComplete);
    document.getElementById('auth-cancel').addEventListener('click', close);
    modal.addEventListener('click', onBackdrop);
    document.addEventListener('keydown', onKey);
}

document.getElementById('setup-connect-btn').addEventListener('click', () => startAuth(null));
document.getElementById('connect-btn').addEventListener('click', () =>
    startAuth(document.getElementById('connect-status')));

// ---------- filters ----------

function populateFilterOptions() {
    const years = [...new Set(allData.map((i) => (i.date || '').slice(0, 4)).filter(Boolean))].sort().reverse();
    const vehicles = [...new Set(allData.map(rowVehicle).filter(Boolean))].sort();

    const yearSel = document.getElementById('filter-year');
    const current = yearSel.value;
    yearSel.replaceChildren(new Option(t('all_years'), ''));
    years.forEach((y) => yearSel.appendChild(new Option(y, y)));
    if ([...yearSel.options].some((o) => o.value === current)) yearSel.value = current;

    const vehicleSel = document.getElementById('filter-vehicle');
    const currentVehicle = vehicleSel.value;
    vehicleSel.replaceChildren(new Option(t('all_vehicles'), ''));
    vehicles.forEach((v) => vehicleSel.appendChild(new Option(v, v)));
    if ([...vehicleSel.options].some((o) => o.value === currentVehicle)) vehicleSel.value = currentVehicle;
}

function applyFilters() {
    const data = filteredData();
    renderCards(data);
    renderCharts(data);
    renderTable(data);
}

document.getElementById('filter-search').addEventListener('input', (e) => {
    filters.search = e.target.value.trim();
    applyFilters();
});
document.getElementById('filter-year').addEventListener('change', (e) => {
    filters.year = e.target.value;
    applyFilters();
});
document.getElementById('filter-vehicle').addEventListener('change', (e) => {
    filters.vehicle = e.target.value;
    applyFilters();
});
document.getElementById('filter-type').addEventListener('change', (e) => {
    filters.type = e.target.value;
    applyFilters();
});
document.querySelectorAll('th.sortable').forEach((th) => {
    th.addEventListener('click', () => {
        if (sort.key === th.dataset.sort) {
            sort.dir = -sort.dir;
        } else {
            sort.key = th.dataset.sort;
            sort.dir = th.dataset.sort === 'date' ? -1 : 1;
        }
        renderTable(filteredData());
    });
});

// ---------- sync status ----------

function renderSyncState() {
    const chip = document.getElementById('sync-chip');
    const banner = document.getElementById('sync-banner');

    if (syncState.running) {
        chip.textContent = t('chip_running');
        chip.classList.remove('warn');
    } else if (syncState.last_success) {
        chip.textContent = t('chip_last', { time: fmtTimestamp(syncState.last_success) });
        chip.classList.remove('warn');
    } else {
        chip.textContent = t('chip_none');
        chip.classList.add('warn');
    }

    // On a fresh install the sync "fails" only because no account is
    // connected yet — the friendly setup banner already covers that,
    // so don't also raise an alarming red error banner.
    const failed = syncState.last_result && syncState.last_result !== 'ok'
        && summary.token_configured !== false;
    banner.hidden = !failed;
    if (failed) {
        banner.textContent = t('banner_failed', { error: syncState.last_result });
    }

    document.getElementById('info-running').textContent = syncState.running ? t('yes') : t('no');
    document.getElementById('info-last-finished').textContent = fmtTimestamp(syncState.last_finished);
    document.getElementById('info-last-kind').textContent = syncState.last_kind || '-';
    document.getElementById('info-last-result').textContent = syncState.last_result || '-';
    document.getElementById('info-last-success').textContent = fmtTimestamp(syncState.last_success);
}

// ---------- data loading ----------

async function loadData() {
    const tbody = document.getElementById('invoice-table-body');
    try {
        const response = await fetch(API_ANALYTICS);
        if (!response.ok) throw new Error('HTTP ' + response.status);
        const result = await response.json();
        summary = result.summary || {};
        allData = result.data || [];
        syncState = result.sync || {};

        // Adopt the server's default language (HA language / LANGUAGE
        // env var) unless the user has picked one via the toggle.
        await setServerLanguage(summary.language);

        populateFilterOptions();
        renderSyncState();
        renderEmailControls();
        renderAccountState();
        renderDownloadsBanner();
        applyFilters();
    } catch (error) {
        console.error('Failed to load analytics:', error);
        tbody.replaceChildren(messageRow(t('load_failed'), true));
    }
}

// ---------- files tab ----------

async function loadFiles() {
    const status = document.getElementById('files-browser-status');
    const list = document.getElementById('files-browser-list');
    const preview = document.getElementById('files-browser-preview');
    try {
        const response = await fetch(API_FILES);
        if (!response.ok) throw new Error('HTTP ' + response.status);
        const result = await response.json();
        const files = result.files || [];
        status.textContent = t('files_count', { n: files.length });
        list.replaceChildren();
        preview.textContent = t('files_select');

        if (files.length === 0) {
            const empty = document.createElement('div');
            empty.className = 'center';
            empty.textContent = t('files_empty');
            list.appendChild(empty);
            return;
        }

        for (const file of files) {
            const item = document.createElement('div');
            item.className = 'browser-item';

            const name = document.createElement('span');
            name.className = 'fname';
            name.textContent = file.name;

            const meta = document.createElement('span');
            meta.className = 'fmeta';
            const tag = document.createElement('span');
            tag.className = 'ftag';
            tag.textContent = file.type;
            const info = document.createElement('span');
            // invoice date from the metadata sidecar; file mtime as fallback
            info.textContent = fmtBytes(file.size) + ' · ' + fmtTimestamp(file.date || file.modified);
            meta.append(tag, info);

            const actions = document.createElement('span');
            actions.className = 'factions';
            if (file.type === 'pdf') {
                const view = document.createElement('button');
                view.className = 'action';
                view.type = 'button';
                view.textContent = t('action_view');
                view.addEventListener('click', (e) => { e.stopPropagation(); openPdf(file.name); });
                actions.appendChild(view);
            }
            const dl = document.createElement('a');
            dl.className = 'action';
            dl.href = API_DOWNLOAD + encodeURIComponent(file.name);
            dl.textContent = t('action_download');
            dl.addEventListener('click', (e) => e.stopPropagation());
            actions.appendChild(dl);
            const del = document.createElement('button');
            del.className = 'action danger';
            del.type = 'button';
            del.textContent = t('action_delete');
            del.addEventListener('click', (e) => { e.stopPropagation(); deleteFile(file.name); });
            actions.appendChild(del);

            item.append(name, meta, actions);
            item.addEventListener('click', () => {
                document.querySelectorAll('.browser-item').forEach((c) => c.classList.remove('active'));
                item.classList.add('active');
                preview.textContent = file.preview || t('no_preview');
            });
            list.appendChild(item);
        }
    } catch (error) {
        console.error('Failed to load files:', error);
        status.textContent = t('files_load_failed');
        list.replaceChildren();
        preview.textContent = t('files_preview_failed');
    }
}

// ---------- maintenance actions ----------

// Maintenance controls that depend on the email configuration
function renderEmailControls() {
    const skipped = summary.email_skipped_count || 0;
    const btn = document.getElementById('send-skipped-btn');
    const status = document.getElementById('send-skipped-status');
    btn.textContent = skipped ? t('btn_send_skipped_n', { n: skipped }) : t('btn_send_skipped');
    if (!summary.email_configured) {
        btn.disabled = true;
        status.textContent = t('email_not_configured');
    } else {
        btn.disabled = skipped === 0 || btn.dataset.busy === '1';
        if (!btn.dataset.busy) status.textContent = skipped === 0 ? t('no_skipped') : '';
    }
}

document.getElementById('send-skipped-btn').addEventListener('click', async () => {
    const btn = document.getElementById('send-skipped-btn');
    const status = document.getElementById('send-skipped-status');
    const result = await showDialog({
        title: t('maint_backlog_title'),
        message: t('dlg_backlog_msg'),
        okLabel: t('btn_send'),
        input: { value: summary.email_default_to || '', placeholder: 'recipient@example.com' },
        validate: validEmail,
    });
    if (result === null) return; // cancelled
    btn.disabled = true;
    btn.dataset.busy = '1';
    status.textContent = t('sending');
    try {
        const response = await apiFetch(API_SEND_SKIPPED, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ to: result.value }),
        });
        const body = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(body.detail || ('HTTP ' + response.status));
        status.textContent = t('sent_summary', { n: body.sent || 0, m: body.emails || 0 });
    } catch (error) {
        console.error('Failed to send skipped invoices:', error);
        status.textContent = t('send_failed', { error: error.message });
    } finally {
        delete btn.dataset.busy;
        loadData();
    }
});

document.getElementById('sync-btn').addEventListener('click', async () => {
    // Emailing is opt-in per run (a full history sync could produce
    // one mail per invoice); the checkbox only appears when the
    // automatic email export is enabled at all. Unchecked — the safe
    // default — marks new invoices as skipped; they can still be
    // sent later via "Email backlog".
    const result = await showDialog({
        title: t('dlg_sync_title'),
        message: t('dlg_sync_msg'),
        okLabel: t('btn_start_sync'),
        checkbox: summary.email_export_enabled
            ? { label: t('dlg_sync_check'), checked: false }
            : null,
    });
    if (result === null) return; // cancelled
    const sendEmails = !!result.checked;
    const btn = document.getElementById('sync-btn');
    const status = document.getElementById('sync-status');
    btn.disabled = true;
    status.textContent = t('sync_starting');
    try {
        const response = await apiFetch(API_SYNC + '&skip_email=' + (sendEmails ? 'false' : 'true'), { method: 'POST' });
        if (response.status === 409) {
            status.textContent = t('sync_already');
        } else if (!response.ok) {
            throw new Error('HTTP ' + response.status);
        } else {
            status.textContent = t('sync_started');
        }
    } catch (error) {
        console.error('Failed to start sync:', error);
        status.textContent = t('sync_failed_start');
    } finally {
        setTimeout(() => { btn.disabled = false; loadData(); }, 5000);
    }
});

document.getElementById('rescan-btn').addEventListener('click', async () => {
    const btn = document.getElementById('rescan-btn');
    const status = document.getElementById('rescan-status');
    btn.disabled = true;
    status.textContent = t('rescan_running');
    try {
        const response = await apiFetch(API_RESCAN, { method: 'POST' });
        if (response.status === 409) {
            status.textContent = t('rescan_conflict');
            return;
        }
        if (!response.ok) throw new Error('HTTP ' + response.status);
        const result = await response.json();
        status.textContent = t('rescan_updated', { n: result.updated || 0 });
        loadData();
        if (filesLoaded) loadFiles();
    } catch (error) {
        console.error('Failed to re-scan PDFs:', error);
        status.textContent = t('rescan_failed');
    } finally {
        btn.disabled = false;
    }
});

async function deleteFile(filename) {
    const isMetadata = filename.toLowerCase().endsWith('.json');
    const message = t(isMetadata ? 'dlg_delete_json' : 'dlg_delete_pdf', { file: filename });
    const result = await showDialog({ title: t('dlg_delete_title'), message, okLabel: t('action_delete') });
    if (result === null) return; // cancelled
    try {
        const response = await apiFetch('api/files/' + encodeURIComponent(filename), { method: 'DELETE' });
        if (!response.ok) {
            const body = await response.json().catch(() => ({}));
            throw new Error(body.detail || ('HTTP ' + response.status));
        }
        loadFiles();
        loadData(); // analytics change when a metadata sidecar is removed
    } catch (error) {
        console.error('Failed to delete file:', error);
        await showError(t('delete_failed', { error: error.message }));
    }
}

document.getElementById('files-refresh-btn').addEventListener('click', loadFiles);

// ---------- startup ----------

// Translations first, so the initial render is never half-translated.
initI18n().then(loadData);
// Refresh periodically so a running sync's results show up — but not
// from hidden tabs (no point hammering the API in the background).
setInterval(() => {
    if (document.hidden) return;
    loadData();
    if (filesTabVisible()) loadFiles();
}, 60000);
document.addEventListener('visibilitychange', () => {
    if (!document.hidden) loadData();
});
