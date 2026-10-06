"""The web page's Live view (data/telemetry/web/index.html, the live-run block): its polling, ingest and drawing
run in node against a stub DOM, fed by real LiveBuffer bodies. The path that matters: away from the tab (hidden
canvases have no width, the page hidden or on Coaching) and back, a run finished while away, the next run."""
import json
import os
import re
import shutil
import subprocess

import pytest

from oversteer.live_buffer import LiveBuffer
from tests.test_live_buffer import drive, reference, row

PAGE = os.path.join(os.path.dirname(__file__), '..', 'data', 'telemetry', 'web', 'index.html')

pytestmark = pytest.mark.skipif(shutil.which('node') is None, reason='no node')

HARNESS = r'''
const vm = require("vm");
const fs = require("fs");
const bodies = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const page = fs.readFileSync(process.argv[3], "utf8");
const first = page.indexOf("let liveTimer = null, polling = false;");
const firstEnd = page.indexOf("function blankLive()");
const second = page.indexOf("// -- the live run: /api/v1/live");
const secondEnd = page.indexOf("// -- keeping the screen on --");
if (first < 0 || second < 0 || firstEnd < first || secondEnd < second) throw new Error("live blocks not found");
const code = page.slice(first, firstEnd) + page.slice(second, secondEnd);

const els = {};
function makeEl(id, tag) {
  const e = {id, tagName: (tag || "div").toUpperCase(), hidden: false, textContent: "", className: "", style: {}, children: [], handlers: {},
    disabled: false, title: "", type: "", clientWidth: 0, width: 0, height: 0, parentNode: {clientWidth: 0}, draws: 0};
  const set = new Set();
  e.classList = {add: (c) => set.add(c), remove: (c) => set.delete(c), contains: (c) => set.has(c) || e.className.split(" ").includes(c),
    toggle: (c, f) => { if (f === undefined ? !set.has(c) : f) set.add(c); else set.delete(c); }};
  e.appendChild = (c) => { e.children.push(c); return c; };
  e.removeChild = (c) => { e.children.splice(e.children.indexOf(c), 1); };
  Object.defineProperty(e, "firstChild", {get: () => e.children[0] || null});
  e.addEventListener = (n, fn) => { e.handlers[n] = fn; };
  e.setAttribute = () => {};
  e.getContext = () => new Proxy({}, {get: (t, k) => (k in t ? t[k] : (...a) => { e.draws++; }), set: (t, k, v) => { t[k] = v; return true; }});
  return e;
}
const doc = {hidden: false, documentElement: {}, getElementById: (id) => els[id] || (els[id] = makeEl(id)),
  createElement: (tag) => makeEl("", tag), createTextNode: (t) => { const e = makeEl("", "#text"); e.textContent = t; return e; }};
const fetched = [];
let queue = [];
const ctx = {document: doc, window: {devicePixelRatio: 1, location: {hash: ""}}, location: {hash: ""}, console,
  getComputedStyle: () => ({getPropertyValue: () => "#ffffff"}),
  setImmediate, setTimeout: () => 0, clearTimeout: () => {}, requestAnimationFrame: (f) => f(), Math, JSON, Object, Array, Set, Number, String, Date,
  $: (id) => doc.getElementById(id),
  el: (tag, text, cls) => { const e = makeEl("", tag); if (text != null) e.textContent = text; if (cls) e.className = cls; return e; },
  clear: (n) => { while (n.firstChild) n.removeChild(n.firstChild); },
  clock: (t) => t == null ? "–" : Math.floor(t / 60) + ":" + (t % 60).toFixed(1).padStart(4, "0"),
  MINUS: "−", conn: {}, updateConn: () => {}, showLive: () => {}, showSplits: () => {}, loadCar: () => { ctx.loads++; },
  showTab: (n) => { ctx.tabs.push(n); }, loads: 0, tabs: [],
  get: async (url) => { fetched.push(url); const b = queue.shift(); if (b === undefined) throw new Error("none"); return b; }};
ctx.bodies = bodies; ctx.queue = queue; ctx.fetched = fetched; ctx.ctx = ctx;
vm.createContext(ctx);
const scenario = fs.readFileSync(process.argv[4], "utf8");
vm.runInContext(code + "\n;(async () => {" + scenario + "})().then((r) => { console.log(JSON.stringify(r)); process.exit(0); }, (e) => { console.log(JSON.stringify({error: String(e && e.stack || e)})); process.exit(1); });", ctx);
'''

def bodies(tmp_path):
    """Bodies as the web page would read them: a run in progress seen at 3 moments, its finish, the next run."""
    buffer = LiveBuffer(clock=lambda: 1000.0 + clock[0])
    clock = [0.0]
    ref = reference(speed=20.0)
    buffer.start_run(1, 1000.0, 'acr', 'acr:test', 'Test', 2000.0)
    buffer.offer_reference(1, ref, {'key': 'acr:test', 'name': 'Test', 'length': 2000.0}, 7)
    out = {}
    end = drive(buffer, 1, 19.0, 300.0)
    clock[0] = end - 1000.0
    out['early'] = buffer.read(0, now=end)
    seen = out['early']['seq']
    out['early_next'] = buffer.read(seen, now=end)
    # away for a while: the page is hidden and the run goes on (rows come to the buffer, none is read)
    t0 = round(end - 1000.0 + 0.1, 6)
    end = drive(buffer, 1, 19.0, 1100.0, start=1000.0 + t0, t0=t0)
    out['back'] = buffer.read(0, now=end)               # what resumeLive reads: since=0
    out['back_seen'] = seen
    buffer.finish(1, end, 61.0)
    out['finished'] = buffer.read(0, now=end)
    buffer.start_run(2, end + 5, 'acr', 'acr:test', 'Test', 2000.0)
    buffer.push(2, end + 5, row(0.0, 0.0))
    out['next'] = buffer.read(0, now=end + 5)
    return out


def run_node(tmp_path, scenario):
    data = tmp_path / 'bodies.json'
    data.write_text(json.dumps(bodies(tmp_path)))
    harness = tmp_path / 'harness.js'
    harness.write_text(HARNESS)
    steps = tmp_path / 'scenario.js'
    steps.write_text(scenario)
    done = subprocess.run(['node', str(harness), str(data), os.path.abspath(PAGE), str(steps)], capture_output=True, text=True,
                          timeout=60)
    assert done.stdout.strip(), done.stderr
    result = json.loads(done.stdout.strip().splitlines()[-1])
    assert 'error' not in result, result['error']
    return result


def test_the_page_has_the_live_block():
    page = open(PAGE, encoding='utf-8').read()
    assert 'api/v1/live?since=' in page and 'function resumeLive' in page and 'id="back-live"' in page
    for name in ('c-pedals', 'c-steer', 'c-gg', 'c-minimap', 'dv', 'dbar', 'done', 'open-run', 'open-debrief'):
        assert 'id="{}"'.format(name) in page


def test_away_and_back_repopulates_the_strips_and_keeps_polling(tmp_path):
    result = run_node(tmp_path, r'''
const b = bodies;
const out = {};
const cells = [0, 1, 2].map((i) => { const c = el("i"); c.tagName = "I"; return c; });
for (const c of cells) $("ribbon").appendChild(c);
lastSplits = {splits: [{d0: 0, d1: 500}, {d0: 500, d1: 1000}, {d0: 1000, d1: 2000}], possible: 99};
// 1. on the tab with canvases that have width: it polls with since and draws
for (const id of ["c-pedals", "c-steer", "c-gg", "c-minimap", "c-hb"]) { $(id).clientWidth = 300; $(id).parentNode.clientWidth = 300; }
$("tab-telemetry").hidden = false; $("live-view").hidden = false;
queue.push(b.early);
await live();
out.rows_early = lr.rows.length; out.since_early = lr.since; out.first_url = fetched[0];
out.drew_early = $("c-pedals").draws > 0;
// 2. the page hidden: no request, and the chain goes on (the timer is re-armed)
document.hidden = true;
const before = fetched.length;
await live();
out.hidden_fetches = fetched.length - before;
// 3. back: canvases were reset to no width while hidden; resumeLive reads from the start of the buffer
document.hidden = false;
$("c-pedals").clientWidth = 0; $("c-pedals").parentNode.clientWidth = 0;
$("c-pedals").draws = 0; $("c-steer").draws = 0;
queue.push(b.back);
$("c-pedals").clientWidth = 300; $("c-pedals").parentNode.clientWidth = 300;
resumeLive();
await new Promise((r) => setImmediate(r));
await new Promise((r) => setImmediate(r));
out.resume_url = fetched[fetched.length - 1];
out.rows_back = lr.rows.length;
const times = lr.rows.map((r) => r.t);
out.sorted_unique = times.every((t, i) => i === 0 || t > times[i - 1]);
out.first_t = times[0]; out.last_t = lr.lastT;
out.polling_free = polling === false;
out.drew_back = $("c-pedals").draws > 0 && $("c-steer").draws > 0;
// the 30 s ring left a hole between what was held and what the buffer still has: the strip is cut there, not drawn across
const gapRows = lr.rows.filter((r, i) => i && r.t - lr.rows[i - 1].t > 0.5).length;
out.gaps = gapRows;
return out;
''')
    assert result['first_url'].endswith('since=0') and result['since_early'] > 0 and result['rows_early'] > 20
    assert result['drew_early'] and result['hidden_fetches'] == 0
    assert result['resume_url'].endswith('since=0')                 # back on the tab: the whole buffer
    assert result['sorted_unique'] and result['polling_free'] and result['drew_back']
    assert result['last_t'] > 50 and result['rows_back'] >= 250      # repopulated from the buffer (the last 30 s)
    assert result['first_t'] > 20                                    # and nothing older than the ring is kept


def test_a_run_finished_while_away_shows_the_card_with_a_way_back(tmp_path):
    result = run_node(tmp_path, r'''
const b = bodies;
const out = {};
for (const id of ["c-pedals", "c-steer", "c-gg", "c-minimap"]) { $(id).clientWidth = 300; }
$("tab-telemetry").hidden = true;                       // on Coaching: nothing is drawn, the state still moves
queue.push(b.early); await live();
queue.push(b.finished); await live();
out.drawn_hidden = $("c-pedals").draws;
$("tab-telemetry").hidden = false;                      // back on Telemetry: the finished card, not a dash
queue.push(b.finished); resumeLive(); await new Promise((r) => setImmediate(r)); await new Promise((r) => setImmediate(r));
out.resume_url = fetched[fetched.length - 1];
out.done_hidden = $("done").hidden; out.dash_hidden = $("live").hidden;
out.time = $("done-t").textContent; out.open_disabled = $("open-run").disabled;
$("back-live").handlers.click();                        // the way back to the dash
out.done_after_back = $("done").hidden; out.dash_after_back = $("live").hidden;
out.note_after_back = $("delta-note").children.length > 0;
queue.push(b.next); await live();                       // the next run starts: cleared, dash, no card
out.rows_next = lr.rows.length; out.run_next = lr.n; out.done_next = $("done").hidden; out.dash_next = $("live").hidden;
$("open-debrief").handlers.click(); out.tab = ctx.tabs[ctx.tabs.length - 1];
return out;
''')
    assert result['resume_url'].endswith('since=0')
    assert result['drawn_hidden'] == 0                              # canvases of a hidden tab are not drawn
    assert result['done_hidden'] is False and result['dash_hidden'] is True
    assert result['time'] == '1:01.0' and result['open_disabled'] is False
    assert result['done_after_back'] is True and result['dash_after_back'] is False and result['note_after_back']
    assert result['rows_next'] == 1 and result['run_next'] == 2
    assert result['done_next'] is True and result['dash_next'] is False
    assert result['tab'] == 'coaching'
