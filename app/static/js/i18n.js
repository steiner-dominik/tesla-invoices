// Dashboard translations. The strings live in static/i18n/<code>.json —
// one flat JSON file per language, listed in static/i18n/languages.json.
// Adding a language means adding those two things, no code changes.
//
// Language precedence: user's toggle choice (per browser) > server default
// (HA language / LANGUAGE env var, arrives with the analytics data) >
// browser language. English is the fallback and also the source of truth
// for missing keys.
//
// NOTE: all URLs are RELATIVE (no leading "/") so they stay inside the
// Home Assistant ingress path prefix instead of escaping to the HA root.

const LANG_KEY = 'tesla-invoices-lang';
const I18N_BASE = 'static/i18n/';

// code -> native name, from languages.json (English is always available
// even if the manifest fails to load).
let languages = { en: 'English' };
// code -> translation map; filled lazily per language.
const I18N = {};

let serverLang = '';
let lang = 'en';

async function fetchJson(url) {
    const response = await fetch(url);
    if (!response.ok) throw new Error('HTTP ' + response.status + ' for ' + url);
    return response.json();
}

async function loadLanguage(code) {
    if (I18N[code]) return true;
    try {
        I18N[code] = await fetchJson(I18N_BASE + code + '.json');
        return true;
    } catch (error) {
        console.error('Failed to load translations for "' + code + '":', error);
        return false;
    }
}

function t(key, params) {
    let text = (I18N[lang] && I18N[lang][key]) ?? (I18N.en && I18N.en[key]) ?? key;
    if (params) {
        for (const [name, value] of Object.entries(params)) {
            text = text.replaceAll('{' + name + '}', value);
        }
    }
    return text;
}

function storedLang() {
    try { return localStorage.getItem(LANG_KEY) || ''; } catch (e) { return ''; }
}

function defaultLang() {
    const candidate = serverLang || (navigator.language || 'en').slice(0, 2).toLowerCase();
    return languages[candidate] ? candidate : 'en';
}

function translatePage() {
    document.documentElement.lang = lang;
    document.querySelectorAll('[data-i18n]').forEach((el) => {
        el.textContent = t(el.dataset.i18n);
    });
    document.querySelectorAll('[data-i18n-placeholder]').forEach((el) => {
        el.placeholder = t(el.dataset.i18nPlaceholder);
    });
}

async function setLanguage(newLang, rerender) {
    if (!languages[newLang] || !(await loadLanguage(newLang))) newLang = 'en';
    lang = newLang;
    document.querySelectorAll('[data-lang-choice]').forEach((btn) => {
        btn.classList.toggle('active', btn.dataset.langChoice === lang);
    });
    translatePage();
    // Everything holding dynamic, translated text is re-rendered by the app
    // (defined in app.js; guarded so i18n.js has no hard dependency on it).
    if (rerender && typeof rerenderAll === 'function') rerenderAll();
}

// Adopt the server's default language (sent with the analytics payload)
// unless the user has picked one via the toggle.
async function setServerLanguage(code) {
    serverLang = (code || '').toLowerCase();
    if (!storedLang() && lang !== defaultLang()) await setLanguage(defaultLang(), false);
}

// One button per language from the manifest, in the header's segmented
// toggle. Codes are shown (EN/DE/…) to keep the header compact; the
// native name goes into the tooltip.
function buildLanguageToggle() {
    const container = document.getElementById('lang-toggle');
    container.replaceChildren();
    for (const [code, name] of Object.entries(languages)) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.dataset.langChoice = code;
        btn.textContent = code.toUpperCase();
        btn.title = name;
        btn.classList.toggle('active', code === lang);
        btn.addEventListener('click', () => {
            try { localStorage.setItem(LANG_KEY, code); } catch (e) { /* not persisted */ }
            setLanguage(code, true);
        });
        container.appendChild(btn);
    }
}

// Resolves once the language list, the English fallback and the initial
// language are loaded — the app waits for this before its first render.
async function initI18n() {
    try {
        languages = await fetchJson(I18N_BASE + 'languages.json');
    } catch (error) {
        console.error('Failed to load the language list:', error);
        languages = { en: 'English' };
    }
    if (!languages.en) languages.en = 'English';
    await loadLanguage('en');
    await setLanguage(storedLang() || defaultLang(), false);
    buildLanguageToggle();
}
