"""DirectoryResolver: fetch rules, the store's rules, one fetch per origin, and the fetcher."""

from __future__ import annotations

import asyncio
import base64
import json
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Callable, Sequence
from functools import partial
from pathlib import Path
from typing import TypedDict

import httpx
import pytest

from iab_agentic_primitives.web_bot_auth import (
    CONFORMANCE_KEYS,
    DIRECTORY_PATH,
    Answer,
    CachePolicy,
    Clock,
    ConfigurationError,
    DirectoryResolver,
    DirectoryResponse,
    DirectoryStore,
    DirectoryStoreUnavailable,
    Ed25519Key,
    FetchLimits,
    HeaderLines,
    HTTPResponse,
    KeyId,
    MemoryDirectoryStore,
    MemoryNonceStore,
    Origin,
    PublishPolicy,
    Refusal,
    RequestVerifier,
    ResolvedKey,
    StaticKeyResolver,
    UnixSeconds,
    WebBotAuthError,
    publish_directory,
    verify_directory,
)
from iab_agentic_primitives.web_bot_auth.fetcher import (
    BlockedAddress,
    HttpxDirectoryFetcher,
    TLSConfig,
)
from iab_agentic_primitives.web_bot_auth.testing import (
    check_directory_store,
    check_nonce_store,
    directory_transport,
)

from . import support as w
from .support import BUYER, BUYER_ORIGIN, SELLER_IDENTITY, T0, Clocked, at, expect_refusal, run


class Server:
    """Serves the buyer's directory and counts fetches; tests swap what it serves."""

    def __init__(
        self,
        keys: Sequence[Ed25519Key],
        now: UnixSeconds = T0,
        policy: PublishPolicy | None = None,
    ) -> None:
        self.fetches = 0
        self.serve(keys, now, policy)

    def serve(
        self, keys: Sequence[Ed25519Key], now: UnixSeconds = T0, policy: PublishPolicy | None = None
    ) -> None:
        answer = publish_directory(BUYER_ORIGIN, keys, now=now, policy=policy or PublishPolicy())
        self.status, self.headers, self.body = (
            answer.status,
            dict(answer.headers.items()),
            answer.body,
        )

    def fail(self, status: int = 503) -> None:
        self.status, self.headers, self.body = status, {}, b""

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.fetches += 1
            assert str(request.url) == BUYER + DIRECTORY_PATH  # exactly the well-known URL
            assert request.headers["accept-encoding"] == "identity"
            return httpx.Response(
                self.status, headers=self.headers, stream=httpx.ByteStream(self.body)
            )

        return httpx.MockTransport(handler)


def over(
    transport: httpx.AsyncBaseTransport,
    *,
    store: DirectoryStore | None = None,
    limits: FetchLimits | None = None,
    clock: Clock = lambda: T0,
    cache: CachePolicy | None = None,
) -> Clocked:
    """A resolver over ``transport``, resolving at ``clock``'s time."""
    resolver = DirectoryResolver(
        HttpxDirectoryFetcher(transport=transport),
        store,
        limits=limits or FetchLimits(),
        cache=cache or CachePolicy(),
        clock=clock,
    )
    return Clocked(resolver, clock)


def resolver(server: Server, now: list[UnixSeconds], **limits: int) -> Clocked:
    return over(server.transport(), limits=FetchLimits(**limits), clock=lambda: now[0])


def resolve(r: Clocked, kid: KeyId, origin: Origin = BUYER_ORIGIN) -> ResolvedKey:
    return run(r.resolve(origin, kid))


class Fetcher:
    """A DirectoryFetcher that serves the buyer's directory, counts calls, and can raise."""

    def __init__(
        self,
        keys: Sequence[Ed25519Key],
        *,
        error: BaseException | None = None,
        delay: float = 0.0,
    ) -> None:
        self.calls = 0
        self.status, self.headers, self.body = w.signed_directory(BUYER, keys)
        self.error, self.delay = error, delay

    async def fetch(
        self, origin: Origin, *, limits: FetchLimits, now: UnixSeconds
    ) -> DirectoryResponse:
        self.calls += 1
        await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return DirectoryResponse(origin.directory_url, self.status, self.headers, self.body, now)


# ── the store's rules ─────────────────────────────────────────────────────


def test_a_fresh_directory_is_not_fetched_again(buyer_key: Ed25519Key) -> None:
    server, now = Server([buyer_key]), [T0]
    r = resolver(server, now)
    assert resolve(r, buyer_key.thumbprint).public_key == buyer_key.public_key
    now[0] = at(299)
    assert resolve(r, buyer_key.thumbprint).public_key == buyer_key.public_key
    assert server.fetches == 1


def test_a_stale_directory_is_fetched_again_and_replaced(buyer_key: Ed25519Key) -> None:
    server, now = Server([buyer_key]), [T0]
    r = resolver(server, now)
    resolve(r, buyer_key.thumbprint)
    now[0] = at(300)
    resolve(r, buyer_key.thumbprint)
    assert server.fetches == 2


def test_a_cached_directory_is_valid_until_its_earliest_expires(buyer_key: Ed25519Key) -> None:
    server, now = Server([buyer_key]), [T0]
    r = resolver(server, now)
    resolve(r, buyer_key.thumbprint)
    server.fail()
    now[0] = at(86400)
    assert (
        resolve(r, buyer_key.thumbprint).public_key == buyer_key.public_key
    )  # stale, still valid, and the refetch failed
    now[0] = at(86401 + 60)
    expect_refusal(lambda: resolve(r, buyer_key.thumbprint), Refusal.DIRECTORY_STATUS)


def test_an_unknown_keyid_never_triggers_a_fetch_while_the_directory_is_fresh(
    buyer_key: Ed25519Key,
) -> None:
    server, now = Server([buyer_key]), [T0]
    r = resolver(server, now)
    resolve(r, buyer_key.thumbprint)
    for i in range(50):
        expect_refusal(partial(resolve, r, KeyId(f"unknown-{i}")), Refusal.KEY_NOT_FOUND)
    assert server.fetches == 1


def _best_seconds(call: Callable[[], object], times: int) -> float:
    """The fastest of five batches of ``times`` calls, per call, in seconds."""
    best = float("inf")
    for _ in range(5):
        start = time.perf_counter()
        for _ in range(times):
            call()
        best = min(best, (time.perf_counter() - start) / times)
    return best


def test_an_unknown_keyid_on_a_fresh_directory_costs_no_verification() -> None:
    # A peer publishes the most keys a directory may hold, then sends random key ids.
    keys = [Ed25519Key.generate() for _ in range(16)]
    fetcher = Fetcher(keys)
    r = Clocked(DirectoryResolver(fetcher, clock=lambda: T0), lambda: T0)
    response = resolve(r, keys[0].thumbprint).directory
    assert response is not None

    def unknown() -> None:
        expect_refusal(partial(resolve, r, KeyId("unknown")), Refusal.KEY_NOT_FOUND)

    def verify() -> None:
        verify_directory(response.evidence, BUYER_ORIGIN, T0)

    # Verifying a 16-key directory checks 16 signatures; a cache hit checks none.
    assert _best_seconds(unknown, 50) * 10 < _best_seconds(verify, 5)
    assert fetcher.calls == 1


def test_a_store_outside_the_process_is_verified_once_per_response(
    buyer_key: Ed25519Key,
) -> None:
    keys = [buyer_key, *(Ed25519Key.generate() for _ in range(15))]
    server, store = Server(keys), JsonStore()
    r = over(server.transport(), store=store)
    resolve(r, buyer_key.thumbprint)
    stored = run(store.get(BUYER_ORIGIN))
    assert stored is not None

    def unknown() -> None:
        expect_refusal(partial(resolve, r, KeyId("unknown")), Refusal.KEY_NOT_FOUND)

    def verify() -> None:
        verify_directory(stored, BUYER_ORIGIN, T0)

    # The store hands back new objects of the bytes the resolver verified.
    assert _best_seconds(unknown, 50) * 10 < _best_seconds(verify, 5)
    assert server.fetches == 1


def test_rotation_shows_once_the_cached_directory_is_stale(
    buyer_key: Ed25519Key, other_key: Ed25519Key
) -> None:
    server, now = Server([buyer_key]), [T0]
    r = resolver(server, now)
    resolve(r, buyer_key.thumbprint)
    server.serve([buyer_key, other_key])
    expect_refusal(
        lambda: resolve(r, other_key.thumbprint), Refusal.KEY_NOT_FOUND
    )  # fresh: not fetched
    now[0] = at(300)
    assert resolve(r, other_key.thumbprint).public_key == other_key.public_key
    server.serve([other_key], now=now[0])
    now[0] = at(600)
    expect_refusal(
        lambda: resolve(r, buyer_key.thumbprint), Refusal.KEY_NOT_FOUND
    )  # removed: evicted
    assert resolve(r, other_key.thumbprint).public_key == other_key.public_key


def test_a_failed_fetch_does_not_evict(buyer_key: Ed25519Key, other_key: Ed25519Key) -> None:
    server, now = Server([buyer_key]), [T0]
    r = resolver(server, now)
    resolve(r, buyer_key.thumbprint)
    server.fail()
    now[0] = at(300)
    assert resolve(r, buyer_key.thumbprint).public_key == buyer_key.public_key
    expect_refusal(lambda: resolve(r, other_key.thumbprint), Refusal.KEY_NOT_FOUND)


def test_a_failed_fetch_is_not_retried_for_60_seconds(buyer_key: Ed25519Key) -> None:
    server, now = Server([buyer_key]), [T0]
    server.fail(500)
    r = resolver(server, now)
    expect_refusal(lambda: resolve(r, buyer_key.thumbprint), Refusal.DIRECTORY_STATUS)
    for i in range(20):
        expect_refusal(partial(resolve, r, KeyId(f"fresh-{i}")), Refusal.DIRECTORY_UNAVAILABLE)
    now[0] = at(59)
    expect_refusal(lambda: resolve(r, buyer_key.thumbprint), Refusal.DIRECTORY_UNAVAILABLE)
    assert server.fetches == 1
    now[0] = at(60)
    server.serve([buyer_key], now=now[0])
    assert resolve(r, buyer_key.thumbprint).public_key == buyer_key.public_key
    assert server.fetches == 2


def test_the_store_is_bounded_and_drops_the_oldest_entry(buyer_key: Ed25519Key) -> None:
    store = MemoryDirectoryStore(max_entries=2)
    origins = [Origin.parse(f"https://a{i}.example.com") for i in range(5)]
    for i, origin in enumerate(origins):
        answer = w.signed_directory(origin, [buyer_key], now=at(i))
        run(store.put(origin, w.fetched(*answer, origin=origin, now=at(i))))
        run(store.record_failure(Origin.parse(f"https://b{i}.example.com"), at(i)))
    kept = [o for o in origins if run(store.get(o)) is not None]
    assert kept == origins[-2:]  # the oldest went first
    failing = [
        i for i in range(5) if run(store.failure_until(Origin.parse(f"https://b{i}.example.com")))
    ]
    assert failing == [3, 4]


@pytest.mark.parametrize(
    "make",
    [MemoryDirectoryStore, lambda: MemoryDirectoryStore(max_entries=2)],
    ids=["default", "bounded"],
)
def test_the_memory_store_keeps_the_directory_store_contract(
    make: Callable[[], DirectoryStore],
) -> None:
    run(check_directory_store(make))


class JsonStore:
    """A store outside the process: it keeps each directory response as JSON text."""

    def __init__(self) -> None:
        self.rows: dict[str, str] = {}
        self.failures: dict[str, UnixSeconds] = {}

    async def get(self, origin: Origin) -> DirectoryResponse | None:
        row = self.rows.get(str(origin))
        if row is None:
            return None
        doc = json.loads(row)
        lines = [(name, value) for name, value in doc["headers"]]
        body = base64.b64decode(doc["body_base64"])
        return DirectoryResponse(doc["url"], doc["status"], lines, body, doc["fetched_at"])

    async def put(self, origin: Origin, response: DirectoryResponse) -> None:
        self.rows[str(origin)] = json.dumps(response.document())

    async def record_failure(self, origin: Origin, until: UnixSeconds) -> None:
        self.failures[str(origin)] = until

    async def failure_until(self, origin: Origin) -> UnixSeconds | None:
        return self.failures.get(str(origin))


def test_a_store_that_keeps_json_keeps_the_directory_store_contract() -> None:
    run(check_directory_store(JsonStore))


def test_a_store_outside_the_process_serves_the_resolver_with_plain_data(
    buyer_key: Ed25519Key,
) -> None:
    # The directory's signatures last 30 s; the store keeps text and decides nothing.
    server, clock = Server([buyer_key], policy=PublishPolicy(lifetime=30)), [T0]
    store = JsonStore()
    r = over(server.transport(), store=store, clock=lambda: clock[0])
    verifier = w.request_verifier(resolver=r.resolver)
    assert w.verify(verifier, w.signed_commit(buyer_key), now=T0).origin == BUYER_ORIGIN
    clock[0] = at(100)  # the stored directory expired at at(30)
    server.serve([buyer_key], now=at(100), policy=PublishPolicy(lifetime=30))
    later = w.signed_commit(buyer_key, now=at(100))
    assert w.verify(verifier, later, now=at(100)).origin == BUYER_ORIGIN
    assert server.fetches == 2
    again = w.signed_commit(buyer_key, now=at(101))
    assert w.verify(verifier, again, now=at(101)).origin == BUYER_ORIGIN
    assert server.fetches == 2  # answered from the stored text, verified again


def test_a_stored_response_that_does_not_verify_is_a_cache_miss(buyer_key: Ed25519Key) -> None:
    server, clock, store = Server([buyer_key]), [T0], JsonStore()
    r = over(server.transport(), store=store, clock=lambda: clock[0])
    resolve(r, buyer_key.thumbprint)
    row = json.loads(store.rows[BUYER])
    row["body_base64"] = base64.b64encode(b'{"keys": []}').decode()
    store.rows[BUYER] = json.dumps(row)
    clock[0] = at(10)
    assert resolve(r, buyer_key.thumbprint).public_key == buyer_key.public_key
    assert server.fetches == 2
    stored = run(store.get(BUYER_ORIGIN))
    assert stored is not None and stored.body == server.body  # the refetch replaced it


def test_the_memory_nonce_store_keeps_the_nonce_store_contract() -> None:
    run(check_nonce_store(lambda clock: MemoryNonceStore(clock=clock)))


def test_a_stored_directory_answers_only_until_it_expires(buyer_key: Ed25519Key) -> None:
    # The store keeps the directory whatever the time; the resolver checks expires.
    server, now = Server([buyer_key]), [T0]
    r = resolver(server, now)
    assert resolve(r, buyer_key.thumbprint).directory is not None
    server.fail()
    now[0] = at(7 * 86400)
    expect_refusal(lambda: resolve(r, buyer_key.thumbprint), Refusal.DIRECTORY_STATUS)


class Down(MemoryDirectoryStore):
    """A store whose backend is down for the methods it names, as Redis is in an outage."""

    def __init__(self, *down: str) -> None:
        super().__init__()
        self.down = frozenset(down)

    def _reach(self, method: str) -> None:
        if method in self.down:
            raise DirectoryStoreUnavailable(f"redis at 10.0.0.5:6379 refused {method}")

    async def get(self, origin: Origin) -> DirectoryResponse | None:
        self._reach("get")
        return await super().get(origin)

    async def put(self, origin: Origin, response: DirectoryResponse) -> None:
        self._reach("put")
        await super().put(origin, response)

    async def record_failure(self, origin: Origin, until: UnixSeconds) -> None:
        self._reach("record_failure")
        await super().record_failure(origin, until)

    async def failure_until(self, origin: Origin) -> UnixSeconds | None:
        self._reach("failure_until")
        return await super().failure_until(origin)


_EVERY_METHOD = ("get", "put", "record_failure", "failure_until")


def test_a_store_that_cant_be_read_is_a_miss_and_the_directory_is_fetched(
    buyer_key: Ed25519Key, caplog: pytest.LogCaptureFixture
) -> None:
    server = Server([buyer_key])
    r = over(server.transport(), store=Down(*_EVERY_METHOD))
    for _ in range(2):
        assert resolve(r, buyer_key.thumbprint).public_key == buyer_key.public_key
    assert server.fetches == 2  # nothing could be kept, so each resolve fetches
    assert "can't be read" in caplog.text and "can't be written" in caplog.text


def test_a_store_that_cant_record_a_failure_keeps_the_refusal(buyer_key: Ed25519Key) -> None:
    server = Server([buyer_key])
    server.fail(500)
    r = over(server.transport(), store=Down("record_failure", "failure_until"))
    for _ in range(2):
        expect_refusal(lambda: resolve(r, buyer_key.thumbprint), Refusal.DIRECTORY_STATUS)
    assert server.fetches == 2  # no failure deadline could be kept


def test_a_store_that_comes_back_is_used_again(buyer_key: Ed25519Key) -> None:
    server, store = Server([buyer_key]), Down(*_EVERY_METHOD)
    r = over(server.transport(), store=store)
    resolve(r, buyer_key.thumbprint)
    store.down = frozenset()
    resolve(r, buyer_key.thumbprint)
    resolve(r, buyer_key.thumbprint)
    assert server.fetches == 2  # the second fetch was kept, and the third resolve read it


def test_a_verifier_over_a_store_that_is_down_answers_with_the_profiles_outcome(
    buyer_key: Ed25519Key,
) -> None:
    served = w.directory_transport_for({BUYER: [buyer_key]})
    verifier = w.request_verifier(resolver=over(served, store=Down(*_EVERY_METHOD)).resolver)
    assert run(verifier.verify(w.sign_request())).origin == BUYER_ORIGIN
    failing = Server([buyer_key])
    failing.fail(503)
    nowhere = over(failing.transport(), store=Down(*_EVERY_METHOD))
    refusing = w.request_verifier(resolver=nowhere.resolver)
    expect_refusal(lambda: refusing.verify(w.sign_request()), Refusal.DIRECTORY_STATUS)


def test_any_other_store_error_is_a_bug_that_propagates(buyer_key: Ed25519Key) -> None:
    class Broken(MemoryDirectoryStore):
        async def get(self, origin: Origin) -> DirectoryResponse | None:
            raise ConnectionError("not a DirectoryStoreUnavailable")

    r = over(Server([buyer_key]).transport(), store=Broken())
    with pytest.raises(ConnectionError):
        resolve(r, buyer_key.thumbprint)


class Lying(MemoryDirectoryStore):
    """A store that answers every origin with the buyer's directory once it holds it."""

    async def get(self, origin: Origin) -> DirectoryResponse | None:
        return await super().get(BUYER_ORIGIN)


def test_a_store_answering_for_another_origin_is_not_believed(
    buyer_key: Ed25519Key, other_key: Ed25519Key
) -> None:
    served = w.directory_transport_for({BUYER: [buyer_key], w.OTHER: [other_key]})
    r = over(served, store=Lying())
    assert resolve(r, buyer_key.thumbprint).directory is not None
    # The store offers the buyer's directory for the other origin; the resolver
    # fetches the other origin's own, and the buyer's key isn't in it.
    expect_refusal(lambda: resolve(r, buyer_key.thumbprint, w.OTHER_ORIGIN), Refusal.KEY_NOT_FOUND)


def test_a_blocked_address_is_a_failed_fetch_held_back_for_the_negative_ttl(
    buyer_key: Ed25519Key,
) -> None:
    calls = []

    def guard(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        raise BlockedAddress("the deployment doesn't fetch from this address", request=request)

    now = [T0]
    r = over(httpx.MockTransport(guard), clock=lambda: now[0])
    for _ in range(3):
        expect_refusal(lambda: resolve(r, buyer_key.thumbprint), Refusal.DIRECTORY_UNAVAILABLE)
    assert len(calls) == 1
    now[0] = at(60)
    expect_refusal(lambda: resolve(r, buyer_key.thumbprint), Refusal.DIRECTORY_UNAVAILABLE)
    assert len(calls) == 2


def test_negative_entries_cover_failed_fetches(buyer_key: Ed25519Key) -> None:
    server, now = Server([buyer_key]), [T0]
    server.fail(500)
    r = resolver(server, now)
    expect_refusal(lambda: resolve(r, buyer_key.thumbprint), Refusal.DIRECTORY_STATUS)
    expect_refusal(lambda: resolve(r, buyer_key.thumbprint), Refusal.DIRECTORY_UNAVAILABLE)
    assert server.fetches == 1


def test_a_store_is_shared_and_clearable(buyer_key: Ed25519Key) -> None:
    store = MemoryDirectoryStore()
    server = Server([buyer_key])
    one, two = over(server.transport(), store=store), over(server.transport(), store=store)
    resolve(one, buyer_key.thumbprint)
    resolve(two, buyer_key.thumbprint)
    assert server.fetches == 1
    store.clear()
    resolve(two, buyer_key.thumbprint)
    assert server.fetches == 2


def test_a_verify_judged_later_neither_stamps_nor_pins_the_cache(
    buyer_key: Ed25519Key, other_key: Ed25519Key
) -> None:
    server, clock = Server([buyer_key]), [T0]
    r = over(server.transport(), clock=lambda: clock[0])
    verifier = w.request_verifier(resolver=r.resolver)
    ahead = w.signed_commit(buyer_key, now=at(3600))
    assert w.verify(verifier, ahead, now=at(3600)).origin == BUYER_ORIGIN  # fetched at T0
    server.serve([other_key], now=at(400))  # the buyer rotates its key out
    clock[0] = at(400)
    rotated = w.signed_commit(buyer_key, now=at(400))
    expect_refusal(lambda: w.verify(verifier, rotated, now=at(400)), Refusal.KEY_NOT_FOUND)
    assert server.fetches == 2


def test_a_failure_judged_later_is_held_back_on_the_resolvers_clock(
    buyer_key: Ed25519Key,
) -> None:
    server, clock = Server([buyer_key]), [T0]
    server.fail(500)
    r = over(server.transport(), clock=lambda: clock[0])
    verifier = w.request_verifier(resolver=r.resolver)
    ahead = w.signed_commit(buyer_key, now=at(3600))
    expect_refusal(lambda: w.verify(verifier, ahead, now=at(3600)), Refusal.DIRECTORY_STATUS)
    clock[0] = at(60)
    server.serve([buyer_key])
    retried = w.signed_commit(buyer_key, now=at(60))
    assert w.verify(verifier, retried, now=at(60)).origin == BUYER_ORIGIN
    assert server.fetches == 2


def test_the_callers_time_judges_the_directorys_expiry(buyer_key: Ed25519Key) -> None:
    server = Server([buyer_key])  # its signatures expire at at(86400)
    r = over(server.transport())
    verifier = w.request_verifier(resolver=r.resolver)
    late = w.signed_commit(buyer_key, now=at(86401))
    expect_refusal(lambda: w.verify(verifier, late, now=at(86401)), Refusal.DIRECTORY_WINDOW)
    assert server.fetches == 1


def test_max_cache_age_and_negative_ttl_are_the_resolvers(buyer_key: Ed25519Key) -> None:
    server, now = Server([buyer_key]), [T0]
    r = over(
        server.transport(),
        cache=CachePolicy(max_cache_age=10, negative_ttl=5),
        clock=lambda: now[0],
    )
    resolve(r, buyer_key.thumbprint)
    now[0] = at(10)
    resolve(r, buyer_key.thumbprint)
    assert server.fetches == 2
    server.fail()
    now[0] = at(20)
    resolve(r, buyer_key.thumbprint)  # the refetch failed; the stored keys answer
    now[0] = at(25)
    resolve(r, buyer_key.thumbprint)
    assert server.fetches == 4


# ── one fetch per origin, and only refusals are failures ──────────────────


def test_fifty_concurrent_cold_resolves_make_one_fetch(buyer_key: Ed25519Key) -> None:
    fetcher = Fetcher([buyer_key], delay=0.01)
    r = Clocked(DirectoryResolver(fetcher, clock=lambda: T0), lambda: T0)

    async def go() -> list[ResolvedKey]:
        return await asyncio.gather(
            *(r.resolve(BUYER_ORIGIN, buyer_key.thumbprint) for _ in range(50))
        )

    resolved = run(go())
    assert fetcher.calls == 1
    assert len({id(k.directory) for k in resolved}) == 1


def test_concurrent_resolves_share_one_refusal(buyer_key: Ed25519Key) -> None:
    fetcher = Fetcher([buyer_key], delay=0.01)
    fetcher.status = 500
    r = Clocked(DirectoryResolver(fetcher, clock=lambda: T0), lambda: T0)

    async def go() -> list[ResolvedKey | BaseException]:
        return await asyncio.gather(
            *(r.resolve(BUYER_ORIGIN, buyer_key.thumbprint) for _ in range(10)),
            return_exceptions=True,
        )

    results = run(go())
    assert fetcher.calls == 1
    errors = [e for e in results if isinstance(e, WebBotAuthError)]
    assert len(errors) == len(results)
    assert {e.reason for e in errors} == {"directory-status"}


def test_an_unexpected_error_in_the_fetcher_propagates_and_records_nothing(
    buyer_key: Ed25519Key,
) -> None:
    store = MemoryDirectoryStore()
    fetcher = Fetcher([buyer_key], error=RuntimeError("Event loop is closed"))
    r = Clocked(DirectoryResolver(fetcher, store, clock=lambda: T0), lambda: T0)
    with pytest.raises(RuntimeError, match="Event loop is closed"):
        resolve(r, buyer_key.thumbprint)
    assert run(store.failure_until(BUYER_ORIGIN)) is None
    fetcher.error = None
    assert (
        resolve(r, buyer_key.thumbprint).public_key == buyer_key.public_key
    )  # not held back by a negative entry
    assert fetcher.calls == 2


@pytest.mark.parametrize("error", [TypeError("bug"), AttributeError("bug"), RecursionError("deep")])
def test_no_defect_becomes_directory_unavailable(buyer_key: Ed25519Key, error: Exception) -> None:
    r = Clocked(DirectoryResolver(Fetcher([buyer_key], error=error), clock=lambda: T0), lambda: T0)
    with pytest.raises(type(error)):
        resolve(r, buyer_key.thumbprint)


# ── fetch rules ───────────────────────────────────────────────────────────


def test_only_https_origins_can_be_resolved() -> None:
    # The resolver takes an Origin, and an Origin is https://<host> by construction.
    with pytest.raises(ConfigurationError):
        Origin.parse("http://buyer.example.com")


def test_redirects_are_not_followed(buyer_key: Ed25519Key) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == DIRECTORY_PATH:
            return httpx.Response(
                302,
                headers={"Location": "https://other.example.com" + DIRECTORY_PATH},
                stream=httpx.ByteStream(b""),
            )
        raise AssertionError("followed a redirect")

    expect_refusal(
        lambda: resolve(over(httpx.MockTransport(handler)), buyer_key.thumbprint),
        Refusal.DIRECTORY_STATUS,
    )


def test_media_type_is_checked_on_fetch(buyer_key: Ed25519Key) -> None:
    server, now = Server([buyer_key]), [T0]
    server.headers["Content-Type"] = "application/json"
    expect_refusal(
        lambda: resolve(resolver(server, now), buyer_key.thumbprint), Refusal.DIRECTORY_MEDIA_TYPE
    )


def test_the_body_cap_applies_to_the_served_bytes(buyer_key: Ed25519Key) -> None:
    server, now = Server([buyer_key]), [T0]
    expect_refusal(
        lambda: resolve(resolver(server, now, max_bytes=100), buyer_key.thumbprint),
        Refusal.DIRECTORY_TOO_LARGE,
    )


def test_the_streamed_read_stops_one_byte_past_the_cap(buyer_key: Ed25519Key) -> None:
    served: list[int] = []

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            for _ in range(300):
                served.append(1)
                yield b" " * 1024

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/http-message-signatures-directory+json"},
            stream=Chunks(),
        )

    expect_refusal(
        lambda: resolve(over(httpx.MockTransport(handler)), buyer_key.thumbprint),
        Refusal.DIRECTORY_TOO_LARGE,
    )
    assert len(served) == 257  # 256 KiB, then the chunk that crosses the cap


def test_the_testing_transport_runs_the_streamed_capped_read(buyer_key: Ed25519Key) -> None:
    status, lines, body = w.signed_directory(BUYER, [buyer_key])
    headers = dict(lines.items())
    transport = directory_transport({BUYER_ORIGIN: Answer(status, HeaderLines(headers), body)})
    assert resolve(over(transport), buyer_key.thumbprint).public_key == buyer_key.public_key
    capped = over(transport, limits=FetchLimits(max_bytes=len(body) - 1))
    expect_refusal(lambda: resolve(capped, buyer_key.thumbprint), Refusal.DIRECTORY_TOO_LARGE)


def test_key_count_cap_on_fetch(buyer_key: Ed25519Key, other_key: Ed25519Key) -> None:
    server, now = Server([buyer_key, other_key]), [T0]
    expect_refusal(
        lambda: resolve(resolver(server, now, max_keys=1), buyer_key.thumbprint),
        Refusal.DIRECTORY_TOO_MANY_KEYS,
    )


def test_total_timeout(buyer_key: Ed25519Key) -> None:
    class Slow(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(5)
            raise AssertionError("not reached")

    slow = over(Slow(), limits=FetchLimits(total_timeout=0.05))
    error = expect_refusal(
        lambda: resolve(slow, buyer_key.thumbprint), Refusal.DIRECTORY_UNAVAILABLE
    )
    assert "timed out" in str(error)


def test_transport_errors_are_403(buyer_key: Ed25519Key) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    expect_refusal(
        lambda: resolve(over(httpx.MockTransport(handler)), buyer_key.thumbprint),
        Refusal.DIRECTORY_UNAVAILABLE,
    )


def test_defaults_match_the_profile() -> None:
    policy = FetchLimits()
    assert (policy.connect_timeout, policy.total_timeout) == (5.0, 10.0)
    assert (policy.max_bytes, policy.max_keys) == (256 * 1024, 16)
    cache = CachePolicy()
    assert (cache.max_cache_age, cache.negative_ttl) == (300, 60)


# ── the injected client, the owned client, and TLS ────────────────────────


def test_an_injected_client_carries_every_fetch_and_stays_open(buyer_key: Ed25519Key) -> None:
    server = Server([buyer_key])

    async def go() -> bool:
        async with httpx.AsyncClient(transport=server.transport()) as client:
            async with DirectoryResolver(HttpxDirectoryFetcher(client), clock=lambda: T0) as r:
                await r.resolve(BUYER_ORIGIN, buyer_key.thumbprint, now=T0)
            async with HttpxDirectoryFetcher(client):
                pass
            return client.is_closed

    assert run(go()) is False and server.fetches == 1


def test_a_callers_transport_stays_open_after_aclose(buyer_key: Ed25519Key) -> None:
    class Counting(httpx.AsyncBaseTransport):
        closed = 0

        def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
            self.inner = inner

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return await self.inner.handle_async_request(request)

        async def aclose(self) -> None:
            self.closed += 1

    transport = Counting(Server([buyer_key]).transport())

    async def go() -> None:
        fetcher = HttpxDirectoryFetcher(transport=transport)
        r = DirectoryResolver(fetcher, clock=lambda: T0)
        await r.resolve(BUYER_ORIGIN, buyer_key.thumbprint, now=T0)
        await r.aclose()
        await fetcher.aclose()

    run(go())
    assert transport.closed == 0


def test_the_resolver_closes_the_fetcher_it_built(buyer_key: Ed25519Key) -> None:
    async def go() -> None:
        async with DirectoryResolver() as r:
            pass
        await r.resolve(BUYER_ORIGIN, buyer_key.thumbprint, now=T0)

    with pytest.raises(ConfigurationError, match="closed"):
        run(go())


def test_the_profile_rules_apply_on_an_injected_client_that_follows_redirects(
    buyer_key: Ed25519Key,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "buyer.example.com":
            return httpx.Response(
                307,
                headers={"Location": "https://other.example.com" + DIRECTORY_PATH},
                stream=httpx.ByteStream(b""),
            )
        raise AssertionError("followed a redirect")

    async def go() -> ResolvedKey:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True
        ) as c:
            return await Clocked(
                DirectoryResolver(HttpxDirectoryFetcher(c), clock=lambda: T0), lambda: T0
            ).resolve(BUYER_ORIGIN, buyer_key.thumbprint)

    assert "redirects" in str(expect_refusal(go, Refusal.DIRECTORY_STATUS))


def test_the_library_adds_no_address_policy(buyer_key: Ed25519Key) -> None:
    # A plain client reaches whatever it reaches; address policy belongs to the client.
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(404, stream=httpx.ByteStream(b""))

    async def go() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            await Clocked(
                DirectoryResolver(HttpxDirectoryFetcher(c), clock=lambda: T0), lambda: T0
            ).resolve(Origin.parse("https://127.0.0.1"), buyer_key.thumbprint)

    expect_refusal(go, Refusal.DIRECTORY_STATUS)
    assert seen == ["https://127.0.0.1" + DIRECTORY_PATH]


class FetcherOptions(TypedDict, total=False):
    """What an injected client can't be combined with."""

    tls: TLSConfig
    transport: httpx.AsyncBaseTransport


@pytest.mark.parametrize(
    "options",
    [
        {"tls": TLSConfig(ca_bundle="/certs/ca.pem")},
        {"tls": TLSConfig(extra_ca="/certs/ca.pem")},
        {"transport": httpx.MockTransport(lambda r: httpx.Response(404))},
    ],
)
def test_tls_or_a_transport_with_an_injected_client_is_a_configuration_error(
    options: FetcherOptions,
) -> None:
    with pytest.raises(ConfigurationError):
        HttpxDirectoryFetcher(httpx.AsyncClient(), **options)


def test_tls_with_a_transport_is_a_configuration_error() -> None:
    with pytest.raises(ConfigurationError):
        HttpxDirectoryFetcher(
            tls=TLSConfig(ca_bundle="/certs/ca.pem"),
            transport=httpx.MockTransport(lambda r: httpx.Response(404)),
        )


def test_ca_bundle_replaces_and_extra_ca_adds_trust_anchors(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        HttpxDirectoryFetcher(tls=TLSConfig(ca_bundle=str(tmp_path / "missing.pem")))
    with pytest.raises(FileNotFoundError):
        TLSConfig(extra_ca=str(tmp_path / "missing.pem")).ssl_context()
    system = TLSConfig().ssl_context()
    assert system.verify_mode.name == "CERT_REQUIRED"
    pem = _self_signed_ca(tmp_path)
    extra = TLSConfig(extra_ca=str(pem)).ssl_context()
    assert extra.cert_store_stats()["x509_ca"] == system.cert_store_stats()["x509_ca"] + 1
    only = TLSConfig(ca_bundle=str(pem)).ssl_context()
    assert only.cert_store_stats()["x509_ca"] == 1


def _self_signed_ca(tmp_path: Path) -> Path:
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "aamp test CA")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    path = tmp_path / "ca.pem"
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return path


# ── other resolvers ───────────────────────────────────────────────────────


def test_static_key_resolver_accepts_keys_public_keys_and_jwks(
    buyer_key: Ed25519Key, other_key: Ed25519Key, seller_key: Ed25519Key
) -> None:
    r = StaticKeyResolver({BUYER: [buyer_key, other_key.public_key, seller_key.public_jwk()]})
    for key in (buyer_key, other_key, seller_key):
        assert run(r.resolve(BUYER_ORIGIN, key.thumbprint, now=T0)).public_key == key.public_key
    expect_refusal(
        lambda: r.resolve(w.OTHER_ORIGIN, buyer_key.thumbprint, now=T0), Refusal.KEY_NOT_FOUND
    )
    with pytest.raises(ValueError, match="32 bytes"):
        StaticKeyResolver({BUYER: [{"kty": "OKP", "crv": "Ed25519", "x": "AAAA"}]})


def test_static_key_resolver_takes_a_verified_directorys_keys(buyer_key: Ed25519Key) -> None:
    directory = verify_directory(
        w.fetched(*w.signed_directory(BUYER, [buyer_key])), BUYER_ORIGIN, T0
    )
    r = StaticKeyResolver({BUYER_ORIGIN: directory.keys})
    assert run(r.resolve(BUYER_ORIGIN, buyer_key.thumbprint, now=T0)).public_key is not None


def test_verifying_through_pinned_keys_loads_no_network_client() -> None:
    program = f"""
import asyncio, sys
from iab_agentic_primitives.web_bot_auth import (
    Ed25519Key, HTTPRequest, MemoryNonceStore, RequestSigner, RequestVerifier,
    ServerIdentity, StaticKeyResolver,
)
key = Ed25519Key.generate()
signed = RequestSigner.of(key, {BUYER!r}).sign(HTTPRequest("GET", "https://seller.example.com/x"))
verifier = RequestVerifier(
    StaticKeyResolver({{{BUYER!r}: [key]}}),
    identity=ServerIdentity.from_config("https://seller.example.com"),
    nonces=MemoryNonceStore(),
)
assert str(asyncio.run(verifier.verify(signed)).origin) == {BUYER!r}
assert "httpx" not in sys.modules, "httpx was imported"
"""
    done = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_directory_transport_serves_only_the_well_known_path(buyer_key: Ed25519Key) -> None:
    transport = directory_transport({BUYER_ORIGIN: w.signed_directory(BUYER, [buyer_key])})
    with httpx.Client(transport=transport) as client:
        assert client.get(BUYER + DIRECTORY_PATH).status_code == 200
        assert client.get(BUYER + "/other").status_code == 404
        assert client.get(w.OTHER + DIRECTORY_PATH).status_code == 404


# ── directory evidence ────────────────────────────────────────────────────


def test_the_verified_directory_carries_the_response_it_was_verified_from(
    buyer_key: Ed25519Key,
) -> None:
    status, lines, body = w.signed_directory(BUYER, [buyer_key])
    headers = dict(lines.items())
    r = over(
        directory_transport({BUYER_ORIGIN: Answer(status, HeaderLines(headers), body)}),
        clock=lambda: at(5),
    )
    resolved = run(r.resolve(BUYER_ORIGIN, buyer_key.thumbprint))
    directory = resolved.directory
    assert directory is not None
    assert resolved.public_key == directory.key(buyer_key.thumbprint)
    evidence = directory.evidence
    assert evidence.url == BUYER + DIRECTORY_PATH
    assert evidence.status == 200 and evidence.body == body and evidence.fetched_at == at(5)
    names = {name.lower() for name, _ in evidence.headers}
    assert {"signature-input", "signature", "content-digest", "content-type"} <= names
    # The evidence verifies again on its own.
    again = verify_directory(evidence, BUYER_ORIGIN, T0)
    assert list(again.keys) == [buyer_key.thumbprint]
    assert json.loads(json.dumps(evidence.document()))["body"] == body.decode()
    # A cache hit verifies the same evidence again.
    hit = run(r.resolve(BUYER_ORIGIN, buyer_key.thumbprint)).directory
    assert hit is not None and hit.evidence == evidence


def test_evidence_is_the_bytes_read_under_the_cap(buyer_key: Ed25519Key) -> None:
    status, lines, body = w.signed_directory(BUYER, [buyer_key])
    headers = dict(lines.items())

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            for i in range(0, len(body), 7):
                yield body[i : i + 7]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers=headers, stream=Chunks())

    directory = run(over(httpx.MockTransport(handler)).resolve(BUYER_ORIGIN, buyer_key.thumbprint))
    assert directory.directory is not None
    assert directory.directory.evidence.body == body


def test_verified_signatures_reach_their_directory(
    buyer_key: Ed25519Key, seller_key: Ed25519Key
) -> None:
    verifier = w.request_verifier()
    result = w.verify(verifier, w.signed_commit(buyer_key))
    assert result.directory is not None
    assert result.directory.evidence.url == BUYER + DIRECTORY_PATH
    assert buyer_key.thumbprint in result.directory.keys
    request, headers = w.signed_result(seller_key, w.signed_commit(buyer_key))
    response = run(
        w.response_verifier().verify(HTTPResponse(200, headers, w.COMMIT_RESULT_BODY, request))
    )
    assert response.directory is not None
    assert response.directory.evidence.url == w.SELLER + DIRECTORY_PATH


def test_a_resolver_without_directories_leaves_it_empty(buyer_key: Ed25519Key) -> None:
    verifier = RequestVerifier(
        StaticKeyResolver({BUYER: [buyer_key]}),
        identity=SELLER_IDENTITY,
        nonces=MemoryNonceStore(clock=lambda: T0),
        clock=lambda: T0,
        keys=CONFORMANCE_KEYS,
    )
    assert w.verify(verifier, w.signed_commit(buyer_key)).directory is None


@pytest.mark.parametrize("encoding", ["gzip", "br", "deflate"])
def test_a_directory_with_a_content_encoding_is_refused(
    buyer_key: Ed25519Key, encoding: str
) -> None:
    server, now = Server([buyer_key]), [T0]
    server.headers["Content-Encoding"] = encoding
    expect_refusal(
        lambda: resolve(resolver(server, now), buyer_key.thumbprint), Refusal.DIRECTORY_ENCODING
    )


def test_the_cap_and_the_evidence_apply_to_the_bytes_on_the_wire(buyer_key: Ed25519Key) -> None:
    status, lines, body = w.signed_directory(BUYER, [buyer_key])
    headers = dict(lines.items())
    seen: list[str] = []

    class Raw(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            seen.append("raw")
            yield body

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status, headers={**headers, "Content-Encoding": "identity"}, stream=Raw()
        )

    resolved = run(over(httpx.MockTransport(handler)).resolve(BUYER_ORIGIN, buyer_key.thumbprint))
    assert resolved.directory is not None
    assert resolved.directory.evidence.body == body and seen == ["raw"]


def test_a_client_error_becomes_a_403_and_is_cached(buyer_key: Ed25519Key) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.InvalidURL("Invalid IPv4 address")

    r = over(httpx.MockTransport(handler))
    expect_refusal(lambda: resolve(r, buyer_key.thumbprint), Refusal.DIRECTORY_UNAVAILABLE)
    expect_refusal(lambda: resolve(r, buyer_key.thumbprint), Refusal.DIRECTORY_UNAVAILABLE)
    assert len(calls) == 1


def test_the_cache_uses_the_same_expires_boundary(buyer_key: Ed25519Key) -> None:
    server, now = Server([buyer_key], now=at(-86400 + 100)), [T0]  # expires at at(100)
    r = resolver(server, now)
    now[0] = at(99)
    resolve(r, buyer_key.thumbprint)
    server.fail()
    now[0] = at(100)  # stale, still valid at the boundary second; the failed refetch doesn't evict
    assert resolve(r, buyer_key.thumbprint).public_key == buyer_key.public_key
    now[0] = at(101 + 60)
    expect_refusal(lambda: resolve(r, buyer_key.thumbprint), Refusal.DIRECTORY_STATUS)


def test_a_fetcher_answering_from_another_origins_url_is_refused(buyer_key: Ed25519Key) -> None:
    # The evidence must name the origin it is attributed to.
    class Elsewhere(Fetcher):
        async def fetch(
            self, origin: Origin, *, limits: FetchLimits, now: UnixSeconds
        ) -> DirectoryResponse:
            fetched = await super().fetch(origin, limits=limits, now=now)
            other = Origin.parse("https://other.example.com")
            return DirectoryResponse(
                other.directory_url, fetched.status, fetched.headers, fetched.body, now
            )

    r = Clocked(DirectoryResolver(Elsewhere([buyer_key]), clock=lambda: T0), lambda: T0)
    expect_refusal(
        lambda: r.resolve(BUYER_ORIGIN, buyer_key.thumbprint), Refusal.DIRECTORY_UNAVAILABLE
    )
