#!/usr/bin/env python3
"""Web UI for the party.py scheduler.

By default this process *also* runs the scheduler (no separate `party.py daemon`
needed). Pass --no-scheduler to disable and use an external daemon instead.

Install:
    pip install flask

Run:
    python3 web.py                  # web + scheduler on 127.0.0.1:5005
    python3 web.py --host 0.0.0.0   # expose on the LAN
    python3 web.py --no-scheduler   # web only; run party.py daemon separately

Endpoints:
    /                        - HTML UI
    /api/status              - GET   live state + daemon info
    /api/schedule            - GET   schedule.json
                             - POST  replace schedule.json, send SIGHUP to daemon
    /api/play/<name>         - POST  play a named event once now (no window/repeats)
    /api/play-next           - POST  fire the next scheduled event
    /api/reload              - POST  reload schedule.json (in-process or SIGHUP)
    /api/volume              - GET / POST {percent} default output (master) volume
    /api/clock               - POST  {enabled: bool, time: "YYYY-MM-DD HH:MM[:SS]"} test clock
"""
from __future__ import annotations

import argparse
import atexit
import json
import os
import signal
import threading
import time

from flask import Flask, Response, jsonify, request

import party

app = Flask(__name__)

# Set by start_scheduler() when the daemon runs in-process.
_scheduler_thread: threading.Thread | None = None


def scheduler_running() -> bool:
    return _scheduler_thread is not None and _scheduler_thread.is_alive()


def start_scheduler() -> None:
    """Run party.daemon_main() in a daemon thread. Refuses if an external
    party.py daemon already holds the PID file."""
    global _scheduler_thread
    existing = party.daemon_pid()
    if existing and existing != os.getpid():
        raise RuntimeError(
            f"another party daemon is running (pid {existing}); "
            f"stop it or run web.py --no-scheduler"
        )
    party._stop = False
    party._reload = False
    _scheduler_thread = threading.Thread(
        target=party.daemon_main,
        kwargs={"from_thread": True},
        daemon=True,
        name="party-scheduler",
    )
    _scheduler_thread.start()


def stop_scheduler() -> None:
    if not scheduler_running():
        return
    party._stop = True
    # Wake the play subprocess if any so the loop exits sooner.
    party._terminate_play()
    _scheduler_thread.join(timeout=10)


atexit.register(stop_scheduler)


# ---------- JSON API --------------------------------------------------------

@app.route("/api/status")
def api_status():
    pid = party.daemon_pid()
    out = {
        "daemon": {
            "pid": pid,
            "running": bool(pid),
            "in_process": scheduler_running() and pid == os.getpid(),
        },
        "now": datetime_now_iso(),
        "clock": {"test": party.test_clock_on(),
                  "real_now": party.real_now_local().strftime("%Y-%m-%d %H:%M:%S")},
    }
    if party.STATE_PATH.exists():
        try:
            out["state"] = json.loads(party.STATE_PATH.read_text())
        except Exception as e:
            out["state_error"] = str(e)
    try:
        events = party.load_config()
        out["events"] = [event_to_dict(e) for e in events]
    except Exception as e:
        out["config_error"] = str(e)
    return jsonify(out)


@app.route("/api/schedule", methods=["GET"])
def api_schedule_get():
    raw = party.CONFIG_PATH.read_text() if party.CONFIG_PATH.exists() else "{}"
    return Response(raw, mimetype="application/json")


@app.route("/api/schedule", methods=["POST"])
def api_schedule_post():
    raw = request.get_data(as_text=True)
    try:
        cfg = json.loads(raw)
    except json.JSONDecodeError as e:
        return jsonify({"error": f"invalid JSON: {e}"}), 400
    backup = party.CONFIG_PATH.read_text() if party.CONFIG_PATH.exists() else None
    # Atomic write: a daemon receiving SIGHUP races us on the file, and a
    # plain write_text() can be observed mid-flush (load_config would then
    # fail with JSONDecodeError and keep the old config). tmp + rename
    # gives the daemon either the old contents or the full new contents.
    tmp = party.CONFIG_PATH.with_suffix(party.CONFIG_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    os.replace(tmp, party.CONFIG_PATH)
    try:
        party.load_config()
    except Exception as e:
        if backup is not None:
            tmp.write_text(backup)
            os.replace(tmp, party.CONFIG_PATH)
        return jsonify({"error": str(e)}), 400
    if scheduler_running():
        party._reload = True
        return jsonify({"ok": True, "reloaded": True})
    pid = party.daemon_pid()
    if pid and pid != os.getpid():
        try:
            os.kill(pid, signal.SIGHUP)
            return jsonify({"ok": True, "reloaded": True})
        except ProcessLookupError:
            pass
    return jsonify({"ok": True, "reloaded": False, "note": "no daemon"})


def _run_play_async(target_name: str | None) -> bool:
    """Spawn a daemon thread that runs play_event_exclusive. Returns True if
    a thread was started, False if the event wasn't found."""
    try:
        events = party.load_config()
    except Exception as e:
        party.log(f"manual play: load_config failed: {e}")
        return False
    if target_name is None:
        nxt = party.find_next(events, party.now_local())
        if not nxt:
            return False
        target = nxt[0]
    else:
        target = next((e for e in events if e.name == target_name), None)
        if not target:
            return False
        # ▶ on a schedule row is "play it once now": ignore the event's
        # window and repeat/gap rules.
        target = target.once()
    threading.Thread(
        target=party.play_event_exclusive,
        args=(target, {"mode": "idle"}),
        kwargs={"preempt": True},
        daemon=True,
        name=f"manual-play-{target.name}",
    ).start()
    return True


@app.route("/api/play/<name>", methods=["POST"])
def api_play(name):
    if _run_play_async(name):
        return jsonify({"ok": True})
    return jsonify({"error": f"no event named '{name}'"}), 404


@app.route("/api/play-next", methods=["POST"])
def api_play_next():
    if _run_play_async(None):
        return jsonify({"ok": True})
    return jsonify({"error": "no scheduled events"}), 404


@app.route("/api/reload", methods=["POST"])
def api_reload():
    if scheduler_running():
        party._reload = True
        return jsonify({"ok": True})
    pid = party.daemon_pid()
    if not pid or pid == os.getpid():
        return jsonify({"error": "scheduler not running"}), 400
    os.kill(pid, signal.SIGHUP)
    return jsonify({"ok": True})


@app.route("/api/volume", methods=["GET"])
def api_volume_get():
    return jsonify({"volume": party.get_volume()})


@app.route("/api/volume", methods=["POST"])
def api_volume_set():
    data = request.get_json(silent=True) or {}
    try:
        pct = int(data.get("percent"))
    except (TypeError, ValueError):
        return jsonify({"error": "body must be {percent: int}"}), 400
    pct = max(0, min(150, pct))
    if not party.set_volume(pct):
        return jsonify({"error": "setting system volume failed"}), 503
    return jsonify({"ok": True, "volume": pct})


# ---------- Test clock -------------------------------------------------------

@app.route("/api/clock", methods=["POST"])
def api_clock():
    data = request.get_json(silent=True) or {}
    try:
        party.apply_config_timezone()
        target = party.parse_clock_time(data.get("time", "")) if data.get("enabled") else None
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    party.set_test_clock(target)
    # The scheduler re-plans by itself once it sees clock.json change; when
    # it runs in this process, also cut the current track short. Only when
    # something is playing: _terminate_play() leaves _aborted set otherwise.
    if scheduler_running() and party._play_proc is not None:
        party._terminate_play()
    return jsonify({"ok": True, "test": target is not None, "now": datetime_now_iso()})


# ---------- helpers ---------------------------------------------------------

def event_to_dict(e: party.Event) -> dict:
    return {
        "name": e.name, "time": e.time,
        "file": e.file, "folder": e.folder, "source": e.source(),
        "volume": e.volume,
        "repeat": e.repeat, "repeat_gap": list(e.repeat_gap),
        "days": e.days, "enabled": e.enabled,
    }


def datetime_now_iso() -> str:
    # Use party's configured zone so the header clock matches the schedule
    # and the JS countdown lines up with the daemon's "next" timestamps.
    return party.now_local().strftime("%Y-%m-%d %H:%M:%S")


# ---------- HTML ------------------------------------------------------------

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Party Scheduler</title>
<style>
  :root { --fg:#1f2937; --muted:#6b7280; --ok:#16a34a; --warn:#d97706; --err:#dc2626; --bg:#f3f4f6; --card:#ffffff; --border:#e5e7eb; }
  body { font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background:var(--bg); color:var(--fg); margin:0; padding:1.5rem; }
  h1 { margin:0 0 .5rem; font-size:1.4rem; }
  .row { display:flex; gap:1rem; flex-wrap:wrap; align-items:flex-start; }
  .card { background:var(--card); border:1px solid var(--border); border-radius:8px; padding:1rem 1.25rem; }
  .card h2 { margin:0 0 .75rem; font-size:1rem; color:var(--muted); text-transform:uppercase; letter-spacing:.05em; }
  .badges { display:flex; gap:.5rem; flex-wrap:wrap; }
  .badge { padding:.15rem .6rem; border-radius:999px; font-size:.8rem; font-weight:600; }
  .b-ok { background:#dcfce7; color:var(--ok); }
  .b-err { background:#fee2e2; color:var(--err); }
  .b-warn { background:#fef3c7; color:var(--warn); }
  .b-mut { background:#e5e7eb; color:var(--muted); }
  table { border-collapse:collapse; width:100%; }
  th, td { padding:.4rem .6rem; text-align:left; border-bottom:1px solid var(--border); font-size:.9rem; }
  th { color:var(--muted); font-weight:600; }
  button { padding:.4rem .9rem; border:1px solid var(--border); background:var(--card); border-radius:6px; cursor:pointer; font-size:.9rem; }
  button:hover { background:#f9fafb; }
  button.primary { background:#2563eb; color:white; border-color:#2563eb; }
  button.primary:hover { background:#1d4ed8; }
  button.danger { background:#dc2626; color:white; border-color:#dc2626; }
  textarea { width:100%; min-height:280px; font: 13px/1.4 ui-monospace, Menlo, monospace; border:1px solid var(--border); border-radius:6px; padding:.6rem; box-sizing:border-box; }
  .mono { font-family: ui-monospace, Menlo, monospace; }
  .muted { color:var(--muted); }
  .actions { display:flex; gap:.5rem; flex-wrap:wrap; margin-top:.5rem; }
  .err { color:var(--err); white-space:pre-wrap; }
  .grow { flex:1 1 460px; min-width:380px; }

  .header { display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:1rem; }
  .clock { display:flex; align-items:center; gap:.5rem; flex-wrap:wrap; margin-top:.4rem; }
  .clock input[type=text] { font: 13px ui-monospace, Menlo, monospace; padding:.3rem .5rem;
                            border:1px solid var(--border); border-radius:6px; width:13.5em; }
  .clock.on input[type=text] { border-color:var(--warn); background:#fffbeb; }
</style>
</head>
<body>
<div class="header">
  <div>
    <h1>Party Scheduler</h1>
    <div class="muted" id="now"></div>
    <div class="clock" id="clockBox">
      <label title="Run the scheduler on a fake clock that starts at the time in the box and keeps ticking">
        <input type="checkbox" id="clockOn" onchange="clockToggle()"> test clock</label>
      <input type="text" id="clockInput" placeholder="YYYY-MM-DD HH:MM:SS" spellcheck="false"
             onkeydown="if (event.key === 'Enter') setClock()">
      <button onclick="setClock()" title="Turn the test clock on at this time">Set</button>
      <button onclick="clockBeforeNext()" title="Jump to 10 seconds before the next scheduled event">10s before next</button>
      <span class="err" id="clockErr"></span>
    </div>
  </div>
</div>

<div class="row" style="margin-top:1rem">
  <div class="card grow">
    <h2>Status</h2>
    <div class="badges" id="badges"></div>
    <div id="volRow" style="margin-top:.7rem; display:flex; align-items:center; gap:.6rem">
      <label for="volSlider" style="min-width:3em" title="Default audio output (master) volume">Vol</label>
      <input id="volSlider" type="range" min="0" max="150" step="1" value="80" style="flex:1; max-width:340px">
      <span id="volReadout" class="mono" style="min-width:3em; text-align:right">--</span>
      <button onclick="refreshVolume()" title="Re-read the output volume">↻</button>
    </div>
    <div id="nowplaying" style="margin-top:.7rem"></div>
    <div id="nextevent" style="margin-top:.5rem"></div>
    <div id="lastevent" style="margin-top:.5rem"></div>
    <div class="actions">
      <button class="primary" onclick="action('play-next')">Play next now</button>
      <button onclick="action('reload')">Reload schedule</button>
    </div>
    <div class="err" id="err"></div>
  </div>

  <div class="card grow">
    <h2>Schedule</h2>
    <table id="schedTable">
      <thead><tr><th></th><th>Name</th><th>Time</th><th>Vol</th><th>Rpt</th><th>Days</th><th>Source</th><th></th></tr></thead>
      <tbody></tbody>
    </table>
  </div>
</div>

<div class="card" style="margin-top:1rem">
  <h2>Edit schedule.json</h2>
  <textarea id="cfg" spellcheck="false"></textarea>
  <div class="actions">
    <button class="primary" onclick="saveCfg()">Save & reload</button>
    <button onclick="loadCfg()">Reset (reload from disk)</button>
  </div>
  <div class="err" id="cfgerr"></div>
</div>

<script>
async function api(path, opts){
  const r = await fetch(path, opts || {});
  if (!r.ok) {
    const t = await r.text();
    throw new Error(`${r.status}: ${t}`);
  }
  const ct = r.headers.get('content-type') || '';
  return ct.includes('json') ? r.json() : r.text();
}

function esc(s){ return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]); }
function badge(text, cls){ return `<span class="badge ${cls}">${esc(text)}</span>`; }

let lastNextTs = null;
let lastNextKey = null;
// Server clock minus browser clock. The countdown runs on the server's clock,
// which is the test clock when that's on (and may be another timezone anyway).
let serverSkewMs = 0;

async function refresh(){
  try {
    const s = await api('/api/status');
    const tz = (s.state && s.state.tz) || 'local';
    const testClock = s.clock && s.clock.test;
    document.getElementById('now').textContent = testClock
      ? `TEST CLOCK: ${s.now}  (real ${s.clock.real_now}, tz: ${tz})`
      : `server time: ${s.now}  (tz: ${tz})`;
    serverSkewMs = new Date(s.now.replace(' ', 'T')).getTime() - Date.now();
    syncClockUi(s.now, testClock);

    const badges = [];
    badges.push(s.daemon.running
      ? badge(`scheduler ${s.daemon.in_process ? 'in-process' : 'pid ' + s.daemon.pid}`, 'b-ok')
      : badge('scheduler stopped', 'b-err'));
    if (testClock) badges.push(badge('test clock', 'b-warn'));
    badges.push(badge(`mode ${(s.state && s.state.mode) || '?'}`, 'b-mut'));
    document.getElementById('badges').innerHTML = badges.join('');

    const cur = s.state && s.state.current;
    document.getElementById('nowplaying').innerHTML = cur
      ? `<b>Now:</b> ${esc(cur.name)} ${cur.of !== 1 ? `[${esc(cur.iteration)}/${esc(cur.of)}]` : ''}
         <span class="mono">${cur.file ? 'file=' + esc(cur.file) : 'src=' + esc(cur.source)}</span>
         vol ${esc(cur.volume)}% — since ${esc(s.state.started_at)}`
      : '<span class="muted">idle</span>';

    const nxt = s.state && s.state.next;
    const nextKey = nxt ? `${nxt.name}|${nxt.time}|${nxt.source}|${nxt.volume}` : 'none';
    if (nextKey !== lastNextKey) {
      lastNextKey = nextKey;
      if (nxt) {
        lastNextTs = nxt.time;
        document.getElementById('nextevent').innerHTML =
          `<b>Next:</b> ${esc(nxt.name)} at <span class="mono">${esc(nxt.time)}</span> — src=${esc(nxt.source)} vol ${esc(nxt.volume)}% <span id="countdown" class="muted"></span>`;
      } else {
        lastNextTs = null;
        document.getElementById('nextevent').innerHTML = '<b>Next:</b> <span class="muted">no scheduled events</span>';
      }
    } else if (nxt) {
      lastNextTs = nxt.time;
    }

    const last = s.state && s.state.last;
    document.getElementById('lastevent').innerHTML = last
      ? `<b>Last:</b> ${esc(last.name)} at <span class="mono">${esc(last.time)}</span> ${last.ok ? '✓' : '✗ ' + esc(last.error||'')} — played=${esc(last.played ?? '?')}`
      : '';

    // schedule table
    const tbody = document.querySelector('#schedTable tbody');
    tbody.innerHTML = '';
    (s.events || []).forEach(e => {
      const days = e.days.length === 7 ? 'every day' : e.days.join(',');
      const rpt = e.repeat > 1 ? `x${e.repeat}` : (e.time.includes('-') ? '∞' : '-');
      const src = e.folder ? `${e.folder}/ (random)` : e.file;
      const tr = document.createElement('tr');
      if (!e.enabled) tr.style.opacity = .5;
      tr.innerHTML = `<td>${e.enabled ? '' : '✕'}</td>
        <td>${esc(e.name)}</td>
        <td class="mono">${esc(e.time)}</td>
        <td>${esc(e.volume)}%</td>
        <td>${esc(rpt)}</td>
        <td class="muted">${esc(days)}</td>
        <td class="mono">${esc(src)}</td>
        <td><button title="Play once now" onclick="action('play/${encodeURIComponent(e.name)}')">▶</button></td>`;
      tbody.appendChild(tr);
    });

    document.getElementById('err').textContent = '';
  } catch (e) {
    document.getElementById('err').textContent = e.message;
  }
}

function tick(){
  const cd = document.getElementById('countdown');
  if (!cd || !lastNextTs) return;
  const dt = new Date(lastNextTs.replace(' ', 'T'));
  const ms = dt.getTime() - (Date.now() + serverSkewMs);
  if (ms <= 0) { cd.textContent = ' — firing…'; return; }
  const s = Math.floor(ms/1000), h = Math.floor(s/3600), m = Math.floor((s%3600)/60), sec = s%60;
  cd.textContent = ` (in ${h}h ${m}m ${sec}s)`;
}

async function action(path){
  try { await api('/api/' + path, { method:'POST' }); refresh(); }
  catch (e) { document.getElementById('err').textContent = e.message; }
}

async function loadCfg(){
  try {
    const raw = await api('/api/schedule');
    document.getElementById('cfg').value = typeof raw === 'string' ? raw : JSON.stringify(raw, null, 2);
    document.getElementById('cfgerr').textContent = '';
  } catch (e) { document.getElementById('cfgerr').textContent = e.message; }
}

async function saveCfg(){
  try {
    const raw = document.getElementById('cfg').value;
    await api('/api/schedule', { method:'POST', headers:{'content-type':'application/json'}, body: raw });
    document.getElementById('cfgerr').textContent = 'saved & reload signalled';
    refresh();
  } catch (e) { document.getElementById('cfgerr').textContent = e.message; }
}

// ---- Volume slider --------------------------------------------------
let volDirty = false;          // user is actively dragging
let volSendTimer = null;
let volLastSent = null;

async function sendVolume(pct){
  try {
    await api('/api/volume', {
      method:'POST',
      headers:{'content-type':'application/json'},
      body: JSON.stringify({percent: pct}),
    });
    volLastSent = pct;
  } catch (e) {
    document.getElementById('err').textContent = 'volume: ' + e.message;
  }
}

async function refreshVolume(){
  if (volDirty) return;        // don't fight the user
  try {
    const r = await api('/api/volume');
    if (typeof r.volume === 'number') {
      const slider = document.getElementById('volSlider');
      const ro = document.getElementById('volReadout');
      slider.value = r.volume;
      ro.textContent = r.volume + '%';
    } else {
      document.getElementById('volReadout').textContent = 'n/a';
    }
  } catch (e) { /* ignore */ }
}

(function initVolSlider(){
  const slider = document.getElementById('volSlider');
  const ro = document.getElementById('volReadout');
  slider.addEventListener('input', () => {
    volDirty = true;
    const pct = +slider.value;
    ro.textContent = pct + '%';
    // Debounce: send 100ms after the last input
    clearTimeout(volSendTimer);
    volSendTimer = setTimeout(() => sendVolume(pct), 100);
  });
  slider.addEventListener('change', () => {
    // Final commit, then release the "dirty" lock after a beat
    const pct = +slider.value;
    sendVolume(pct);
    setTimeout(() => { volDirty = false; }, 500);
  });
})();

// ---- Test clock -----------------------------------------------------
let clockBusy = false;

function syncClockUi(now, testOn){
  const box = document.getElementById('clockBox');
  const cb = document.getElementById('clockOn');
  const inp = document.getElementById('clockInput');
  box.classList.toggle('on', !!testOn);
  if (!clockBusy) cb.checked = !!testOn;
  // Keep the box showing the (ticking) clock unless the user is editing it.
  if (document.activeElement !== inp && !inp.dataset.dirty) inp.value = now;
}

(function(){
  const inp = document.getElementById('clockInput');
  inp.addEventListener('input', () => { inp.dataset.dirty = '1'; });
  inp.addEventListener('blur', () => { if (!inp.value.trim()) delete inp.dataset.dirty; });
})();

async function postClock(enabled, time){
  const err = document.getElementById('clockErr');
  const inp = document.getElementById('clockInput');
  clockBusy = true;
  try {
    await api('/api/clock', { method:'POST', headers:{'content-type':'application/json'},
                              body: JSON.stringify({enabled, time}) });
    err.textContent = '';
    delete inp.dataset.dirty;
    inp.blur();
  } catch (e) {
    err.textContent = apiError(e);
    document.getElementById('clockOn').checked = !enabled;
  } finally {
    clockBusy = false;
    refresh();
  }
}

function setClock(){
  document.getElementById('clockOn').checked = true;
  postClock(true, document.getElementById('clockInput').value);
}

function clockToggle(){
  const on = document.getElementById('clockOn').checked;
  postClock(on, document.getElementById('clockInput').value);
}

function clockBeforeNext(){
  if (!lastNextTs) { document.getElementById('clockErr').textContent = 'no next event'; return; }
  const d = new Date(new Date(lastNextTs.replace(' ', 'T')).getTime() - 10000);
  const p = n => String(n).padStart(2, '0');
  const t = `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  document.getElementById('clockInput').value = t;
  setClock();
}

function apiError(e){
  // api() throws "502: {json}" — surface just the server's error text.
  const m = /^\\d+: (.*)$/s.exec(e.message);
  try { return JSON.parse(m[1]).error || e.message; } catch { return e.message; }
}

loadCfg();
refresh();
refreshVolume();
setInterval(refresh, 2000);
setInterval(tick, 1000);
// Re-read the output volume every 5s in case it changed from outside the UI
setInterval(refreshVolume, 5000);
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return INDEX_HTML


# ---------- main ------------------------------------------------------------

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0", help="bind address (use 0.0.0.0 for LAN)")
    ap.add_argument("--port", type=int, default=5005)
    ap.add_argument("--no-scheduler", action="store_true",
                    help="do not run the scheduler in-process (use external party.py daemon)")
    args = ap.parse_args()

    if not args.no_scheduler:
        try:
            start_scheduler()
            print("scheduler started (in-process)")
        except RuntimeError as e:
            print(f"scheduler not started: {e}")
    else:
        print("scheduler disabled (--no-scheduler)")

    print(f"web UI on http://{args.host}:{args.port}")
    # Disable Flask's reloader — it would spawn a second process and a second scheduler.
    app.run(host=args.host, port=args.port, threaded=True, use_reloader=False)
