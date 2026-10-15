import { BASE, api, esc, toast } from './core.js';
import { onAction, onChange } from './delegate.js';

// ---------------- Fountain (fork-only: fountain_app/) ----------------
// One W7H's visits for a local day, each with Evyatar's label buttons and the
// visit's videos, plus per-cat stats for the last 7 or 30 days. Labels live in
// the fountain app's own table (see fountain_app/__init__.py), never in the
// events' pet column, which upstream rewrites.
let FX_DEVICE = '',
  FX_DATE = null,
  FX_DAYS = 7;
//: The last visits payload, so a label click can re-render one card.
let FX_DATA = null;

// Same rule as the Timeline: the server cuts days at LOCAL midnight.
function todayLocal() {
  const d = new Date();
  const pad = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}
// Per path SEGMENT, as media.js does: device folder names contain '#'.
function mediaUrl(rel) {
  return rel ? BASE + 'api/media/' + rel.split('/').map(encodeURIComponent).join('/') : '';
}
function hhmm(ts) {
  return ts
    ? new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    : '';
}
function fmtDur(sec) {
  if (sec == null) return '';
  const s = Math.round(sec);
  if (s < 60) return s + ' s';
  return Math.floor(s / 60) + ' min' + (s % 60 ? ' ' + (s % 60) + ' s' : '');
}
function fmtMin(sec) {
  return (sec / 60).toFixed(sec && sec < 600 ? 1 : 0) + ' min';
}

function labelChips(v, pets) {
  const choices = pets
    .map(p => [String(p.id), p.pet_name || 'pet ' + p.id])
    .concat([
      ['unknown', 'Unknown'],
      ['not_a_cat', 'Not a cat'],
    ]);
  return `<div class="chips fx-labels" role="group" aria-label="Who was this?">
    ${choices
      .map(
        ([k, name]) =>
          `<button class="chip-btn${v.label === k ? ' on' : ''}" data-action="fx-label" data-visit="${esc(v.visit_key)}" data-label="${esc(k)}">${esc(name)}</button>`,
      )
      .join('')}
    ${v.label ? `<button class="chip-btn" data-action="fx-unlabel" data-visit="${esc(v.visit_key)}" title="Remove the label">✕</button>` : ''}
  </div>`;
}

// One block per device episode: an appearance and a drinking episode are
// filmed separately, so a merged visit has two of each video.
function episodeHtml(e) {
  const m = e.media || {};
  const what = e.kind === 'appearance' ? 'Appeared' : 'Drinking';
  let body = '';
  if (m.highlight_url) {
    body += `<video class="fx-clip" controls preload="none" playsinline src="${esc(mediaUrl(m.highlight_url))}"${
      m.snapshot_url ? ` poster="${esc(mediaUrl(m.snapshot_url))}"` : ''
    }></video>`;
  } else if (m.snapshot_url) {
    body += `<img class="fx-clip hide-on-error" src="${esc(mediaUrl(m.snapshot_url))}" alt="">`;
  }
  let full = '';
  if (m.playback_url) {
    full = `<button class="act mini-ex" data-action="open-video" data-media="${esc(
      JSON.stringify([['Full recording', m.playback_url]]),
    )}" data-pending="0">▶ Full recording</button>`;
  } else if (m.video_pending) {
    full = '<span class="pill">Full recording processing…</span>';
  }
  return `<div class="fx-ep">
    <div class="mut fx-ep-head">${esc(what)} ${esc(hhmm(e.start_ts))}</div>
    ${body || '<div class="mut">No video.</div>'}
    ${full ? `<div class="fx-ep-foot">${full}</div>` : ''}
  </div>`;
}

function visitCard(v, pets) {
  const bits = [];
  if (v.duration_sec != null) bits.push('drank ' + fmtDur(v.duration_sec));
  else if (!v.has_drinking) bits.push('no drinking');
  if (v.drink_time_sec != null) bits.push('device drink time ' + fmtDur(v.drink_time_sec));
  const guess = v.device_guess != null ? v.device_guess_name || 'pet ' + v.device_guess : 'none';
  return `<div class="card fx-visit" data-visit="${esc(v.visit_key)}">
    <div class="row fx-head">
      <b>${esc(hhmm(v.start_ts))}</b>
      <span class="mut">${esc(bits.join(' · '))}</span>
      <span class="grow"></span>
      <span class="pill" title="The cat the fountain itself recognised">Fountain guess: ${esc(guess)}</span>
    </div>
    ${labelChips(v, pets)}
    <div class="fx-eps">${(v.episodes || []).map(episodeHtml).join('')}</div>
  </div>`;
}

function controlsHtml(data) {
  const many = (data.fountains || []).length > 1;
  return `<div class="card">
    <div class="row" style="align-items:center">
      <button class="ghost act" data-action="fx-shift" data-n="-1">◂</button>
      <input type="date" value="${esc(data.date || FX_DATE)}" data-change="fx-date" style="width:auto">
      <button class="ghost act" data-action="fx-shift" data-n="1">▸</button>
      ${
        many
          ? `<select data-change="fx-device" style="width:auto">${data.fountains
              .map(
                d =>
                  `<option value="${esc(d.id)}"${d.id === data.device.id ? ' selected' : ''}>${esc(d.name)} #${esc(d.id)}</option>`,
              )
              .join('')}</select>`
          : `<span class="mut">${esc(data.device.name)} #${esc(data.device.id)}</span>`
      }
      <span class="grow"></span>
      <button class="act" data-action="fx-refresh">Refresh</button>
      <button class="act" data-action="fx-export" title="Write every labeled visit's images to a dataset folder on the server">Export dataset</button>
    </div>
  </div>`;
}

function statsHtml(st) {
  if (!st || st.error) {
    return `<div class="card fx-stats"><p class="mut">${esc((st && st.error) || 'No stats.')}</p></div>`;
  }
  const n = st.dates.length;
  const name = s => s.label_name || s.key;
  const rows = st.series
    .map(
      s => `<tr><td>${esc(name(s))}</td><td>${esc(s.visits)}</td><td>${esc(fmtMin(s.drink_sec))}</td>
        <td>${esc((s.visits / n).toFixed(1))}</td><td>${esc(fmtMin(s.drink_sec / n))}</td><td>${esc(s.labeled)}</td></tr>`,
    )
    .join('');
  const daily = st.dates
    .slice()
    .reverse()
    .map((d, ri) => {
      const i = n - 1 - ri;
      return `<tr><td>${esc(d)}</td>${st.series
        .map(s => {
          const c = s.daily[i];
          return `<td>${c.visits ? esc(c.visits + ' · ' + fmtMin(c.drink_sec)) : '<span class="mut">·</span>'}</td>`;
        })
        .join('')}</tr>`;
    })
    .join('');
  return `<div class="card fx-stats">
    <div class="row" style="align-items:center;margin-bottom:8px">
      <h3 style="margin:0">Last ${esc(st.days)} days</h3>
      <div class="chips" role="group" aria-label="Range">
        ${[7, 30]
          .map(
            d =>
              `<button class="chip-btn${d === FX_DAYS ? ' on' : ''}" data-action="fx-days" data-n="${d}">${d} days</button>`,
          )
          .join('')}
      </div>
    </div>
    <table>
      <tr><th>Cat</th><th>Visits</th><th>Drinking</th><th>Visits / day</th><th>Drinking / day</th><th>Labeled</th></tr>
      ${rows}
    </table>
    <p class="sub">${esc(st.labeled)} of ${esc(st.visits)} visits labeled. ${esc(st.not_a_cat)} marked not a cat (not counted above). A visit counts for its label, else the fountain's guess, else Unknown.</p>
    <details><summary class="mut">Per day</summary>
      <table><tr><th>Day</th>${st.series.map(s => `<th>${esc(name(s))}</th>`).join('')}</tr>${daily}</table>
    </details>
  </div>`;
}

async function loadStats() {
  const q = new URLSearchParams({ days: String(FX_DAYS) });
  if (FX_DEVICE) q.set('device', FX_DEVICE);
  const st = await api('fountain/stats?' + q.toString());
  const el = document.querySelector('#fountainView .fx-stats');
  if (el) el.outerHTML = statsHtml(st);
}

// Active alerts as a banner, with the no-drink threshold beside them. The
// server raises and clears them once a minute; this only reads, on tab load,
// on each `fountain_alert` hub event (ws.js) and on a poll while visible.
function alertItemsHtml(al) {
  const since = a => {
    const ts = a.kind === 'tank' ? a.detail.since_ts : a.detail.last_drink_ts;
    if (!ts) return '';
    const what = a.kind === 'tank' ? 'since' : 'last drank';
    return ` <span class="mut">(${what} ${esc(new Date(ts * 1000).toLocaleString())})</span>`;
  };
  if (!al.active.length) return '<span class="mut">No active alerts.</span>';
  const items = al.active
    .map(a => `<li><b>${esc(a.detail.message || a.subject)}</b>${since(a)}</li>`)
    .join('');
  return `<ul class="fx-alert-list">${items}</ul>`;
}
function alertsHtml(al) {
  if (!al || al.error) return '<div class="fx-alerts"></div>';
  const hours = al.settings ? al.settings.no_drink_hours : 12;
  const setting = `<div class="row fx-alert-setting"><span class="mut">Alert when a cat has not drunk for</span>
    <input type="number" class="fx-hours" min="1" max="168" step="1" value="${esc(hours)}"> <span class="mut">hours</span>
    <button class="ghost act" data-action="fx-alert-save">Save</button></div>`;
  const cls = al.active.length ? 'card notice fx-alerts' : 'card fx-alerts';
  return `<div class="${cls}"><div class="fx-alert-body">${alertItemsHtml(al)}</div>${setting}</div>`;
}

//: Bumped per request, so a slow answer never overwrites a newer one.
let FX_ALERT_SEQ = 0;
async function loadAlerts() {
  const seq = ++FX_ALERT_SEQ;
  const al = await api('fountain/alerts');
  if (seq !== FX_ALERT_SEQ) return;
  const el = document.querySelector('#fountainView .fx-alerts');
  if (!el) return;
  const body = el.querySelector('.fx-alert-body');
  if (!body) {
    el.outerHTML = alertsHtml(al);
    return;
  }
  if (!al || al.error) return; // keep what is shown rather than blank it
  // Only the list changes: the threshold field may hold an unsaved edit.
  body.innerHTML = alertItemsHtml(al);
  el.classList.toggle('notice', al.active.length > 0);
}

function fountainVisible() {
  const tab = document.getElementById('tab-fountain');
  return !!tab && !tab.classList.contains('hidden') && document.visibilityState === 'visible';
}
let FX_ALERT_TIMER = null;
// Called by ws.js for each `fountain_alert` hub event (raised or cleared). A
// burst of them costs one request; a hidden tab reloads on its next visit.
function scheduleFountainAlerts() {
  if (FX_ALERT_TIMER || !fountainVisible()) return;
  FX_ALERT_TIMER = setTimeout(() => {
    FX_ALERT_TIMER = null;
    loadAlerts();
  }, 300);
}
// The poll covers a hub event missed while the socket was reconnecting.
const FX_ALERT_POLL_MS = 60000;
setInterval(() => {
  if (fountainVisible()) loadAlerts();
}, FX_ALERT_POLL_MS);
document.addEventListener('visibilitychange', () => {
  if (fountainVisible()) loadAlerts();
});

async function loadFountain() {
  if (!FX_DATE) FX_DATE = todayLocal();
  const q = new URLSearchParams({ date: FX_DATE });
  if (FX_DEVICE) q.set('device', FX_DEVICE);
  const data = await api('fountain/visits?' + q.toString());
  const v = document.getElementById('fountainView');
  if (data.error) {
    FX_DATA = null;
    const msg =
      data.error === 'no fountain device'
        ? 'No W7H fountain is registered. Visits show up here once one connects.'
        : data.error;
    v.innerHTML = `<div class="card"><p class="mut">${esc(msg)}</p></div>`;
    return;
  }
  FX_DATA = data;
  FX_DEVICE = String(data.device.id);
  FX_DATE = data.date;
  const visits = data.visits.length
    ? data.visits.map(x => visitCard(x, data.pets)).join('')
    : '<div class="card"><p class="mut">No visits on this day.</p></div>';
  v.innerHTML =
    controlsHtml(data) +
    '<div class="fx-alerts"></div>' +
    '<div class="card fx-stats"><p class="mut">Loading stats…</p></div>' +
    `<div id="fxList">${visits}</div>`;
  loadAlerts();
  loadStats();
}

// A label click re-renders only that card's label chips, so a video playing
// in it (or anywhere else on the page) keeps playing.
async function setLabel(key, label) {
  const device = FX_DEVICE;
  const r = label
    ? await api(`fountain/visits/${encodeURIComponent(key)}/label`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ device: Number(device), label }),
      })
    : await api(
        `fountain/visits/${encodeURIComponent(key)}/label?device=${encodeURIComponent(device)}`,
        {
          method: 'DELETE',
        },
      );
  if (r.error) {
    toast('Could not save the label: ' + r.error);
    return;
  }
  const visit = FX_DATA && FX_DATA.visits.find(x => x.visit_key === key);
  if (visit) {
    visit.label = label || null;
    const card = [...document.querySelectorAll('#fxList .fx-visit')].find(
      c => c.dataset.visit === key,
    );
    const chips = card && card.querySelector('.fx-labels');
    if (chips) chips.outerHTML = labelChips(visit, FX_DATA.pets);
  }
  toast(label ? 'Labeled ' + (r.label_name || label) : 'Label removed');
  loadStats();
}

function shiftDay(n) {
  const d = new Date(FX_DATE + 'T00:00:00Z');
  d.setUTCDate(d.getUTCDate() + n);
  FX_DATE = d.toISOString().slice(0, 10);
  loadFountain();
}

onAction('fx-label', el => setLabel(el.dataset.visit, el.dataset.label));
onAction('fx-unlabel', el => setLabel(el.dataset.visit, null));
onAction('fx-shift', el => shiftDay(Number(el.dataset.n)));
onAction('fx-refresh', () => loadFountain());
onAction('fx-alert-save', async el => {
  const input = el.closest('.fx-alerts').querySelector('.fx-hours');
  const r = await api('fountain/alert-settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ no_drink_hours: Number(input.value) }),
  });
  if (r.error) {
    toast('Could not save: ' + r.error);
    return;
  }
  toast(`No-drink alert set to ${r.settings.no_drink_hours} hours`);
  loadAlerts();
});
onAction('fx-export', async el => {
  el.disabled = true;
  const label = el.textContent;
  el.textContent = 'Exporting…';
  const q = FX_DEVICE ? '?device=' + encodeURIComponent(FX_DEVICE) : '';
  const r = await api('fountain/export' + q, { method: 'POST' });
  el.disabled = false;
  el.textContent = label;
  if (r.error) {
    toast('Export failed: ' + r.error);
    return;
  }
  toast(
    `Exported ${r.images} images from ${r.visits} visits to ${r.path}` +
      (r.ffmpeg ? '' : ' (snapshots only: ffmpeg is missing)'),
  );
});
onAction('fx-days', el => {
  FX_DAYS = Number(el.dataset.n);
  loadStats();
});
onChange('fx-date', el => {
  FX_DATE = el.value;
  loadFountain();
});
onChange('fx-device', el => {
  FX_DEVICE = el.value;
  loadFountain();
});

export { loadFountain, scheduleFountainAlerts };
