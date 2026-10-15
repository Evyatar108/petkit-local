// The panel's entry point. index.html loads THIS file and nothing else, as a
// `<script type="module">`; every other module is reached from here, so there
// is no classic script on the page and no global namespace to collide in.
//
// The map:
//   core       BASE, api(), esc, the formatters, toast, copyText
//   delegate   the [data-action]/[data-change]/[data-input]/[data-toggle]
//              tables, the document listeners, and the section registry
//   format     one entity value, rendered the way Home Assistant renders it
//   help       the `?` chips and their popover
//   nav        the tab bar; each tab's loader lives in its own module
//   devices    the device panels, their state, and the refresh scheduler
//   entities   entityTable / controlRow — the rows a panel is built from
//   commands   the write path: a control becomes a POST (cf. ha/commands.py)
//   schedules  the schedule editors, registered into the devices panel
//   log        the live traffic log, and the devRun.log the device uploads
//   ws         the WebSocket that drives every live refresh
//   capture · setup · patchers · provision   one tab each
//   timeline   the day view; media  the thumbnails, player and gallery
//   pets       the AI/Pets tab; cropper  the mugshot cropper it opens
//
// SECURITY INVARIANT, and the reason the split changes nothing about it:
// `esc` in core.js is the ONE escape, markup is built as strings and assigned
// through innerHTML, and there is not a single inline `on*=` handler attribute
// anywhere in these files — behaviour is bound once at the document root and
// looked up by name in the delegation table, so nothing device-supplied ever
// reaches a place the browser would parse as code. See the comment above `esc`.

import { api } from './core.js';
import { onAction } from './delegate.js';
import { loadDevices } from './devices.js';
import { connectWS } from './ws.js';

// Imported for their side effects: each one registers its handlers into the
// delegation table and binds whatever listeners it owns.
import './help.js';
import './nav.js';
import './commands.js';
import './schedules.js';
import './log.js';
import './capture.js';
import './setup.js';
import './patchers.js';
import './provision.js';
import './timeline.js';
import './media.js';
import './pets.js';
import './fountain.js';
import './cropper.js';

// Is this page the one the server would serve now?
//
// The Setup tab already shows the running version, and it answers a different
// question: it comes from `/api/info`, so it reports the SERVER's build. A
// fresh server behind a stale page reports the new number while running the old
// code, which is exactly the case that keeps costing an afternoon — a fix is
// deployed, served, and the panel quietly goes on doing the old thing with
// nothing anywhere to say so.
//
// `PANEL_ASSET_V` came with the document and may have come out of a cache;
// `asset_version` from `/api/info` is answered live. Only a stale document can
// make them differ, so the check has no other way to fire.
async function checkPageIsCurrent() {
  const mine = window.PANEL_ASSET_V;
  if (!mine) return; // an older index, or one served without the stamp
  const info = await api('info').catch(() => null);
  if (!info || !info.asset_version || info.asset_version === mine) return;
  const bar = document.createElement('div');
  bar.className = 'stale-banner';
  bar.innerHTML =
    '<b>This page is out of date.</b> The app has been updated since it was ' +
    'loaded, and your browser is still running the older panel. ' +
    '<button class="mini" data-action="reload-panel">Reload</button>';
  document.body.prepend(bar);
}
onAction('reload-panel', () => location.reload());

loadDevices();
connectWS();
checkPageIsCurrent();
