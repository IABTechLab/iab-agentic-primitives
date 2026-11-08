/**
 * The profile's rules, written as the hooks that Cloudflare's `web-bot-auth`
 * and `http-message-sig` libraries expose.
 *
 * The library enforces the Web Bot Auth (WBA) rules itself. This file holds only
 * what the profile adds, one section per rule in WEB_BOT_AUTH.md ("Profile
 * rules"). A reviewer reads this file to see everything the profile adds to WBA.
 *
 * Some rules are WBA MUSTs that the pinned library does not enforce (an `https`
 * origin, no path in the origin, a member keyed to the signature label, the
 * directory media type). They live here until the library enforces them; the
 * harness's tier check reports the day it does.
 *
 * Response signatures are an OpenProposal addition: web-bot-auth 0.2.0 has no
 * response verifier, so the judge verifies them with `http-message-sig`.
 */
import { createHash } from 'node:crypto';
import { component, isSignatureError } from 'http-message-sig';
import { parseDictionary } from 'structured-headers';
import { HTTP_MESSAGE_SIGNATURES_DIRECTORY, HTTP_MESSAGE_SIGNATURE_TAG } from 'web-bot-auth';

/**
 * A refusal with the status the profile answers (rule "Failures"). `rule` is a
 * name from refusals.json, the vocabulary the Python implementation's Refusal
 * enum generates, so the harness compares the two verdicts rule by rule.
 */
export class PolicyError extends Error {
  constructor(status, rule, message) {
    super(message);
    this.status = status;
    this.rule = rule;
  }
}
const refuse = (rule, message) => {
  throw new PolicyError(403, rule, message);
};

export const header = (headers, name) => Object.entries(headers).find(([k]) => k.toLowerCase() === name)?.[1];
const componentName = c => (typeof c === 'string' ? c : c.name);

// ── Identity ──────────────────────────────────────────────────────────────
// The library checks the tag, the thumbprint keyid, and that exactly one
// `signature-agent` member is covered. The profile adds the value rule: the
// member value is exactly the key directory URL
// `https://<host>/.well-known/http-message-signatures-directory` (draft -01
// fetches it directly) or exactly the origin `https://<host>` (the draft -00
// form), with a lowercase host and no port, query, fragment, or userinfo. Both
// name the origin `https://<host>`. A member that carries parameters is
// ignored (draft -01 section 5.2.1). The library accepts `http`
// (cloudflare/web-bot-auth#162) and does not check the rest.

const HOST = String.raw`(?:\[[0-9a-f:.]+\]|[a-z0-9](?:[a-z0-9.-]*[a-z0-9.])?)`;
const VALUE = new RegExp(`^https://(${HOST})(?:${HTTP_MESSAGE_SIGNATURES_DIRECTORY.replace(/[.]/g, '\\.')})?$`);

/** The origin a Signature-Agent value names, or undefined when the value breaks the rule. */
export function originOfValue(value) {
  const match = VALUE.exec(value);
  return match ? `https://${match[1]}` : undefined;
}

/** Signature-Agent members without parameters; a member with parameters is ignored. */
export function agentMembers(headers) {
  const agents = parseDictionary(header(headers, 'signature-agent') ?? '');
  return new Map([...agents].filter(([, [, params]]) => params.size === 0));
}

/** Checks the covered Signature-Agent member before its directory is read. */
export function checkSignatureAgent(entry, headers) {
  const all = parseDictionary(header(headers, 'signature-agent') ?? '');
  if (all.get(entry.label)?.[1]?.size > 0) refuse('signature-agent-parameters', `Signature-Agent member ${entry.label} carries parameters`);
  if (originOfValue(entry.uri) === undefined) refuse('signature-agent-value', `Signature-Agent ${entry.uri} is not https://<host>${HTTP_MESSAGE_SIGNATURES_DIRECTORY} or https://<host>`);
}

/** The covered member is the one keyed to the signature's label (WBA 5.2.1). */
function checkMemberKeyedToLabel(result) {
  const member = result.components.find(c => componentName(c) === 'signature-agent');
  if (member?.parameters?.key !== result.label) {
    refuse('signature-agent-missing', `covered Signature-Agent member ${member?.parameters?.key} is not keyed to label ${result.label}`);
  }
}

// ── Authority ─────────────────────────────────────────────────────────────
// The request's @authority is the verifier's own configured authority, never
// the Host it was sent (OpenProposal § 2.8.4 step 5). The library has no
// such check.

export function checkAuthority(request, authority) {
  if (authority !== undefined && new URL(request.url).host !== authority) {
    refuse('authority', `the request is for ${new URL(request.url).host}, not ${authority}`);
  }
}

// ── Request components ────────────────────────────────────────────────────

export function requiredComponents(request) {
  const names = ['@method', '@target-uri', '@authority'];
  if (request.body) names.push('content-type', 'content-digest');
  return names;
}

/** True when the Content-Digest field's sha-256 member is the digest of `body` (RFC 9530). */
export function digestMatches(field, body) {
  if (field === undefined) return false;
  let member;
  try {
    member = parseDictionary(field).get('sha-256');
  } catch {
    return false;
  }
  if (!member || !(member[0] instanceof ArrayBuffer)) return false;
  return Buffer.from(member[0]).equals(createHash('sha256').update(body).digest());
}

// ── Signature parameters ──────────────────────────────────────────────────
// The library requires created, expires, keyid, alg, and tag, and checks that a
// nonce is 64 bytes as unpadded base64url or canonical padded base64
// (`validateNonce`). The profile adds that a request's nonce is present.

export const ALGORITHMS = ['ed25519'];
export const MAX_LIFETIME = 300;
export const CLOCK_SKEW = 60;

/** The `validate` hook for `verify()`: the request-level rules above. */
export function validate(result, request) {
  checkMemberKeyedToLabel(result);
  const covered = result.components.map(componentName);
  for (const name of requiredComponents(request)) {
    if (!covered.includes(name)) refuse('components', `${name} is not covered`);
  }
  if (request.body && !digestMatches(header(request.headers, 'content-digest'), request.body)) {
    refuse('content-digest', 'Content-Digest does not match the body');
  }
  if (typeof result.nonce !== 'string') refuse('nonce-missing', 'nonce is required on every signed request');
  checkLifetime(result.parameters);
  return true;
}

/** The one lifetime rule of a request or response signature: at most 300 s. */
export function checkLifetime({ created, expires }) {
  if (expires - created > MAX_LIFETIME) refuse('lifetime', `expires is more than ${MAX_LIFETIME} s after created`);
}

// ── Keys ──────────────────────────────────────────────────────────────────
// Key lookup is by (origin, keyid), never by keyid alone (WBA 5.4): the
// resolver reads only the directory of the origin the covered member names.

/** True for a key the profile uses: the library imports it and its algorithm is allowed. */
export const usable = verifier => verifier !== undefined && ALGORITHMS.includes(verifier.algorithm);

/** WBA section 6.8: verifiers SHOULD reject published test keys. The harness passes none here. */
export function checkNotTestKey(keyid, rejectKids = []) {
  if (rejectKids.includes(keyid)) refuse('rejected-key', `${keyid} is a published test key`);
}

// ── Directory responses ───────────────────────────────────────────────────
// Served with the directory media type. One signature per Ed25519 key, covering
// "@authority";req and content-digest, with created, expires, keyid, and the
// directory tag. Keys the profile does not use, and their signatures, are
// ignored (RFC 7517 section 5). Content-Digest matches the body (Appendix B.1).

export const DIRECTORY_MEDIA_TYPE = 'application/http-message-signatures-directory+json';
export const DIRECTORY_TAG = 'http-message-signatures-directory';

/**
 * The `verifySignature` policy for one directory signature (WBA Appendix B.1).
 * The library applies `clockSkew` to both created and expires; the profile
 * allows the skew on created only, so the hook refuses any expires before now.
 */
export const directorySignaturePolicy = now => ({
  algorithms: ALGORITHMS,
  now,
  clockSkew: CLOCK_SKEW,
  requiredComponents: [component('@authority', { req: true }), 'content-digest'],
  requiredParameters: ['created', 'expires', 'keyid', 'tag'],
  validate: v => {
    if (v.parameters.expires < now) refuse('directory-window', 'the directory signature has expired');
    return v.parameters.tag === DIRECTORY_TAG;
  },
});

/** The label of the one signature by `keyid`, or a refusal. Labels by other keys are never read. */
export function directoryLabelFor(signatureInput, keyid) {
  const labels = [...signatureInput].filter(([, [, params]]) => params.get('keyid') === keyid).map(([label]) => label);
  if (labels.length !== 1) refuse('directory-one-signature-per-key', `${labels.length} directory signatures by ${keyid}`);
  return labels[0];
}

export function checkDirectoryBody(response, verifiedKeyids) {
  if (!digestMatches(header(response.headers, 'content-digest'), response.body)) {
    refuse('directory-content-digest', 'directory Content-Digest does not match the body');
  }
  if (verifiedKeyids.length === 0) refuse('directory-no-usable-key', 'the directory holds no Ed25519 key');
}

// ── Fetches ───────────────────────────────────────────────────────────────
// The harness serves directories from data, so the network rules (HTTPS,
// resolved-address checks, no redirects, size and time limits) and caching are
// not exercised here; the Python unit tests grade them. The response-level
// rules are.

export const WELL_KNOWN = HTTP_MESSAGE_SIGNATURES_DIRECTORY;
export const directoryUrl = origin => `${new URL(origin).origin}${HTTP_MESSAGE_SIGNATURES_DIRECTORY}`;

export function checkDirectoryResponse(response) {
  if (response.status !== 200) refuse('directory-status', `directory status ${response.status}`);
  const type = header(response.headers, 'content-type')?.split(';')[0].trim().toLowerCase();
  if (type !== DIRECTORY_MEDIA_TYPE) refuse('directory-media-type', `directory media type ${type}`);
}

// ── One signature ─────────────────────────────────────────────────────────
// Exactly one web-bot-auth signature and one Signature-Agent member. Members
// with other tags are not read. The library selects no label by tag and
// requires one when a message carries several, so the profile selects it.

/**
 * Parses the three signature fields (`400` on failure), then returns the label
 * of the one web-bot-auth signature. The library then checks every member's
 * shape, whatever its tag (RFC 9421 sections 4.1 and 4.2).
 */
export function selectLabel(message) {
  let input;
  let agents;
  try {
    for (const name of ['signature', 'signature-input', 'signature-agent']) {
      const value = header(message.headers, name);
      if (value === undefined) refuse('signature-missing', `no ${name} field`);
      const parsed = parseDictionary(value);
      if (name === 'signature-input') input = parsed;
      if (name === 'signature-agent') agents = parsed;
    }
  } catch (e) {
    if (e instanceof PolicyError) throw e;
    throw new PolicyError(400, 'parse', e.message);
  }
  const tagged = [...input].filter(([, [, params]]) => params.get('tag') === HTTP_MESSAGE_SIGNATURE_TAG).map(([label]) => label);
  if (tagged.length === 0) refuse('signature-missing', 'no web-bot-auth signature');
  if (tagged.length > 1) refuse('multiple-signatures', `${tagged.length} web-bot-auth signatures`);
  const usable = [...agents].filter(([, [, params]]) => params.size === 0);
  if (usable.length > 1) refuse('multiple-signature-agents', `${usable.length} Signature-Agent members`);
  return tagged[0];
}

// ── Response signatures (OpenProposal addition) ───────────────────────────
// @status, "@method";req, "@target-uri";req, "signature-agent";key=<label>;
// with a body, content-type and content-digest; and, when the request carried
// a web-bot-auth signature, "signature";req;key=<request label>. No nonce.

/** The label of the request's one web-bot-auth signature, or undefined. */
export function requestLabel(request) {
  try {
    return selectLabel(request);
  } catch {
    return undefined;
  }
}

export function responseRequiredComponents(response, request, label) {
  const out = ['@status', component('@method', { req: true }), component('@target-uri', { req: true }), component('signature-agent', { key: label })];
  if (response.body) out.push('content-type', 'content-digest');
  const bound = requestLabel(request);
  if (bound !== undefined) out.push(component('signature', { req: true, key: bound }));
  return out;
}

export const responsePolicy = (response, request, label, now) => ({
  algorithms: ALGORITHMS,
  now,
  clockSkew: CLOCK_SKEW,
  requiredComponents: responseRequiredComponents(response, request, label),
  requiredParameters: ['created', 'expires', 'keyid', 'alg', 'tag'],
  validate: v => {
    if (v.parameters.tag !== HTTP_MESSAGE_SIGNATURE_TAG) refuse('signature-missing', `tag ${v.parameters.tag}`);
    checkLifetime(v.parameters);
    if (response.body && !digestMatches(header(response.headers, 'content-digest'), response.body)) {
      refuse('content-digest', 'Content-Digest does not match the body');
    }
    return true;
  },
});

// ── Failures ──────────────────────────────────────────────────────────────
// 400 when a signature field does not parse, 429 for a reused nonce, 403
// otherwise (WBA 5.3 and 5.4).

/** The policy's own refusal behind `error`, if it carries one. */
const policyCause = error => (error instanceof PolicyError ? error : error?.cause instanceof PolicyError ? error.cause : undefined);

/**
 * The library's errors, named in the vocabulary by what they report and the
 * kind of message: `request` and `response` signatures, or a `directory`.
 */
const LIBRARY_RULES = [
  [/created in the future/, { request: 'created-in-future', response: 'created-in-future', directory: 'directory-window' }],
  [/has expired/, { request: 'expired', response: 'expired', directory: 'directory-window' }],
  [/parameter alg is absent|Algorithm .* is not allowed/, { request: 'alg', response: 'alg', directory: 'directory-signature' }],
  [/Required component "signature";/, { request: 'components', response: 'response-not-bound', directory: 'directory-components' }],
  [/Required component/, { request: 'components', response: 'components', directory: 'directory-components' }],
  [/cover exactly one signature-agent member/, { request: 'signature-agent-not-covered', response: 'signature-agent-not-covered', directory: 'directory-signature' }],
  [/nonce/, { request: 'nonce-invalid', response: 'nonce-invalid', directory: 'directory-signature' }],
];

/** The vocabulary rule of a refusal `error` while verifying a message of `kind`. */
export function ruleOf(error, kind) {
  const own = policyCause(error);
  if (own) return own.rule;
  const text = `${error?.message ?? ''} ${error?.cause?.message ?? ''}`;
  if (isSignatureError(error) && (error.code === 'MalformedField' || error.code === 'DuplicateLabel')) {
    return kind === 'directory' ? 'directory-signature-parse' : 'parse';
  }
  for (const [pattern, rules] of LIBRARY_RULES) if (pattern.test(text)) return rules[kind];
  return kind === 'directory' ? 'directory-signature' : 'signature-invalid';
}

export function statusOf(error) {
  const cause = error?.cause;
  if (error instanceof PolicyError) return error.status;
  if (cause instanceof PolicyError) return cause.status;
  if (isSignatureError(error) && (error.code === 'MalformedField' || error.code === 'DuplicateLabel')) return 400;
  return 403;
}

// ── Replay protection ─────────────────────────────────────────────────────
// A nonce seen before for the same identity is refused with 429.

export class ReplayStore {
  #seen = new Set();

  check(identity, nonce) {
    const entry = `${identity} ${nonce}`;
    if (this.#seen.has(entry)) throw new PolicyError(429, 'nonce-replayed', `nonce seen before for ${identity}`);
    this.#seen.add(entry);
  }
}
