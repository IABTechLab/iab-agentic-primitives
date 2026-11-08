/**
 * The judge: Cloudflare's `web-bot-auth` and `http-message-sig` at the versions
 * pinned in package.json, run two ways.
 *
 * - Bare: the library as shipped, with only the resolver every caller writes:
 *   derive the origin of the covered Signature-Agent member, read that origin's
 *   well-known directory, and return the key whose thumbprint is `keyid`.
 * - Policy: the library with the profile's rules from policy.mjs in its hooks.
 *
 * Run directly, this file is an adapter (see conformance/PROTOCOL.md) that signs with the
 * library and verifies with the policy, so `run.mjs --self` grades the judge
 * through the same protocol as the Python implementation.
 */
import { createHash, randomBytes } from 'node:crypto';
import { pathToFileURL } from 'node:url';
import { component, createSignature, verifySignature } from 'http-message-sig';
import { parseDictionary } from 'structured-headers';
import { fromWire, toWire } from './docs.mjs';
import { generateNonce, sign, verify } from 'web-bot-auth';
import { signerFromJWK, verifierFromJWK } from 'web-bot-auth/crypto';
import * as policy from './policy.mjs';

const { header } = policy;
const fields = headers => Object.entries(headers).map(([name, value]) => ({ name: name.toLowerCase(), value }));
/**
 * The URL as it travels on the wire: what a native Request's `url` holds, the
 * WHATWG serialization, without the fragment that never goes on the wire. A
 * Worker signing `new Request(url)` signs this as `@target-uri`, and a Worker
 * verifying reads it from the request it received. A malformed URL is passed
 * as written, for http-message-sig to refuse.
 */
export const onTheWire = url => {
  try {
    const parsed = new URL(url);
    parsed.hash = '';
    return parsed.href;
  } catch {
    return url;
  }
};
const message = r => ({ kind: 'request', method: r.method, targetUri: onTheWire(r.url), fields: fields(r.headers) });
const responseMessage = (response, request) => ({ kind: 'response', status: response.status, fields: fields(response.headers), request: message(request) });
const digest = body => `sha-256=:${createHash('sha256').update(body).digest('base64')}:`;
const publicJwk = ({ d, p, q, dp, dq, qi, ...pub }) => pub;
const failure = (error, kind) => ({
  ok: false,
  status: policy.statusOf(error),
  rule: policy.ruleOf(error, kind),
  error: `${error?.message ?? error}${error?.cause ? `: ${error.cause.message ?? error.cause}` : ''}`,
});
const verifierOrUndefined = jwk => verifierFromJWK(jwk).catch(() => undefined);

const directoryMessage = (origin, response) => ({
  kind: 'response',
  status: response.status,
  fields: fields(response.headers),
  request: { kind: 'request', method: 'GET', targetUri: policy.directoryUrl(origin), fields: [] },
});

// ── Directories ───────────────────────────────────────────────────────────

/** Verifies a signed directory response; returns { ok, keyids } or a refusal. */
export async function verifyDirectory({ origin, response, now }, { bare = false } = {}) {
  try {
    const keys = JSON.parse(response.body).keys ?? [];
    const signed = directoryMessage(origin, response);
    const keyids = [];
    if (bare) {
      // Every signature in the response verifies with the key the body gives for its keyid.
      const input = parseDictionary(header(response.headers, 'signature-input') ?? '');
      const verifiers = (await Promise.all(keys.map(verifierOrUndefined))).filter(Boolean);
      for (const [label, [, params]] of input) {
        const verifier = verifiers.find(v => v.keyid === params.get('keyid'));
        if (verifier === undefined) throw new Error(`no key ${params.get('keyid')} in the directory`);
        await verifySignature(signed, {
          label,
          policy: { algorithms: ['ed25519', 'rsa-pss-sha512'], now, requiredComponents: [], requiredParameters: [] },
          resolveVerifier: async () => verifier,
        });
        keyids.push(verifier.keyid);
      }
      if (keyids.length === 0) throw new Error('the directory response carries no signature');
      return { ok: true, keyids };
    }
    policy.checkDirectoryResponse(response);
    const input = parseDictionary(header(response.headers, 'signature-input') ?? '');
    for (const verifier of await Promise.all(keys.map(verifierOrUndefined))) {
      if (!policy.usable(verifier)) continue;
      await verifySignature(signed, {
        label: policy.directoryLabelFor(input, verifier.keyid),
        policy: policy.directorySignaturePolicy(now),
        resolveVerifier: async () => verifier,
      });
      keyids.push(verifier.keyid);
    }
    policy.checkDirectoryBody(response, keyids);
    return { ok: true, keyids: keyids.sort() };
  } catch (e) {
    return failure(e, 'directory');
  }
}

async function resolveKey(directories, entry, keyid, now, bare, headers) {
  if (!bare) policy.checkSignatureAgent(entry, headers);
  // The verifier derives the origin's well-known directory from the member value (WBA section 5.5).
  const origin = new URL(entry.uri).origin;
  const response = directories[origin];
  if (response === undefined) throw new policy.PolicyError(403, 'directory-unavailable', `no directory at ${origin}`);
  if (!bare) {
    const checked = await verifyDirectory({ origin, response, now });
    if (!checked.ok) throw new policy.PolicyError(403, checked.rule, `directory of ${entry.uri}: ${checked.error}`);
    policy.checkNotTestKey(keyid);
  }
  for (const jwk of JSON.parse(response.body).keys ?? []) {
    const verifier = await verifierOrUndefined(jwk);
    if (verifier?.keyid === keyid && (bare || policy.usable(verifier))) return verifier;
  }
  throw new policy.PolicyError(403, 'key-not-found', `key ${keyid} is not in the directory of ${entry.uri}`);
}

// ── Requests ──────────────────────────────────────────────────────────────

async function verifyOne(request, directories, now, bare, replay, authority) {
  if (!bare) policy.checkAuthority(request, authority);
  const label = bare ? undefined : policy.selectLabel(request);
  const result = await verify(message(request), {
    now: new Date(now * 1000),
    label,
    algorithms: bare ? undefined : policy.ALGORITHMS,
    clockSkew: bare ? undefined : policy.CLOCK_SKEW,
    resolver: async candidate => {
      if (candidate.signatureAgent === undefined) throw new policy.PolicyError(403, 'signature-agent-not-covered', 'no covered Signature-Agent member');
      return resolveKey(directories, candidate.signatureAgent, candidate.keyid, now, bare, request.headers);
    },
    validate: bare ? undefined : r => policy.validate(r, request),
  });
  // Both value forms name the origin; the bare resolver derives it the same way.
  const identity = new URL(result.signatureAgent.uri).origin;
  if (!bare) replay.check(identity, result.nonce);
  return { ok: true, identity, keyid: result.keyid };
}

/**
 * Verifies `request` after verifying each of `prior` with the same replay
 * store, so a replay is built black-box. Returns { ok, identity, keyid } or a
 * refusal with its status.
 */
export async function verifyRequest({ request, authority, directories, now, prior = [] }, { bare = false } = {}) {
  const replay = new policy.ReplayStore();
  for (const [i, earlier] of prior.entries()) {
    try {
      await verifyOne(earlier, directories, now, bare, replay, authority);
    } catch (e) {
      return { ...failure(e, 'request'), rule: `prior-${i}` };
    }
  }
  try {
    return await verifyOne(request, directories, now, bare, replay, authority);
  } catch (e) {
    return failure(e, 'request');
  }
}

// ── Responses ─────────────────────────────────────────────────────────────

/** The Signature-Agent member a response signature covers, as the bare resolver reads it. */
function coveredAgent(response, candidate) {
  const covered = candidate.components.find(c => c.name === 'signature-agent');
  const agents = parseDictionary(header(response.headers, 'signature-agent') ?? '');
  const member = agents.get(covered?.parameters?.key ?? [...agents.keys()][0]);
  if (member === undefined) throw new Error('no covered Signature-Agent member');
  const type = member[1].get('type');
  return { label: covered?.parameters?.key, uri: member[0], type: type === undefined ? 'directory' : String(type) };
}

/**
 * Verifies a response to `request` with http-message-sig: web-bot-auth 0.2.0
 * has no response verifier. Bare: any one signature, no required components.
 * Policy: the profile's response rules, including the request binding.
 */
export async function verifyResponse({ request, response, directories, now, expected_origin }, { bare = false } = {}) {
  try {
    const label = bare ? undefined : policy.selectLabel(response);
    let identity;
    let keyid;
    const verified = await verifySignature(responseMessage(response, request), {
      label,
      policy: bare
        ? { algorithms: ['ed25519', 'rsa-pss-sha512'], now, requiredComponents: [], requiredParameters: [] }
        : policy.responsePolicy(response, request, label, now),
      resolveVerifier: async candidate => {
        const entry = coveredAgent(response, candidate);
        identity = new URL(entry.uri).origin;
        keyid = candidate.parameters.keyid;
        return resolveKey(directories, entry, keyid, now, bare, response.headers);
      },
    });
    if (!bare && expected_origin !== undefined && identity !== expected_origin) {
      throw new policy.PolicyError(403, 'origin-mismatch', `the response is from ${identity}, not ${expected_origin}`);
    }
    return { ok: true, identity, keyid, bound: verified.components.some(c => c.name === 'signature' && c.parameters.req === true) };
  } catch (e) {
    return failure(e, 'response');
  }
}

// ── Signing ───────────────────────────────────────────────────────────────

/** Signs a request with the library's own `sign()`, with the options the profile needs. */
export async function signRequest({ key, origin, request, now, label = 'sig1', form = 'url' }) {
  // form 'url' sends the key directory URL (draft -01); form 'origin' sends the bare origin (draft -00).
  const value = form === 'origin' ? origin : `${origin}${policy.WELL_KNOWN}`;
  const headers = { ...request.headers, 'signature-agent': `${label}="${value}"` };
  const additionalComponents = ['@method', '@target-uri'];
  if (request.body) {
    headers['content-digest'] = digest(request.body);
    additionalComponents.push('content-type', 'content-digest');
  }
  const signature = await sign(message({ ...request, headers }), {
    signer: await signerFromJWK(key),
    created: new Date(now * 1000),
    expires: new Date((now + policy.MAX_LIFETIME) * 1000),
    nonce: generateNonce(),
    label,
    additionalComponents,
  });
  return { ...request, headers: { ...headers, 'signature-input': signature.signatureInput, signature: signature.signature } };
}

/** Signs a response to `request` with http-message-sig, under the profile's response rules. */
export async function signResponse({ key, origin, request, response, now, label = 'sig1', components, parameters = {}, after_signing = {} }) {
  const headers = { ...response.headers, 'signature-agent': `${label}="${origin}${policy.WELL_KNOWN}"` };
  if (response.body) headers['content-digest'] = digest(response.body);
  const signer = await signerFromJWK(key);
  const ids = components
    ? components.map(c => parseComponent(c.replace('{label}', label).replace('{request_label}', policy.requestLabel(request) ?? 'sig1')))
    : policy.responseRequiredComponents(response, request, label);
  const out = await createSignature(responseMessage({ ...response, headers }, request), {
    label,
    components: ids,
    parameters: {
      created: now + (parameters.created ?? 0),
      expires: now + (parameters.expires ?? policy.MAX_LIFETIME),
      keyid: signer.keyid,
      alg: signer.algorithm,
      tag: parameters.tag ?? 'web-bot-auth',
    },
    signer,
  });
  const signed = { ...response, headers: { ...headers, 'signature-input': out.signatureInput, signature: out.signature } };
  return { ...signed, status: after_signing.status ?? signed.status, body: signed.body + (after_signing.append_to_body ?? '') };
}

/**
 * Signs a key directory response per WBA Appendix B.1. The options build the
 * directory scenarios in scenarios.json: `authority` signs for another host,
 * `parameters` moves `created` and `expires` (offsets from `now`),
 * `components` replaces the covered components, `unsigned_keys` adds public
 * keys that do not sign, `content_type` changes the media type, and
 * `after_signing.append_to_body` changes the body after signing.
 */
/** The profile's own directory signing: created now, a day's lifetime, and max-age 300. */
export const PROFILE_DIRECTORY = { parameters: { created: 0, expires: 86400 }, max_age: 300 };

export async function signDirectory({ keys, origin, now, authority, parameters = {}, components, unsigned_keys = [], content_type, after_signing = {}, defaults = PROFILE_DIRECTORY }) {
  const published = [];
  for (const key of keys) published.push({ ...publicJwk(key), kid: (await signerFromJWK(key)).keyid, use: 'sig' });
  const body = JSON.stringify({ keys: [...published, ...unsigned_keys] }, null, 2);
  const base = {
    'content-type': content_type ?? policy.DIRECTORY_MEDIA_TYPE,
    'cache-control': `max-age=${defaults.max_age}`,
    'content-digest': digest(body),
  };
  const inputs = [];
  const signatures = [];
  for (const [i, key] of keys.entries()) {
    const signer = await signerFromJWK(key);
    const out = await createSignature(directoryMessage(authority ? `https://${authority}` : origin, { status: 200, headers: base }), {
      label: `key${i + 1}`,
      components: (components ?? ['@authority;req', 'content-digest']).map(parseComponent),
      parameters: {
        created: now + (parameters.created ?? defaults.parameters.created),
        expires: now + (parameters.expires ?? defaults.parameters.expires),
        keyid: signer.keyid,
        alg: signer.algorithm,
        tag: policy.DIRECTORY_TAG,
      },
      signer,
    });
    inputs.push(out.signatureInput);
    signatures.push(out.signature);
  }
  const headers = keys.length === 0 ? base : { ...base, 'signature-input': inputs.join(', '), signature: signatures.join(', ') };
  return { url: policy.directoryUrl(origin), status: 200, headers, body: body + (after_signing.append_to_body ?? '') };
}

/** `@authority;req` or `signature-agent;key=sig1`, as scenarios.json spells a component. */
export function parseComponent(id) {
  const [name, ...params] = id.split(';');
  if (params.length === 0) return name;
  return component(name, Object.fromEntries(params.map(p => (p.includes('=') ? p.split('=') : [p, true]))));
}

const NONCE_ENCODINGS = {
  base64url: bytes => bytes.toString('base64url'),
  base64: bytes => bytes.toString('base64'),
  'base64url-padded': bytes => `${bytes.toString('base64url')}${'='.repeat((4 - (bytes.toString('base64url').length % 4)) % 4)}`,
};
function nonceValue(spec) {
  if (!spec || typeof spec !== 'object') return spec;
  for (;;) {
    const value = NONCE_ENCODINGS[spec.encoding](randomBytes(spec.bytes));
    // Padded base64url with no '-' or '_' is also canonical padded base64, which the profile accepts.
    if (spec.encoding !== 'base64url-padded' || /[-_]/.test(value)) return value;
  }
}

/**
 * Signs one request scenario from scenarios.json at `now`. http-message-sig
 * signs any component and parameter set, so a scenario can carry what the
 * profile refuses. `keys` maps a kid to its private JWK.
 */
export async function signScenario(scenarios, scenario, keys, now) {
  const { signer: who, requests, components: defaultComponents, parameters: defaultParameters } = scenarios;
  const base = requests[scenario.base];
  const label = scenario.label ?? 'sig1';
  const headers = { ...base.headers, 'signature-agent': scenario.signature_agent ?? `${label}="${who.signature_agent}"` };
  if (base.body) headers['content-digest'] = digest(base.body);
  const request = { ...base, headers };
  const first = await signOne(request, {
    key: keys[who.kid],
    label,
    components: scenario.components ?? defaultComponents[scenario.base].map(c => c.replace('{label}', label)),
    parameters: { ...defaultParameters, ...scenario.parameters },
    now,
  });
  let signed = first;
  if (scenario.extra_signature) {
    const extra = scenario.extra_signature;
    const second = await signOne(request, { key: keys[extra.kid], label: extra.label, components: extra.components, parameters: extra.parameters, now });
    signed = { signatureInput: `${first.signatureInput}, ${second.signatureInput}`, signature: `${first.signature}, ${second.signature}` };
  }
  const after = scenario.after_signing ?? {};
  // append_members adds raw members, so a scenario can carry a member the library would not sign.
  const append = (name, value) => (after.append_members?.[name] ? `${value}, ${after.append_members[name]}` : value);
  return {
    ...request,
    headers: {
      ...headers,
      'signature-input': append('signature-input', signed.signatureInput),
      signature: append('signature', signed.signature),
      ...after.headers,
    },
    body: base.body + (after.append_to_body ?? ''),
  };
}

/** Signs one response scenario at `now`: returns { request, response }. */
export async function signResponseScenario(scenarios, scenario, keys, now) {
  const kind = scenario.request ?? 'bound';
  const request = kind === 'unbound' ? { ...scenarios.requests.post } : await signScenario(scenarios, { base: 'post' }, keys, now);
  const response = await signResponse({
    key: keys[scenarios.responder.kid],
    origin: scenarios.responder.identity,
    request,
    response: scenarios.response,
    now,
    components: scenario.components ?? scenarios.response_components[kind],
    parameters: { ...scenarios.response_parameters, ...scenario.parameters },
    after_signing: scenario.after_signing,
  });
  const requestHeaders = { ...request.headers };
  for (const [name, value] of Object.entries(scenario.after_signing?.request_headers ?? {})) {
    for (const k of Object.keys(requestHeaders)) if (k.toLowerCase() === name.toLowerCase()) delete requestHeaders[k];
    requestHeaders[name] = value;
  }
  return { request: { ...request, headers: requestHeaders }, response };
}

async function signOne(request, { key, label, components, parameters, now }) {
  const real = await signerFromJWK(key);
  const claimed = parameters.alg === undefined ? real.algorithm : parameters.alg;
  // A claimed alg other than the key's signs with the key and claims the other value.
  const signer = claimed && claimed !== real.algorithm ? { algorithm: claimed, keyid: real.keyid, sign: d => real.sign(d) } : real;
  const values = {
    created: parameters.created === null ? null : now + parameters.created,
    expires: parameters.expires === null ? null : now + parameters.expires,
    nonce: nonceValue(parameters.nonce),
    keyid: parameters.keyid === undefined ? real.keyid : parameters.keyid,
    alg: claimed,
    tag: parameters.tag,
  };
  return createSignature(message(request), {
    label,
    components: components.map(parseComponent),
    parameters: Object.fromEntries(Object.entries(values).filter(([, v]) => v !== null && v !== undefined)),
    signer,
  });
}

// ── Adapter entry point ───────────────────────────────────────────────────

const OPERATIONS = {
  sign_request: signRequest,
  sign_directory: signDirectory,
  sign_response: signResponse,
  verify_request: op => verifyRequest(op),
  verify_directory: op => verifyDirectory(op),
  verify_response: op => verifyResponse(op),
};

if (process.argv[1] !== undefined && import.meta.url === pathToFileURL(process.argv[1]).href) {
  let input = '';
  for await (const chunk of process.stdin) input += chunk;
  const op = fromWire(JSON.parse(input));
  let out;
  try {
    out = await OPERATIONS[op.op](op);
  } catch (e) {
    out = { ok: false, status: null, rule: 'adapter-error', error: String(e?.stack ?? e) };
  }
  process.stdout.write(JSON.stringify(toWire(out)));
}
