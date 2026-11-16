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
// Before the first paint, so the page does not flash in another look. No choice yet: the dark look.
document.documentElement.dataset.theme = THEMES.some(([id]) => id === store.get('theme')) ? store.get('theme') : 'glass';

let lang = LANGS.includes(store.get('lang')) ? store.get('lang') : 'en';
document.documentElement.lang = lang;

/* Text in the chosen language; {name} parts are replaced from vars. */
function t(key, vars) {
  const text = window.I18N[lang][key] ?? window.I18N.en[key] ?? key;
  return String(text).replace(/\{(\w+)\}/g, (whole, name) => (vars && name in vars ? vars[name] : whole));
}

// The mascot gives the tips: on until the user hides them; hidden, the old grey hint line comes back.
let tipsOn = store.get('tips') !== 'off';
// plain: the hint shown when tips are hidden — a text (a grey line), a node (shown as is) or nothing.
function tip(pose, text, plain, extra) {
  if (!tipsOn) return typeof plain === 'string' ? el('p', { class: 'hint', text: plain }) : plain || null;
  return el('div', { class: 'tip' }, window.Mascot.node(pose), el('div', { class: 'bubble' }, el('span', { text }), extra));
}

const S = { state: null, plan: null, filter: null, open: new Set(), playing: new Map(), timer: null, target: null,
            setup: null, link: '', files: null };

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
const label = name => name === 'text' ? t('songList') : name === 'deezer' ? 'Deezer'   // source only, not in platforms
  : (S.state.platforms.find(p => p.name === name) || { label: name }).label;
function songs(count) {   // "1 song", "3 пісні", "5 песен": each language has its own plural rules
  const forms = window.I18N[lang].songs;
  return `${count} ${forms[new Intl.PluralRules(lang).select(count)] || forms.other}`;
}

function sourceOf(url) {
  if (url.includes('\n')) return 'text';   // several lines: a list of songs (same rule as platforms.detect)
  if (/(^|\/\/|\.)spotify\.com\/|^spotify:/i.test(url)) return 'spotify';
  if (/(^|\/\/|\.)(youtube\.com|youtu\.be)\//i.test(url)) return 'youtube';
  if (/(^|\/\/|\.)soundcloud\.com\/|^soundcloud:/i.test(url)) return 'soundcloud';
  if (/(^|\/\/|\.)deezer\.com\//i.test(url)) return 'deezer';
  return url && !url.includes('/') ? 'text' : null;
}
const listLines = text => text.split('\n').filter(line => line.trim() && !line.trim().startsWith('#')).length;

/* Spotify "Download your data" → Account data. Playlist1.json: playlists[].items[].track {trackName,
   artistName}; YourLibrary.json (liked songs): tracks[] {artist, track}. Spotify does not document the
   format (written from memory, not checked on a real export yet), so it is read leniently. The names only
   ever become text in the song field. */
function exportPlaylists(data) {
  const song = item => {
    const title = item && (item.trackName || (typeof item.track === 'string' && item.track) || item.name);
    const artist = item && (item.artistName || item.artist || '');
    return title ? String(artist ? `${artist} - ${title}` : title).replace(/\s+/g, ' ').trim() : null;
  };
  const lists = (Array.isArray(data && data.playlists) ? data.playlists : []).map(p => ({
    name: String((p && p.name) || '').replace(/\s+/g, ' ').trim(),
    songs: ((p && p.items) || []).map(item => song(item && item.track)).filter(Boolean) }));
  if (Array.isArray(data && data.tracks)) lists.unshift({ name: t('likedSongs'), songs: data.tracks.map(song).filter(Boolean) });
  return lists.filter(list => list.songs.length);
}
const listText = list => [`# ${list.name || t('songList')}`, ...list.songs].join('\n');

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
  if ((track.candidates || []).length) return 'ask';
  // after a clean transfer to SoundCloud an unapproved song keeps nothing to choose from: it was left out
  return S.plan && S.plan.cleared && S.plan.to === 'soundcloud' ? 'skip' : 'none';
}
const FILTERS = [['ask', 'fAsk', ['ask']], ['all', 'fAll', ['auto', 'ok', 'ask', 'skip', 'none']],
                 ['in', 'fIn', ['auto', 'ok']], ['out', 'fOut', ['skip', 'none']]];
const STATUS_TEXT = { auto: 'sAuto', ok: 'sOk', ask: 'sAsk', skip: 'sSkip', none: 'sNone' };

// --- Top bar ---------------------------------------------------------------------------------------

// Redraws a row of top-bar buttons; a keyboard user who was in the row stays on its chosen button.
function redrawPicks(box, buttons) {
  const inside = box.contains(document.activeElement);
  box.replaceChildren(...buttons);
  if (inside) box.querySelector('[aria-pressed="true"]')?.focus();
}

function drawThemes() {
  const box = document.getElementById('themes');
  box.setAttribute('aria-label', t('look'));
  redrawPicks(box, THEMES.map(([id, key]) => el('button', {
    class: 'theme-pick', type: 'button', 'data-look': id, title: t('lookTitle', { name: t(key) }),
    'aria-pressed': String(document.documentElement.dataset.theme === id),
    onclick() { document.documentElement.dataset.theme = id; store.set('theme', id); drawThemes(); },
  }, el('span', { class: 'swatch', 'aria-hidden': 'true' }), t(key))));
}

function drawTipsSwitch() {
  const name = tipsOn ? t('tipsHide') : t('tipsShow');
  const box = document.getElementById('tips-switch');
  const focused = box.contains(document.activeElement);   // a keyboard user stays on the button
  const button = el('button', {
    class: 'tips-toggle', type: 'button', title: name, 'aria-label': name, 'aria-pressed': String(tipsOn),
    onclick() {
      tipsOn = !tipsOn;
      store.set('tips', tipsOn ? 'on' : 'off');
      S.signin = null;   // the sign-in card is kept while its link waits: rebuild it with or without the tip
      drawTipsSwitch(); route();
    },
  }, window.Mascot.node('stand'));
  box.replaceChildren(button);
  if (focused) button.focus();
}

function drawLanguages() {   // small flags; the language's own name is the button's name and tooltip
  const box = document.getElementById('lang');
  box.setAttribute('aria-label', t('language'));
  redrawPicks(box, LANGS.map(code => el('button', {
    class: 'lang-pick', type: 'button', lang: code, title: window.I18N[code]._name,
    'aria-label': window.I18N[code]._name, 'aria-pressed': String(code === lang),
    onclick() {
      lang = code;
      store.set('lang', lang);
      document.documentElement.lang = lang;
      S.signin = null;   // the kept sign-in card is in the old language
      drawThemes(); drawLanguages(); drawTipsSwitch(); route();
    },
  }, el('img', { src: `/flag-${code}.svg`, alt: '', width: 20, height: 14 }))));
}

// --- Home ------------------------------------------------------------------------------------------

// The song field is as tall as its text, wrapped lines included (a long link takes two lines on a phone),
// up to 8 lines; then it scrolls. The 1 px allows for rounding of the line height.
function fitField(area) {
  if (!area.isConnected) return;
  const top = area.scrollTop;   // shrinking to 1 line to measure would otherwise move a long list
  area.rows = 1;
  while (area.rows < 8 && area.scrollHeight > area.clientHeight + 1) area.rows += 1;
  area.scrollTop = top;
  area.classList.toggle('multi', area.rows > 1);
}
addEventListener('resize', () => {   // a turned phone or a resized window wraps the text differently
  const area = document.querySelector('textarea.link-input');
  if (area) fitField(area);
});

function home() {
  // A textarea, so a list of songs keeps its lines; it grows with the list.
  const input = el('textarea', { class: 'link-input', rows: 1, autocomplete: 'off', spellcheck: 'false',
    placeholder: t('linkPlaceholder'), 'aria-label': t('linkLabel'), text: S.link });
  const targets = el('div', { class: 'tiles', role: 'group', 'aria-label': t('whereTo') });
  const hint = el('div', { class: 'hint-slot' });
  const go = el('button', { class: 'btn primary big', type: 'submit', text: t('findSongs') });
  // A Spotify data file: read here, in the page; the chosen playlist becomes lines in the song field.
  const picker = el('input', { type: 'file', accept: '.json,application/json', hidden: true, async onchange() {
    let lists = [];
    try { lists = exportPlaylists(JSON.parse(await picker.files[0].text())); } catch { /* not JSON */ }
    picker.value = '';
    if (!lists.length) { toast(t('fileUnknown'), true); return; }
    S.files = lists.length > 1 ? lists : null;
    if (lists.length === 1) S.link = listText(lists[0]);
    redrawHome();
  } });
  const fromFile = el('p', { class: 'hint' }, picker,
    el('button', { class: 'link', type: 'button', text: t('fromFile'), onclick: () => picker.click() }), ' ' + t('fromFileHow'));
  const chooser = S.files && el('div', { class: 'file-lists' }, el('span', { class: 'field-label', text: t('chooseList') }),
    el('div', { class: 'chips' }, S.files.map(list => el('button', { class: 'chip', type: 'button',
      text: `${list.name || t('songList')} · ${songs(list.songs.length)}`,
      onclick() { S.link = listText(list); S.files = null; redrawHome(); } }))));

  function drawTargets() {
    S.link = input.value;   // kept when the page is redrawn (set-up panel, language)
    fitField(input);
    const source = sourceOf(input.value.trim());
    const allowed = S.state.platforms.filter(p => p.name !== source);
    if (!S.target || S.target === source) S.target = source ? allowed[0].name : null;
    // One big tile per service, by its mark (serviceMark). The source service is crossed out as text:
    // a faded logo would alter the mark.
    targets.replaceChildren(...S.state.platforms.map(p => el('button', {
      class: 'tile', type: 'button', disabled: p.name === source, 'aria-label': p.label,
      'aria-pressed': String(p.name === S.target), onclick() { S.target = p.name; drawTargets(); },
    }, p.name === source ? el('span', { class: 'svc-name', text: p.label }) : serviceMark(p))));
    const text = input.value.trim(), button = t('findSongs');
    if (text) store.set('greeted', '1');   // the greeting is for the very first visit only
    const how = el('button', { class: 'link', type: 'button', text: t('tipHow'), 'aria-expanded': String(!!S.how), onclick() {
      S.how = !S.how;
      drawTargets();
      hint.querySelector('.bubble .link')?.focus();   // the button was redrawn: keep the keyboard on it
    } });
    const count = songs(listLines(input.value));
    // every branch has its grey line, so hidden tips never leave the slot empty
    hint.replaceChildren(!text ? (S.how ? tip('think', t('tipHowLink'), t('hintEmpty'), how)
        : store.get('greeted') ? tip('point_up', t('tipEmpty'), t('hintEmpty'), how)
        : tip('wave', t('tipHello'), t('hintEmpty'), how))
      : source === 'text' ? tip('thumbs_up', t('tipGotList', { songs: count, button }), t('hintText', { songs: count }))
      : source ? tip('thumbs_up', t('tipGot', { service: label(source), button }), t('hintFrom', { from: label(source) }))
      : tip('shrug', t('tipUnknown'), t('hintUnknown')));
    go.disabled = !source || !S.target;
  }
  input.addEventListener('input', drawTargets);
  input.addEventListener('keydown', event => {   // Enter starts the search for a link; in a list it is a new line
    if (event.key === 'Enter' && !event.shiftKey && !['text', null].includes(sourceOf(input.value.trim()))) {
      event.preventDefault();
      if (!go.disabled) form.requestSubmit();
    }
  });

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
    hint, fromFile, chooser);

  const view = el('div', { class: 'home' },
    S.state.update && el('div', { class: 'banner' }, `${t('updateText', { version: S.state.update.version })} `,
      el('a', { href: RELEASES, target: '_blank', rel: 'noopener noreferrer', text: t('updateLink') })),
    S.state.demo && el('div', { class: 'banner' },
      el('strong', { text: t('demoStrong') }), ` ${t('demoText')} ${t('demoLeave')} `,
      el('button', { class: 'link', type: 'button', text: t('demoPaste'), onclick() {
        input.value = 'https://open.spotify.com/playlist/demo'; drawTargets(); input.focus();
      } })),
    el('section', { class: 'hero' },
      el('h1', { text: t('heroTitle') }),
      el('p', { class: 'lead', text: t('heroLead') }),
      form),
    plansSection(), connectionsSection());
  drawTargets();
  requestAnimationFrame(() => fitField(input));   // measured once the page shows it
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
   Keys go to the program on this computer and are saved in its data folder; the page never shows them again. */
const SETUP = {
  spotify: { intro: () => t('setSp0'), steps: () => [{ text: t('setSp1'), href: 'https://developer.spotify.com/dashboard' },
    { text: t('setSp2') }, { text: t('setSp3') }] },
  youtube: { intro: () => t('setYt0'), steps: () => [
    { text: t('setYt1'), href: 'https://console.cloud.google.com/apis/library/youtube.googleapis.com' },
    { text: t('setYt2'), href: 'https://console.cloud.google.com/auth/overview' },
    { text: t('setYt3'), href: 'https://console.cloud.google.com/auth/clients' }] },
};
// Official marks, shown only next to the service's own name or data (never as Dorie Play branding).
// Monochrome, as both services' rules ask: white on the dark look, black on the light one (the CSS hides the other).
// Spotify: the full logo. SoundCloud: its official logo files, without a badge.
// YouTube has no mark: its minimum height does not fit a label.
const spotifyMark = () => el('span', { class: 'sp-mark' },
  el('img', { class: 'sp-white', src: '/spotify-white.svg', alt: 'Spotify' }),
  el('img', { class: 'sp-black', src: '/spotify-black.svg', alt: 'Spotify' }));
const soundcloudMark = link => {
  const imgs = [el('img', { class: 'sc-white', src: '/soundcloud-white.png', alt: t('scLogo') }),
    el('img', { class: 'sc-black', src: '/soundcloud-black.png', alt: t('scLogo') })];
  return link ? el('a', { class: 'sc-mark', href: 'https://soundcloud.com', target: '_blank', rel: 'noopener noreferrer' }, imgs)
    : el('span', { class: 'sc-mark' }, imgs);
};
// A service in the big tiles and the Services list. YouTube's rules allow its logo only at 100 px or more
// and forbid copying its lettering, so it is its name in our own font.
const serviceMark = p => p.name === 'spotify' ? spotifyMark() : p.name === 'soundcloud' ? soundcloudMark(false)
  : el('span', { class: 'svc-name', text: p.label });
const service = name => name === 'spotify' ? spotifyMark() : label(name);
const redrawHome = () => document.getElementById('view').replaceChildren(home());
const GUIDE = 'https://github.com/9w1voo/DoriePlay#connecting-the-services';
const RELEASES = 'https://github.com/9w1voo/DoriePlay/releases/latest';

function setupPanel(p) {
  const fields = p.name === 'youtube'
    ? [el('textarea', { class: 'key-text', rows: 4, spellcheck: 'false', placeholder: t('ytPaste'), 'aria-label': t('ytPaste') })]
    : [el('input', { class: 'link-input', autocomplete: 'off', placeholder: t('spPaste'), 'aria-label': t('spPaste') })];
  const replace = p.key && el('input', { type: 'checkbox' });
  async function save() {
    try {
      await api('keys/' + p.name, { value: fields[0].value, replace: Boolean(replace && replace.checked) });
      fields.forEach(field => { field.value = ''; });
      S.setup = null;
      S.state = await api('state');
      redrawHome();
      toast(t('keySaved'));
    } catch (error) { toast(error.message, true); }
  }
  const setup = SETUP[p.name];
  return el('div', { class: 'setup' },
    el('p', { class: 'hint', text: setup.intro() }),
    el('ol', { class: 'setup-steps' }, setup.steps().map(({ text, href }) => el('li', {}, text,
      href && el('br'), href && el('a', { href, target: '_blank', rel: 'noopener noreferrer', text: href })))),
    el('p', { class: 'hint' }, el('a', { href: GUIDE, target: '_blank', rel: 'noopener noreferrer', text: t('setGuide') })),
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
      el('span', { class: 'conn-name' }, serviceMark(p)),
      p.no_key ? el('span', { class: 'dot on', text: t('keyNotNeeded') })
        : el('span', { class: 'dot ' + (p.key ? 'on' : 'off'), text: p.key ? t('keyYes') : t('keyNo') }),
      el('span', { class: 'dot ' + (p.signed_in ? 'on' : 'idle'), text: p.signed_in ? t('signedYes') : t('signedLater') }),
      p.no_key ? (p.signed_in && !S.state.demo
        ? [el('button', { class: 'btn ghost small conn-setup', type: 'button', text: t('signOut'),
            async onclick() {
              this.disabled = true;   // one request: a second click would sign out twice
              try {
                const r = await api('signout/' + p.name, {});
                S.state = await api('state');
                redrawHome();
                toast(r.revoked ? t('signedOut') : t('signedOutLocal'));
              } catch (error) { this.disabled = false; toast(error.message, true); }
            } }),
           el('p', { class: 'hint conn-hint', text: t('signOutHint') })]
        : el('p', { class: 'hint conn-hint', text: t('scNoKey') }))
      : el('button', { class: 'btn ghost small conn-setup', type: 'button', 'aria-expanded': String(S.setup === p.name),
        text: p.key ? t('setChange') : t('setUp'),
        onclick() { S.setup = S.setup === p.name ? null : p.name; redrawHome(); } }),
      S.setup === p.name && !p.no_key && setupPanel(p)))),
    songbaseRow(),
    el('p', { class: 'hint', text: t('ytLeft', { left }) }),
    !S.state.demo && el('p', { class: 'hint' }, t('demoTryHint') + ' ',
      el('button', { class: 'link', type: 'button', text: t('demoTry'), async onclick() {
        this.disabled = true;
        try { S.state = await api('demo', {}); redrawHome(); scrollTo(0, 0); }
        catch (error) { this.disabled = false; toast(error.message, true); }
      } })));
}

function songbaseRow() {   // not in the demo (it never asks the base); an older server sends no songbase
  const base = S.state.songbase;
  if (S.state.demo || !base) return null;
  const box = el('input', { type: 'checkbox', checked: base.on, disabled: base.locked, async onchange() {
    try { S.state.songbase = (await api('settings', { songbase: box.checked })).songbase; }
    catch (error) { box.checked = base.on; toast(error.message, true); }
  } });
  return el('div', { class: 'songbase' },
    el('label', { class: 'songbase-switch' }, box, ' ', t('songbase')),
    el('p', { class: 'hint', text: t('songbaseHint') }),
    base.locked && el('p', { class: 'hint', text: t('songbaseLocked') }));
}

// --- Plan ------------------------------------------------------------------------------------------

// The hosts platforms/signin.py accepts for a sign-in link; checked here again before anything is shown.
const SIGNIN_HOSTS = ['accounts.spotify.com', 'accounts.google.com', 'secure.soundcloud.com'];
function isSigninLink(url) {
  try { const parsed = new URL(url); return parsed.protocol === 'https:' && SIGNIN_HOSTS.includes(parsed.hostname); }
  catch { return false; }
}
// Shown while a service waits for the browser. The node is kept (S.signin) while the link stays the same.
const signinShown = job => !!(job && job.signin && S.signin && S.signin.url === job.signin.url && S.signin.node.isConnected);

function signinCard(signin) {
  if (S.signin && S.signin.url === signin.url) return S.signin.node;
  const field = el('input', { class: 'link-input signin-link', type: 'text', readonly: true, value: signin.url,
    'aria-label': t('signinTitle', { service: signin.service }), onfocus: event => event.target.select() });
  const copy = () => Promise.resolve().then(() => navigator.clipboard.writeText(signin.url))
    .then(() => toast(t('signinCopied')), () => { field.focus(); field.select(); });
  const node = el('div', { class: 'signin' },
    el('h3', { text: t('signinTitle', { service: signin.service }) }),
    tip('wave', t('tipSignin', { service: signin.service })),
    el('a', { class: 'btn primary', href: signin.url, target: '_blank', rel: 'noopener noreferrer', text: t('signinOpen') }),
    field,
    el('button', { class: 'btn ghost', type: 'button', text: t('signinCopy'), onclick: copy }),
    el('p', { class: 'hint', text: t('signinHint') }));
  S.signin = { url: signin.url, node };
  return node;
}

function jobCard(job) {
  const searching = job.kind === 'plan', checking = job.kind === 'check';
  const share = job.total ? Math.min(100, Math.round(100 * job.done / job.total)) : 0;
  // After an error the sign-in server is closed and the link is dead: the error says what to do.
  const signin = job.state === 'running' && job.signin && isSigninLink(job.signin.url) ? signinCard(job.signin) : null;
  if (!signin) S.signin = null;
  const progress = job.total ? t('xOfY', job) : (searching ? t('readingPlaylist') : t('preparing'));
  if (job.state === 'error') {
    return el('div', { class: 'card job bad' },
      el('h2', { text: searching ? t('searchStopped') : checking ? t('checkStopped') : t('transferStopped') }),
      el('p', { class: 'job-error', text: job.error }),
      // the YouTube limit: the search allows about 100 songs a day, adding about 200 (a different text)
      job.quota ? tip('yawn', job.kind === 'plan' ? t('tipQuota') : t('tipQuotaAdd'), t('savedHint'))
        : tip('stand', t('tipStopped'), t('savedHint')));
  }
  return el('div', { class: 'card job' },
    el('h2', { text: searching ? t('searching') : checking ? t('checking') : t('adding') }),
    signin,
    el('div', { class: 'bar', role: 'progressbar', 'aria-valuenow': share, 'aria-valuemin': 0, 'aria-valuemax': 100 },
      el('span', { style: `width:${job.total ? share : 4}%` })),
    // one mascot per screen: while the sign-in card waits, it is the one talking
    signin ? el('p', { class: job.total ? 'job-count' : 'job-wait', text: progress })
      : tip('jump', progress, el('p', { class: job.total ? 'job-count' : 'job-wait', text: progress })),
    el('p', { class: 'job-line', text: job.lines[job.lines.length - 1] || '' }));
}

// Spotify ignores "public: false" from apps and opens any playlist by its link (tested 14 Nov 2026).
const spotifyNote = () => S.plan.to === 'spotify' && tip('think', t('tipSpotify'), t('spotifyPublic'));
// The transfer button's name; the mascot's "press …" quotes the same text.
const applyLabel = () => S.plan.applied ? t('addNew') : S.plan.to === 'spotify' ? t('createPlaylist') : t('createPrivate');

function resultCard(job) {
  const result = job.result;
  const link = safeLink(result.url);
  return el('div', { class: 'card done' },
    el('h2', { text: t('done') }),
    el('p', { text: t('addedLine', result) }),
    !result.skipped.length && tip('celebrate', t('tipDone')),
    // one mascot per screen: the Spotify heads-up was the mascot's in the dialog, here it is the grey line
    S.plan.to === 'spotify' && el('p', { class: 'hint', text: t('spotifyPublic') }),
    link ? el('a', { class: 'btn primary', href: link, target: '_blank', rel: 'noopener noreferrer', text: t('openPlaylist') })
         : S.state.demo && el('p', { class: 'hint', text: t('demoNoPlaylist') }),
    result.skipped.length > 0 && el('div', { class: 'skipped' },
      el('h3', { text: t('couldNotAdd', { count: result.skipped.length }) }),
      el('ul', {}, result.skipped.map(([song, reason]) => el('li', {}, el('b', { text: song }), ' — ' + reason))),
      tip('shrug', t('tipSkipped'), t('pickAnother'))));
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
        text: `${index + 1}. ` + [S.plan.tracks[index].title || (S.plan.from === 'soundcloud' ? t('scTrack') : ''),
          (S.plan.tracks[index].artists || []).join(', ')].filter(Boolean).join(' — ') })))),
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
  const delta = track.duration_ms ? candidate.seconds - Math.round(track.duration_ms / 1000) : 0;   // lists may have no lengths
  const key = index + '/' + candidate.id;
  const slot = el('div', { class: 'player ' + platform });
  if (S.playing.get(index) === candidate.id && frameUrl) {
    slot.append(el('iframe', { src: frameUrl, title: t('player'), loading: 'lazy', allow: 'encrypted-media',
      // YouTube refuses to play (error 153) without a referrer; only our origin is sent, never the path.
      referrerpolicy: 'strict-origin-when-cross-origin', sandbox: 'allow-scripts allow-same-origin allow-presentation' }));
  }
  const bare = !candidate.title && S.plan.to === 'soundcloud';   // a clean transfer to SoundCloud kept only the id: no name, author, length or score
  return el('li', { class: 'cand' + (chosen ? ' chosen' : ''), 'data-key': key },
    !bare && el('span', { class: 'score', title: t('scoreTitle'), text: Math.round((candidate.score || 0) * 100) + '%' }),
    el('span', { class: 'cand-text' },
      el('b', { text: bare ? t('scTrack') : candidate.title }),
      !bare && el('i', { text: `${candidate.artist || '—'} · ${clock(candidate.seconds || 0)}` +
        (delta ? ` (${delta > 0 ? '+' : '−'}${Math.abs(delta)} ${t('sec')})` : '') +
        (candidate.source === 'discogs' ? ` · ${t('fromDiscogs')}` : '') +
        (platform === 'soundcloud' && candidate.uploader ? ` · ${t('scUploader', { name: candidate.uploader })}` : '') })),
    el('span', { class: 'cand-actions' },
      frameUrl && el('button', { class: 'btn ghost', type: 'button', text: S.playing.get(index) === candidate.id ? t('hide') : t('listen'),
        onclick() { S.playing.get(index) === candidate.id ? S.playing.delete(index) : S.playing.set(index, candidate.id); drawPlan(); } }),
      link && el('a', { class: 'btn ghost', href: link, target: '_blank', rel: 'noopener noreferrer',
        text: platform === 'soundcloud' ? t('openSc') : t('open') }),
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
    : match && !match.title && S.plan.to === 'soundcloud' ? el('span', { class: 'dst' }, el('b', { text: t('scTrack') }))
    : match ? el('span', { class: 'dst' }, el('b', { text: match.title }),
        el('i', { text: `${match.artist} · ${clock(match.seconds || 0)}` + (match.source === 'discogs' ? ` · ${t('fromDiscogs')}` : '') }))
    : el('span', { class: 'dst empty', text: t('noCandidate') });
  // a cleared SoundCloud song has no name and no artists: show only what is left, never "— · 0:00"
  const info = [(track.artists || []).join(', '), track.duration_ms ? clock(track.duration_ms / 1000) : ''].filter(Boolean).join(' · ');
  const head = el('button', { class: 'row-main', type: 'button', 'aria-expanded': String(open), onclick() {
    open ? S.open.delete(index) : S.open.add(index); drawPlan();
  } },
    el('span', { class: 'num', text: String(index + 1).padStart(2, '0') }),
    el('span', { class: 'src' }, el('b', { text: track.title || (S.plan.from === 'soundcloud' ? t('scTrack') : '—') }),
      (track.title || info) && el('i', { text: info || '—' })),
    el('span', { class: 'arrow', 'aria-hidden': 'true', text: '→' }), right,
    el('span', { class: 'badge', text: t(STATUS_TEXT[status]) }));
  const details = open && el('div', { class: 'cands' },
    track.source_title && el('p', { class: 'hint', text: t('originalTitle', { title: track.source_title }) }),
    track.uploader && el('p', { class: 'hint', text: t('scUploader', { name: track.uploader }) }),
    safeLink(track.source_url) && el('p', { class: 'hint' }, el('a', { href: safeLink(track.source_url), target: '_blank',
      rel: 'noopener noreferrer', text: t('openSc') })),
    (track.candidates || []).length
      ? el('ul', {}, track.candidates.map(candidate => candidateRow(index, track, candidate)))
      : el('p', { class: 'hint', text: S.plan.cleared && S.plan.to === 'soundcloud' ? t('scCleared') : t('searchEmpty') }),
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

function confirmApply(count, ask) {
  const dialog = document.getElementById('confirm');
  dialog.replaceChildren(...[   // replaceChildren would show a false child as the word "false"
    el('h2', { text: S.plan.applied ? t('addQ') : t('createQ') }),
    el('p', { text: S.plan.applied ? t('addBody', { to: label(S.plan.to), title: S.plan.title, count })
      : S.plan.to === 'spotify' ? t('createBodySpotify', { title: S.plan.title, count })
      : t('createBody', { to: label(S.plan.to), title: S.plan.title, count }) }),
    ask > 0 && !S.plan.cleared && (S.plan.from === 'soundcloud' || S.plan.to === 'soundcloud')
      // to SoundCloud the undecided lose their candidates; from SoundCloud they keep them but lose their names
      && el('p', { class: 'hint', text: S.plan.to === 'soundcloud' ? t('scPendingLost', { count: ask })
        : t('scPendingNames', { count: ask }) }),
    !S.plan.applied && spotifyNote(),
    el('div', { class: 'dialog-actions' },
      el('button', { class: 'btn ghost', type: 'button', text: t('cancel'), onclick: () => dialog.close() }),
      el('button', { class: 'btn primary', type: 'button', text: t('yesTransfer'), async onclick() {
        dialog.close();
        try { S.state.job = await api(`plans/${S.plan.name}/apply`, {}); drawPlan(); poll(); }
        catch (error) { toast(error.message, true); }
      } }))].filter(Boolean));
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

  // an error or result card above has its own mascot; one mascot per screen
  const cardShown = job && (job.state === 'error' || job.state === 'done' && job.kind === 'apply' && job.result);
  view.replaceChildren(el('div', { class: 'plan' }, back,
    el('header', { class: 'plan-head' },
      el('h1', { text: S.plan.title || S.plan.name }),
      el('p', { class: 'route' }, service(S.plan.from), ' → ', service(S.plan.to), ` · ${songs(tracks.length)}`),
      (S.plan.from === 'soundcloud' || S.plan.to === 'soundcloud') && soundcloudMark(true),
      S.plan.cleared && el('p', { class: 'hint', text: t('scCleared') })),
    job && job.state === 'error' && jobCard(job),
    job && job.state === 'done' && job.kind === 'apply' && job.result && resultCard(job),
    // The three counters are the filter (Ivan kept clicking them); pressing the chosen one again shows all.
    el('div', { class: 'stats', role: 'group', 'aria-label': t('whichSongs') },
      [['in', 's-auto', going, t('statIn')], ['ask', 's-ask', counts.ask, t('statAsk')],
       ['out', 's-none', counts.none + counts.skip, t('statOut')]].map(([id, tone, number, text]) =>
        el('button', { class: 'stat ' + tone, type: 'button', 'aria-pressed': String(S.filter === id),
          onclick() { S.filter = S.filter === id ? 'all' : id; drawPlan(); } },
          el('b', { text: number }), el('span', { text })))),
    // under the counters: what to do next on this screen (nothing here when tips are hidden)
    cardShown ? null : counts.ask > 0 ? ([...S.open].some(index => tracks[index] && statusOf(tracks[index]) === 'ask')
        ? tip('listen', t('tipListen', { listen: t('listen') })) : tip('point_up', t('tipReview')))
      : going > 0 && !S.plan.applied && tip('jump', t('tipAllSet', { button: applyLabel() })),
    el('div', { class: 'chips' }, el('button', { class: 'chip', type: 'button', 'aria-pressed': String(S.filter === 'all'),
      onclick() { S.filter = 'all'; drawPlan(); } }, t('fAll'), el('span', { class: 'chip-count', text: tracks.length }))),
    el('div', { class: 'rows' }, rows.length ? rows
      : el('p', { class: 'empty-list', text: S.filter === 'ask' ? t('emptyAsk') : t('emptyOther') })),
    videoIds.length > 0 && musicCard(videoIds, gone),
    el('div', { class: 'apply-bar' },
      el('span', { text: going ? t('barGoing', { to: label(S.plan.to), songs: songs(going) }) : t('barNone') }),
      S.plan.applied && safeLink(S.plan.target_url) && el('a', { class: 'btn ghost', href: safeLink(S.plan.target_url),
        target: '_blank', rel: 'noopener noreferrer', text: t('playlistLink') }),
      el('button', { class: 'btn primary', type: 'button', disabled: !going,
        text: applyLabel(),
        onclick: () => confirmApply(going, counts.ask) }))));
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
        // While the same sign-in link waits nothing else changes; a redraw would drop the field's focus.
        if (job && job.state !== 'running') await loadPlan(location.hash.slice(7)); else if (!signinShown(job)) drawPlan();
      }
    } catch { /* the program was stopped; the next tick tries again */ }
    poll();
  }, 500);
}

async function route() {
  clearTimeout(S.timer);
  try { S.state = await api('state'); }
  catch (error) { document.getElementById('view').replaceChildren(el('p', { class: 'hint', text: error.message })); return; }
  document.getElementById('version').textContent = S.state.version || '';
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
  drawTipsSwitch();
  window.addEventListener('hashchange', route);
  route();
});
