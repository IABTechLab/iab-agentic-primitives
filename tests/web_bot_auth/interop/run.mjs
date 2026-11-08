#!/usr/bin/env node
/**
 * Grades a Web Bot Auth implementation against the judge (judge.mjs), through
 * the adapter protocol in src/iab_agentic_primitives/web_bot_auth/conformance/PROTOCOL.md.
 *
 *   node run.mjs --adapter "<command>" [--persistent] [--vectors <dir>] [--json <file>]
 *   node run.mjs --self [--json <file>]
 *
 * Every check records three verdicts on the same message: the adapter's, the
 * judge's (Cloudflare's library with the profile's rules), and the bare
 * library's (Cloudflare's library as shipped). A check passes when:
 *
 * - the adapter and the judge both give the expected outcome: a pass, or a
 *   refusal with the expected status and rule (refusals.json), and
 * - for tier "wba", the bare library gives it too; for tier "profile", the
 *   bare library gives the opposite outcome, so only the profile's rules
 *   decide it.
 *
 * Suites:
 *
 * - vectors: the committed vectors, at their reference_now.
 * - live: the adapter signs and the judge verifies; the judge signs and the
 *   adapter verifies; tampered copies fail in both. Each target URI in
 *   scenarios.json is a GET each side signs and the other verifies.
 * - scenarios: the judge signs every scenario in scenarios.json, and the
 *   adapter verifies it.
 *
 * A vector's kind says which operation grades it; nothing infers it. The exit
 * code is 1 when any check fails.
 */
import { spawn, spawnSync } from 'node:child_process';
import { readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';
import { fromWire, toWire } from './docs.mjs';
import * as judge from './judge.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const { values: args } = parseArgs({
  options: {
    adapter: { type: 'string' },
    persistent: { type: 'boolean', default: false },
    self: { type: 'boolean', default: false },
    vectors: { type: 'string', default: join(HERE, '../../../src/iab_agentic_primitives/web_bot_auth/vectors') },
    json: { type: 'string' },
  },
});
if (!args.self && !args.adapter) {
  console.error('usage: run.mjs --adapter "<command>" [--persistent] | --self [--vectors <dir>] [--json <file>]');
  process.exit(2);
}
const command = args.self ? `"${process.execPath}" "${join(HERE, 'judge.mjs')}"` : args.adapter;

// ── Adapter ───────────────────────────────────────────────────────────────

let persistent;
if (args.persistent && !args.self) {
  // One process for every operation: one JSON object per line each way.
  const child = spawn(command, { shell: true, stdio: ['pipe', 'pipe', 'inherit'] });
  const lines = createInterface({ input: child.stdout });
  const waiting = [];
  lines.on('line', line => waiting.shift()?.(line));
  persistent = {
    call: op =>
      new Promise(resolve => {
        waiting.push(resolve);
        child.stdin.write(`${JSON.stringify(toWire(op))}\n`);
      }),
    close: () => child.stdin.end(),
  };
}

async function adapter(op) {
  if (args.self) {
    const fn = { sign_request: judge.signRequest, sign_directory: judge.signDirectory, sign_response: judge.signResponse,
      verify_request: judge.verifyRequest, verify_directory: judge.verifyDirectory, verify_response: judge.verifyResponse }[op.op];
    try {
      return await fn(op);
    } catch (e) {
      return { ok: false, status: null, rule: 'adapter-error', error: String(e) };
    }
  }
  let out;
  if (persistent) {
    out = await persistent.call(op);
  } else {
    const r = spawnSync(command, { shell: true, input: JSON.stringify(toWire(op)), encoding: 'utf8', maxBuffer: 1 << 24 });
    out = r.stdout || r.stderr || String(r.error);
  }
  try {
    return fromWire(JSON.parse(out));
  } catch {
    return { ok: false, status: null, rule: 'adapter-crash', error: String(out).slice(0, 600) };
  }
}

// ── Data ──────────────────────────────────────────────────────────────────

const read = path => fromWire(JSON.parse(readFileSync(join(args.vectors, path), 'utf8')));
const KEYS = read('keys.json').keys;
const PRIVATE = Object.fromEntries(KEYS.map(k => [k.kid, { kty: k.kty, crv: k.crv, x: k.x, d: k._private_d_for_test_only }]));
const SCENARIOS = read('scenarios.json');
// The refusal vocabulary, generated from the Python Refusal enum. Every refusal either side gives is named in it.
const RULES = new Map(read('refusals.json').refusals.map(r => [r.rule, r.status]));
const SIGNER = SCENARIOS.signer;
// What every directory scenario signs unless it moves it, as scenarios.json states it.
const DIRECTORY_DEFAULTS = { parameters: SCENARIOS.directory_parameters, max_age: SCENARIOS.directory_max_age };
const RESPONDER = SCENARIOS.responder;
const vectors = dir =>
  readdirSync(join(args.vectors, dir))
    .filter(f => f.endsWith('.json'))
    .sort()
    .map(f => ({ id: `${dir}/${f.replace(/\.json$/, '')}`, ...read(join(dir, f)) }));

// ── Grading ───────────────────────────────────────────────────────────────

const checks = [];
const describe = r => (r.ok ? `pass${r.identity ? ` as ${r.identity}` : ''}` : `${r.status ?? 'no status'} ${r.rule ?? ''}: ${r.error ?? ''}`.trim());
const want = e => (e.ok ? `pass${e.identity ? ` as ${e.identity}` : ''}` : `${e.status} ${e.rule}`);
/** A pass with the expected identity, or a refusal with the expected status and a rule of the vocabulary. */
const meets = (expected, got) =>
  expected.ok
    ? got.ok === true && (expected.identity == null || got.identity === expected.identity)
    : got.ok === false && got.status === expected.status && got.rule === expected.rule && RULES.get(got.rule) === got.status;
const verdict = r => (r === undefined ? undefined : r.ok ? 'pass' : `${r.status ?? 'no status'} ${r.rule}`);
const refused = rule => ({ ok: false, status: RULES.get(rule), rule });

/**
 * Records one check. `python` is the adapter's result, `cloudflare` the
 * judge's, and `bare` the bare library's (undefined when the check has no
 * bare verdict, such as an adapter-only replay).
 */
function record(suite, id, expected, { python, cloudflare, bare, tier }) {
  const bareOk = bare === undefined || tier === undefined ? true : tier === 'wba' ? bare.ok === expected.ok : bare.ok !== expected.ok;
  const ok = (python === undefined || meets(expected, python)) && (cloudflare === undefined || meets(expected, cloudflare)) && bareOk;
  checks.push({
    suite,
    id,
    tier,
    ok,
    expected: want(expected),
    python: verdict(python),
    cloudflare: verdict(cloudflare),
    cloudflare_bare: verdict(bare),
    detail: { python: python && describe(python), cloudflare: cloudflare && describe(cloudflare), bare: bare && describe(bare) },
  });
}

async function signedDirectories(map, now) {
  const out = {};
  for (const [origin, kids] of Object.entries(map)) {
    out[origin] = await judge.signDirectory({ keys: kids.map(k => PRIVATE[k]), origin, now });
  }
  return out;
}

async function verifyBoth(kind, op) {
  const fn = { request: judge.verifyRequest, directory: judge.verifyDirectory, response: judge.verifyResponse }[kind];
  return { cloudflare: await fn(op), bare: await fn(op, { bare: true }) };
}

// ── Vectors ───────────────────────────────────────────────────────────────

async function runVectors() {
  // Each vector carries the operation that grades it, ready to send as it is.
  for (const v of [...vectors('positive'), ...vectors('negative')]) {
    const kind = { verify_request: 'request', verify_response: 'response', verify_directory: 'directory' }[v.operation.op];
    if (kind === undefined) throw new Error(`${v.id} has no verify operation`);
    const { op: _, ...op } = v.operation;
    record('vectors', v.id, v.expected, { python: await adapter(v.operation), ...(await verifyBoth(kind, op)), tier: v.tier });
  }
}

// ── Live ──────────────────────────────────────────────────────────────────

const tamperUrl = r => ({ ...r, url: `${r.url}${r.url.includes('?') ? '&' : '?'}t=1` });
const tamperBody = r => ({ ...r, body: `${r.body} ` });

async function runLive(now) {
  const key = PRIVATE[SIGNER.kid];
  const origin = SIGNER.identity;
  const authority = SCENARIOS.verifier;
  const ok = { ok: true, identity: origin };
  const tampered = refused('signature-invalid');
  const bodyChanged = refused('content-digest');
  const verifyOp = (request, directories, extra = {}) => ({ request, authority, directories, now, ...extra });

  const theirs = await adapter({ op: 'sign_directory', keys: [key], origin, now });
  record('live', 'Python signs the directory, Cloudflare verifies', { ok: true }, { ...(await verifyBoth('directory', { origin, response: theirs, now })), tier: 'wba' });
  const tamperedDirectory = { ...theirs, body: `${theirs.body}\n` };
  record('live', 'Python signs the directory, body changed, both refuse', refused('directory-content-digest'), {
    python: await adapter({ op: 'verify_directory', origin, response: tamperedDirectory, now }),
    ...(await verifyBoth('directory', { origin, response: tamperedDirectory, now })),
    tier: 'profile',
  });
  const ours = await judge.signDirectory({ keys: [key], origin, now });
  record('live', 'Cloudflare signs the directory, Python verifies', { ok: true }, { python: await adapter({ op: 'verify_directory', origin, response: ours, now }) });

  for (const [name, request] of Object.entries(SCENARIOS.requests)) {
    const N = name.toUpperCase();
    const signed = await adapter({ op: 'sign_request', key, origin, request, now });
    const dirs = { [origin]: theirs };
    record('live', `Python signs ${N}, Cloudflare verifies`, ok, { ...(await verifyBoth('request', verifyOp(signed, dirs))), tier: 'wba' });
    const urlChanged = tamperUrl(signed);
    record('live', `Python signs ${N}, URL changed, both refuse`, tampered, {
      python: await adapter({ op: 'verify_request', ...verifyOp(urlChanged, dirs) }),
      ...(await verifyBoth('request', verifyOp(urlChanged, dirs))),
      tier: 'wba',
    });
    if (request.body) {
      const changed = tamperBody(signed);
      record('live', `Python signs ${N}, body changed, both refuse`, bodyChanged, {
        python: await adapter({ op: 'verify_request', ...verifyOp(changed, dirs) }),
        ...(await verifyBoth('request', verifyOp(changed, dirs))),
        tier: 'profile',
      });
    }

    const mine = await judge.signRequest({ key, origin, request, now, form: 'url' });
    const mineDirs = { [origin]: ours };
    record('live', `Cloudflare signs ${N} with the key URL, Python verifies`, ok, { python: await adapter({ op: 'verify_request', ...verifyOp(mine, mineDirs) }), ...(await verifyBoth('request', verifyOp(mine, mineDirs))), tier: 'wba' });
    const mineOrigin = await judge.signRequest({ key, origin, request, now, form: 'origin' });
    record('live', `Cloudflare signs ${N} with the bare origin, Python verifies`, ok, { python: await adapter({ op: 'verify_request', ...verifyOp(mineOrigin, mineDirs) }), ...(await verifyBoth('request', verifyOp(mineOrigin, mineDirs))), tier: 'wba' });
    record('live', `Cloudflare signs ${N}, Python verifies it twice`, refused('nonce-replayed'), {
      python: await adapter({ op: 'verify_request', ...verifyOp(mine, mineDirs, { prior: [mine] }) }),
      cloudflare: await judge.verifyRequest(verifyOp(mine, mineDirs, { prior: [mine] })),
    });
    const mineUrl = tamperUrl(mine);
    record('live', `Cloudflare signs ${N}, URL changed, both refuse`, tampered, {
      python: await adapter({ op: 'verify_request', ...verifyOp(mineUrl, mineDirs) }),
      ...(await verifyBoth('request', verifyOp(mineUrl, mineDirs))),
      tier: 'wba',
    });
    if (request.body) {
      const mineBody = tamperBody(mine);
      record('live', `Cloudflare signs ${N}, body changed, both refuse`, bodyChanged, {
        python: await adapter({ op: 'verify_request', ...verifyOp(mineBody, mineDirs) }),
        ...(await verifyBoth('request', verifyOp(mineBody, mineDirs))),
        tier: 'profile',
      });
    }
  }

  // Target URIs: each side signs a GET to the URL as written, and the other verifies it.
  for (const c of SCENARIOS.target_uris) {
    const request = { method: 'GET', url: c.url, headers: {}, body: '' };
    const id = `${c.id} ${c.name}`;
    const signed = await adapter({ op: 'sign_request', key, origin, request, now });
    const whatwg = judge.onTheWire(c.url);
    const sameUrl = whatwg === c.wire ? { ok: true } : { ok: false, status: null, rule: 'target-uri', error: `${whatwg}, not ${c.wire}` };
    record('live', `${id}: its wire URL is the WHATWG serialization`, { ok: true }, { cloudflare: sameUrl });
    record('live', `Python signs ${id}, Cloudflare verifies`, ok, { ...(await verifyBoth('request', verifyOp(signed, { [origin]: theirs }))), tier: 'wba' });
    const mine = await judge.signRequest({ key, origin, request, now });
    const mineOp = verifyOp(mine, { [origin]: ours });
    record('live', `Cloudflare signs ${id}, Python verifies`, ok, { python: await adapter({ op: 'verify_request', ...mineOp }), ...(await verifyBoth('request', mineOp)), tier: 'wba' });
  }

  // Responses: the seller's key signs, bound to a request the buyer signed.
  const sellerKey = PRIVATE[RESPONDER.kid];
  const sellerOk = { ok: true, identity: RESPONDER.identity };
  const sellerDirs = await signedDirectories({ [RESPONDER.identity]: [RESPONDER.kid] }, now);
  const request = await judge.signRequest({ key, origin, request: SCENARIOS.requests.post, now });
  const response = await adapter({ op: 'sign_response', key: sellerKey, origin: RESPONDER.identity, request, response: SCENARIOS.response, now });
  const op = { request, response, directories: sellerDirs, now, expected_origin: RESPONDER.identity };
  record('live', 'Python signs a response, Cloudflare verifies it and its binding', sellerOk, { ...(await verifyBoth('response', op)), tier: 'wba' });
  const otherRequest = await judge.signRequest({ key, origin, request: SCENARIOS.requests.post, now });
  record('live', 'Python signs a response, presented with another request, both refuse', tampered, {
    python: await adapter({ op: 'verify_response', ...op, request: otherRequest }),
    ...(await verifyBoth('response', { ...op, request: otherRequest })),
    tier: 'wba',
  });
  const mineResponse = await judge.signResponse({ key: sellerKey, origin: RESPONDER.identity, request, response: SCENARIOS.response, now });
  const mineOp = { request, response: mineResponse, directories: sellerDirs, now, expected_origin: RESPONDER.identity };
  record('live', 'Cloudflare signs a response, Python verifies it and its binding', sellerOk, { python: await adapter({ op: 'verify_response', ...mineOp }), cloudflare: await judge.verifyResponse(mineOp) });
  const changed = { ...mineResponse, status: 201 };
  record('live', 'Cloudflare signs a response, status changed, both refuse', tampered, {
    python: await adapter({ op: 'verify_response', ...mineOp, response: changed }),
    ...(await verifyBoth('response', { ...mineOp, response: changed })),
    tier: 'wba',
  });
}

// ── Scenarios ─────────────────────────────────────────────────────────────

async function runScenarios(now) {
  const defaultDirectories = { [SIGNER.identity]: [SIGNER.kid] };
  for (const s of SCENARIOS.request_scenarios) {
    const request = await judge.signScenario(SCENARIOS, s, PRIVATE, now);
    const op = { request, authority: s.authority ?? SCENARIOS.verifier, directories: await signedDirectories(s.directories ?? defaultDirectories, now), now };
    record('scenarios', `${s.id} ${s.name}`, s.expected, { python: await adapter({ op: 'verify_request', ...op }), ...(await verifyBoth('request', op)), tier: s.tier });
  }
  for (const s of SCENARIOS.directory_scenarios) {
    const { id, name, expected, tier, basis, keys = [SIGNER.kid], ...options } = s;
    const response = await judge.signDirectory({ ...options, keys: keys.map(k => PRIVATE[k]), origin: SIGNER.identity, now, defaults: DIRECTORY_DEFAULTS });
    const op = { origin: SIGNER.identity, response, now };
    record('scenarios', `${id} ${name}`, expected, { python: await adapter({ op: 'verify_directory', ...op }), ...(await verifyBoth('directory', op)), tier });
  }
  const sellerDirs = await signedDirectories({ [RESPONDER.identity]: [RESPONDER.kid] }, now);
  for (const s of SCENARIOS.response_scenarios) {
    const pair = await judge.signResponseScenario(SCENARIOS, s, PRIVATE, now);
    const op = { ...pair, directories: sellerDirs, now };
    record('scenarios', `${s.id} ${s.name}`, s.expected, { python: await adapter({ op: 'verify_response', ...op }), ...(await verifyBoth('response', op)), tier: s.tier });
  }
}

// ── Report ────────────────────────────────────────────────────────────────

const now = Math.floor(Date.now() / 1000);
await runVectors();
await runLive(now);
await runScenarios(now);
persistent?.close();

console.log(`Web Bot Auth interop: ${args.self ? 'judge self-check' : `adapter ${command}`}\n`);
console.log('ok    suite     tier     python  cloudflare  bare    check');
for (const c of checks) {
  console.log(`${c.ok ? 'pass' : 'FAIL'}  ${c.suite.padEnd(9)} ${(c.tier ?? '-').padEnd(8)} ${String(c.python ?? '-').padEnd(7)} ${String(c.cloudflare ?? '-').padEnd(11)} ${String(c.cloudflare_bare ?? '-').padEnd(7)} ${c.id}`);
  if (!c.ok) console.log(`        expected ${c.expected}\n        python   ${c.detail.python}\n        judge    ${c.detail.cloudflare}\n        bare     ${c.detail.bare}`);
}
const failed = checks.filter(c => !c.ok);
console.log(`\n${checks.length - failed.length} of ${checks.length} checks pass.`);
if (args.json) writeFileSync(args.json, `${JSON.stringify({ now, checks }, null, 2)}\n`);
process.exitCode = failed.length === 0 ? 0 : 1;
