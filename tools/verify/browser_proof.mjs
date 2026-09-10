#!/usr/bin/env node
// Real-browser proof for the preview MessageChannel bootstrap (F04).
//
// Serves a synthetic Studio origin and a synthetic preview origin from two loopback ports, drives real
// Chrome over the DevTools Protocol (Node's built-in WebSocket; no new dependency), and exercises the
// handshake in contracts/preview/frame-policy-v1.json under the real sandbox=allow-scripts opaque origin.
//
// NOT a Studio or preview qualification: the origins, manifest and assets are synthetic and no candidate
// code runs. It establishes only that the bootstrap in the contract works in a real engine and that each
// rejection path rejects.
import http from 'node:http';
import { spawn } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const CHROME = process.env.ANVILKIT_CHROME || '/usr/bin/google-chrome-stable';
const MSG_MAX = 65536;          // preview.frameMessageMaxBytes
const READY_TIMEOUT_MS = 400;   // test-scale stand-in for preview.frame.readyTimeoutSeconds

// --------------------------------------------------------------------------- the frame session shell
const SHELL = (studioOrigin, mode) => `<!doctype html><meta charset="utf-8">
<title>preview session shell</title>
<script nonce="NONCEVALUE">
const STUDIO_ORIGIN = ${JSON.stringify(studioOrigin)};   // preconfigured, never learned from a message
const MODE = ${JSON.stringify(mode)};
let port = null, bootstrapped = false;
addEventListener('message', (e) => {
  // the bootstrap is the only window-level message the frame ever accepts
  if (e.source !== window.parent) return;
  if (e.origin !== STUDIO_ORIGIN) { return; }               // wrong parent origin: drop
  if (bootstrapped) { try { port && port.postMessage({ duplicate: true }); } catch {} return; }
  const m = e.data;
  if (!m || m.type !== 'bootstrap' || !e.ports || e.ports.length !== 1) return;
  bootstrapped = true;
  port = e.ports[0];
  port.start();
  port.onmessage = (ev) => {
    const msg = ev.data;
    if (msg && msg.type === 'init') port.postMessage({ ...base(msg), type: 'rendered', payload: { ok: true } });
  };
  if (MODE === 'no-ready') return;                          // timeout path
  const challenge = MODE === 'replay' ? 'STALECHALLENGESTALECHALLENGE' : m.payload.challenge;
  const sessionId = MODE === 'wrong-session' ? 'sess-impostor' : m.sessionId;
  const ready = { protocolVersion: 1, sessionId, requestId: m.requestId, sourceDigest: m.sourceDigest,
                  clientSequence: '1', type: 'ready',
                  payload: { previewManifestDigest: 'sha256:' + 'b'.repeat(64),
                             timing: { scriptLoadMs: 1, firstPaintMs: 2 }, challenge } };
  if (MODE === 'oversize') ready.payload.pad = 'x'.repeat(${MSG_MAX} + 1024);
  port.postMessage(ready);
  if (MODE === 'stray-window') parent.postMessage({ type: 'ready', hijack: true }, '*');
  if (MODE === 'sequence-regression') { port.postMessage({ ...ready, clientSequence: '0' }); }
});
function base(m) { return { protocolVersion: 1, sessionId: m.sessionId, requestId: m.requestId,
                            sourceDigest: m.sourceDigest, clientSequence: '2' }; }
</script>
<body>preview session shell</body>`;

const NAVIGATED = `<!doctype html><meta charset="utf-8"><title>navigated away</title><body>gone</body>`;

// --------------------------------------------------------------------------- the Studio host page
const HOST = (previewOrigin) => `<!doctype html><meta charset="utf-8"><title>studio host</title>
<body><script>
const PREVIEW_ORIGIN = ${JSON.stringify(previewOrigin)};
const MSG_MAX = ${MSG_MAX}, READY_TIMEOUT_MS = ${READY_TIMEOUT_MS};
const results = [];
const record = (name, pass, detail = '') => results.push({ name, pass: !!pass, detail: String(detail) });
const rand = (n) => { const a = new Uint8Array(n); crypto.getRandomValues(a);
  return btoa(String.fromCharCode(...a)).replace(/\\+/g, '-').replace(/\\//g, '_').replace(/=+$/, ''); };

// One session, implementing the contract's host side exactly.
function openSession({ targetOrigin = '*', mode = 'ok', shellStudioOrigin = location.origin } = {}) {
  const sessionId = 'sess-' + rand(9), requestId = 'preq-' + rand(9);
  const sourceDigest = 'sha256:' + '3'.repeat(64);
  const challenge = rand(24);
  const ch = new MessageChannel();
  const state = { sessionId, requestId, sourceDigest, challenge, port: ch.port1, disposed: false,
                  readyAccepted: false, lastInbound: 0n, outSeq: 0n, events: [], bootstraps: 0 };
  const url = new URL(PREVIEW_ORIGIN + '/s/' + sessionId + '/');
  url.searchParams.set('requestId', requestId);
  url.searchParams.set('sourceDigest', sourceDigest);
  url.searchParams.set('mode', mode);
  url.searchParams.set('studioOrigin', shellStudioOrigin);
  const frame = document.createElement('iframe');
  frame.setAttribute('sandbox', 'allow-scripts');   // opaque origin, kept; allow-same-origin never added
  frame.src = url.toString();
  state.frame = frame;
  ch.port1.onmessage = (ev) => {
    if (state.disposed) { state.events.push('after-dispose'); return; }
    const raw = JSON.stringify(ev.data ?? null);
    if (raw.length > MSG_MAX) { state.events.push('oversize-dropped'); dispose(state); return; }
    const m = ev.data;
    if (!m || m.protocolVersion !== 1) { state.events.push('bad-envelope'); return; }
    if (m.sessionId !== state.sessionId || m.requestId !== state.requestId
        || m.sourceDigest !== state.sourceDigest) { state.events.push('identity-mismatch'); dispose(state); return; }
    if (!['ready', 'rendered', 'error'].includes(m.type)) { state.events.push('wrong-direction'); return; }
    const seq = BigInt(m.clientSequence);
    if (seq <= state.lastInbound) { state.events.push('sequence-regression'); dispose(state); return; }
    state.lastInbound = seq;
    if (m.type === 'ready') {
      if (state.readyAccepted) { state.events.push('second-ready'); dispose(state); return; }
      if (m.payload.challenge !== state.challenge) { state.events.push('challenge-mismatch'); dispose(state); return; }
      state.readyAccepted = true; state.events.push('ready-accepted');
      state.outSeq += 1n;
      state.port.postMessage({ protocolVersion: 1, sessionId, requestId, sourceDigest,
        clientSequence: String(state.outSeq), type: 'init', payload: { viewport: { width: 1440, height: 900 } } });
    } else state.events.push(m.type);
  };
  ch.port1.start();
  frame.addEventListener('load', () => {
    state.bootstraps += 1;
    if (state.bootstraps > 1) { state.events.push('duplicate-bootstrap'); dispose(state); return; }
    if (state.disposed) return;
    frame.contentWindow.postMessage({ protocolVersion: 1, sessionId, requestId, sourceDigest,
      clientSequence: '0', type: 'bootstrap',
      payload: { studioOrigin: location.origin, previewOrigin: PREVIEW_ORIGIN, challenge } },
      targetOrigin, [ch.port2]);
  });
  document.body.appendChild(frame);
  state.timer = setTimeout(() => { if (!state.readyAccepted) { state.events.push('ready-timeout'); dispose(state); } },
                           READY_TIMEOUT_MS);
  return state;
}
function dispose(s) {
  if (s.disposed) return;
  s.disposed = true; clearTimeout(s.timer);
  try { s.port.close(); } catch {}
  s.frame.remove();
  s.events.push('disposed');
}
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

// the host never accepts a window-level message for a session
let strayWindowMessages = 0;
addEventListener('message', () => { strayWindowMessages += 1; });

(async () => {
  // 1. the audited counterexample: a targeted transfer to an opaque-origin frame never arrives
  let s = openSession({ targetOrigin: PREVIEW_ORIGIN });
  await wait(READY_TIMEOUT_MS + 250);
  record('F04 counterexample: targetOrigin=previewOrigin never delivers the port to a sandboxed frame',
         !s.readyAccepted && s.events.includes('ready-timeout'), s.events.join(','));

  // 2. the fix
  s = openSession({});
  await wait(500);
  record('F04 one wildcard bootstrap to the exact contentWindow completes the handshake',
         s.readyAccepted && s.events.includes('rendered'), s.events.join(','));
  const okSession = s;

  // 3. the frame refuses a parent whose origin is not its preconfigured Studio origin
  s = openSession({ shellStudioOrigin: 'https://not-studio.invalid' });
  await wait(READY_TIMEOUT_MS + 250);
  record('F04 the frame refuses a bootstrap from an unexpected parent origin',
         !s.readyAccepted && s.events.includes('ready-timeout'), s.events.join(','));

  // 4. replay: a ready that does not echo this bootstrap's challenge
  s = openSession({ mode: 'replay' });
  await wait(400);
  record('F04 a ready that does not echo the challenge is rejected and the session disposed',
         !s.readyAccepted && s.events.includes('challenge-mismatch') && s.disposed, s.events.join(','));

  // 5. wrong identity
  s = openSession({ mode: 'wrong-session' });
  await wait(400);
  record('F04 a ready bearing another sessionId is rejected and the session disposed',
         !s.readyAccepted && s.events.includes('identity-mismatch') && s.disposed, s.events.join(','));

  // 6. size limit, enforced before the payload is used
  s = openSession({ mode: 'oversize' });
  await wait(400);
  record('F04 a message above the byte bound is dropped before parsing and disposes the session',
         !s.readyAccepted && s.events.includes('oversize-dropped') && s.disposed, s.events.join(','));

  // 7. sequence regression
  s = openSession({ mode: 'sequence-regression' });
  await wait(400);
  record('F04 a sequence regression is rejected and the session disposed',
         s.events.includes('sequence-regression') && s.disposed, s.events.join(','));

  // 8. timeout with no ready at all
  s = openSession({ mode: 'no-ready' });
  await wait(READY_TIMEOUT_MS + 250);
  record('F04 a frame that never replies times out and is disposed',
         !s.readyAccepted && s.events.includes('ready-timeout') && s.disposed, s.events.join(','));

  // 9. navigation invalidates the established session
  s = openSession({});
  await wait(400);
  const beforeNav = s.readyAccepted;
  s.frame.src = PREVIEW_ORIGIN + '/navigated';
  await wait(400);
  record('F04 navigation of an established frame invalidates the session',
         beforeNav && s.events.includes('duplicate-bootstrap') && s.disposed, s.events.join(','));

  // 10. disposal: nothing is accepted afterwards
  const before = okSession.events.length;
  dispose(okSession);
  okSession.port.postMessage({ protocolVersion: 1, type: 'ready' });
  await wait(200);
  record('F04 after disposal the port is closed and no further message is accepted',
         okSession.disposed && okSession.events.length === before + 1
         && okSession.events[before] === 'disposed', okSession.events.slice(before).join(','));

  // 11. replacement: a fresh session does not reuse the old channel
  const a = openSession({}); await wait(400);
  const b = openSession({}); await wait(400);
  record('F04 a replacement session uses a new sessionId, challenge and channel',
         a.sessionId !== b.sessionId && a.challenge !== b.challenge && b.readyAccepted,
         a.sessionId + ' / ' + b.sessionId);
  dispose(a); dispose(b);

  // 12. the host ignores window-level traffic entirely
  s = openSession({ mode: 'stray-window' });
  await wait(400);
  record('F04 a stray window.postMessage from the frame reaches no session state',
         s.readyAccepted && strayWindowMessages > 0 && !s.events.includes('hijack'),
         'window messages seen: ' + strayWindowMessages);
  dispose(s);

  window.__results = results;
  window.__done = true;
})();
</script></body>`;

// --------------------------------------------------------------------------- servers
function serve(handler) {
  return new Promise((res) => {
    const s = http.createServer(handler);
    s.listen(0, '127.0.0.1', () => res(s));
  });
}

const results = [];
let chrome, profile;

async function main() {
  const previewSrv = await serve((req, rsp) => {
    const u = new URL(req.url, 'http://x');
    if (u.pathname === '/navigated') {
      rsp.writeHead(200, { 'content-type': 'text/html' }); return rsp.end(NAVIGATED);
    }
    const nonce = Buffer.from(String(Math.random())).toString('base64url').slice(0, 22);
    const body = SHELL(u.searchParams.get('studioOrigin') || '', u.searchParams.get('mode') || 'ok')
      .replaceAll('NONCEVALUE', nonce);
    rsp.writeHead(200, {
      'content-type': 'text/html',
      'content-security-policy': `default-src 'none'; script-src 'nonce-${nonce}'; style-src 'nonce-${nonce}'; ` +
        `connect-src 'none'; base-uri 'none'; form-action 'none'; object-src 'none'`,
    });
    rsp.end(body);
  });
  const previewOrigin = `http://127.0.0.1:${previewSrv.address().port}`;
  const studioSrv = await serve((req, rsp) => {
    rsp.writeHead(200, { 'content-type': 'text/html' });
    rsp.end(HOST(previewOrigin));
  });
  const studioOrigin = `http://127.0.0.1:${studioSrv.address().port}`;

  profile = mkdtempSync(join(tmpdir(), 'anvilkit-browser-proof-'));
  const port = 9333 + (process.pid % 500);
  chrome = spawn(CHROME, ['--headless=new', `--remote-debugging-port=${port}`, '--remote-debugging-address=127.0.0.1',
    `--user-data-dir=${profile}`, '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
    '--no-first-run', '--disable-extensions', '--disable-background-networking', 'about:blank'],
    { stdio: 'ignore' });

  let version = null;
  for (let i = 0; i < 120; i++) {
    try { version = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json(); break; }
    catch { await new Promise((r) => setTimeout(r, 250)); }
  }
  if (!version) { console.error('UNEXECUTED: Chrome never exposed a DevTools endpoint'); process.exit(2); }
  console.log(`environment: ${version.Browser}; ${process.version}`);
  console.log(`origins: studio ${studioOrigin}, preview ${previewOrigin} (synthetic, loopback)\n`);

  const ws = new WebSocket(version.webSocketDebuggerUrl);
  await new Promise((r, j) => { ws.onopen = r; ws.onerror = j; });
  let id = 0; const pending = new Map();
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
  };
  const send = (method, params = {}, sessionId) => new Promise((res) => {
    const mid = ++id; pending.set(mid, res);
    ws.send(JSON.stringify({ id: mid, method, params, ...(sessionId ? { sessionId } : {}) }));
  });

  const { result: target } = await send('Target.createTarget', { url: studioOrigin + '/' });
  const { result: attached } = await send('Target.attachToTarget', { targetId: target.targetId, flatten: true });
  const sid = attached.sessionId;
  const out = await send('Runtime.evaluate', {
    expression: `new Promise((res) => { const t0 = Date.now(); const i = setInterval(() => {
        if (window.__done) { clearInterval(i); res(JSON.stringify(window.__results)); }
        else if (Date.now() - t0 > 60000) { clearInterval(i); res('TIMEOUT'); } }, 100); })`,
    awaitPromise: true, returnByValue: true,
  }, sid);
  const value = out.result?.result?.value;
  if (!value || value === 'TIMEOUT') { console.error('UNEXECUTED: the page never finished'); cleanup(); process.exit(2); }
  for (const r of JSON.parse(value)) {
    results.push(r);
    console.log(`${r.pass ? 'PASS' : 'FAIL'}  ${r.name}${r.pass ? '' : '    [' + r.detail + ']'}`);
  }
  ws.close(); studioSrv.close(); previewSrv.close();
}

function cleanup() {
  try { chrome && chrome.kill(); } catch {}
  try { profile && rmSync(profile, { recursive: true, force: true }); } catch {}
}

main().then(() => {
  cleanup();
  const failed = results.filter((r) => !r.pass);
  console.log(`\n${results.length} checks, ${failed.length} failures`);
  console.log('\nScope: real-engine proof of the bootstrap protocol on synthetic loopback origins. '
    + 'Chrome runs with --no-sandbox because this proof runs as root; that is a property of this harness, '
    + 'not of the preview runtime. NOT Studio or preview qualification.');
  process.exit(failed.length ? 1 : 0);
}).catch((e) => { cleanup(); console.error('UNEXECUTED:', e.message); process.exit(2); });
