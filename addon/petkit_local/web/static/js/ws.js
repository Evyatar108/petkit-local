import { BASE } from './core.js';
import { loadDevices, scheduleRefresh, scheduleDetail } from './devices.js';
import { scheduleFountainAlerts } from './fountain.js';
import { pushLog } from './log.js';
import { loadPatchers } from './patchers.js';
import { scheduleTimeline } from './timeline.js';

// ---------------- WebSocket ----------------

//: Retry delay, doubled per attempt. The cap is a whole Home Assistant restart
//: long: the endpoint is gone for as long as that takes, and a tab left open
//: overnight must not keep knocking at the base delay for the rest of it.
const WS_RETRY_MIN = 2000,
  WS_RETRY_MAX = 30000;
let _wsRetry = WS_RETRY_MIN;
function connectWS() {
  const st = document.getElementById('wsState');
  const down = () => {
    st.textContent = 'reconnecting';
    st.className = 'pill bad';
    setTimeout(connectWS, _wsRetry);
    _wsRetry = Math.min(_wsRetry * 2, WS_RETRY_MAX);
  };
  let ws;
  try {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(proto + '//' + location.host + BASE + 'api/ws');
  } catch (e) {
    // The constructor throws on a URL it cannot parse, and a connection that
    // never existed has no close event — so this is the only place left that
    // can re-arm the retry, and without it the pill says "reconnecting" for
    // the life of the page while nothing is trying.
    down();
    return;
  }
  ws.onopen = () => {
    _wsRetry = WS_RETRY_MIN;
    st.textContent = 'live';
    st.className = 'pill ok';
  };
  ws.onclose = down;
  ws.onmessage = ev => {
    let e;
    try {
      e = JSON.parse(ev.data);
    } catch (_) {
      return; // one malformed frame must not take the handler down with it
    }
    if (e.kind === 'ping') {
      loadDevices();
      scheduleDetail(null);
      return;
    }
    pushLog(e);
    if (e.kind === 'patcher') {
      const m = (e.summary || '').match(/^\[(\w+)\]/);
      if (m) {
        const lg = document.getElementById('plog_' + e.device_id + '_' + m[1]);
        if (lg) {
          lg.style.display = 'block';
          lg.textContent += e.summary + '\n';
          lg.scrollTop = lg.scrollHeight;
          if (/done|FAILED/.test(e.summary)) setTimeout(loadPatchers, 2000);
        }
      }
    }
    scheduleRefresh();
    if (e.device_id != null) scheduleDetail(e.device_id);
    else if (e.kind === 'connect') scheduleDetail(null);
    // media becomes "ready" (and new events land) asynchronously, well after
    // the request that triggered them returns — without this the Timeline
    // can render before the file exists, its <img> 404s, and nothing ever
    // retries (see the hide-on-error handling in mediaThumbs() below).
    if (e.kind === 'media' || e.kind === 'event') scheduleTimeline();
    if (e.kind === 'fountain_alert') scheduleFountainAlerts();
  };
}

export { connectWS };
