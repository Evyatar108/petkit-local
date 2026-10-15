import { onAction } from './delegate.js';
import { loadDevices } from './devices.js';
import { loadDeviceLogs } from './log.js';
import { loadCapture } from './capture.js';
import { loadPatchers } from './patchers.js';
import { loadSetup } from './setup.js';
import { loadProvision } from './provision.js';
import { loadTimeline } from './timeline.js';
import { loadPets } from './pets.js';
import { loadFountain } from './fountain.js';

document.querySelectorAll('nav button').forEach(b =>
  b.addEventListener('click', () => {
    document.querySelectorAll('nav button').forEach(x => {
      x.classList.remove('on');
      x.setAttribute('aria-selected', 'false');
    });
    b.classList.add('on');
    b.setAttribute('aria-selected', 'true');
    [
      'devices',
      'timeline',
      'fountain',
      'pets',
      'provision',
      'log',
      'capture',
      'patchers',
      'setup',
    ].forEach(t =>
      document.getElementById('tab-' + t).classList.toggle('hidden', t !== b.dataset.tab),
    );
    // Devices needs an entry now: refreshes are skipped while the tab is
    // hidden, so coming back to it has to repaint rather than wait for a tick.
    if (b.dataset.tab === 'devices') loadDevices();
    if (b.dataset.tab === 'log') loadDeviceLogs();
    if (b.dataset.tab === 'capture') loadCapture();
    if (b.dataset.tab === 'patchers') loadPatchers();
    if (b.dataset.tab === 'setup') loadSetup();
    if (b.dataset.tab === 'provision') loadProvision();
    if (b.dataset.tab === 'timeline') loadTimeline();
    if (b.dataset.tab === 'pets') loadPets();
    if (b.dataset.tab === 'fountain') loadFountain();
  }),
);

function gotoTab(t) {
  document.querySelector('nav button[data-tab="' + t + '"]').click();
}
onAction('goto-tab', el => gotoTab(el.dataset.tab));
