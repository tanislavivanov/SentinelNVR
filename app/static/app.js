const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const S = {
  mode: 'live', stream: 'homecam', day: null, days: [], segs: [], dayStart: 0,
  cur: null, zoom: 0, status: null,
  events: [], people: [], filter: 'all', clusters: [], profileId: null,
};
const isRec = () => S.mode === 'rec' || S.mode === 'activity'; // both play recordings in the stage
const STREAM_NAMES = { homecam: 'Main', homecam_sub: 'Sub', homecam_h264: 'Compat' };
const ZOOMS = [1, 2, 4, 8];
const WD = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

// ---------------------------------------------------------------- helpers
async function api(path, opts = {}) {
  const r = await fetch(path, {
    credentials: 'same-origin', ...opts,
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
  });
  if (r.status === 401) { showLogin(); throw new Error('auth'); }
  if (!r.ok) throw new Error((await r.text()) || r.status);
  return r;
}
const getJ = async (p) => (await api(p)).json();
const post = async (p, body) => (await api(p, { method: 'POST', body: JSON.stringify(body || {}) })).json();
const p2 = (n) => String(n).padStart(2, '0');
const fmtT = (sec) => { // seconds into the day -> HH:MM:SS
  sec = Math.max(0, Math.round(sec));
  return `${p2(Math.floor(sec / 3600) % 24)}:${p2(Math.floor(sec / 60) % 60)}:${p2(sec % 60)}`;
};
const fmtDur = (sec) => { sec = Math.max(0, Math.floor(sec || 0)); return `${p2(Math.floor(sec / 60))}:${p2(sec % 60)}`; };
const toSec = (hms) => { const [h, m, s] = hms.split(':').map(Number); return h * 3600 + m * 60 + (s || 0); };
const iso = (d) => `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())}`;
const todayIso = () => iso(new Date());
const parseDay = (day) => new Date(day + 'T12:00:00');
function dayLabel(day) {
  const t = new Date(); const y = new Date(); y.setDate(t.getDate() - 1);
  if (day === iso(t)) return 'Today';
  if (day === iso(y)) return 'Yesterday';
  const d = parseDay(day);
  return `${WD[d.getDay()]}, ${d.getDate()} ${MON[d.getMonth()]}`;
}
const icon = (id) => `<svg><use href="#${id}"/></svg>`;
const setIcon = (btn, id) => { btn.querySelector('use').setAttribute('href', '#' + id); };
function setRangeFill(el) { el.style.setProperty('--p', ((el.value - el.min) / (el.max - el.min)) * 100 + '%'); }

let toastTimer = null;
function toast(text, err = false) {
  const t = $('#toast');
  t.textContent = text; t.classList.toggle('err', err); t.classList.add('show');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove('show'), 2600);
}
// hide broken thumbnails, keep the neutral placeholder
document.addEventListener('error', (e) => { if (e.target.tagName === 'IMG') e.target.classList.add('broken'); }, true);

function stageMsg(text, spin = true) {
  const m = $('#stage-msg');
  m.classList.toggle('gone', text == null);
  if (text != null) { $('#live-msg').textContent = text; m.querySelector('.spinner').hidden = !spin; }
}

// ---------------------------------------------------------------- login
function showLogin() { stopLive(); $('#login').hidden = false; $('#pw').focus(); }
$('#login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const btn = $('#login-form button'); btn.disabled = true;
  try {
    const r = await fetch('/api/login', { method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password: $('#pw').value }) });
    if (r.ok) { $('#pw').value = ''; $('#login-err').textContent = ''; $('#login').hidden = true; boot(); }
    else $('#login-err').textContent = r.status === 429 ? 'Too many attempts — wait a few minutes.' : 'That password didn’t work.';
  } finally { btn.disabled = false; }
});
$('#logout').onclick = async () => { await fetch('/api/logout', { method: 'POST' }); showLogin(); };

// ---------------------------------------------------------------- status
async function refreshStatus() {
  try {
    const s = S.status = await getJ('/api/status');
    let cls = 'ok', txt = 'Recording';
    if (!s.nvr_ok) { cls = 'bad'; txt = 'NAS not mounted'; }
    else if (!s.recording) { cls = 'warn'; txt = 'Not recording'; }
    if (!s.go2rtc) { cls = 'bad'; txt = 'Stream service down'; }
    $('#status').className = 'status ' + cls;
    $('#status span').textContent = txt;
    const box = $('#storage');
    if (s.nvr_ok && s.total_gb) {
      box.hidden = false;
      const pct = Math.min(100, ((s.total_gb - s.free_gb) / s.total_gb) * 100);
      const tb = (g) => (g >= 1000 ? (g / 1000).toFixed(1) + ' TB' : Math.round(g) + ' GB');
      $('#storage-txt').textContent = `${tb(s.free_gb)} free · ${s.retention_days} days kept`;
      $('#storage-bar').style.width = pct + '%';
      $('#storage-bar').parentElement.classList.toggle('hot', pct > 90);
    } else box.hidden = true;
  } catch { /* login handled */ }
}

// ---------------------------------------------------------------- mode
$$('.rail-btn[data-mode]').forEach((b) => b.onclick = () => setMode(b.dataset.mode));
function setMode(m) {
  S.mode = m;
  $$('.rail-btn[data-mode]').forEach((b) => b.classList.toggle('on', b.dataset.mode === m));
  const live = m === 'live', people = m === 'people';
  $('#stage').hidden = people; $('.title h1').textContent = people ? 'People' : 'Camera';
  $('#panel-live').hidden = !live; $('#live-bar').hidden = !live; $('#stream-btns').hidden = !live; $('#snap').hidden = !live;
  $('#panel-rec').hidden = m !== 'rec'; $('#panel-activity').hidden = m !== 'activity'; $('#panel-people').hidden = !people;
  $('#rec-bar').hidden = live; $('#compat-wrap').hidden = live || people;
  $('#player').hidden = live; $('#live-box').hidden = !live;
  $('#stage').classList.remove('idle');
  if (live) {
    player.pause(); startLive(); loadPresets();
  } else if (people) {
    stopLive(); player.pause(); openPeople();
  } else {
    stopLive();
    if (S.cur) stageMsg(null);
    else stageMsg(idleMsg(), false);
    if (m === 'activity') { renderActivity(); loadPeople(); }
    renderTimeline(); scrollTimelineTo(S.cur ? S.cur.start + player.currentTime : Date.now() / 1000);
  }
  updateOverlayTime(); renderFeed();
}

// ---------------------------------------------------------------- live
let liveEl = null, liveTimer = null;
async function startLive() {
  stopLive();
  stageMsg('Connecting to camera…');
  $('#ov-stream').textContent = STREAM_NAMES[S.stream];
  try {
    await import('/go2rtc/video-stream.js');
    liveEl = document.createElement('video-stream');
    liveEl.background = false;
    liveEl.src = new URL('/go2rtc/api/ws?src=' + S.stream, location.href);
    $('#live-box').append(liveEl);
    // go2rtc turns on the browser's native controls; they fight with our overlay, so use our own.
    if (liveEl.video) liveEl.video.controls = false;
    syncLiveMute();
    liveTimer = setTimeout(() => {
      if (liveEl && !$('#stage-msg').classList.contains('gone')) stageMsg('Still no picture — try the Compat stream', false);
    }, 12000);
  } catch {
    stageMsg('Live view unavailable — is go2rtc running?', false);
  }
}
// <video-stream> renders its <video> in light DOM, so the non-bubbling 'playing' event can be captured here.
$('#live-box').addEventListener('playing', () => { clearTimeout(liveTimer); if (S.mode === 'live') stageMsg(null); }, true);
function syncLiveMute() {
  const v = liveEl?.video, b = $('#live-mute');
  const muted = !v || v.muted || v.volume === 0;
  setIcon(b, muted ? 'i-mute' : 'i-vol'); b.title = muted ? 'Unmute' : 'Mute';
}
$('#live-box').addEventListener('volumechange', syncLiveMute, true);
$('#live-mute').onclick = () => {
  const v = liveEl?.video; if (!v) return;
  v.muted = !v.muted; if (!v.muted && v.volume === 0) v.volume = 1;
  v.play?.().catch(() => {});
  syncLiveMute();
};
function stopLive() {
  clearTimeout(liveTimer);
  if (!liveEl) return;
  const el = liveEl; liveEl = null;
  // Tear down now instead of letting video-rtc do it 5 s after removal: its teardown sets video.src = '',
  // which fires its own error handler ("[VideoRTC] Video error: Empty src attribute"). background = true
  // makes its disconnectedCallback a no-op; detaching the source this way raises no error event.
  el.background = true;
  el.remove();
  el.wsState = el.pcState = WebSocket.CLOSED;  // stops its onclose() from scheduling a reconnect
  clearTimeout(el.reconnectTID);
  el.ws?.close(); el.ws = null;
  el.pc?.close(); el.pc = null;
  if (el.video) { el.video.pause(); el.video.removeAttribute('src'); el.video.srcObject = null; el.video.load(); }
}
$$('#stream-btns button').forEach((b) => b.onclick = () => {
  if (S.stream === b.dataset.s) return;
  S.stream = b.dataset.s;
  $$('#stream-btns button').forEach((x) => x.classList.toggle('on', x === b));
  startLive();
});
$('#full').onclick = () => {
  if (document.fullscreenElement) document.exitFullscreen();
  else if ($('#stage').requestFullscreen) $('#stage').requestFullscreen();
  else (S.mode === 'live' ? $('#live-box video') : player)?.webkitEnterFullscreen?.();
};
$('#snap').onclick = async () => {
  const b = $('#snap'); b.disabled = true;
  try {
    const r = await api('/api/snapshot');
    const a = document.createElement('a');
    a.href = URL.createObjectURL(await r.blob());
    a.download = `homecam-${Date.now()}.jpg`; a.click();
    toast('Snapshot saved');
  } catch { toast('Snapshot failed', true); } finally { b.disabled = false; }
};
function updateOverlayTime() {
  if (S.mode === 'live') {
    const d = new Date();
    $('#ov-time').textContent = `${dayLabel(todayIso())} · ${p2(d.getHours())}:${p2(d.getMinutes())}:${p2(d.getSeconds())}`;
  } else if (S.cur) {
    $('#ov-time').textContent = `${dayLabel(S.cur.day)} · ${fmtT(S.cur.start + (player.currentTime || 0) - dayStartOf(S.cur.day))}`;
  } else $('#ov-time').textContent = dayLabel(S.day || todayIso());
}
setInterval(() => { if (S.mode === 'live') updateOverlayTime(); }, 1000);

// ---------------------------------------------------------------- PTZ
let holdTimer = null;
const speed = () => parseFloat($('#speed').value);
function syncSpeed() { $('#speed-val').textContent = Math.round(speed() * 100) + '%'; setRangeFill($('#speed')); }
$('#speed').addEventListener('input', syncSpeed); syncSpeed();
function ptzStart(x, y) {
  const send = () => post('/api/ptz/move', { pan: x * speed(), tilt: y * speed(), seconds: 3 }).catch(() => toast('Camera did not respond', true));
  send(); clearInterval(holdTimer); holdTimer = setInterval(send, 2000);
}
function ptzStop() {
  clearInterval(holdTimer); holdTimer = null;
  $$('#pad .hold').forEach((b) => b.classList.remove('hold'));
  post('/api/ptz/stop').catch(() => {});
}
$$('#pad button[data-x]').forEach((b) => {
  const x = +b.dataset.x, y = +b.dataset.y;
  b.addEventListener('pointerdown', (e) => { e.preventDefault(); b.setPointerCapture(e.pointerId); b.classList.add('hold'); ptzStart(x, y); });
  ['pointerup', 'pointercancel'].forEach((ev) => b.addEventListener(ev, ptzStop));
  b.addEventListener('contextmenu', (e) => e.preventDefault());
});
$('#ptz-stop').onclick = ptzStop;
const keys = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, 1], ArrowDown: [0, -1] };
let keyDown = null;
addEventListener('keydown', (e) => {
  if (/INPUT|TEXTAREA/.test(e.target.tagName) || !$('#login').hidden) return;
  if (S.mode === 'live') {
    if (!keys[e.key] || e.repeat) return;
    e.preventDefault(); keyDown = e.key;
    $(`#pad button[data-x="${keys[e.key][0]}"][data-y="${keys[e.key][1]}"]`)?.classList.add('hold');
    ptzStart(...keys[e.key]);
  } else if (S.cur) {
    if (e.key === ' ') { e.preventDefault(); togglePlay(); }
    else if (e.key === 'ArrowLeft') { e.preventDefault(); skip(-10); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); skip(10); }
  }
});
addEventListener('keyup', (e) => { if (e.key === keyDown) { keyDown = null; ptzStop(); } });

async function loadPresets() {
  const box = $('#presets');
  box.querySelectorAll('.chip').forEach((c) => c.remove());
  try {
    (await getJ('/api/ptz/presets')).forEach((p) => {
      const b = document.createElement('button'); b.className = 'chip';
      b.innerHTML = icon('i-pin'); b.append(p.name);
      b.onclick = () => post('/api/ptz/preset/' + encodeURIComponent(p.token))
        .then(() => toast(`Moving to ${p.name}`)).catch(() => toast('Camera rejected the preset', true));
      box.append(b);
    });
  } catch { /* camera may not support presets */ }
  $('#presets-empty').hidden = !!box.querySelector('.chip');
}
$('#preset-save').onclick = async () => {
  const name = prompt('Name this position'); if (!name) return;
  try { await post('/api/ptz/preset-set', { name }); toast('Preset saved'); loadPresets(); }
  catch { toast('Camera rejected the preset', true); }
};

// ---------------------------------------------------------------- days / segments
const dayStarts = {};
const dayStartOf = (day) => dayStarts[day] ?? parseDay(day).setHours(0, 0, 0, 0) / 1000;
async function loadDays() {
  try { S.days = await getJ('/api/days'); } catch { S.days = []; }
  const inp = $('#day');
  if (S.days.length) inp.min = S.days[0];
  inp.max = todayIso();
  await setDay(S.day || todayIso());
}
async function setDay(day) {
  const changed = day !== S.day;
  S.day = day; $('#day').value = day; $('#day-label').textContent = dayLabel(day);
  try {
    const d = await getJ('/api/segments?day=' + day);
    S.segs = d.segments; S.dayStart = d.day_start; dayStarts[day] = d.day_start;
  } catch { S.segs = []; S.dayStart = dayStartOf(day); }
  await loadEvents(day);
  renderDays(); renderTimeline(); renderHours(); renderFeed(); renderActivity();
  if (isRec() && !S.cur) stageMsg(idleMsg(), false);
  if (changed && S.mode === 'rec') scrollTimelineTo(S.cur?.day === day ? S.cur.start + player.currentTime : (S.segs.at(-1)?.end ?? S.dayStart));
  if (S.mode !== 'live') updateOverlayTime();
}
$('#day').onchange = (e) => e.target.value && setDay(e.target.value);
$('.day-btn').addEventListener('click', (e) => { try { $('#day').showPicker(); e.preventDefault(); } catch { /* fallback: native tap */ } });
const shiftDay = (n) => { const d = parseDay(S.day); d.setDate(d.getDate() + n); if (iso(d) <= todayIso()) setDay(iso(d)); };
$('#day-prev').onclick = () => shiftDay(-1);
$('#day-next').onclick = () => shiftDay(1);

function renderDays() {
  const box = $('#days'); box.innerHTML = '';
  const have = new Set(S.days); have.add(todayIso());
  for (let i = 6; i >= 0; i--) {
    const d = new Date(); d.setDate(d.getDate() - i);
    const k = iso(d);
    const b = document.createElement('button');
    b.className = (k === S.day ? 'on ' : '') + (have.has(k) ? '' : 'empty');
    b.innerHTML = `<small>${WD[d.getDay()]}</small><b>${p2(d.getDate())}</b>`;
    b.onclick = () => setDay(k);
    box.append(b);
  }
}

function hourGroups() {
  const g = new Map();
  S.segs.forEach((s) => {
    const h = Math.floor((s.start - S.dayStart) / 3600);
    if (!g.has(h)) g.set(h, { h, segs: [], secs: 0, live: false });
    const x = g.get(h); x.segs.push(s); x.secs += s.end - s.start; x.live ||= s.live;
  });
  return [...g.values()].sort((a, b) => a.h - b.h);
}
const thumbUrl = (s) => `/api/thumb/${s.day}/${s.name}`;
const curHour = () => (S.cur && S.cur.day === S.day ? Math.floor((S.cur.start - S.dayStart) / 3600) : -1);

function renderHours() {
  const box = $('#hours'); box.innerHTML = '';
  const groups = hourGroups();
  if (!groups.length) { box.innerHTML = '<div class="hours-empty">Nothing recorded on this day.</div>'; return; }
  const ch = curHour();
  groups.forEach((g) => {
    const b = document.createElement('button');
    b.className = 'hour' + (g.h === ch ? ' on' : ''); b.dataset.h = g.h;
    b.innerHTML = `<span class="thumb"><img loading="lazy" alt="" src="${thumbUrl(g.segs[0])}">${g.live ? '<span class="badge"><i></i>REC</span>' : ''}<span class="dur">${Math.round(g.secs / 60)}m</span></span>
      <span class="hour-cap"><b>${p2(g.h)}:00</b><span>${g.segs.length} clip${g.segs.length === 1 ? '' : 's'}</span></span>`;
    b.onclick = () => { if (S.mode !== 'rec') setMode('rec'); play(g.segs[0]); };
    box.append(b);
  });
}

let liveThumbTs = Date.now();
function renderFeed() {
  const box = $('#feed'); box.innerHTML = '';
  const live = document.createElement('button');
  live.className = 'item' + (S.mode === 'live' ? ' on' : '');
  live.innerHTML = `<span class="thumb"><img alt="" src="/go2rtc/api/frame.jpeg?src=homecam_sub&t=${liveThumbTs}"><span class="badge"><i></i>LIVE</span></span>
    <span class="item-txt"><b>Live view</b><span>Camera · now</span></span>`;
  live.onclick = () => setMode('live');
  box.append(live);

  const groups = hourGroups().reverse();
  const ch = S.mode === 'rec' ? curHour() : -1;
  groups.forEach((g) => {
    const b = document.createElement('button');
    b.className = 'item' + (g.h === ch ? ' on' : '');
    const mins = Math.round(g.secs / 60);
    b.innerHTML = `<span class="thumb"><img loading="lazy" alt="" src="${thumbUrl(g.segs[0])}"><span class="dur">${mins}m</span></span>
      <span class="item-txt"><b>${p2(g.h)}:00 – ${p2((g.h + 1) % 24)}:00</b>
      <span>${g.segs.length} clip${g.segs.length === 1 ? '' : 's'} · ${dayLabel(S.day)}</span>
      <span>${g.live ? 'Recording now' : fmtT(g.segs[0].start - S.dayStart).slice(0, 5) + ' – ' + fmtT(g.segs.at(-1).end - S.dayStart).slice(0, 5)}</span></span>`;
    b.onclick = () => { if (S.mode !== 'rec') setMode('rec'); play(g.segs[0]); };
    box.append(b);
  });
  if (!groups.length) box.insertAdjacentHTML('beforeend', `<div class="feed-empty">No recordings on ${dayLabel(S.day).toLowerCase() === 'today' ? 'this day yet' : dayLabel(S.day)}.</div>`);
  const total = S.segs.reduce((a, s) => a + s.end - s.start, 0);
  $('#feed-count').textContent = S.segs.length ? `${Math.floor(total / 3600)}h ${p2(Math.round((total % 3600) / 60))}m recorded` : '';
}

// ---------------------------------------------------------------- timeline
const tlInner = $('#tl-inner'), tlScroll = $('#tl-scroll'), track = $('#timeline');
const pct = (t) => ((t - S.dayStart) / 86400) * 100;
function renderTimeline() {
  const z = ZOOMS[S.zoom];
  tlInner.style.width = z * 100 + '%';
  track.style.setProperty('--tick', (100 / (z >= 4 ? 96 : 24)) + '%');
  // ruler
  const step = { 1: 3, 2: 2, 4: 1, 8: 0.5 }[z];
  const r = $('#tl-ruler'); r.innerHTML = '';
  for (let h = 0; h <= 24 + 1e-9; h += step) {
    const s = document.createElement('span');
    s.style.left = (h / 24) * 100 + '%';
    s.textContent = `${p2(Math.floor(h) % 25)}:${p2(Math.round((h % 1) * 60))}`;
    if (h === 0) s.className = 'first'; else if (h >= 24) s.className = 'last';
    r.append(s);
  }
  // recorded blocks (adjacent segments merged)
  const fill = $('#tl-fill'); fill.innerHTML = '';
  const blocks = [];
  S.segs.forEach((s) => {
    const last = blocks.at(-1);
    if (last && s.start - last.end < 10 && !!s.live === !!last.live) last.end = Math.max(last.end, s.end);
    else blocks.push({ start: s.start, end: s.end, live: s.live });
  });
  blocks.forEach((b) => {
    const i = document.createElement('i'); if (b.live) i.className = 'live';
    i.style.left = pct(b.start) + '%';
    i.style.width = `max(2px, ${((b.end - b.start) / 86400) * 100}%)`;
    fill.append(i);
  });
  // motion events underneath: grey = motion, dark = person, amber = face, green = animal
  const em = $('#tl-events'); em.innerHTML = '';
  S.events.forEach((e) => {
    const i = document.createElement('i');
    i.className = e.faces.length ? 'face' : animalsOf(e).length ? 'animal' : hasPerson(e) ? 'person' : '';
    i.style.left = pct(e.start) + '%';
    i.style.width = `max(3px, ${((e.end - e.start) / 86400) * 100}%)`;
    em.append(i);
  });
  updatePlayhead();
}
function updatePlayhead() {
  const ph = $('#ph');
  if (!S.cur || S.cur.day !== S.day) { ph.hidden = true; return; }
  const t = S.cur.start + (player.currentTime || 0);
  ph.hidden = false; ph.style.left = pct(t) + '%';
  $('#ph-label').textContent = fmtT(t - S.dayStart);
}
function scrollTimelineTo(t) {
  requestAnimationFrame(() => {
    const x = (pct(t) / 100) * tlInner.offsetWidth;
    tlScroll.scrollLeft = x - tlScroll.clientWidth / 2;
  });
}
const tlTime = (e) => { const r = track.getBoundingClientRect(); return S.dayStart + Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)) * 86400; };
track.addEventListener('click', (e) => playAt(tlTime(e)));
track.addEventListener('pointermove', (e) => {
  if (e.pointerType === 'touch') return;
  const t = tlTime(e), h = $('#tl-hover');
  h.hidden = false; h.style.left = pct(t) + '%';
  h.firstElementChild.textContent = fmtT(t - S.dayStart);
});
track.addEventListener('pointerleave', () => { $('#tl-hover').hidden = true; });
function setZoom(z) {
  const centerT = S.dayStart + ((tlScroll.scrollLeft + tlScroll.clientWidth / 2) / tlInner.offsetWidth) * 86400;
  S.zoom = Math.max(0, Math.min(ZOOMS.length - 1, z));
  $('#zoom').value = S.zoom; setRangeFill($('#zoom'));
  renderTimeline();
  scrollTimelineTo(S.cur && S.cur.day === S.day ? S.cur.start + player.currentTime : centerT);
}
$('#zoom').addEventListener('input', (e) => setZoom(+e.target.value));
$('#z-in').onclick = () => setZoom(S.zoom + 1);
$('#z-out').onclick = () => setZoom(S.zoom - 1);
setRangeFill($('#zoom'));

// ---------------------------------------------------------------- playback
const player = $('#player');
function playAt(t) {
  if (!S.segs.length) { toast('No recordings on this day'); return; }
  let seg = S.segs.find((s) => s.start <= t && t <= s.end), off = 0;
  if (seg) off = t - seg.start;
  else seg = S.segs.find((s) => s.start > t) || S.segs.at(-1);
  if (S.mode === 'live') setMode('rec');
  play(seg, off);
}
function play(seg, off = 0) {
  const prevHour = curHour();
  S.cur = seg;
  const compat = $('#compat').checked;
  const base = `/rec/${seg.day}/${seg.name}`;
  stageMsg('Loading clip…');
  player.src = base + (compat ? '?h264=1' : '');
  player.onloadedmetadata = () => { if (off && !compat) player.currentTime = off; player.play().catch(() => {}); };
  player.onerror = () => stageMsg('This clip can’t play here — turn on H.264 above.', false);
  $('#dl').href = base + '?dl=1';
  $('#seek').disabled = compat;
  updatePlayhead(); updateOverlayTime();
  if (curHour() !== prevHour) {
    renderFeed();
    $$('.hour').forEach((h) => h.classList.toggle('on', +h.dataset.h === curHour()));
    $('.hour.on')?.scrollIntoView({ inline: 'nearest', block: 'nearest', behavior: 'smooth' });
  }
}
player.addEventListener('playing', () => { if (isRec()) stageMsg(null); });
player.addEventListener('play', () => { setIcon($('#playpause'), 'i-pause'); kickIdle(); });
player.addEventListener('pause', () => { setIcon($('#playpause'), 'i-play'); $('#stage').classList.remove('idle'); });
player.addEventListener('timeupdate', () => {
  if (!S.cur) return;
  const d = player.duration, ct = player.currentTime;
  $('#ctrl-time').textContent = `${fmtDur(ct)} / ${isFinite(d) ? fmtDur(d) : fmtDur(S.cur.end - S.cur.start)}`;
  const seek = $('#seek');
  if (!seeking) { seek.value = isFinite(d) && d ? (ct / d) * 1000 : 0; setRangeFill(seek); }
  updatePlayhead(); updateOverlayTime();
});
player.addEventListener('ended', () => {
  const i = S.segs.indexOf(S.cur);
  if (i >= 0 && S.segs[i + 1]) play(S.segs[i + 1]);
});
function togglePlay() { if (!S.cur) return; player.paused ? player.play().catch(() => {}) : player.pause(); }
function skip(d) {
  if (!S.cur) return;
  if ($('#compat').checked) { toast('Seeking is off in H.264 mode'); return; }
  const t = player.currentTime + d, dur = isFinite(player.duration) ? player.duration : S.cur.end - S.cur.start;
  if (t >= 0 && t < dur) player.currentTime = t;
  else playAt(S.cur.start + t);
}
function stepSeg(n) {
  if (!S.cur) return;
  const all = S.segs.includes(S.cur) ? S.segs : null;
  const i = all ? all.indexOf(S.cur) : -1;
  if (all && all[i + n]) play(all[i + n]); else toast(n > 0 ? 'This is the latest clip' : 'This is the first clip of the day');
}
let seeking = false;
$('#seek').addEventListener('input', (e) => {
  seeking = true; setRangeFill(e.target);
  if (isFinite(player.duration)) player.currentTime = (e.target.value / 1000) * player.duration;
});
$('#seek').addEventListener('change', () => { seeking = false; });
$('#playpause').onclick = togglePlay;
$('#back10').onclick = () => skip(-10);
$('#fwd10').onclick = () => skip(10);
$('#seg-prev').onclick = () => stepSeg(-1);
$('#seg-next').onclick = () => stepSeg(1);
$('#mute').onclick = () => { player.muted = !player.muted; setIcon($('#mute'), player.muted ? 'i-mute' : 'i-vol'); };
$('#go-live').onclick = () => setMode('live');
$('#dl').addEventListener('click', (e) => { if (!S.cur) e.preventDefault(); });
$('#compat').onchange = () => { if (S.cur) play(S.cur, $('#compat').checked ? 0 : player.currentTime); };
player.addEventListener('click', togglePlay);

// auto-hide controls while a recording plays
let idleTimer = null;
function kickIdle() {
  const st = $('#stage'); st.classList.remove('idle'); clearTimeout(idleTimer);
  idleTimer = setTimeout(() => { if (isRec() && !player.paused) st.classList.add('idle'); }, 2800);
}
$('#stage').addEventListener('pointermove', kickIdle);

// export
const playheadSec = () => (S.cur ? S.cur.start + player.currentTime - S.dayStart : 0);
$('#export-open').onclick = () => {
  const ex = $('#export'); ex.hidden = !ex.hidden;
  if (!ex.hidden && S.cur && !$('#ex-from').value) $('#ex-from').value = fmtT(playheadSec());
};
$('#ex-from-ph').onclick = () => { $('#ex-from').value = fmtT(playheadSec()); };
$('#ex-to-ph').onclick = () => { $('#ex-to').value = fmtT(playheadSec()); };
$('#ex-go').onclick = () => {
  const f = $('#ex-from').value, t = $('#ex-to').value;
  if (!f || !t) { toast('Set both From and To', true); return; }
  const a = S.dayStart + toSec(f), b = S.dayStart + toSec(t);
  if (b <= a || b - a > 1800) { toast('Pick a range between 1 second and 30 minutes', true); return; }
  toast('Preparing clip…');
  location.href = `/api/export?start=${a}&end=${b}${$('#compat').checked ? '&h264=1' : ''}`;
};

// ---------------------------------------------------------------- activity (events with people / animals / faces)
const idleMsg = () => (!S.segs.length ? 'No recordings on this day'
  : S.mode === 'activity' ? 'Pick an event below to play it' : 'Choose a time on the timeline or a clip from the list');
const esc = (t) => String(t ?? '').replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
const clock = (t, day = S.day) => fmtT(t - dayStartOf(day));
const ANIMALS = { bird: 'Bird', cat: 'Cat', dog: 'Dog', horse: 'Horse', sheep: 'Sheep', cow: 'Cow', bear: 'Bear' };
const animalsOf = (e) => e.objects.filter((o) => ANIMALS[o.label]);
const hasPerson = (e) => e.objects.some((o) => o.label === 'person') || e.faces.length > 0;
const FILTERS = [
  ['all', 'All', (e) => hasPerson(e) || animalsOf(e).length],
  ['people', 'People', hasPerson],
  ['animals', 'Animals', (e) => animalsOf(e).length],
  ['unknown', 'Unknown', (e) => e.unknown > 0],
  ['motion', 'Motion', () => true],
];
function ago(ts) {
  if (!ts) return 'never';
  const s = Date.now() / 1000 - ts;
  if (s < 90) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  const d = new Date(ts * 1000);
  if (iso(d) === todayIso()) return `today ${p2(d.getHours())}:${p2(d.getMinutes())}`;
  if (s < 2 * 86400) return `yesterday ${p2(d.getHours())}:${p2(d.getMinutes())}`;
  return `${Math.floor(s / 86400)} days ago`;
}
const dur = (a, b) => { const s = Math.max(1, Math.round(b - a)); return s >= 60 ? `${Math.round(s / 60)} min` : `${s} s`; };

async function loadEvents(day = S.day) {
  try { S.events = await getJ('/api/activity?day=' + day); } catch { S.events = []; }
}
async function loadPeople() {
  try { S.people = await getJ('/api/people'); } catch { S.people = []; }
  $('#people-list').innerHTML = S.people.map((p) => `<option value="${esc(p.name)}">`).join('');
}

function eventTags(e) {
  const tags = e.people.map((p) => `<span class="tag ${p.confirmed ? 'known' : 'guess'}">${esc(p.name)}${p.confirmed ? '' : '?'}</span>`);
  // Count people with the person detector (one face slot per person isn't guaranteed); older events have no
  // detector data, so they only say "Person" when nobody was recognised.
  const persons = e.objects.find((o) => o.label === 'person');
  const unnamed = persons ? persons.n - e.people.length : (e.unknown && !e.people.length ? 1 : 0);
  if (unnamed > 0) tags.push(`<span class="tag">${unnamed > 1 ? unnamed + ' people' : e.people.length ? '+1 person' : 'Person'}</span>`);
  animalsOf(e).forEach((o) => tags.push(`<span class="tag animal">${ANIMALS[o.label]}${o.n > 1 ? ' ×' + o.n : ''}</span>`));
  return tags.join('');
}

function faceBtn(f) {
  const b = document.createElement('button');
  b.className = 'face' + (f.confirmed ? ' known' : f.name ? ' guess' : '') + (f.q != null && f.q < 0.3 ? ' poor' : '');
  b.innerHTML = `<img alt="" loading="lazy" src="${f.img}"><span>${esc(!f.name ? 'Unknown' : f.confirmed ? f.name : f.name + '?')}</span>`;
  b.title = f.sim ? `Looks like ${f.name} (${Math.round(f.sim * 100)}%) — tap to confirm` : f.confirmed ? f.name : 'Tap to name';
  b.onclick = (ev) => { ev.stopPropagation(); openFace(f); };
  return b;
}

function renderActivity() {
  if (S.mode !== 'activity') return;
  $('#act-day').textContent = dayLabel(S.day);
  const fs = S.status?.faces, note = $('#act-note');
  let msg = null;
  if (fs && !fs.enabled) msg = `Detection is off: ${fs.reason}`;
  else if (fs && !fs.motion_ok) msg = 'Not receiving video for detection — check the camera and go2rtc.';
  else if (fs && !fs.objects) msg = 'Person and animal detection is off (model missing) — only faces are detected.';
  note.hidden = !msg; note.textContent = msg || '';

  const seg = $('#act-filters'); seg.innerHTML = '';
  FILTERS.forEach(([k, label, fn]) => {
    const n = S.events.filter(fn).length;
    const b = document.createElement('button');
    b.className = S.filter === k ? 'on' : '';
    b.innerHTML = `${label}<small>${n}</small>`;
    b.onclick = () => { S.filter = k; renderActivity(); };
    seg.append(b);
  });

  const fn = FILTERS.find((f) => f[0] === S.filter)[2];
  const evs = S.events.filter(fn).reverse();
  const box = $('#events'); box.innerHTML = '';
  if (!evs.length) {
    box.innerHTML = `<div class="hours-empty">${!S.events.length ? 'No motion on this day.'
      : S.filter === 'all' ? 'Motion only — nobody was recognised as a person or animal. See “Motion”.' : 'Nothing like that on this day.'}</div>`;
    return;
  }
  evs.forEach((e) => {
    const card = document.createElement('div'); card.className = 'ev';
    card.innerHTML = `<button class="ev-play" title="Play from ${clock(e.start)}"><span class="thumb">${e.thumb ? `<img loading="lazy" alt="" src="${e.thumb}">` : ''}
        <span class="tags">${e.live ? '<span class="badge"><i></i>NOW</span>' : ''}${eventTags(e)}</span>
        <span class="dur">${dur(e.start, e.end)}</span></span></button>
      <div class="ev-cap"><b class="mono">${clock(e.start).slice(0, 5)}</b></div>
      <div class="ev-faces"></div>`;
    card.querySelector('.ev-play').onclick = () => playAt(e.start - 2);
    const fbox = card.querySelector('.ev-faces');
    e.faces.forEach((f) => fbox.append(faceBtn(f)));
    animalsOf(e).forEach((o) => {
      const b = document.createElement('button'); b.className = 'face animal';
      b.innerHTML = `<img alt="" loading="lazy" src="${o.img}"><span>${ANIMALS[o.label]}</span>`;
      b.title = `${ANIMALS[o.label]} (${Math.round(o.conf * 100)}%) — play`;
      b.onclick = () => playAt(e.start - 2);
      fbox.append(b);
    });
    box.append(card);
  });
}
$$('[data-shift]').forEach((b) => b.onclick = () => shiftDay(+b.dataset.shift));
async function refreshAll() {
  await Promise.all([loadPeople(), loadEvents()]);
  renderActivity(); renderTimeline();
  if (S.mode === 'people') (S.profileId ? openProfile(S.profileId, true) : renderPeopleHome());
}

// ---------------------------------------------------------------- face dialog
let dlgFace = null;
const dlg = $('#face-dlg');
function openFace(f) {
  dlgFace = f;
  $('#fd-img').src = f.img;
  $('#fd-name').value = f.name || '';
  const poor = f.q != null && f.q < 0.3 ? ' · side-on or blurry, a poor sample' : '';
  $('#fd-meta').textContent = `${dayLabel(iso(new Date(f.ts * 1000)))} ${clock(f.ts, iso(new Date(f.ts * 1000))).slice(0, 5)} · ${f.size}px`
    + (f.sim ? ` · looks like ${f.name} (${Math.round(f.sim * 100)}%)` : f.confirmed ? ' · named by you' : '') + poor;
  const rj = $('#fd-reject');
  rj.hidden = !(f.name && !f.confirmed);
  rj.querySelector('span').textContent = `Not ${f.name || ''}`;
  dlg.showModal();
  if (matchMedia('(pointer:fine)').matches) $('#fd-name').select();
}
dlg.addEventListener('click', (e) => { if (e.target === dlg) dlg.close(); });
$('#face-form').addEventListener('submit', async (e) => {
  e.preventDefault(); if (!dlgFace) return;
  const name = $('#fd-name').value.trim();
  try {
    await post(`/api/faces/${dlgFace.id}/label`, { name });
    dlg.close(); toast(name ? `Saved as ${name}` : 'Name cleared'); await refreshAll();
  } catch { toast('Could not save the name', true); }
});
$('#fd-reject').onclick = async () => {
  try { await post(`/api/faces/${dlgFace.id}/reject`); dlg.close(); toast(`Won’t suggest ${dlgFace.name} for that face`); await refreshAll(); }
  catch { toast('Could not update it', true); }
};
$('#fd-del').onclick = async () => {
  if (!dlgFace) return;
  try { await api('/api/faces/' + dlgFace.id, { method: 'DELETE' }); dlg.close(); toast('Removed'); await refreshAll(); }
  catch { toast('Could not remove it', true); }
};
$('#fd-play').onclick = () => { if (dlgFace) { dlg.close(); playFrom(dlgFace.ts - 3); } };

// generic "type a name" dialog; resolves to the name or null
const ndlg = $('#name-dlg');
let ndResolve = null;
function askName({ title, meta = '', faces = [], value = '' }) {
  $('#nd-title').textContent = title; $('#nd-meta').textContent = meta;
  $('#nd-faces').innerHTML = faces.slice(0, 6).map((f) => `<img alt="" src="${f.img}">`).join('');
  $('#nd-faces').hidden = !faces.length;
  $('#nd-name').value = value;
  ndlg.showModal();
  if (matchMedia('(pointer:fine)').matches) $('#nd-name').select();
  return new Promise((res) => { ndResolve = res; });
}
function ndClose(v) { const r = ndResolve; ndResolve = null; ndlg.close(); r?.(v); }
$('#name-form').addEventListener('submit', (e) => { e.preventDefault(); ndClose($('#nd-name').value.trim() || null); });
$('#nd-cancel').onclick = () => ndClose(null);
ndlg.addEventListener('close', () => ndResolve && ndClose(null));
ndlg.addEventListener('click', (e) => { if (e.target === ndlg) ndClose(null); });

// play a moment on any day (from People)
async function playFrom(t) {
  const day = iso(new Date(t * 1000));
  if (S.mode === 'people' || S.mode === 'live') setMode('activity');
  if (day !== S.day) await setDay(day);
  playAt(t);
  $('#stage').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// ---------------------------------------------------------------- people
async function openPeople() {
  S.profileId = null;
  $('#profile').hidden = true; $('#people-home').hidden = false;
  renderPeopleHome();
  await loadPeople(); renderPeopleHome();
  try { S.clusters = await getJ('/api/faces/unknown'); } catch { S.clusters = []; }
  renderClusters();
}
function renderPeopleHome() {
  const g = $('#people-grid'); g.innerHTML = '';
  $('#people-count').textContent = S.people.length ? `${S.people.length} known` : '';
  if (!S.people.length) {
    g.innerHTML = '<div class="hours-empty">Nobody named yet. Name a face below or in Activity, and SentinelNVR starts recognising that person.</div>';
    return;
  }
  [...S.people].sort((a, b) => (b.last_seen || 0) - (a.last_seen || 0)).forEach((p) => {
    const b = document.createElement('button'); b.className = 'pcard';
    b.innerHTML = `<span class="avatar">${p.img ? `<img alt="" src="${p.img}">` : ''}${p.guesses ? `<span class="pip" title="${p.guesses} suggested match${p.guesses > 1 ? 'es' : ''} to review">${p.guesses}</span>` : ''}</span>
      <b>${esc(p.name)}</b><span>Seen ${ago(p.last_seen)}</span><span>${p.sightings} visit${p.sightings === 1 ? '' : 's'} · 30 days</span>`;
    b.onclick = () => openProfile(p.id);
    g.append(b);
  });
}
function renderClusters() {
  const box = $('#clusters'); box.innerHTML = '';
  if (!S.clusters?.length) { box.innerHTML = '<div class="hours-empty">No unnamed faces. Nice.</div>'; return; }
  S.clusters.forEach((c) => {
    const row = document.createElement('div'); row.className = 'cluster';
    row.innerHTML = `<div class="cl-faces">${c.faces.slice(0, 8).map((f) => `<img alt="" loading="lazy" src="${f.img}" class="${f.q != null && f.q < 0.3 ? 'poor' : ''}">`).join('')}</div>
      <div class="cl-meta"><b>${c.count} face${c.count > 1 ? 's' : ''}</b><span>last ${ago(c.last)}</span></div>
      <div class="cl-actions"><button class="btn-ghost danger" data-a="del" title="Delete these — not faces / not wanted"><svg><use href="#i-trash"/></svg></button>
      <button class="btn-dark" data-a="name">Name…</button></div>`;
    row.querySelector('[data-a=name]').onclick = async () => {
      const name = await askName({ title: 'Who is this?', meta: `Names all ${c.count} face${c.count > 1 ? 's' : ''} in this group. Pick an existing name to add them to that person.`, faces: c.faces });
      if (!name) return;
      try { await post('/api/faces/label', { ids: c.ids, name }); toast(`Saved as ${name}`); await openPeople(); }
      catch { toast('Could not save the name', true); }
    };
    row.querySelector('[data-a=del]').onclick = async () => {
      if (!confirm(`Delete these ${c.count} faces? This can't be undone.`)) return;
      try { await post('/api/faces/delete', { ids: c.ids }); toast('Deleted'); await openPeople(); }
      catch { toast('Could not delete them', true); }
    };
    box.append(row);
  });
}

async function openProfile(pid, keepScroll = false) {
  let p;
  try { p = await getJ('/api/people/' + pid); } catch { toast('Could not load that person', true); return openPeople(); }
  S.profileId = pid;
  $('#people-home').hidden = true;
  const box = $('#profile'); box.hidden = false;
  const max = Math.max(1, ...p.hours);
  const peak = p.hours.indexOf(Math.max(...p.hours));
  box.innerHTML = `
    <div class="pf-top">
      <button class="ic-sm" id="pf-back" aria-label="All people"><svg><use href="#i-left"/></svg></button>
      <span class="avatar xl">${p.img ? `<img alt="" src="${p.img}">` : ''}</span>
      <div class="pf-id">
        <h2>${esc(p.name)}</h2>
        <p class="sub">Last seen ${ago(p.last_seen)} · ${p.total_sightings} visit${p.total_sightings === 1 ? '' : 's'} · ${p.samples.length} photo${p.samples.length === 1 ? '' : 's'}</p>
      </div>
      <div class="pf-actions">
        <button class="btn-ghost" id="pf-rename"><svg><use href="#i-edit"/></svg>Rename</button>
        <button class="btn-ghost danger" id="pf-forget"><svg><use href="#i-trash"/></svg>Forget</button>
      </div>
    </div>
    <section class="pf-sec">
      <div class="pf-h"><h3>Usually around</h3><span class="sub">${p.hours.some(Boolean) ? `visits by hour, last 30 days · busiest ${p2(peak)}:00–${p2((peak + 1) % 24)}:00` : 'no visits in the last 30 days'}</span></div>
      <div class="hours-chart" role="img" aria-label="Visits by hour of day">${p.hours.map((n, h) =>
        `<i style="--h:${(n / max) * 100}%" class="${n ? '' : 'zero'}" title="${p2(h)}:00 — ${n} visit${n === 1 ? '' : 's'}"></i>`).join('')}</div>
      <div class="hours-axis"><span>00</span><span>06</span><span>12</span><span>18</span><span>24</span></div>
    </section>
    ${p.review.length ? `<section class="pf-sec review">
      <div class="pf-h"><h3>Is this ${esc(p.name)}?</h3><span class="sub">${p.review.length} suggested match${p.review.length > 1 ? 'es' : ''} — confirming them makes recognition better</span>
        <button class="btn-dark sm" id="pf-confirm-all"><svg><use href="#i-check"/></svg>Confirm all</button></div>
      <div class="tiles" id="pf-review"></div></section>` : ''}
    <section class="pf-sec">
      <div class="pf-h"><h3>Photos</h3><span class="sub">named faces used for recognition · faded ones are side-on or blurry and are only used if there's nothing better</span></div>
      <div class="tiles" id="pf-samples"></div>
    </section>
    <section class="pf-sec">
      <div class="pf-h"><h3>Visits</h3><span class="sub">tap to play</span></div>
      <div class="visits" id="pf-visits"></div>
    </section>`;
  $('#pf-back').onclick = openPeople;
  $('#pf-rename').onclick = async () => {
    const name = await askName({ title: 'Rename', meta: 'Using another person’s name merges the two.', value: p.name });
    if (!name || name === p.name) return;
    try { const r = await post('/api/people/' + pid, { name }); toast(r.merged ? `Merged into ${name}` : 'Renamed'); await loadPeople(); openProfile(r.id); }
    catch { toast('Could not rename', true); }
  };
  $('#pf-forget').onclick = async () => {
    if (!confirm(`Forget ${p.name}? Their faces stay, but become unnamed.`)) return;
    try { await api('/api/people/' + pid, { method: 'DELETE' }); toast(`Forgot ${p.name}`); openPeople(); }
    catch { toast('Could not forget that person', true); }
  };
  const rv = $('#pf-review');
  if (rv) {
    p.review.forEach((f) => {
      const t = document.createElement('div'); t.className = 'tile' + (f.q != null && f.q < 0.3 ? ' poor' : '');
      t.dataset.id = f.id;
      t.innerHTML = `<img alt="" loading="lazy" src="${f.img}"><span class="sim">${Math.round((f.sim || 0) * 100)}%</span>
        <div class="tile-acts"><button class="yes" title="Yes, it’s ${esc(p.name)}"><svg><use href="#i-check"/></svg></button>
        <button class="no" title="No, not ${esc(p.name)}"><svg><use href="#i-x"/></svg></button></div>`;
      t.querySelector('img').onclick = () => openFace(f);
      t.querySelector('.yes').onclick = async () => { t.classList.add('gone'); await post(`/api/faces/${f.id}/label`, { name: p.name }).catch(() => toast('Failed', true)); };
      t.querySelector('.no').onclick = async () => { t.classList.add('gone'); await post(`/api/faces/${f.id}/reject`).catch(() => toast('Failed', true)); };
      rv.append(t);
    });
    $('#pf-confirm-all').onclick = async () => {
      const ids = [...rv.querySelectorAll('.tile:not(.gone)')].map((t) => +t.dataset.id);
      if (!ids.length) return;
      try { await post('/api/faces/label', { ids, name: p.name }); toast(`Confirmed ${ids.length}`); openProfile(pid); }
      catch { toast('Could not confirm', true); }
    };
  }
  const sm = $('#pf-samples');
  p.samples.forEach((f) => {
    const t = document.createElement('div'); t.className = 'tile' + (f.q != null && f.q < 0.3 ? ' poor' : '') + (f.id === p.cover ? ' cover' : '');
    t.innerHTML = `<img alt="" loading="lazy" src="${f.img}">
      <div class="tile-acts"><button title="Use as profile photo"><svg><use href="#i-star"/></svg></button>
      <button title="Remove from ${esc(p.name)} (keeps the face, unnamed)"><svg><use href="#i-x"/></svg></button></div>`;
    t.querySelector('img').onclick = () => openFace(f);
    const [star, rm] = t.querySelectorAll('button');
    star.onclick = async () => { try { await post('/api/people/' + pid, { cover: f.id }); toast('Profile photo updated'); openProfile(pid, true); } catch { toast('Failed', true); } };
    rm.onclick = async () => {
      try { await post(`/api/faces/${f.id}/label`, { name: '' }); t.classList.add('gone'); toast('Removed from ' + p.name); }
      catch { toast('Failed', true); }
    };
    sm.append(t);
  });
  const vs = $('#pf-visits');
  if (!p.sightings.length) vs.innerHTML = '<div class="hours-empty">No visits recorded yet.</div>';
  p.sightings.forEach((v) => {
    const b = document.createElement('button'); b.className = 'visit';
    b.innerHTML = `<span class="thumb">${v.thumb ? `<img alt="" loading="lazy" src="${v.thumb}">` : ''}</span>
      ${v.face ? `<img class="vf" alt="" loading="lazy" src="${v.face}">` : ''}
      <span class="item-txt"><b>${dayLabel(v.day)} · ${clock(v.start, v.day).slice(0, 5)}</b><span>${dur(v.start, v.end)}</span></span>`;
    b.onclick = () => playFrom(v.start - 2);
    vs.append(b);
  });
  if (!keepScroll) $('#panel-people').scrollIntoView({ block: 'start' });
}

// ---------------------------------------------------------------- boot
async function boot() {
  refreshStatus();
  await loadDays();
  setMode(S.mode);
}
setInterval(() => {
  if (!$('#login').hidden) return;
  refreshStatus();
  liveThumbTs = Date.now();
  if (S.day === todayIso()) setDay(S.day); else renderFeed();
}, 30000);
if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
boot();
