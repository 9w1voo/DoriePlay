'use strict';
/* Dorie Play web interface. No framework and no build step: the page talks to server.py on this
   computer. Everything that comes from a music service (titles, names, links) is untrusted text:
   it is only ever put on the page with textContent, and links and players are allow-listed. */

const THEMES = [['glass', 'themeGlass'], ['glass-light', 'themeGlassLight']];
const LANGS = Object.keys(window.I18N);   // English first: it is the default and the fallback
const store = {
  get(key) { try { return localStorage.getItem(key); } catch { return null; } },
  set(key, value) { try { localStorage.setItem(key, value); } catch { /* private window */ } },
};
// Before the first paint, so the page does not flash in another look. No choice yet: follow the system.
document.documentElement.dataset.theme = THEMES.some(([id]) => id === store.get('theme')) ? store.get('theme')
  : matchMedia('(prefers-color-scheme: light)').matches ? 'glass-light' : 'glass';

let lang = LANGS.includes(store.get('lang')) ? store.get('lang') : 'en';
document.documentElement.lang = lang;

/* Text in the chosen language; {name} parts are replaced from vars. */
function t(key, vars) {
  const text = window.I18N[lang][key] ?? window.I18N.en[key] ?? key;
  return String(text).replace(/\{(\w+)\}/g, (whole, name) => (vars && name in vars ? vars[name] : whole));
}

const S = { state: null, plan: null, filter: null, open: new Set(), playing: new Map(), timer: null, target: null,
            setup: null, link: '' };

function el(tag, attrs, ...kids) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value == null || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'style') node.style.cssText = value;   // a style attribute would be blocked by the CSP
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? '' : value);
  }
  for (const kid of kids.flat()) {
    if (kid != null && kid !== false) node.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return node;
}

async function api(path, body) {
  const response = await fetch('/api/' + path, {
    method: body ? 'POST' : 'GET',
    headers: { 'X-Dorie-Token': document.querySelector('meta[name=dorie-token]').content,
               'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await response.json().catch(() => ({ error: t('noAnswer') }));
  if (!response.ok) throw new Error(data.error || t('wrong'));
  return data;
}

function toast(text, bad) {
  const box = document.getElementById('toast');
  box.textContent = text;
  box.className = 'toast' + (bad ? ' bad' : '');
  box.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { box.hidden = true; }, bad ? 7000 : 2500);
}

const clock = seconds => `${Math.floor(seconds / 60)}:${String(Math.round(seconds % 60)).padStart(2, '0')}`;
const label = name => (S.state.platforms.find(p => p.name === name) || { label: name }).label;
function songs(count) {   // "1 song", "3 пісні", "5 песен": each language has its own plural rules
  const forms = window.I18N[lang].songs;
  return `${count} ${forms[new Intl.PluralRules(lang).select(count)] || forms.other}`;
}

function sourceOf(url) {
  if (/(^|\/\/|\.)spotify\.com\/|^spotify:/i.test(url)) return 'spotify';
  if (/(^|\/\/|\.)(youtube\.com|youtu\.be)\//i.test(url)) return 'youtube';
  if (/(^|\/\/|\.)soundcloud\.com\/|^soundcloud:/i.test(url)) return 'soundcloud';
  return null;
}

function safeLink(url) {
  try {
    const parsed = new URL(url);
    return parsed.protocol === 'https:' &&
      /(^|\.)(youtube\.com|youtu\.be|spotify\.com|soundcloud\.com)$/.test(parsed.hostname) ? parsed.href : null;
  } catch { return null; }
}

function playerUrl(platform, candidate) {
  const id = String(candidate.id || '');
  if (platform === 'youtube' && /^[\w-]{11}$/.test(id)) return 'https://www.youtube-nocookie.com/embed/' + id;
  if (platform === 'spotify' && /^[A-Za-z0-9]{22}$/.test(id)) return 'https://open.spotify.com/embed/track/' + id;
  const track = /^soundcloud:tracks:(\d+)$/.exec(id);
  if (platform === 'soundcloud' && track) {
    return 'https://w.soundcloud.com/player/?url=' +
      encodeURIComponent('https://api.soundcloud.com/tracks/' + track[1]);
  }
  return null;
}

/* Saving to YouTube without the API quota. www.youtube.com/watch_videos turns up to 50 video ids into a
   temporary list (later ids are dropped, unavailable videos skipped); only YouTube Music has a Save
   button for that list, and only once it exists, so the user brings us the address YouTube opened.
   Undocumented by YouTube (checked by hand 14 Nov 2026): it may stop working. */
const PART = 50;
function videoIdOf(track) {   // match_id is an id or, hand-edited, a candidate's link (chosen_id in dorieplay.py)
  const value = String(track.match_id || '').trim();
  const candidate = (track.candidates || []).find(c => c.id === value || c.url === value);
  const id = candidate ? candidate.id : value;
  return /^[\w-]{11}$/.test(id) ? id : null;
}
/* youtube.com/watch?v=<id>&list=<list> (with or without https://, www., m., or as a youtu.be share link)
   -> the same list on YouTube Music. The new link is rebuilt from the checked parts only. */
function musicLink(text) {
  try {
    const raw = String(text).replace(/[​-‏⁠﻿]/g, '').trim();
    const url = new URL(/^https?:\/\//i.test(raw) ? raw : 'https://' + raw);
    const video = url.hostname === 'youtu.be' ? url.pathname.slice(1)
      : url.pathname === '/watch' ? url.searchParams.get('v') || '' : '';
    const list = url.searchParams.get('list') || '';
    return /^((www|m|music)\.)?youtube\.com$|^youtu\.be$/.test(url.hostname) &&
      /^[\w-]{11}$/.test(video) && /^[\w-]{10,80}$/.test(list)
      ? `https://music.youtube.com/watch?v=${video}&list=${list}` : null;
  } catch { return null; }
}

/* auto: approved by the program; ok: chosen by the user; ask: needs a decision;
   skip: the user left it out; none: nothing was found. */
function statusOf(track) {
  if (track.approved && track.match_id) return track.decided ? 'ok' : 'auto';
  if (track.decided) return 'skip';
  return (track.candidates || []).length ? 'ask' : 'none';
}
const FILTERS = [['ask', 'fAsk', ['ask']], ['all', 'fAll', ['auto', 'ok', 'ask', 'skip', 'none']],
                 ['in', 'fIn', ['auto', 'ok']], ['out', 'fOut', ['skip', 'none']]];
const STATUS_TEXT = { auto: 'sAuto', ok: 'sOk', ask: 'sAsk', skip: 'sSkip', none: 'sNone' };

// --- Top bar ---------------------------------------------------------------------------------------

function drawThemes() {
  const box = document.getElementById('themes');
  box.setAttribute('aria-label', t('look'));
  box.replaceChildren(...THEMES.map(([id, key]) => el('button', {
    class: 'theme-pick', type: 'button', 'data-look': id, title: t('lookTitle', { name: t(key) }),
    'aria-pressed': String(document.documentElement.dataset.theme === id),
    onclick() { document.documentElement.dataset.theme = id; store.set('theme', id); drawThemes(); },
  }, el('span', { class: 'swatch', 'aria-hidden': 'true' }), t(key))));
}

function drawLanguages() {
  const select = document.getElementById('lang');
  select.setAttribute('aria-label', t('language'));
  select.replaceChildren(...LANGS.map(code => el('option', { value: code, selected: code === lang,
    text: window.I18N[code]._name })));
  select.onchange = () => {
    lang = select.value;
    store.set('lang', lang);
    document.documentElement.lang = lang;
    drawThemes(); drawLanguages(); route();
  };
}

// --- Home ------------------------------------------------------------------------------------------

function home() {
  const input = el('input', { class: 'link-input', type: 'url', inputmode: 'url', autocomplete: 'off',
    spellcheck: 'false', placeholder: 'https://open.spotify.com/playlist/…', 'aria-label': t('linkLabel'), value: S.link });
  const targets = el('div', { class: 'seg', role: 'group', 'aria-label': t('whereTo') });
  const hint = el('p', { class: 'hint' });
  const go = el('button', { class: 'btn primary big', type: 'submit', text: t('findSongs') });

  function drawTargets() {
    S.link = input.value;   // kept when the page is redrawn (set-up panel, language)
    const source = sourceOf(input.value.trim());
    const allowed = S.state.platforms.filter(p => p.name !== source);
    if (!S.target || S.target === source) S.target = source ? allowed[0].name : null;
    targets.replaceChildren(...S.state.platforms.map(p => el('button', {
      class: 'seg-item', type: 'button', disabled: p.name === source, text: p.label,
      'aria-pressed': String(p.name === S.target), onclick() { S.target = p.name; drawTargets(); },
    })));
    hint.textContent = !input.value.trim() ? t('hintEmpty')
      : source ? t('hintFrom', { from: label(source) }) : t('hintUnknown');
    go.disabled = !source || !S.target;
  }
  input.addEventListener('input', drawTargets);

  const form = el('form', { class: 'start', async onsubmit(event) {
    event.preventDefault();
    go.disabled = true;
    try {
      const job = await api('plans', { url: input.value.trim(), to: S.target });
      S.state.job = job;
      S.link = '';
      location.hash = '#/plan/' + job.plan;
    } catch (error) { toast(error.message, true); go.disabled = false; }
  } },
    el('label', { class: 'field-label', text: t('linkLabel') }), input,
    el('div', { class: 'start-row' },
      el('div', { class: 'to' }, el('span', { class: 'field-label', text: t('to') }), targets), go),
    hint);

  const view = el('div', { class: 'home' },
    S.state.demo && el('div', { class: 'banner' },
      el('strong', { text: t('demoStrong') }), ` ${t('demoText')} `,
      el('button', { class: 'link', type: 'button', text: t('demoPaste'), onclick() {
        input.value = 'https://open.spotify.com/playlist/demo'; drawTargets(); input.focus();
      } })),
    el('section', { class: 'hero' },
      el('h1', { text: t('heroTitle') }),
      el('p', { class: 'lead', text: t('heroLead') }),
      form),
    plansSection(), connectionsSection());
  drawTargets();
  return view;
}

function plansSection() {
  const plans = S.state.plans;
  if (!plans.length) return null;
  return el('section', { class: 'block' }, el('h2', { text: t('yourPlans') }),
    el('div', { class: 'plan-list' }, plans.map(plan => el('a', { class: 'plan-card', href: '#/plan/' + plan.name },
      el('span', { class: 'plan-title', text: plan.title || plan.name }),
      el('span', { class: 'route', text: `${label(plan.from)} → ${label(plan.to)}` }),
      el('span', { class: 'meter', 'aria-hidden': 'true' },
        el('span', { style: `width:${plan.total ? Math.round(100 * plan.approved / plan.total) : 0}%` })),
      el('span', { class: 'plan-meta', text: t('planMeta', plan) +
        (plan.applied ? ' · ' + t('transferred') : '') })))));
}

/* Set-up help for each service: where to get the key (official pages only) and how it is pasted.
   Keys go to the program on this computer and are saved next to it; the page never shows them again. */
const SETUP = {
  spotify: { site: 'https://developer.spotify.com/dashboard', steps: () => [t('setSp1'), t('setSp2'), t('setSp3')] },
  youtube: { site: 'https://console.cloud.google.com/', steps: () => [t('setYt1'), t('setYt2'), t('setYt3')] },
  soundcloud: { site: 'https://soundcloud.com/you/apps', steps: () => [t('setSc1'), t('setSc2')] },
};
const redrawHome = () => document.getElementById('view').replaceChildren(home());
const GUIDE = 'https://github.com/9w1voo/DoriePlay#connecting-the-services';

function setupPanel(p) {
  const fields = p.name === 'soundcloud'
    ? [el('input', { class: 'link-input', autocomplete: 'off', placeholder: t('scId'), 'aria-label': t('scId') }),
       el('input', { class: 'link-input', type: 'password', autocomplete: 'off', placeholder: t('scSecret'),
         'aria-label': t('scSecret') })]
    : p.name === 'youtube'
      ? [el('textarea', { class: 'key-text', rows: 4, spellcheck: 'false', placeholder: t('ytPaste'), 'aria-label': t('ytPaste') })]
      : [el('input', { class: 'link-input', autocomplete: 'off', placeholder: t('spPaste'), 'aria-label': t('spPaste') })];
  const replace = p.key && el('input', { type: 'checkbox' });
  async function save() {
    const value = p.name === 'soundcloud'
      ? JSON.stringify({ client_id: fields[0].value.trim(), client_secret: fields[1].value.trim() }) : fields[0].value;
    try {
      await api('keys/' + p.name, { value, replace: Boolean(replace && replace.checked) });
      fields.forEach(field => { field.value = ''; });
      S.setup = null;
      S.state = await api('state');
      redrawHome();
      toast(t('keySaved'));
    } catch (error) { toast(error.message, true); }
  }
  const setup = SETUP[p.name];
  return el('div', { class: 'setup' },
    el('ol', { class: 'setup-steps' }, setup.steps().map(text => el('li', { text }))),
    el('p', { class: 'hint' }, el('a', { href: setup.site, target: '_blank', rel: 'noopener noreferrer', text: setup.site }),
      ' · ', el('a', { href: GUIDE, target: '_blank', rel: 'noopener noreferrer', text: t('setGuide') })),
    fields,
    replace && el('label', { class: 'setup-replace' }, replace, ' ', t('setReplace')),
    el('div', { class: 'setup-actions' },
      el('button', { class: 'btn primary small', type: 'button', text: t('setSave'), onclick: save }),
      el('span', { class: 'hint', text: t('setLocal') })));
}

function connectionsSection() {
  const left = S.state.youtube_searches_left;
  return el('section', { class: 'block' }, el('h2', { text: t('services') }),
    el('div', { class: 'conn-list' }, S.state.platforms.map(p => el('div', { class: 'conn' },
      el('span', { class: 'conn-name', text: p.label }),
      el('span', { class: 'dot ' + (p.key ? 'on' : 'off'), text: p.key ? t('keyYes') : t('keyNo') }),
      el('span', { class: 'dot ' + (p.signed_in ? 'on' : 'idle'), text: p.signed_in ? t('signedYes') : t('signedLater') }),
      el('button', { class: 'btn ghost small conn-setup', type: 'button', 'aria-expanded': String(S.setup === p.name),
        text: p.key ? t('setChange') : t('setUp'),
        onclick() { S.setup = S.setup === p.name ? null : p.name; redrawHome(); } }),
      S.setup === p.name && setupPanel(p)))),
    el('p', { class: 'hint', text: t('ytLeft', { left }) }));
}

// --- Plan ------------------------------------------------------------------------------------------

function jobCard(job) {
  const searching = job.kind === 'plan', checking = job.kind === 'check';
  const share = job.total ? Math.min(100, Math.round(100 * job.done / job.total)) : 0;
  if (job.state === 'error') {
    return el('div', { class: 'card job bad' },
      el('h2', { text: searching ? t('searchStopped') : checking ? t('checkStopped') : t('transferStopped') }),
      el('p', { class: 'job-error', text: job.error }),
      el('p', { class: 'hint', text: t('savedHint') }));
  }
  return el('div', { class: 'card job' },
    el('h2', { text: searching ? t('searching') : checking ? t('checking') : t('adding') }),
    el('div', { class: 'bar', role: 'progressbar', 'aria-valuenow': share, 'aria-valuemin': 0, 'aria-valuemax': 100 },
      el('span', { style: `width:${job.total ? share : 4}%` })),
    el('p', { class: job.total ? 'job-count' : 'job-wait', text: job.total ? t('xOfY', job)
      : (searching ? t('readingPlaylist') : t('preparing')) }),
    el('p', { class: 'job-line', text: job.lines[job.lines.length - 1] || '' }));
}

function resultCard(job) {
  const result = job.result;
  const link = safeLink(result.url);
  return el('div', { class: 'card done' },
    el('h2', { text: t('done') }),
    el('p', { text: t('addedLine', result) }),
    link ? el('a', { class: 'btn primary', href: link, target: '_blank', rel: 'noopener noreferrer', text: t('openPlaylist') })
         : S.state.demo && el('p', { class: 'hint', text: t('demoNoPlaylist') }),
    result.skipped.length > 0 && el('div', { class: 'skipped' },
      el('h3', { text: t('couldNotAdd', { count: result.skipped.length }) }),
      el('ul', {}, result.skipped.map(([song, reason]) => el('li', {}, el('b', { text: song }), ' — ' + reason))),
      el('p', { class: 'hint', text: t('pickAnother') })));
}

async function startCheck() {
  try { S.state.job = await api(`plans/${S.plan.name}/check`, {}); drawPlan(); poll(); }
  catch (error) { toast(error.message, true); }
}

function openRow(index) {
  S.filter = 'all';
  S.open.add(index);
  drawPlan();
  const row = document.querySelectorAll('.rows .row')[index];
  if (row) row.scrollIntoView({ block: 'center' });
}

/* gone: songs whose chosen video YouTube no longer has ([{index, id}]), or null before a check. */
function musicCard(ids, gone) {
  const open = el('a', { class: 'btn primary', target: '_blank', rel: 'noopener noreferrer', hidden: true, text: t('ytmOpen') });
  const note = el('p', { class: 'hint' });
  const input = el('input', { class: 'link-input', type: 'url', inputmode: 'url', autocomplete: 'off',
    placeholder: t('ytmPaste'), 'aria-label': t('ytmPaste'), oninput(event) {
      const value = event.target.value.trim();
      const link = value && musicLink(value);
      open.hidden = !link;
      if (link) open.href = link;
      note.textContent = value && !link ? t('ytmBad') : '';
    } });
  const parts = [];
  for (let start = 0; start < ids.length; start += PART) parts.push(ids.slice(start, start + PART));
  return el('div', { class: 'card ytm' },
    el('h2', { text: t('ytmTitle') }),
    el('ol', { class: 'ytm-steps' }, el('li', { text: t('ytmStep1') }), el('li', { text: t('ytmStep2') }),
      el('li', { text: t('ytmStep3') })),
    el('div', { class: 'ytm-check' },
      el('button', { class: 'btn ghost', type: 'button', text: t('ytmCheck'), onclick: startCheck }),
      gone && el('p', { class: 'hint', text: gone.length ? t('ytmGone', { count: gone.length }) : t('ytmAllOk') })),
    gone && gone.length > 0 && el('ul', { class: 'ytm-gone' }, gone.map(({ index }) => el('li', {},
      el('button', { class: 'link', type: 'button', onclick: () => openRow(index),
        text: `${index + 1}. ${S.plan.tracks[index].title || '—'} — ${(S.plan.tracks[index].artists || []).join(', ')}` })))),
    el('div', { class: 'ytm-parts' }, parts.map((part, n) => el('a', { class: 'btn ghost', target: '_blank',
      rel: 'noopener noreferrer', href: 'https://www.youtube.com/watch_videos?video_ids=' + part.join(','),
      text: t('ytmPart', { n: n + 1, from: n * PART + 1, to: n * PART + part.length }) }))),
    input, note, open,
    el('p', { class: 'hint', text: t('ytmNote') }));
}

function candidateRow(index, track, candidate) {
  const chosen = track.approved && track.match_id === candidate.id;
  const platform = S.plan.to;
  const frameUrl = playerUrl(platform, candidate);
  const link = safeLink(candidate.url);
  const delta = candidate.seconds - Math.round((track.duration_ms || 0) / 1000);
  const key = index + '/' + candidate.id;
  const slot = el('div', { class: 'player ' + platform });
  if (S.playing.get(index) === candidate.id && frameUrl) {
    slot.append(el('iframe', { src: frameUrl, title: t('player'), loading: 'lazy', allow: 'encrypted-media',
      // YouTube refuses to play (error 153) without a referrer; only our origin is sent, never the path.
      referrerpolicy: 'strict-origin-when-cross-origin', sandbox: 'allow-scripts allow-same-origin allow-presentation' }));
  }
  return el('li', { class: 'cand' + (chosen ? ' chosen' : ''), 'data-key': key },
    el('span', { class: 'score', title: t('scoreTitle'), text: Math.round((candidate.score || 0) * 100) + '%' }),
    el('span', { class: 'cand-text' },
      el('b', { text: candidate.title || '—' }),
      el('i', { text: `${candidate.artist || '—'} · ${clock(candidate.seconds || 0)}` +
        (delta ? ` (${delta > 0 ? '+' : '−'}${Math.abs(delta)} ${t('sec')})` : '') })),
    el('span', { class: 'cand-actions' },
      frameUrl && el('button', { class: 'btn ghost', type: 'button', text: S.playing.get(index) === candidate.id ? t('hide') : t('listen'),
        onclick() { S.playing.get(index) === candidate.id ? S.playing.delete(index) : S.playing.set(index, candidate.id); drawPlan(); } }),
      link && el('a', { class: 'btn ghost', href: link, target: '_blank', rel: 'noopener noreferrer', text: t('open') }),
      chosen ? el('span', { class: 'picked', text: t('chosen') })
             : el('button', { class: 'btn small', type: 'button', text: t('takeThis'), onclick: () => decide(index, true, candidate.id) })),
    slot);
}

function trackRow(track, index) {
  const status = statusOf(track);
  const open = S.open.has(index);
  const match = (track.candidates || []).find(c => c.id === track.match_id);
  const right = status === 'none' ? el('span', { class: 'dst empty', text: t('nothingFound') })
    : status === 'skip' ? el('span', { class: 'dst empty', text: t('notTransferred') })
    : match ? el('span', { class: 'dst' }, el('b', { text: match.title }),
        el('i', { text: `${match.artist} · ${clock(match.seconds || 0)}` }))
    : el('span', { class: 'dst empty', text: t('noCandidate') });
  const head = el('button', { class: 'row-main', type: 'button', 'aria-expanded': String(open), onclick() {
    open ? S.open.delete(index) : S.open.add(index); drawPlan();
  } },
    el('span', { class: 'num', text: String(index + 1).padStart(2, '0') }),
    el('span', { class: 'src' }, el('b', { text: track.title || '—' }),
      el('i', { text: `${(track.artists || []).join(', ') || '—'} · ${clock((track.duration_ms || 0) / 1000)}` })),
    el('span', { class: 'arrow', 'aria-hidden': 'true', text: '→' }), right,
    el('span', { class: 'badge', text: t(STATUS_TEXT[status]) }));
  const details = open && el('div', { class: 'cands' },
    track.source_title && el('p', { class: 'hint', text: t('originalTitle', { title: track.source_title }) }),
    (track.candidates || []).length
      ? el('ul', {}, track.candidates.map(candidate => candidateRow(index, track, candidate)))
      : el('p', { class: 'hint', text: t('searchEmpty') }),
    el('div', { class: 'cands-foot' },
      status !== 'skip' && status !== 'none' && el('button', { class: 'btn ghost', type: 'button', text: t('dontTransfer'),
        onclick: () => decide(index, false, null) })));
  return el('article', { class: `row s-${status}` + (open ? ' open' : '') }, head, details);
}

async function decide(index, approved, matchId) {
  try {
    const saved = await api(`plans/${S.plan.name}/track`, { index, approved, match_id: matchId });
    S.plan.tracks[index] = saved.track;
    S.plan.approved = saved.approved;
    S.open.delete(index);
    S.playing.delete(index);
    drawPlan();
    toast(approved ? t('saved') : t('skippedToast'));
  } catch (error) { toast(error.message, true); }
}

function confirmApply(count) {
  const dialog = document.getElementById('confirm');
  dialog.replaceChildren(
    el('h2', { text: S.plan.applied ? t('addQ') : t('createQ') }),
    el('p', { text: t(S.plan.applied ? 'addBody' : 'createBody',
      { to: label(S.plan.to), title: S.plan.title, count }) }),
    el('div', { class: 'dialog-actions' },
      el('button', { class: 'btn ghost', type: 'button', text: t('cancel'), onclick: () => dialog.close() }),
      el('button', { class: 'btn primary', type: 'button', text: t('yesTransfer'), async onclick() {
        dialog.close();
        try { S.state.job = await api(`plans/${S.plan.name}/apply`, {}); drawPlan(); poll(); }
        catch (error) { toast(error.message, true); }
      } })));
  dialog.showModal();
}

function drawPlan() {
  const view = document.getElementById('view');
  const name = location.hash.slice(7);
  const job = S.state.job && S.state.job.plan === name ? S.state.job : null;
  const back = el('a', { class: 'back', href: '#/', text: t('allPlans') });
  if (job && job.state === 'running' || job && job.state === 'error' && !S.plan) {
    view.replaceChildren(el('div', { class: 'plan' }, back, jobCard(job)));
    return;
  }
  if (!S.plan) { view.replaceChildren(el('div', { class: 'plan' }, back, el('p', { class: 'hint', text: t('loading') }))); return; }

  const tracks = S.plan.tracks;
  const counts = { auto: 0, ok: 0, ask: 0, skip: 0, none: 0 };
  tracks.forEach(track => { counts[statusOf(track)] += 1; });
  const going = counts.auto + counts.ok;
  if (!S.filter) S.filter = counts.ask ? 'ask' : 'all';
  const shown = FILTERS.find(([id]) => id === S.filter)[2];
  const rows = tracks.map((track, index) => shown.includes(statusOf(track)) ? trackRow(track, index) : null).filter(Boolean);
  const videoIds = S.plan.to === 'youtube'
    ? tracks.filter(track => ['auto', 'ok'].includes(statusOf(track))).map(videoIdOf).filter(Boolean) : [];
  // A song the user has since changed or left out drops off the list of gone videos.
  const gone = job && job.kind === 'check' && job.state === 'done' && job.result
    ? job.result.unavailable.filter(({ index, id }) => tracks[index] && ['auto', 'ok'].includes(statusOf(tracks[index]))
      && videoIdOf(tracks[index]) === id) : null;

  view.replaceChildren(el('div', { class: 'plan' }, back,
    el('header', { class: 'plan-head' },
      el('h1', { text: S.plan.title || S.plan.name }),
      el('p', { class: 'route', text: `${label(S.plan.from)} → ${label(S.plan.to)} · ${songs(tracks.length)}` })),
    job && job.state === 'error' && jobCard(job),
    job && job.state === 'done' && job.kind === 'apply' && job.result && resultCard(job),
    el('div', { class: 'stats' },
      el('div', { class: 'stat s-auto' }, el('b', { text: going }), el('span', { text: t('statIn') })),
      el('div', { class: 'stat s-ask' }, el('b', { text: counts.ask }), el('span', { text: t('statAsk') })),
      el('div', { class: 'stat s-none' }, el('b', { text: counts.none + counts.skip }), el('span', { text: t('statOut') }))),
    el('div', { class: 'chips', role: 'group', 'aria-label': t('whichSongs') }, FILTERS.map(([id, key, statuses]) =>
      el('button', { class: 'chip', type: 'button', 'aria-pressed': String(id === S.filter),
        onclick() { S.filter = id; drawPlan(); } },
        t(key), el('span', { class: 'chip-count', text: statuses.reduce((sum, status) => sum + counts[status], 0) })))),
    el('div', { class: 'rows' }, rows.length ? rows
      : el('p', { class: 'empty-list', text: S.filter === 'ask' ? t('emptyAsk') : t('emptyOther') })),
    videoIds.length > 0 && musicCard(videoIds, gone),
    el('div', { class: 'apply-bar' },
      el('span', { text: going ? t('barGoing', { to: label(S.plan.to), songs: songs(going) }) : t('barNone') }),
      S.plan.applied && safeLink(S.plan.target_url) && el('a', { class: 'btn ghost', href: safeLink(S.plan.target_url),
        target: '_blank', rel: 'noopener noreferrer', text: t('playlistLink') }),
      el('button', { class: 'btn primary', type: 'button', disabled: !going,
        text: S.plan.applied ? t('addNew') : t('createPrivate'),
        onclick: () => confirmApply(going) }))));
}

// --- Routing and polling ----------------------------------------------------------------------------

async function loadPlan(name) {
  try { S.plan = await api('plans/' + name); }
  catch (error) { S.plan = null; if (!(S.state.job && S.state.job.plan === name)) { toast(error.message, true); location.hash = '#/'; return; } }
  drawPlan();
}

function poll() {
  clearTimeout(S.timer);
  if (!S.state.job || S.state.job.state !== 'running') return;
  S.timer = setTimeout(async () => {
    try {
      const before = S.state.job.plan;
      S.state.job = (await api('job')).job;
      const job = S.state.job;
      if (job && job.plan !== before && location.hash === '#/plan/' + before) location.hash = '#/plan/' + job.plan;
      if (location.hash.startsWith('#/plan/')) {
        if (job && job.state !== 'running') await loadPlan(location.hash.slice(7)); else drawPlan();
      }
    } catch { /* the program was stopped; the next tick tries again */ }
    poll();
  }, 500);
}

async function route() {
  clearTimeout(S.timer);
  try { S.state = await api('state'); }
  catch (error) { document.getElementById('view').replaceChildren(el('p', { class: 'hint', text: error.message })); return; }
  if (location.hash.startsWith('#/plan/')) {
    const name = location.hash.slice(7);
    if (!S.plan || S.plan.name !== name) { S.plan = null; S.filter = null; S.open.clear(); S.playing.clear(); }
    drawPlan();
    if (!(S.state.job && S.state.job.plan === name && S.state.job.state === 'running')) await loadPlan(name);
  } else {
    S.plan = null;
    document.getElementById('view').replaceChildren(home());
  }
  window.scrollTo(0, 0);
  poll();
}

document.addEventListener('DOMContentLoaded', () => {
  drawThemes();
  drawLanguages();
  window.addEventListener('hashchange', route);
  route();
});
