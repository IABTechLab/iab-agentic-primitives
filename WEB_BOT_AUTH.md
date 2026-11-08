# Web Bot Auth profile

`iab_agentic_primitives.web_bot_auth` implements the Web Bot Auth (WBA) profile
that OpenProposal 3.0 § 2.8 defines for agent-to-agent HTTP traffic. With it, a
buyer agent signs each request it sends, a seller agent verifies the signature
and learns which origin sent the request, and the seller signs each response and
binds it to the request it answers. Neither party needs the other's key in
advance: each one publishes its keys in a signed key directory at its own
origin, and the verifier fetches that directory.

The module confirms a signature and returns the verified origin. It doesn't
decide who the caller is in your system. Your identity boundary maps
`VerifiedRequest.origin` to the principal in your own records, the same way it
maps a bearer token.

The module is self-contained. It imports nothing else from
`iab_agentic_primitives`, and its dependencies are the `wba` extra:

```bash
pip install 'iab-agentic-primitives[wba]'
```

The module uses existing libraries for everything except the profile's rules:

| Library | Use |
| :--- | :--- |
| `http-message-signatures==3.0.0` | RFC 9421 signature bases, Ed25519 signing and verification, the `;key` and `;req` component parameters, and RFC 9530 `Content-Digest` |
| `http-sf` | RFC 8941 Structured Fields for `Signature`, `Signature-Input`, and `Signature-Agent`, with duplicate keys refused |
| `joserfc` | JWK import and export, and RFC 7638 thumbprints for Ed25519 (RFC 8037) |
| `idna` | IDNA-2008 host canonicalization for the origin rule |
| `cryptography` | Ed25519 keys |
| `httpx` | Directory fetches, through a client you can inject |

The module overrides one hook of `http-message-signatures`, the verifier's
time-window check, so the profile can check the window against an injected
clock. `tests/web_bot_auth/test_wba_library_contract.py` pins the library
behavior the module relies on, through the module's own signers and
verifiers. The ASGI helpers are plain ASGI, so they need no web framework.

## Profile rules

The profile selects options that WBA and RFC 9421 already define. It adds no
header, tag, key field, or error code.

| Rule | Profile choice |
| :--- | :--- |
| Identity | An origin `https://<host>`: a canonical host with no port, and no path, query, fragment, or userinfo. A canonical host is lowercase IDNA-2008 A-labels whose last label isn't a number, or an IP literal in its compressed form (IPv6 in brackets, with no zone). One agent per origin. |
| `Signature-Agent` value | A signer sends its key directory URL, `https://<host>/.well-known/http-message-signatures-directory`, with no member parameters. A verifier accepts exactly that URL or exactly the bare origin `https://<host>` (the draft -00 form), with the same host rules, compares without normalizing, and refuses any other value with `403`. Both forms name the origin `https://<host>`. A member that carries parameters is ignored. |
| Keys | The origin's key directory, `GET https://<origin>/.well-known/http-message-signatures-directory`, a JWK Set served as `application/http-message-signatures-directory+json`. Signing keys are Ed25519 (`kty` `OKP`, `crv` `Ed25519`), and each `kid` is the key's RFC 7638 thumbprint. The verifier looks a key up by the pair (origin, `keyid`), never by `keyid` alone. |
| Directory signatures | The directory response is signed once per Ed25519 key, covering `"@authority";req` and `content-digest`, with `created`, `expires`, `keyid`, `tag="http-message-signatures-directory"`, and, when present, `alg="ed25519"`. Signers send `alg`; a verifier accepts a signature without it, as draft -01 Appendix E.2.3 shows. The same 60 s clock skew as for requests applies to `created`, so a `created` up to 60 s in the future is accepted. It doesn't apply to `expires`: a directory signature verifies until its `expires` and not one second after. Every Ed25519 key signs. Keys of other types, and their signatures, are ignored. The server signs for its own configured authority, never for the `Host` a client sends. |
| Fetches | The verifier fetches exactly `https://<host>/.well-known/http-message-signatures-directory` with `Accept-Encoding: identity`. `https` only, no redirects (any `3xx` is refused), status `200` only, the directory media type, no `Content-Encoding` other than `identity`, a 256 KiB cap on the raw body read as a stream, a `Content-Digest` with `sha-256`, a UTF-8 JSON body, at most 16 keys, and 5 s to connect and 10 s in total. |
| Cache | A fetched directory is fresh for 300 s and valid while the current time is at or before the earliest `expires` among its signatures, the same rule the directory check applies. While it's fresh, it answers every `keyid` without a fetch, so an unknown `keyid` never triggers one. A stale directory is fetched again, and a directory that resolves replaces the entry, with or without the key. A failed fetch doesn't evict a valid entry, and the origin isn't fetched again for 60 s. Both caches are bounded. |
| One signature | Exactly one `Signature-Input` member with `tag="web-bot-auth"` and exactly one `Signature-Agent` member, keyed to that member's label. A repeat of that member's label is refused with `403`. The verifier doesn't read members with other tags, nor their repeats, but every member must be well formed. |
| Request components | `@method`, `@target-uri`, `@authority`, and `"signature-agent";key=<label>`. With a body, also `content-type` and `content-digest`, which must carry `sha-256`, and the verifier checks the digest against the body bytes. The verifier rebuilds `@target-uri` and `@authority` from its own configured origin, never from `Host`, and can refuse a URL for any authority but its own. |
| Target URI | `@target-uri` is the URL as it travels on the wire, and `@authority` is its authority: see "The target URI". |
| Request parameters | `created`, `expires` (at most 300 s after `created`), `nonce` (64 random bytes, as unpadded base64url or padded base64), `keyid`, `alg="ed25519"`, and `tag="web-bot-auth"`. The verifier allows 60 s of clock skew. |
| Response components | `@status`, `"@method";req`, `"@target-uri";req`, and `"signature-agent";key=<label>`. With a body, also `content-type` and `content-digest`. When the request carried a `web-bot-auth` signature, also `"signature";req;key=<request label>`, which binds the response to that exact request. A response to `HEAD` has no body to cover, whatever the application produced, since the server doesn't send one: signer and verifier read the content through the one rule, `RESPONSE.content`. |
| Response parameters | `created`, `expires` (at most 300 s after `created`), `keyid`, `alg="ed25519"`, and `tag="web-bot-auth"`. No nonce: the binding gives freshness. |
| Replay | The verifier records each nonce per origin and refuses one it has seen within the signature's lifetime. |
| Failures | `400` when a signature field doesn't parse, `403` when verification or a profile rule fails, `429` for a replayed nonce, and `503` when the verifier can't record a nonce. |

### The target URI

`@target-uri` is the request URL as it travels on the wire: the URL an HTTP
client sends and a server receives. It is the WHATWG URL serialization, which
httpx and `fetch` send and Cloudflare's `web-bot-auth` signs, normalized per
RFC 3986 section 6.2.2:

- The scheme and host are lowercase, a host name in IDNA A-labels and an IP
  literal in compressed form, and a default port (`:443`, `:80`) is dropped.
- Dot segments are removed, `%2e` included, and an empty path is `/`.
- The characters WHATWG percent-encodes are percent-encoded: controls, space,
  non-ASCII (as UTF-8), `"`, `<`, and `>`, plus `` ` ``, `{`, and `}` in the
  path and `'` in the query. Every other character stays as written, so
  `?ids[]=1`, `?f=a|b`, `?q={json}`, and `?q=a^b` are signed as they are sent.
  An existing escape, such as `%20` or even `%zz`, is never changed.
- Userinfo and the fragment, which never go on the wire, are dropped, and so
  is an empty query: a server can't tell `/p?` from `/p`.

`@authority` is the authority of the same URL: the host and a port other
than the default. `message.target_uri(url)` is this rule, and
`values.TargetURI` holds its parts. The signers sign it, the verifiers
recompute it, and the server's edge rebuilds it from the bytes it received,
so a signer can't sign a URL that a verifier or an edge reads differently.
`RequestSigner.sign` returns the request at that URL: send it to that URL
with a client that sends it as given, as httpx and `fetch` do. The library
hands http-message-signatures the parsed parts through its
`component_resolver_class` extension point, since the library's own parser
accepts only the strict RFC 3986 grammar, which refuses the characters
clients send raw. Vectors `positive/005` to `positive/012` and the
target-URI cases of the interop harness grade the rule.

### Verification procedure

This is the one statement of the procedure. `verifier.py` implements it, one
function per step, and names each step it implements;
`tests/web_bot_auth/test_wba_docs.py` checks that the names agree.
The procedure applies to a signed message, one that carries
`Signature-Input`. `is_signed(headers)` is the one test of that, and both
edges decide with it, before any body is read: a message without
`Signature-Input`, such as one with only another scheme's `Signature`, is
unsigned, and its `Signature` is never parsed. Asked to verify an unsigned
message, a verifier refuses it `403` `signature-missing`.
`RequestVerifier.verify` runs these steps in order:

1. **Parse the fields.** Parse `Signature`, `Signature-Input`, and
   `Signature-Agent` as Structured Field Dictionaries, once. Any parse
   failure, a repeated parameter, a `Signature-Input` member that isn't an
   Inner List of Strings, a parameter of another type than RFC 9421 section
   2.3 gives it (`created` and `expires` Integers; `keyid`, `alg`, `nonce`,
   and `tag` Strings), or a `Signature` member that isn't a Byte Sequence is
   `400`. Every member is checked, whatever its tag, before any step-2 rule
   runs.
2. **Select the signature.** Select the one `web-bot-auth` member and the
   `Signature-Agent` member with the same label, and read the origin the
   member names. More than one of either, a repeat of the selected label, an
   absent member, or a member the signature doesn't cover is `403`. A label
   repeated on another signer's member is not this signature's concern. A
   member with parameters is ignored (`403` when it is the covered one), and
   a value that isn't the key URL or the bare origin is `403`. Both forms
   give the origin `https://<host>`.
3. **Check the authority.** The request URL names the verifier's own
   `identity.request_authority`, the authority of its `base_url`, which the
   edges rebuild each request URL on: a URL for another authority is `403`
   `authority`.
4. **Check the parameters.** `keyid`, `alg`, the window, the lifetime, and
   the nonce rule: `403`.
5. **Check the components.** The signature covers every component the rules
   require: `403` `components`.
6. **Resolve the key.** Refuse a `keyid` the verifier's `KeyPolicy` refuses:
   always the published test keys (`rules.TEST_KEY_IDS`), plus any
   `KeyPolicy(reject_keyids=...)` lists; only a conformance run accepts the
   test keys, with `keys=CONFORMANCE_KEYS` (`403` `rejected-key`), then
   resolve the key by (origin, `keyid`): `403` when the directory doesn't
   hold it, or with the directory's own refusal when its fetch or check fails.
7. **Verify the signature.** Check a covered `Content-Digest` against the
   body, then verify the signature: `403`. The credential's `created`,
   `expires`, and `nonce` are the parameters the library authenticated.
8. **Check the nonce.** Check and record the nonce for the origin: `429` when
   seen before, and `503` when the nonce store can't record it.

`ResponseVerifier.verify` runs the same steps under the response rules, with
no authority or nonce step. Its components include the binding to the signed
request it answers, whose absence is `response-not-bound`. After them it runs
one more step, **Check the signer.** The response is signed by the origin
`expected_origin` names (`origin-mismatch`): by default `SAME_ORIGIN`, the
identity of the host the request went to, which `Origin.of_url` maps from
the request URL through IDNA, so a request to `https://bücher.example`
expects `https://xn--bcher-kva.example`; an `Origin`; or `None` for any
identity. A response carries no nonce.

A failure raises `WebBotAuthError`, the refusal a verifier sends to the peer
that sent the message. A client doesn't answer the server it called, so the
httpx client reports what it found out about a response as a value instead
(see "Call a seller with httpx"). Its `refusal` is a member of the
`Refusal` enum, which carries the reason code, the status, and how the refusal
is presented. `status` is `400`, `403`, `429`, or `503`; no other status can be
expressed. `reason` is the machine string:

| Status | `reason` values |
| :--- | :--- |
| `400` | `parse` |
| `403` | `signature-missing`, `multiple-signatures`, `signature-agent-missing`, `multiple-signature-agents`, `signature-agent-not-covered`, `signature-agent-parameters`, `signature-agent-value`, `keyid`, `alg`, `window`, `lifetime`, `created-in-future`, `expired`, `nonce-missing`, `nonce-invalid`, `components`, `content-digest`, `signature-invalid`, `key-not-found`, `rejected-key`, `authority`, and the directory reasons (`directory-status`, `directory-encoding`, `directory-media-type`, `directory-unavailable`, `directory-too-large`, `directory-too-many-keys`, `directory-content-digest`, `directory-format`, `directory-kid`, `directory-no-usable-key`, `directory-signature-parse`, `directory-one-signature-per-key`, `directory-tag`, `directory-components`, `directory-window`, `directory-signature`). Responses add `response-not-bound` and `origin-mismatch`. |
| `429` | `nonce-replayed` |
| `503` | `nonce-store-unavailable` |

`refusal_response(error, has_body=...)` returns the response that refuses a
message, as an `Answer` (`status`, `headers`, `body`): the refusal's status, the JSON body
`{"error": reason, "status": status}`, `Accept-Signature` on a `403` or `429` (the
value that `accept_signature()` returns, so the signer can sign again), and
`Retry-After: 1` on a `503`. The body never carries the error's detail, such as
a directory fetch error or a nonce store's message, which could tell an
unauthenticated peer about the verifier's network; the verifiers log it. `RequireSignatureMiddleware` sends it, and a
framework route returns it, so every refusal looks the same on the wire.

A mistake in configuration, such as a malformed origin, a lifetime out of
range, a bound that isn't a whole number, or a string where an identity is
expected, raises `ConfigurationError` (a `ValueError`) when the value, policy,
signer, verifier, resolver, store, or middleware is made. So does a resolver
used on a second event loop or after it is closed. It is never a refusal and
never goes on the wire. Every configuration type checks itself when it is
made, and the roles, the resolver, and the edges are fixed once made.

## Modules

Each module states its public names once, in a sorted `__all__`; the table
lists them, and `tests/web_bot_auth/test_wba_docs.py` compares it with the
code. A name that isn't in `__all__` is not part of the API.

| Module | What it holds | `__all__` |
| :--- | :--- | :--- |
| `values` | identities, key ids, time, and where a directory lives | `DIRECTORY_MEDIA_TYPE`, `DIRECTORY_PATH`, `OMIT`, `Authority`, `Clock`, `KeyId`, `Omit`, `Origin`, `RequestAuthority`, `Seconds`, `ServerIdentity`, `TargetURI`, `UnixSeconds`, `authority_of`, `identifier_set`, `is_canonical_host`, `is_decimal`, `system_clock`, `whole_number` |
| `rules` | the profile rules, stated once | `ALGORITHM`, `CACHE`, `CLOCK_SKEW`, `CONTENT_DIGEST`, `DIRECTORY`, `DIRECTORY_TAG`, `MAX_LIFETIME`, `MESSAGE_WINDOW`, `NONCE_BYTES`, `NONCE_RULE`, `REQUEST`, `RESPONSE`, `SIGNATURE_TAG`, `STREAMING`, `TEST_KEY_IDS`, `CacheRules`, `ComponentId`, `ComponentParam`, `Rules`, `SignatureParams`, `Stamp`, `Streaming`, `Window`, `accept_signature`, `component_id`, `signature_agent_component`, `validate_nonce`, `web_bot_auth_label` |
| `errors` | the refusal vocabulary and the errors | `RETRY_AFTER`, `ConfigurationError`, `Refusal`, `SigningError`, `Status`, `WebBotAuthError` |
| `presentation` | a refusal, and the other ASGI answers, as an HTTP response | `NO_KEYS_RETRY_AFTER`, `body_too_large_response`, `method_not_allowed_response`, `no_keys_response`, `refusal_response`, `unsignable_response` |
| `keys` | Ed25519 keys and JWK thumbprints | `JWK`, `Ed25519Key`, `PrivateJwk`, `PublicJwk`, `public_jwk_of`, `public_key_from_jwk`, `thumbprint` |
| `directory` | publishing and verifying the key directory, with no I/O | `DEFAULT_DIRECTORY_LIFETIME`, `DEFAULT_DIRECTORY_MAX_AGE`, `DEFAULT_LIMITS`, `DEFAULT_PUBLISH`, `MAX_DIRECTORY_BYTES`, `MAX_DIRECTORY_KEYS`, `DirectoryPublisher`, `DirectoryResponse`, `FetchLimits`, `KeyInput`, `KeySet`, `KeySource`, `PublishPolicy`, `VerifiedDirectory`, `build_directory`, `check_fetch_rules`, `current_keys`, `parse_jwk_set`, `publish_directory`, `verify_directory` |
| `ports` | the resolution ports | `DirectoryFetcher`, `DirectoryStore`, `DirectoryStoreUnavailable`, `KeyResolver`, `ResolvedKey` |
| `resolver` | directory resolution and its cache | `DEFAULT_CACHE`, `CachePolicy`, `DirectoryResolver`, `MemoryDirectoryStore`, `StaticKeyResolver` |
| `fetcher` | the default directory fetcher, over httpx | `BlockedAddress`, `HttpxDirectoryFetcher`, `TLSConfig` |
| `testing` | directories served from data, and the store contracts | `check_directory_store`, `check_nonce_store`, `directory_transport`, `served_resolver` |
| `replay` | nonce stores | `MemoryNonceStore`, `NonceStore`, `NonceStoreUnavailable` |
| `message` | the immutable message model | `Answer`, `HTTPRequest`, `HTTPResponse`, `HeaderLines`, `Headers`, `target_uri` |
| `signer` | request and response signing | `RequestSigner`, `ResponseSigner`, `SignerConfig`, `generate_nonce` |
| `verifier` | request and response verification, and the credentials | `CONFORMANCE_KEYS`, `DEFAULT_KEYS`, `SAME_ORIGIN`, `ExpectedOrigin`, `KeyPolicy`, `RequestVerifier`, `ResponseVerifier`, `VerifiedRequest`, `VerifiedResponse`, `is_signed` |
| `roles` | the role ports the edges and façades take | `RequestConfirmer`, `RequestSigning`, `ResponseConfirmer`, `ResponseSigning` |
| `httpx` | `WebBotAuth`, the httpx clients it builds, and what they found out | `EXTENSION`, `UNSIGNED`, `ClientOptions`, `Expect`, `ResponseNotVerified`, `Unsigned`, `Verification`, `VerificationFailed`, `WebBotAuth`, `sent_request`, `verification`, `verified_response` |
| `keyfile` | a key kept in a `0600` file, safe on concurrent first boots | `load_or_create` |
| `sync` | the event loop synchronous code verifies on, and the sync façades | `SyncRequestVerifier`, `SyncResponseVerifier`, `SyncRuntime` |
| `asgi` | response signing, the directory route, and the opt-in verifier | `DEFAULT_REQUIRE`, `DEFAULT_SIGNING`, `MAX_BODY_BYTES`, `ASGIApp`, `DirectoryMiddleware`, `Message`, `OnUnsigned`, `Receive`, `RequirePolicy`, `RequireSignatureMiddleware`, `ResponseSigningMiddleware`, `Scope`, `Send`, `ServerEdge`, `SigningPolicy`, `request_url`, `verified_request` |
| `extra` | the `wba` extra and its install check | `EXTRA_MODULES` |
| `conformance` | the vectors, the runner, the adapters, and the generator | `PROTOCOL`, `VECTORS`, `Check`, `CommandAdapter`, `ConformanceReport`, `LibraryAdapter`, `keys_document`, `meets`, `published_key_ids`, `published_keys`, `run_conformance`, `scenarios_document`, `vector_operation`, `vectors` |

The package root exports the roles, the credentials, the ports, the errors,
the values, the message and directory types their signatures name, and the
profile's constants. Helpers stay in their modules. Importing the root, or
verifying with a `StaticKeyResolver`, never imports httpx. The root's
`__all__`:

```text
ALGORITHM
CLOCK_SKEW
CONFORMANCE_KEYS
DIRECTORY_MEDIA_TYPE
DIRECTORY_PATH
DIRECTORY_TAG
EXTRA_MODULES
MAX_LIFETIME
SAME_ORIGIN
SIGNATURE_TAG
Answer
Authority
CachePolicy
Clock
ConfigurationError
DirectoryFetcher
DirectoryPublisher
DirectoryResolver
DirectoryResponse
DirectoryStore
DirectoryStoreUnavailable
Ed25519Key
ExpectedOrigin
FetchLimits
HTTPRequest
HTTPResponse
HeaderLines
Headers
KeyId
KeyPolicy
KeyResolver
KeySet
MemoryDirectoryStore
MemoryNonceStore
NonceStore
NonceStoreUnavailable
Origin
PublishPolicy
Refusal
RequestAuthority
RequestConfirmer
RequestSigner
RequestSigning
RequestVerifier
ResolvedKey
ResponseConfirmer
ResponseSigner
ResponseSigning
ResponseVerifier
Seconds
ServerIdentity
SigningError
StaticKeyResolver
TargetURI
UnixSeconds
VerifiedDirectory
VerifiedRequest
VerifiedResponse
WebBotAuthError
accept_signature
is_signed
publish_directory
refusal_response
system_clock
target_uri
thumbprint
verify_directory
```

## Sign requests

Load or create your agent's key once, and sign each request as your origin:

```python
from iab_agentic_primitives.web_bot_auth import HTTPRequest, RequestSigner
from iab_agentic_primitives.web_bot_auth.keyfile import load_or_create

# Mode 0600, written atomically: workers that boot together share one key.
key = load_or_create("/var/lib/buyer/wba-key.jwk")
signer = RequestSigner.of(key, "https://buyer.example.com")

body = b'{"entry": {...}, "writes": {...}}'
request = HTTPRequest(
    "POST",
    "https://seller.example.com/proposals/prop_7/commit",
    {"Content-Type": "application/json"},
    body,
)
signed = signer.sign(request)
# signed.headers is request.headers followed by Content-Digest, Signature-Agent,
# Signature-Input, and Signature. The request itself is unchanged.
```

`RequestSigner.of(key, origin)` uses the default label (`sig1`), lifetime
(300 s), and clock. For any other, build a `SignerConfig(key, origin,
label=..., lifetime=..., clock=...)` and pass it to `RequestSigner(config)`;
one config serves a `ResponseSigner` too. A signer signs what the profile
requires and takes no parameter that departs from it.

When you pass your own `Content-Digest`, the signer keeps it only if it matches
the body and carries a `sha-256` member; otherwise it raises `SigningError` (a
`ValueError`), so a signer never sends what a verifier under the profile
refuses. A signer raises `SigningError` for every message it can't sign, such
as a body without `Content-Type` or a URL that isn't absolute.

Serve your directory so verifiers can find `key`. A `DirectoryPublisher`
publishes your identity's keys, and with an ASGI app, `DirectoryMiddleware`
serves it at the well-known path:

```python
from iab_agentic_primitives.web_bot_auth import DirectoryPublisher, ServerIdentity
from iab_agentic_primitives.web_bot_auth.asgi import DirectoryMiddleware

BUYER = ServerIdentity.from_config("https://buyer.example.com")
app = DirectoryMiddleware(app, DirectoryPublisher(BUYER.origin, [key]))
```

The directory is signed for the identity's own authority, whatever `Host` the
client sends and whatever port the server listens on: a verifier fetches
`https://<host>/.well-known/http-message-signatures-directory`. A framework
route answers the same with `await publisher.answer(now)`, an `Answer`.
`publish_directory(origin, keys, now=..., policy=...)` is the one function
that builds and signs a directory, and it publishes only what
`verify_directory` accepts: no keys, more than 16, or a key listed twice (one
keyfile in both rotation slots) raises `ConfigurationError`.

To rotate a key:

1. Publish the new key next to the old one.
2. Wait before you sign with the new key, at least the directory's
   `Cache-Control` `max-age` (`PublishPolicy(max_age=...)`, 300 s by default)
   plus the verifiers' `CachePolicy.max_cache_age` (300 s by default). A CDN
   can serve the old directory for `max-age`, and a verifier answers from its
   cached copy for `max_cache_age`, so a request signed earlier with the new
   key can be refused.
3. Sign with the new key.
4. Remove the old key after the last signatures made with it expire.

Pass a callable, sync or async, instead of a list so the directory shows the
current keys: the publisher reads it and signs the directory afresh on every
request.

```python
app = DirectoryMiddleware(app, DirectoryPublisher(BUYER.origin, keystore.published_keys))
```

When the callable returns no keys, the directory answers `503`; when it
returns keys `publish_directory` refuses, the answer raises
`ConfigurationError`.
`PublishPolicy(lifetime=..., max_age=...)` sets each signature's lifetime
(86400 s by default, whole seconds from 1) and the response's `max-age`.

## Verify requests in FastAPI

Verify at the boundary where you resolve every credential, and map the verified
origin to your own principal:

```python
from fastapi import Depends, FastAPI, Request
from fastapi.responses import Response

from iab_agentic_primitives.web_bot_auth import (
    DirectoryPublisher,
    DirectoryResolver,
    HTTPRequest,
    RequestVerifier,
    ResponseSigner,
    ServerIdentity,
    VerifiedRequest,
    WebBotAuthError,
    refusal_response,
)
from iab_agentic_primitives.web_bot_auth.asgi import ResponseSigningMiddleware, ServerEdge
from iab_agentic_primitives.web_bot_auth.fetcher import HttpxDirectoryFetcher
from iab_agentic_primitives.web_bot_auth.keyfile import load_or_create

SELLER = ServerIdentity.from_config("https://seller.example.com")
key = load_or_create("/var/lib/seller/wba-key.jwk")
verifier = RequestVerifier(
    # See "Fetch directories through your own client".
    DirectoryResolver(HttpxDirectoryFetcher(ssrf_guarded_client)),
    identity=SELLER,  # refuse a request signed for another server
    nonces=RedisNonceStore(redis),  # shared by every worker; MemoryNonceStore() serves one process
)


class Refused(Exception):
    def __init__(self, error: WebBotAuthError, has_body: bool) -> None:
        self.error, self.has_body = error, has_body


async def signature(request: Request) -> VerifiedRequest:
    body = await request.body()
    url = EDGE.request_url(request.scope)  # the public URL, behind a TLS proxy
    try:
        return await verifier.verify(HTTPRequest(request.method, url, request.headers.items(), body))
    except WebBotAuthError as e:
        raise Refused(e, has_body=bool(body)) from e


api = FastAPI()


@api.exception_handler(Refused)
async def refused(request: Request, exc: Refused) -> Response:
    # The status, JSON body, Accept-Signature, and Retry-After, as the middleware sends them.
    answer = refusal_response(exc.error, has_body=exc.has_body)
    return Response(answer.body, status_code=answer.status, headers=dict(answer.headers.items()))


@api.post("/proposals/{proposal_id}/commit")
async def commit(proposal_id: str, verified: VerifiedRequest = Depends(signature)):
    principal = lookup_principal(str(verified.origin))  # your records, not the library's
    ...


EDGE = ServerEdge(
    SELLER,
    signer=ResponseSigner.of(key, SELLER.origin),
    publisher=DirectoryPublisher(SELLER.origin, [key]),  # also served at the well-known path
    confirmer=verifier,  # checked: it verifies for SELLER's base URL
)
app = ResponseSigningMiddleware(api, EDGE)
```

The server's identity is a `ServerIdentity`, built once from configuration
with `ServerIdentity.from_config(base_url)`. `base_url` is the server's public
base URL, such as `https://seller.example.com` or
`https://seller.example.com:8443`: a lowercase scheme, a canonical host, and a
port only when it isn't the scheme's default, with no userinfo or path.
Anything else raises `ConfigurationError`. The identity's `origin` is always
`https://<host>`, so a server on `:8443` signs as `https://seller.example.com`.
Its `request_authority` is the base URL's, and it is the only authority a
request to the server may name: the edges rebuild every request URL on
`base_url`, so one fact sets both what they rebuild and what the verifier
accepts. A `ServerIdentity` checks itself when it is made. No helper reads the authority from the `Host` header:
the `Host` a client sends is a claim, and covering `@authority` only protects
a request or a directory when the server compares it with its own.

A `ServerEdge(identity, signer=..., publisher=..., confirmer=...)` is the one
owner of the server's identity at its edges. It checks, when it is made, that
the signer signs as `identity.origin`, that the publisher is that origin's,
that a publisher with a list of keys lists the signer's key (`signer.keyid`),
and that a confirmer, when given, verifies for the same `ServerIdentity`, base
URL included. A mismatch raises `ConfigurationError` at startup, rather than
every peer refusing later.

`asgi.request_url(identity, scope)` rebuilds a request's URL on
`identity.base_url` from the path and query bytes the client sent, under the
URL rule of "The target URI", so it is the URL `RequestSigner` signed. It
needs only the `ServerIdentity`, so a server that verifies requests and
signs nothing calls it with no edge, signer, or key.
`edge.request_url(scope)` is the same rebuild on the edge's identity. A
caller that verifies a response to `/p?#x` verifies it as the server rebuilt
`/p`, and `WebBotAuth` sends the URL as signed. Both middlewares rebuild it
the same way, so the request a server verifies and the request it binds its
response to are one. An application that verifies at its own boundary
passes that URL to its verifier: a verifier configured for another base URL
then refuses the request, instead of the server answering with responses
every peer refuses. Build the edge without a `confirmer` when the verifier
is made later, such as in a lifespan, and give that verifier
`identity=edge.identity`.

`ResponseSigningMiddleware(app, edge)` signs each buffered response with
`edge.signer` and binds it to the request, and serves `edge.publisher`'s
directory at the well-known path, as `DirectoryMiddleware` does. `DirectoryMiddleware` alone serves a party
that publishes keys but signs no responses. `SigningPolicy` holds the
options: it passes the responses its `streaming` rule names through
unsigned, since a stream can't be buffered to be signed. The rule is a
`rules.Streaming`, `rules.STREAMING` (`text/event-stream` alone) by default,
and the client takes the same type, so a seller and its buyers configure one
value. A response it can't sign under the profile, such as a body
without `Content-Type`, a second `http.response.start`, or a body over
`max_body_bytes` (1 MiB by default, declared or streamed), becomes a `500`
with error `response-unsignable`, and the middleware logs it, so a route never
sends an unsigned answer by accident. The app's scope doesn't advertise
`http.response.pathsend`, `http.response.zerocopy`, or
`http.response.trailers`, so a file response is sent as a body and signed.

If you prefer a middleware that verifies requests, wrap the app in the opt-in
`RequireSignatureMiddleware(app, edge, policy=RequirePolicy(prefixes=("/proposals",)))`,
and read the credential with `verified_request(request.scope)`. It verifies
with the edge's `confirmer`, any `RequestConfirmer`, such as a
`RequestVerifier` or your own wrapper around one; an edge without one raises
`ConfigurationError`. Wrap it in `ResponseSigningMiddleware` with the same
edge. It decides on an
unsigned request, one without `Signature-Input` (`is_signed`), from its
header before reading any body: refused `403` `signature-missing`, with
`Accept-Signature`, with
`RequirePolicy(on_unsigned="refuse")` (the default), passed on with no
credential with `"pass"`. A signed request whose signature fails is refused in
either mode. A body over `max_body_bytes` (1 MiB by default) is answered
`413`. The directory path is always let through unsigned.

The verifiers log each refusal at `INFO`, with the status, the reason, and the
origin and keyid when known, and the resolver logs each failed directory fetch
at `WARNING`, with the origin and the time until it is retried. No record
carries a nonce or a body.

### Share the nonce store across workers

`MemoryNonceStore` holds nonces in one process. When several workers verify
requests for one deployment, back `NonceStore` with shared storage. The method
must record the pair and return `True` when it's new, atomically, and keep it
for `ttl` seconds. When it can't record the pair, for example because the
store is full or down, it raises `NonceStoreUnavailable`, and the verifier
answers `503` with `nonce-store-unavailable` instead of `429`: the nonce isn't
a replay, and a client that signs again would only be refused again. Any
other error is a bug and propagates. `MemoryNonceStore` raises
`NonceStoreUnavailable` when full. `testing.check_nonce_store(make)` checks a
store against this contract.

```python
import redis.exceptions

from iab_agentic_primitives.web_bot_auth import NonceStoreUnavailable, Origin, Seconds


class RedisNonceStore:
    def __init__(self, redis):
        self.redis = redis

    async def check_and_record(self, origin: Origin, nonce: str, ttl: Seconds) -> bool:
        key = f"wba:nonce:{origin}:{nonce}"
        try:
            return bool(await self.redis.set(key, 1, nx=True, ex=max(1, ttl)))
        except redis.exceptions.RedisError as e:
            raise NonceStoreUnavailable(str(e)) from e
```

### Fetch directories through your own client

`DirectoryResolver` composes two ports: a `DirectoryFetcher`, which fetches an
origin's `DirectoryResponse`, and a `DirectoryStore`, which keeps plain data,
each origin's `DirectoryResponse` and failure deadline, and decides nothing.
`put(origin, response)` and `get(origin)` take and return the response's URL,
status, field lines, body bytes, and `fetched_at`, so a store shared by
several workers, such as Redis, serializes it with `response.document()`.
The resolver owns the rules. It verifies a directory once, when it enters the
process: from the fetcher, or as stored bytes it hasn't verified, such as a
response another worker wrote. It holds each verified directory in memory by
origin and answers from it while the store returns the response it was
verified from, so a cache hit or an unknown `kid` costs no verification.
Stored bytes are verified for the origin it asked for, at the response's
`fetched_at`, and under its own `FetchLimits`; a response that doesn't verify
is a cache miss, never a refusal. A directory answers only until its `expires`. The resolver's own
`clock` (the system clock by default) stamps each fetch and judges the cache:
a directory is fresh for `CachePolicy.max_cache_age` (300 s), and a failed
fetch isn't retried for `CachePolicy.negative_ttl` (60 s). The time a verifier
judges at decides only whether the directory has expired by then
(`directory-window`); it never stamps or ages the cache. The defaults are
`HttpxDirectoryFetcher` and `MemoryDirectoryStore`. Concurrent resolves of
one origin share one fetch. Only a refusal counts as a failed fetch; any
other error propagates and leaves no negative entry.
`testing.check_directory_store(make)` checks a store against its contract.

A store whose backend is down, such as Redis in an outage, raises
`DirectoryStoreUnavailable` from the method that failed, and nothing else;
any other error is a bug that propagates. The resolver reads a failed `get`
or `failure_until` as nothing stored, so it fetches the directory afresh,
and it logs a failed `put` or `record_failure` at `WARNING` and goes on, so
the refusal of a failed fetch stays the refusal. An outage costs a fetch per
resolve, never a `500`, and the store is used again once it answers.

```python
import json

import redis.exceptions
from iab_agentic_primitives.web_bot_auth import DirectoryStoreUnavailable


class RedisDirectoryStore:
    async def put(self, origin, response):
        try:
            await self.redis.set(f"wba:directory:{origin}", json.dumps(response.document()))
        except redis.exceptions.RedisError as e:
            raise DirectoryStoreUnavailable(str(e)) from e

    ...  # get, record_failure, and failure_until wrap their calls the same way
```

A resolver belongs to the event loop it first resolves on, as its fetches in
flight and its client's connections do. A resolve on another loop raises
`ConfigurationError`, and so does one after `aclose()`. Synchronous code
reaches a resolver through one `SyncRuntime`.

`HttpxDirectoryFetcher` fetches through one `httpx.AsyncClient`. Pass your
client to it; the fetcher never closes a client or a transport it was given.
Without one, it builds one client, once, and the resolver that built it closes
it in `aclose()` or at the end of `async with`. The profile's fetch rules apply
on any client, and a redirect is never followed. Address policy is left to the
client: a production verifier injects a client that guards against server-side
request forgery, for example by checking the resolved address and connecting
only to the address it checked. A guarding transport raises
`fetcher.BlockedAddress`, an `httpx.TransportError`, for an address it
refuses; the fetch is then refused with `directory-unavailable` and held back
like any failed fetch.

To trust a test CA, inject a client with `verify` set, or pass a `TLSConfig`
with `extra_ca` (added to the system trust store) or `ca_bundle` (in place of
it) for the client the fetcher builds. TLS options together with an injected
client raise `ConfigurationError`:

```python
import httpx

from iab_agentic_primitives.web_bot_auth import CachePolicy, DirectoryResolver
from iab_agentic_primitives.web_bot_auth.fetcher import HttpxDirectoryFetcher, TLSConfig

resolver = DirectoryResolver(
    HttpxDirectoryFetcher(httpx.AsyncClient(verify="/certs/test-ca.pem")),
    cache=CachePolicy(max_cache_age=60),
)
async with DirectoryResolver(HttpxDirectoryFetcher(tls=TLSConfig(extra_ca="/certs/test-ca.pem"))) as resolver:
    ...
```

In tests, serve signed directories from data with
`testing.served_resolver({origin: answer}, clock=...)`, or
`HttpxDirectoryFetcher(transport=directory_transport({...}))`. Each streams
the body, so the fetch runs the same bounded read as over the network.

### Keep the directory you verified against

OpenProposal § 4.1.2 asks a party to keep the bytes of the key directory it
verified against. `VerifiedRequest.directory` is that directory, when the
resolver provides it, as `DirectoryResolver` does. Its `origin` is the
identity it belongs to, and its `evidence` is the `DirectoryResponse` the keys
were verified from: `url` (always `origin.directory_url`), `status`,
`headers`, `body` (the exact bytes verified, as read off the wire under the
256 KiB cap), and `fetched_at`. `evidence.document()` returns them as
JSON-ready data, with the body both as text and, losslessly, as
`body_base64`. `verify_directory` verifies a stored response again later, and
`StaticKeyResolver` takes the keys it returns. A verified directory or
credential can't be copied or pickled: verify the evidence again.

```python
verified = await verifier.verify(HTTPRequest(method, url, headers, body))
store_evidence(str(verified.origin), verified.keyid, verified.directory.evidence.document())

stored = DirectoryResponse(origin.directory_url, status, headers, body, fetched_at)
directory = verify_directory(stored, origin, fetched_at)
offline = StaticKeyResolver({origin: directory.keys})
```

## Call a seller with httpx

`WebBotAuth` is how a client signs and verifies, and the library builds the
client from it: `auth.async_client()` returns an `httpx.AsyncClient`, and
`auth.client()` an `httpx.Client`. Signing happens in the client's one
transport, which wraps the transport you pass (`httpx.AsyncHTTPTransport()`
by default). It signs each request on the wire for its own URL and, with a
`ResponseVerifier`, verifies the signed response to it and its binding to the
exact request sent. The client always returns the response, with what it
found out stored in it: `verification(response)` reads a `VerifiedResponse`,
`Unsigned`, or `VerificationFailed` (a `reason`, a `Refusal` member, and a
`detail`), `verified_response(response)` reads the `VerifiedResponse` alone,
and `sent_request(response)` returns the request as signed and sent, an
`HTTPRequest`, which the response is bound to. `response.request` is the
request before signing.

```python
from iab_agentic_primitives.web_bot_auth import DirectoryResolver, RequestSigner, ResponseVerifier
from iab_agentic_primitives.web_bot_auth.httpx import WebBotAuth, sent_request, verified_response

async with DirectoryResolver() as resolver:
    auth = WebBotAuth(RequestSigner.of(key, "https://buyer.example.com"), ResponseVerifier(resolver))
    async with auth.async_client(timeout=10) as client:
        response = await client.post("https://seller.example.com/proposals/prop_7/commit", json=commit)
        verified = verified_response(response)  # a VerifiedResponse
        str(verified.origin), verified.origin.host  # "https://seller.example.com", "seller.example.com"
        sent = sent_request(response)  # the signed request, to keep beside the response
```

The client takes the httpx settings `ClientOptions` lists, such as
`headers`, `timeout`, `base_url`, `auth` (for a bearer token, say), and
`follow_redirects`. Connection settings, such as TLS, a proxy, or HTTP/2,
go on the transport you pass. Any other setting, such as `mounts` or
`proxy`, would route requests around the signing transport, so it raises
`ConfigurationError`, and the client reads no proxy from the environment, for
the same reason. `WebBotAuth` is not an
`httpx.Auth`: `httpx.AsyncClient(auth=WebBotAuth(...))` raises `TypeError`.

The client decides from the response's header lines whether it is signed
(`is_signed`). It returns an unsigned response unread, so an event stream
arrives as the seller sends it, and reads and verifies a signed one. A
response must be signed by `https://<host>` of the host each request went
to, whatever the URL's scheme and port, which `Origin.of_url` maps through
IDNA (`expect="same-origin"`, the `ResponseVerifier`'s own default).
`expect=Origin.parse(...)` names another identity, and `expect=None` accepts
any.

By default (`require_signed_response`), a `2xx` that isn't verified, unsigned
or failed, raises `ResponseNotVerified`. Its `response` is the response, body
read, and its `verification` says why, so a commit the seller executed is
never lost, only unverified. An unsigned response of a media type the
client's `streaming` rule names, which a server streams unsigned, is let
through: `rules.STREAMING` (`text/event-stream`) by default. A seller that
also streams NDJSON configures
`SigningPolicy(streaming=Streaming(("text/event-stream", "application/x-ndjson")))`,
and its buyers pass the same `Streaming` as `WebBotAuth(..., streaming=...)`.
Every response that isn't a `2xx` is let through too: an unsigned `403`, `429`, or `503` from a verifier in front of the
seller, the server's own `500` `response-unsignable`, a redirect, or a
proxy's `502` reaches you with its own status, `Retry-After`, and body, and
`verification()` says `Unsigned`. With `require_signed_response=False`, no
response raises. The settings are fixed when the `WebBotAuth` is made.

Each wire hop is signed for its own URL, so a signature never reaches
another URI. A redirect comes back as it is, with `follow_redirects=False`
(httpx's default), and `response.next_request` carries no signature;
`await client.send(response.next_request)` signs it for its new URL. With
`follow_redirects=True`, the client signs each redirected request for its
new URL as it sends it, and verifies each hop's response for the host that
hop went to. Following a `307` or `308` sends the body to the new URL too, as
httpx does; leave redirects off to decide on each one.

For a call that authenticates another way, such as a bearer token, build the
configuration with `WebBotAuth.verify_only(response_verifier)`. Its client
sends the request unsigned and still verifies the signed response, which
needs no binding because the request carried no signature.
`verified_response(response)` returns it as before, with `bound` set to
`False`. When it verifies responses, the client sends `Accept-Encoding:
identity`, because `Content-Digest` covers the content as sent.

`WebBotAuth` takes any `RequestSigning` and `ResponseConfirmer`. A
synchronous client verifies on a `SyncRuntime` you own, passed as
`runtime=`; without one, `auth.client()` raises `ConfigurationError`. With a
runtime, async clients verify on it too, so one configuration serves an
`httpx.Client` and an `httpx.AsyncClient` alike. It starts no thread, and
each client closes the transport it wraps:

```python
from iab_agentic_primitives.web_bot_auth.sync import SyncRuntime

resolver = DirectoryResolver()
with SyncRuntime(resolver) as runtime:  # closes the resolver on its own loop
    auth = WebBotAuth(signer, ResponseVerifier(resolver), runtime=runtime)
    with auth.client() as client:
        response = client.post(url, json=commit)
```

### Verify from synchronous code

The verifiers are async, and a resolver belongs to one event loop. For
synchronous code, create one `SyncRuntime` where you create the resolver: it
runs one event loop on its own thread, under `asyncio.Runner`, for its whole
life. Wrap a verifier in a façade from
`iab_agentic_primitives.web_bot_auth.sync` that runs on it:

```python
from iab_agentic_primitives.web_bot_auth.sync import SyncRequestVerifier, SyncRuntime

with SyncRuntime(resolver) as runtime:
    sync = SyncRequestVerifier(verifier, runtime=runtime)
    verified = sync.verify(HTTPRequest(method, url, headers, body))
```

Every façade and auth that verifies through the same resolver takes the same
runtime. `SyncResponseVerifier` does the same for a `ResponseVerifier`.
Closing the runtime cancels the calls still running, which raise
`ConfigurationError`, closes the resolver it owns, and stops the thread. A
façade call made inside a running event loop raises `RuntimeError` instead of
blocking that loop; await `verify()` there, or `runtime.run_async(...)` for a
resolver the runtime owns.

A runtime belongs to the process that made it. A forked child inherits the
runtime but not its thread, so a call there raises `ConfigurationError` at
once instead of waiting on a loop nothing runs. Under a pre-fork server, such
as gunicorn with `--preload` or uWSGI without lazy apps, create the runtime
and its resolver in each worker, for example in a post-fork hook.

## Conformance vectors

The vectors are package data in `web_bot_auth/vectors/`; their
[README](src/iab_agentic_primitives/web_bot_auth/vectors/README.md) states the
format. Three published test keys sign them, each derived from the seed
`sha256("aamp-wba-test/<name>")`, at the fixed clock 2026-11-07T12:00:00Z.
Request bodies are an OpenProposal `Commit` posted to
`https://seller.example.com/proposals/prop_7/commit`, and the response body is its
`CommitResult`.

- To grade an implementation, run
  `python -m iab_agentic_primitives.web_bot_auth.conformance run --adapter "<command>"`.
  Without `--adapter`, the command grades this library. The adapter protocol is
  `conformance/PROTOCOL.md`, which ships in the package.
- To regenerate the vectors, run
  `python -m iab_agentic_primitives.web_bot_auth.conformance --regenerate`, the
  same flag as the repository's own conformance CLI. It writes the
  generator's own files and deletes a vector it no longer produces; nothing
  else is touched. `--check` names each file that differs from the
  generator, is missing, or isn't generated, and exits 1 when there is one.

The documents are typed (`conformance.models`) and read strictly: JSON
booleans and integers, no undeclared member, identities as `Origin`. Each
vector carries the one operation that grades it, ready to send to an adapter.
An outcome is a pass or a refusal; a refusal names its `rule`, and its status
is the rule's. `vectors/refusals.json`, generated from `Refusal`, is the shared
vocabulary, and a result passes only with the expected rule. A profile
message is signed by `RequestSigner`, `ResponseSigner`, or `publish_directory`.
A vector or scenario that departs from the profile is signed raw by the
conformance interpreter, through a private signer that only `conformance`
imports.

Each vector and scenario names a tier. Tier `wba` means Cloudflare's library,
with no profile rules, already gives the expected outcome. Tier `profile` means
the bare library gives the opposite outcome, so only the profile's rules decide
it.

## Interop with Cloudflare's library

`tests/web_bot_auth/interop/` is a Node harness pinned to `web-bot-auth@0.2.0`,
`http-message-sig@0.3.0`, and `structured-headers@2.0.3` from npm. It runs this
library through the adapter protocol in `conformance/PROTOCOL.md`, and it
records three verdicts for each message: this library's, Cloudflare's library with the profile's rules
(`policy.mjs`), and Cloudflare's library as shipped. web-bot-auth 0.2.0 has no
response verifier, so the harness verifies responses with `http-message-sig`.
The harness hands Cloudflare's library each request URL as a native `Request`
carries it, the WHATWG serialization without the fragment, which is what a
Worker signs and verifies. For each target-URI case of `scenarios.json`, both
sides sign a `GET` to the URL as written, and the other side verifies it.

From the repository root:

```bash
make interop
```

It installs the harness's Node dependencies and runs the tests marked
`interop`, which `uv run pytest` deselects by default; CI runs them in their
own job. Once selected, they fail, rather than skip, when `node` or those
dependencies are absent.

All 128 checks pass. A refusal reads as its status and rule, from the shared
vocabulary in `vectors/refusals.json`; the harness requires the Python and the
Cloudflare + profile verdicts to match the expected status and rule. A `-` means
the check has no verdict from that side, for example a message the other side
signed. `tests/web_bot_auth/test_wba_interop.py` compares this table with
`run.mjs --json`.

| Suite | Check | Tier | Python | Cloudflare + profile | Cloudflare bare |
| :--- | :--- | :--- | :--- | :--- | :--- |
| vectors | positive/001-single-signer-request | wba | pass | pass | pass |
| vectors | positive/002-signed-directory-response | wba | pass | pass | pass |
| vectors | positive/003-signed-response | wba | pass | pass | pass |
| vectors | positive/004-origin-form-request | wba | pass | pass | pass |
| vectors | positive/005-target-uri-array-brackets | wba | pass | pass | pass |
| vectors | positive/006-target-uri-pipe | wba | pass | pass | pass |
| vectors | positive/007-target-uri-braces | wba | pass | pass | pass |
| vectors | positive/008-target-uri-caret | wba | pass | pass | pass |
| vectors | positive/009-target-uri-escaped-space | wba | pass | pass | pass |
| vectors | positive/010-target-uri-default-port | wba | pass | pass | pass |
| vectors | positive/011-target-uri-mixed-case | wba | pass | pass | pass |
| vectors | positive/012-target-uri-dot-segments | wba | pass | pass | pass |
| vectors | negative/001-missing-nonce | profile | 403 nonce-missing | 403 nonce-missing | pass |
| vectors | negative/002-replayed-nonce | profile | 429 nonce-replayed | 429 nonce-replayed | pass |
| vectors | negative/003-signature-agent-not-covered | wba | 403 signature-agent-not-covered | 403 signature-agent-not-covered | 403 signature-agent-not-covered |
| vectors | negative/004-key-not-in-named-directory | wba | 403 key-not-found | 403 key-not-found | 403 key-not-found |
| vectors | negative/005-malformed-signature-agent | wba | 400 parse | 400 parse | 400 parse |
| vectors | negative/006-two-signatures | wba | 403 multiple-signatures | 403 multiple-signatures | 403 signature-invalid |
| vectors | negative/007-response-not-bound | profile | 403 response-not-bound | 403 response-not-bound | pass |
| vectors | negative/008-key-not-in-named-directory-warm-cache | wba | 403 key-not-found | 403 key-not-found | 403 key-not-found |
| live | Python signs the directory, Cloudflare verifies | wba | - | pass | pass |
| live | Python signs the directory, body changed, both refuse | profile | 403 directory-content-digest | 403 directory-content-digest | pass |
| live | Cloudflare signs the directory, Python verifies | - | pass | - | - |
| live | Python signs POST, Cloudflare verifies | wba | - | pass | pass |
| live | Python signs POST, URL changed, both refuse | wba | 403 signature-invalid | 403 signature-invalid | 403 signature-invalid |
| live | Python signs POST, body changed, both refuse | profile | 403 content-digest | 403 content-digest | pass |
| live | Cloudflare signs POST with the key URL, Python verifies | wba | pass | pass | pass |
| live | Cloudflare signs POST with the bare origin, Python verifies | wba | pass | pass | pass |
| live | Cloudflare signs POST, Python verifies it twice | - | 429 nonce-replayed | 429 nonce-replayed | - |
| live | Cloudflare signs POST, URL changed, both refuse | wba | 403 signature-invalid | 403 signature-invalid | 403 signature-invalid |
| live | Cloudflare signs POST, body changed, both refuse | profile | 403 content-digest | 403 content-digest | pass |
| live | Python signs GET, Cloudflare verifies | wba | - | pass | pass |
| live | Python signs GET, URL changed, both refuse | wba | 403 signature-invalid | 403 signature-invalid | 403 signature-invalid |
| live | Cloudflare signs GET with the key URL, Python verifies | wba | pass | pass | pass |
| live | Cloudflare signs GET with the bare origin, Python verifies | wba | pass | pass | pass |
| live | Cloudflare signs GET, Python verifies it twice | - | 429 nonce-replayed | 429 nonce-replayed | - |
| live | Cloudflare signs GET, URL changed, both refuse | wba | 403 signature-invalid | 403 signature-invalid | 403 signature-invalid |
| live | U01 Array brackets in the query: its wire URL is the WHATWG serialization | - | - | pass | - |
| live | Python signs U01 Array brackets in the query, Cloudflare verifies | wba | - | pass | pass |
| live | Cloudflare signs U01 Array brackets in the query, Python verifies | wba | pass | pass | pass |
| live | U02 A pipe in the query: its wire URL is the WHATWG serialization | - | - | pass | - |
| live | Python signs U02 A pipe in the query, Cloudflare verifies | wba | - | pass | pass |
| live | Cloudflare signs U02 A pipe in the query, Python verifies | wba | pass | pass | pass |
| live | U03 Braces in the query: its wire URL is the WHATWG serialization | - | - | pass | - |
| live | Python signs U03 Braces in the query, Cloudflare verifies | wba | - | pass | pass |
| live | Cloudflare signs U03 Braces in the query, Python verifies | wba | pass | pass | pass |
| live | U04 A caret in the query: its wire URL is the WHATWG serialization | - | - | pass | - |
| live | Python signs U04 A caret in the query, Cloudflare verifies | wba | - | pass | pass |
| live | Cloudflare signs U04 A caret in the query, Python verifies | wba | pass | pass | pass |
| live | U05 An escaped space in the path: its wire URL is the WHATWG serialization | - | - | pass | - |
| live | Python signs U05 An escaped space in the path, Cloudflare verifies | wba | - | pass | pass |
| live | Cloudflare signs U05 An escaped space in the path, Python verifies | wba | pass | pass | pass |
| live | U06 The default port: its wire URL is the WHATWG serialization | - | - | pass | - |
| live | Python signs U06 The default port, Cloudflare verifies | wba | - | pass | pass |
| live | Cloudflare signs U06 The default port, Python verifies | wba | pass | pass | pass |
| live | U07 A mixed-case scheme and host: its wire URL is the WHATWG serialization | - | - | pass | - |
| live | Python signs U07 A mixed-case scheme and host, Cloudflare verifies | wba | - | pass | pass |
| live | Cloudflare signs U07 A mixed-case scheme and host, Python verifies | wba | pass | pass | pass |
| live | U08 Dot segments in the path: its wire URL is the WHATWG serialization | - | - | pass | - |
| live | Python signs U08 Dot segments in the path, Cloudflare verifies | wba | - | pass | pass |
| live | Cloudflare signs U08 Dot segments in the path, Python verifies | wba | pass | pass | pass |
| live | Python signs a response, Cloudflare verifies it and its binding | wba | - | pass | pass |
| live | Python signs a response, presented with another request, both refuse | wba | 403 signature-invalid | 403 signature-invalid | 403 signature-invalid |
| live | Cloudflare signs a response, Python verifies it and its binding | - | pass | pass | - |
| live | Cloudflare signs a response, status changed, both refuse | wba | 403 signature-invalid | 403 signature-invalid | 403 signature-invalid |
| scenarios | R00 Baseline | wba | pass | pass | pass |
| scenarios | R01 Body changed after signing | profile | 403 content-digest | 403 content-digest | pass |
| scenarios | R02 Covered header changed after signing | wba | 403 signature-invalid | 403 signature-invalid | 403 signature-invalid |
| scenarios | R03 Expired | wba | 403 expired | 403 expired | 403 expired |
| scenarios | R04 created 120 seconds in the future | wba | 403 created-in-future | 403 created-in-future | 403 created-in-future |
| scenarios | R05 created 30 seconds in the future | profile | pass | pass | 403 created-in-future |
| scenarios | R06 Lifetime of 3600 seconds | profile | 403 lifetime | 403 lifetime | pass |
| scenarios | R07 Nonce of 32 bytes | wba | 403 nonce-invalid | 403 nonce-invalid | 403 nonce-invalid |
| scenarios | R08 Nonce in canonical padded base64 | wba | pass | pass | pass |
| scenarios | R09 Nonce in padded base64url | wba | 403 nonce-invalid | 403 nonce-invalid | 403 nonce-invalid |
| scenarios | R10 No alg parameter | wba | 403 alg | 403 alg | 403 alg |
| scenarios | R11 alg other than ed25519 | wba | 403 alg | 403 alg | 403 alg |
| scenarios | R12 GET covering only @authority and the Signature-Agent member | profile | 403 components | 403 components | pass |
| scenarios | R13 Body without content-digest covered | profile | 403 components | 403 components | pass |
| scenarios | R13b Body without content-type covered | profile | 403 components | 403 components | pass |
| scenarios | R14 tag other than web-bot-auth | wba | 403 signature-missing | 403 signature-missing | 403 signature-invalid |
| scenarios | R23 Components in another order | wba | pass | pass | pass |
| scenarios | R24 No nonce | profile | 403 nonce-missing | 403 nonce-missing | pass |
| scenarios | R25 GET with the profile's components | wba | pass | pass | pass |
| scenarios | R15 Bare-string Signature-Agent | profile | 400 parse | 400 parse | pass |
| scenarios | R16 http:// key URL | profile | 403 signature-agent-value | 403 signature-agent-value | pass |
| scenarios | R16b http:// origin | profile | 403 signature-agent-value | 403 signature-agent-value | pass |
| scenarios | R17 Path other than the well-known path | profile | 403 signature-agent-value | 403 signature-agent-value | pass |
| scenarios | R17a Well-known path with a trailing / | profile | 403 signature-agent-value | 403 signature-agent-value | pass |
| scenarios | R17b Well-known path in another case | profile | 403 signature-agent-value | 403 signature-agent-value | pass |
| scenarios | R17c Origin with a trailing / | profile | 403 signature-agent-value | 403 signature-agent-value | pass |
| scenarios | R17d Uppercase host | profile | 403 signature-agent-value | 403 signature-agent-value | pass |
| scenarios | R17e Explicit :443 | profile | 403 signature-agent-value | 403 signature-agent-value | pass |
| scenarios | R17f Other port | profile | 403 signature-agent-value | 403 signature-agent-value | pass |
| scenarios | R17g Query | profile | 403 signature-agent-value | 403 signature-agent-value | pass |
| scenarios | R17h Fragment | profile | 403 signature-agent-value | 403 signature-agent-value | pass |
| scenarios | R17i Userinfo | profile | 403 signature-agent-value | 403 signature-agent-value | pass |
| scenarios | R17j Origin with a query | profile | 403 signature-agent-value | 403 signature-agent-value | pass |
| scenarios | R18 Member with type=jwks_uri | profile | 403 signature-agent-parameters | 403 signature-agent-parameters | pass |
| scenarios | R18c Origin member with type=directory as a Token | profile | 403 signature-agent-parameters | 403 signature-agent-parameters | pass |
| scenarios | R18d Key URL member with type="directory" as a String | profile | 403 signature-agent-parameters | 403 signature-agent-parameters | pass |
| scenarios | R18e Bare origin, the draft -00 form | wba | pass | pass | pass |
| scenarios | R19 Signature-Agent member keyed differently from the label | profile | 403 signature-agent-missing | 403 signature-agent-missing | pass |
| scenarios | R21 Two Signature-Agent members, one signature | profile | 403 multiple-signature-agents | 403 multiple-signature-agents | pass |
| scenarios | R22 Label other than sig1 | wba | pass | pass | pass |
| scenarios | R20 Second signature with tag cdn-edge | profile | pass | pass | 403 signature-invalid |
| scenarios | R20b Second member with tag cdn-edge whose Signature value is a token | wba | 400 parse | 400 parse | 400 parse |
| scenarios | R26 Signed for seller.example.com, verified by seller-b.example.com | profile | 403 authority | 403 authority | pass |
| scenarios | D00 Baseline | wba | pass | pass | pass |
| scenarios | D01 Signed for another authority | wba | 403 directory-signature | 403 directory-signature | 403 directory-signature |
| scenarios | D02 Body changed after signing | profile | 403 directory-content-digest | 403 directory-content-digest | pass |
| scenarios | D03 Unsigned | wba | 403 directory-one-signature-per-key | 403 directory-one-signature-per-key | 403 directory-signature |
| scenarios | D04 Signature created in the future | wba | 403 directory-window | 403 directory-window | 403 directory-window |
| scenarios | D05 Covers "@authority";req only | profile | 403 directory-components | 403 directory-components | pass |
| scenarios | D06 Expired signature | wba | 403 directory-window | 403 directory-window | 403 directory-window |
| scenarios | D07 Extra P-256 key that does not sign | wba | pass | pass | pass |
| scenarios | D08 Media type application/json | profile | 403 directory-media-type | 403 directory-media-type | pass |
| scenarios | D09 Signature expired one second ago | wba | 403 directory-window | 403 directory-window | 403 directory-window |
| scenarios | D10 Signature that expires now | wba | pass | pass | pass |
| scenarios | P00 Baseline, bound to the signed request | wba | pass | pass | pass |
| scenarios | P01 Body changed after signing | profile | 403 content-digest | 403 content-digest | pass |
| scenarios | P02 Status changed after signing | wba | 403 signature-invalid | 403 signature-invalid | 403 signature-invalid |
| scenarios | P03 Signed request, response not bound to it | profile | 403 response-not-bound | 403 response-not-bound | pass |
| scenarios | P04 Bound to another request | wba | 403 signature-invalid | 403 signature-invalid | 403 signature-invalid |
| scenarios | P05 @status not covered | profile | 403 components | 403 components | pass |
| scenarios | P06 Lifetime of 3600 seconds | profile | 403 lifetime | 403 lifetime | pass |
| scenarios | P07 Unsigned request, response without a binding | wba | pass | pass | pass |
| scenarios | P08 Response signed with tag other than web-bot-auth | profile | 403 signature-missing | 403 signature-missing | pass |

## Differences from Cloudflare web-bot-auth 0.2.0

The bare library differs from the profile in these ways. Each is a tier
`profile` row above:

- It accepts any `http` or `https` URL as a `Signature-Agent` value and leaves
  key lookup to the caller's resolver. A resolver that derives the origin, as
  -00 does, accepts an `http` URL (R16, R16b), another path, a trailing `/`, a
  query, a fragment, userinfo, an uppercase host, or a port (R17 to R17j). WBA
  requires `https`, so R16 is a library gap.
- It accepts members with parameters, including `type=directory` and
  `type=jwks_uri` (R18, R18c, R18d), which draft -01 says a verifier ignores.
- It accepts a covered `Signature-Agent` member keyed to a label other than the
  signature's (R19), and two `Signature-Agent` members (R21).
- It accepts the legacy bare-string `Signature-Agent` (R15). WBA -00 lets a
  verifier accept it; the profile parses the field only as a Dictionary, so the
  request gets `400`.
- It doesn't compare `Content-Digest` with the body, in requests (R01),
  directories (D02), or responses (P01).
- It allows no clock skew by default (R05) and allows a 24-hour lifetime (R06).
- It treats the nonce as optional (R24, negative/001) and keeps no replay state
  (negative/002).
- It needs only one of `@authority` or `@target-uri` (R12), and no
  `content-type` or `content-digest` with a body (R13, R13b).
- It needs a label to pick one signature when a request carries two, whatever
  their tags (R20).
- It doesn't check the directory media type (D08) or that a directory signature
  covers `content-digest` (D05).
- It has no response profile, so it doesn't require the request binding (P03,
  negative/007) or `@status` (P05).

## Draft -00 and draft -01

The profile follows draft-ietf-webbotauth-httpsig-protocol-01 (29 October
2026): signers send the key directory URL that -01 defines, and verifiers also
accept the bare origin a -00 signer sends. Cloudflare's library 0.2.0, which
the interop harness runs, implements -00. Draft -01 changes the following:

| Area | -00 | -01 | Effect on the profile |
| :--- | :--- | :--- | :--- |
| Key discovery (§ 5.5) | A `directory` member value is an origin; the verifier fetches the well-known path. Types `directory`, `jwks_uri`, and `cimd` | "A verifier fetches that URL directly and interprets the response as a JWK Set." The types are removed | Resolved: a signer sends the key directory URL, which a -01 verifier fetches directly. The verifier also accepts the bare origin a -00 signer sends and fetches the same URL. |
| Origin-only value (§ 5.5) | "The member value MUST be the ASCII serialization of an origin as defined in Section 6.2 of [ORIGIN], and a verifier MUST ignore a member carrying anything else (an empty path / MAY be accepted though)." A trailing `/` was therefore allowed | Removed | A strict -00 verifier ignores the key URL a signer now sends. Cloudflare's 0.2.0 library doesn't enforce this rule: it passes the URL to the caller's resolver, which can derive the origin. |
| Member parameters (§ 5.2.1) | A member carries `type`; absent means `directory` | "Dictionary members MUST NOT carry parameters. A verifier MUST ignore a member that carries parameters." | Applied: signers send no parameters, and the verifier ignores a member that carries any. Cloudflare 0.2.0 interop doesn't need `type=directory`: its signer sends none. |
| `kid` at the well-known URL (§ 5.5) | `kid` is the thumbprint | `kid` MAY appear, and MUST be the thumbprint | Compatible. The library already refuses a directory `kid` that isn't the thumbprint. |
| JWK `alg` (§ 5.5.1) | Restricted to HTTP Signature Algorithms | A JWK `alg` follows JOSE; the signature `alg` follows HTTP Signature Algorithms | Compatible. The library publishes no JWK `alg`. |
| Requesting a signature (§ 5.3) | An origin MAY request one | An origin that expects one SHOULD request it with `Accept-Signature`, and SHOULD include `Accept-Signature` on the 403 | Compatible. Use `accept_signature()`. |
| Distinct agents (§ 6.5) | Distinct keys and directories | Distinct `Signature-Agent` URLs, keys, and directories for a crawler and a user-directed agent | Compatible with one agent per origin. |
| Test vectors (Appendix E) | Member `agent2`, covered as `key="agent2"` under label `sig2` | Member keyed to the label (`sig2`) | Compatible: the profile requires the member keyed to the label. |
| Section numbers | C.10 Proxies and Intermediaries | C.11 (C.10 is now Rollout and Fallback) | Re-cite C.10 as C.11. Sections 5.x, 6.x, and B.1 keep their numbers. |

The directory media type, directory signatures (Appendix B.1), the legacy
bare-string form, `400` and `429`, and the nonce rules don't change.

## Choices the profile leaves open

The library makes these choices where the profile is silent:

- A body is non-empty bytes. A request with an empty body covers no
  `content-digest`.
- The 60 s clock skew applies to directory signatures too, a deliberate
  relaxation of Appendix B.1, which rejects any future `created`.
- A directory signature lasts 24 hours by default (`PublishPolicy(lifetime=...)`).
  The resolver treats a fetched directory as fresh for 300 s
  (`CachePolicy.max_cache_age`), so a new or removed key shows within
  five minutes, and an unknown `keyid` never triggers a fetch while the
  directory is fresh. The `Cache-Control` header on the directory is not read.
- A stale directory still answers while a refetch fails, until its earliest
  `expires`; an expired entry is never used.
- A failed fetch blocks fetches of that origin for 60 s, so a broken directory
  isn't fetched on every request, whatever `keyid` a request names.
- The verifier records a nonce only after the signature verifies, so a forged
  request can't burn a real nonce.
- A response signature may carry a nonce; the verifier neither requires nor
  records it.
- `MemoryNonceStore` refuses new nonces when full, with `503`, instead of
  forgetting old ones, and keeps each pair for at least one second.
- A request whose `web-bot-auth` signature has no `Signature` member gets an
  unbound response.
- A host in `Signature-Agent` is lowercase ASCII; an internationalized name
  must arrive as its A-label. A dotted-decimal IPv4 or bracketed IPv6 literal
  is a host; a name whose last label is a number, such as `127.1` or
  `2130706433`, isn't.
- Members with parameters are dropped before the verifier counts members, so
  an ignored member doesn't make a request carry two.
