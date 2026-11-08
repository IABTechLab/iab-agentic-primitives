"""The sync runtime and façades against real directory servers over one pooled client.

A uvicorn server on localhost serves each signed key directory. The injected
``AsyncClient`` keeps its connections alive, so a call run on any loop but the
resolver's one would strand the pool on a closed loop and fail.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import threading
import time
import warnings
from collections.abc import Callable, Iterator

import httpx
import pytest
import uvicorn

from iab_agentic_primitives.web_bot_auth import (
    CONFORMANCE_KEYS,
    CachePolicy,
    ConfigurationError,
    DirectoryPublisher,
    DirectoryResolver,
    HTTPRequest,
    HTTPResponse,
    MemoryNonceStore,
    Origin,
    RequestSigner,
    RequestVerifier,
    ResponseSigner,
    ResponseVerifier,
    ServerIdentity,
    StaticKeyResolver,
    VerifiedResponse,
    system_clock,
)
from iab_agentic_primitives.web_bot_auth.asgi import (
    ASGIApp,
    DirectoryMiddleware,
    Receive,
    Scope,
    Send,
)
from iab_agentic_primitives.web_bot_auth.fetcher import HttpxDirectoryFetcher
from iab_agentic_primitives.web_bot_auth.httpx import WebBotAuth, verified_response
from iab_agentic_primitives.web_bot_auth.sync import (
    SyncRequestVerifier,
    SyncResponseVerifier,
    SyncRuntime,
)

from .support import (
    BUYER,
    BUYER_ORIGIN,
    COMMIT_URL,
    JSON,
    SELLER,
    SELLER_IDENTITY,
    SELLER_ORIGIN,
    buyer_key,
    seller_key,
)


async def _not_found(scope: Scope, receive: Receive, send: Send) -> None:
    await send({"type": "http.response.start", "status": 404, "headers": []})
    await send({"type": "http.response.body", "body": b""})


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


#: Directory fetches served, by host.
FETCHES: dict[str, int] = {}


def _counting(host: str, app: ASGIApp) -> ASGIApp:
    async def counted(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            FETCHES[host] = FETCHES.get(host, 0) + 1
        await app(scope, receive, send)

    return counted


@pytest.fixture(scope="module")
def ports() -> Iterator[dict[str, int]]:
    """One uvicorn directory server per identity, by host."""
    servers: list[tuple[uvicorn.Server, threading.Thread]] = []
    ports: dict[str, int] = {}
    for origin, key in ((BUYER, buyer_key()), (SELLER, seller_key())):
        identity = ServerIdentity.from_config(origin)
        app = _counting(
            identity.origin.host,
            DirectoryMiddleware(_not_found, DirectoryPublisher(identity.origin, [key])),
        )
        port = _free_port()
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        servers.append((server, thread))
        ports[identity.origin.host] = port
    deadline = time.monotonic() + 10
    while not all(server.started for server, _ in servers):
        assert time.monotonic() < deadline, "the directory servers didn't start"
        time.sleep(0.01)
    yield ports
    for server, thread in servers:
        server.should_exit = True
        thread.join(timeout=5)


class _Local(httpx.AsyncHTTPTransport):
    """A real pooled transport that sends each identity's directory fetch to its server."""

    def __init__(self, ports: dict[str, int]) -> None:
        super().__init__()
        self.ports = ports

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # The client's own request keeps the identity's URL; only the connection goes local.
        port = self.ports[request.url.host]
        url = request.url.copy_with(scheme="http", host="127.0.0.1", port=port)
        local = httpx.Request(request.method, url, headers=request.headers, stream=request.stream)
        return await super().handle_async_request(local)


def _resolver(ports: dict[str, int]) -> DirectoryResolver:
    # max_cache_age=0: every verify fetches again, over the same pool.
    client = httpx.AsyncClient(transport=_Local(ports))
    return DirectoryResolver(HttpxDirectoryFetcher(client), cache=CachePolicy(max_cache_age=0))


async def _failing(resolver: DirectoryResolver, origin: str) -> bool:
    """True when the resolver's store holds a failed fetch of ``origin``."""
    until = await resolver.store.failure_until(Origin.parse(origin))
    return until is not None and system_clock() < until


def _signed() -> HTTPRequest:
    return RequestSigner.of(buyer_key(), BUYER).sign(HTTPRequest("GET", COMMIT_URL))


def test_a_hundred_sync_verifies_share_one_pooled_client(ports: dict[str, int]) -> None:
    resolver = _resolver(ports)
    verifier = RequestVerifier(
        resolver, identity=SELLER_IDENTITY, nonces=MemoryNonceStore(), keys=CONFORMANCE_KEYS
    )
    before = FETCHES.get("buyer.example.com", 0)
    with SyncRuntime(resolver) as runtime:
        sync = SyncRequestVerifier(verifier, runtime=runtime)
        origins = {str(sync.verify(_signed()).origin) for _ in range(100)}
        # Every call refetched, and none failed: a stranded pool fails a fetch, and the
        # cached directory would hide it from the verdict.
        assert not runtime.run(_failing(resolver, BUYER))
    assert origins == {BUYER}
    assert FETCHES["buyer.example.com"] - before == 100


def test_two_facades_over_one_resolver_share_its_runtime(ports: dict[str, int]) -> None:
    resolver = _resolver(ports)
    requests = RequestVerifier(
        resolver, identity=SELLER_IDENTITY, nonces=MemoryNonceStore(), keys=CONFORMANCE_KEYS
    )
    with SyncRuntime(resolver) as runtime:
        first = SyncRequestVerifier(requests, runtime=runtime)
        second = SyncRequestVerifier(requests, runtime=runtime)
        assert first.verify(_signed()).origin == second.verify(_signed()).origin


def test_a_sync_verify_inside_a_running_loop_raises(ports: dict[str, int]) -> None:
    resolver = _resolver(ports)
    verifier = RequestVerifier(
        resolver, identity=SELLER_IDENTITY, nonces=MemoryNonceStore(), keys=CONFORMANCE_KEYS
    )

    async def inside(sync: SyncRequestVerifier) -> None:
        sync.verify(_signed())

    with SyncRuntime(resolver) as runtime, pytest.raises(RuntimeError, match="running"):
        asyncio.run(inside(SyncRequestVerifier(verifier, runtime=runtime)))


def test_close_stops_the_runtime_thread(ports: dict[str, int]) -> None:
    before = set(threading.enumerate())
    runtime = SyncRuntime(_resolver(ports))
    owned = set(threading.enumerate()) - before
    assert len(owned) == 1
    runtime.close()
    assert not owned & set(threading.enumerate())
    runtime.close()  # a second close does nothing
    sync = SyncResponseVerifier(
        ResponseVerifier(_resolver(ports), keys=CONFORMANCE_KEYS), runtime=runtime
    )
    unsigned = HTTPResponse(200, {}, b"", HTTPRequest("GET", f"{SELLER}/proposals/prop_7"))
    with pytest.raises(ConfigurationError, match="closed"):
        sync.verify(unsigned)


def _seller(signer: ResponseSigner) -> httpx.MockTransport:
    def answer(request: httpx.Request) -> httpx.Response:
        incoming = HTTPRequest(
            request.method, str(request.url), request.headers.multi_items(), request.content
        )
        body = json.dumps({"ok": True}).encode()
        signed = signer.sign(HTTPResponse(200, JSON, body, incoming))
        return httpx.Response(200, headers=signed.headers.items(), content=body)

    return httpx.MockTransport(answer)


def test_a_sync_client_verifies_every_response_over_one_pooled_client(
    ports: dict[str, int],
) -> None:
    resolver = _resolver(ports)
    before = FETCHES.get("seller.example.com", 0)
    with SyncRuntime(resolver) as runtime:
        auth = WebBotAuth(
            RequestSigner.of(buyer_key(), BUYER),
            ResponseVerifier(resolver, keys=CONFORMANCE_KEYS),
            runtime=runtime,
        )
        seller = _seller(ResponseSigner.of(seller_key(), SELLER))
        with auth.client(transport=seller) as client:
            bound = [verified_response(client.post(COMMIT_URL, json={})) for _ in range(20)]
        assert not runtime.run(_failing(resolver, SELLER))
    assert all(v is not None and v.bound and str(v.origin) == SELLER for v in bound)
    assert FETCHES["seller.example.com"] - before == 20


def test_one_auth_serves_a_sync_and_an_async_client(ports: dict[str, int]) -> None:
    resolver = _resolver(ports)
    seller = _seller(ResponseSigner.of(seller_key(), SELLER))
    with SyncRuntime(resolver) as runtime:
        auth = WebBotAuth(
            RequestSigner.of(buyer_key(), BUYER),
            ResponseVerifier(resolver, keys=CONFORMANCE_KEYS),
            runtime=runtime,
        )
        with auth.client(transport=seller) as client:
            first = [verified_response(client.post(COMMIT_URL, json={})) for _ in range(3)]

        async def post() -> list[VerifiedResponse | None]:
            async with auth.async_client(transport=seller) as client:
                return [verified_response(await client.post(COMMIT_URL, json={})) for _ in range(3)]

        second = asyncio.run(post())
        with auth.client(transport=seller) as client:
            third = [verified_response(client.post(COMMIT_URL, json={})) for _ in range(3)]
    verified = [*first, *second, *third]
    assert all(v is not None and v.origin == SELLER_ORIGIN for v in verified)


def test_a_sync_client_without_a_runtime_is_refused_as_configuration() -> None:
    auth = WebBotAuth(
        RequestSigner.of(buyer_key(), BUYER),
        ResponseVerifier(StaticKeyResolver({}), keys=CONFORMANCE_KEYS),
    )
    seller = _seller(ResponseSigner.of(seller_key(), SELLER))
    with pytest.raises(ConfigurationError, match="SyncRuntime"):
        auth.client(transport=seller)


def test_closed_sync_clients_leave_no_thread_behind(ports: dict[str, int]) -> None:
    before = set(threading.enumerate())
    resolver = _resolver(ports)
    seller = _seller(ResponseSigner.of(seller_key(), SELLER))
    with SyncRuntime(resolver) as runtime:
        for _ in range(20):
            auth = WebBotAuth(
                RequestSigner.of(buyer_key(), BUYER),
                ResponseVerifier(resolver, keys=CONFORMANCE_KEYS),
                runtime=runtime,
            )
            with auth.client(transport=seller) as client:
                assert verified_response(client.post(COMMIT_URL, json={})) is not None
        assert len(set(threading.enumerate()) - before) == 1  # the runtime's, and only it
    assert not set(threading.enumerate()) - before


async def _hang(request: httpx.Request) -> httpx.Response:
    await asyncio.sleep(3600)
    raise AssertionError("unreachable")


def test_closing_the_runtime_mid_verify_frees_the_caller_in_time() -> None:
    hanging = DirectoryResolver(HttpxDirectoryFetcher(transport=httpx.MockTransport(_hang)))
    runtime = SyncRuntime(hanging)
    sync = SyncRequestVerifier(
        RequestVerifier(
            hanging, identity=SELLER_IDENTITY, nonces=MemoryNonceStore(), keys=CONFORMANCE_KEYS
        ),
        runtime=runtime,
    )
    outcome: list[BaseException] = []

    def call() -> None:
        try:
            sync.verify(_signed())
        except BaseException as e:  # the test reads what the caller got
            outcome.append(e)

    caller = threading.Thread(target=call)
    caller.start()
    time.sleep(0.2)  # the fetch is in flight
    started = time.monotonic()
    runtime.close()
    caller.join(timeout=2)
    assert not caller.is_alive() and time.monotonic() - started < 2
    assert len(outcome) == 1 and isinstance(outcome[0], ConfigurationError)


def test_a_cancelled_verify_leaves_the_next_one_to_succeed(ports: dict[str, int]) -> None:
    # A verify cancelled while its fetch is in flight leaves no dead fetch behind it.
    resolver = _resolver(ports)
    verifier = RequestVerifier(
        resolver, identity=SELLER_IDENTITY, nonces=MemoryNonceStore(), keys=CONFORMANCE_KEYS
    )

    async def go() -> str:
        task = asyncio.ensure_future(verifier.verify(_signed()))
        await asyncio.sleep(0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return str((await verifier.verify(_signed())).origin)

    assert asyncio.run(go()) == BUYER


def test_a_resolver_belongs_to_the_loop_it_first_resolves_on(ports: dict[str, int]) -> None:
    resolver = _resolver(ports)
    verifier = RequestVerifier(
        resolver, identity=SELLER_IDENTITY, nonces=MemoryNonceStore(), keys=CONFORMANCE_KEYS
    )
    assert asyncio.run(verifier.verify(_signed())).origin == BUYER_ORIGIN
    with pytest.raises(ConfigurationError, match="another event loop"):
        asyncio.run(verifier.verify(_signed()))


def _in_a_forked_child(call: Callable[[], object]) -> str:
    """Runs ``call`` in a forked child and returns what it ended with, as ``Type: message``.

    The parent reads the child's answer until the child exits, with no
    timeout: a call that hangs in the child hangs the test.
    """
    readable, writable = os.pipe()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)  # fork with the runtime's thread
        pid = os.fork()
    if pid == 0:  # the child: report, and leave without running the parent's teardown
        os.close(readable)
        try:
            call()
            outcome = "returned"
        except BaseException as e:  # the test reads whatever the child ended with
            outcome = f"{type(e).__name__}: {e}"
        os.write(writable, outcome.encode())
        os._exit(0)
    os.close(writable)
    with os.fdopen(readable, "rb") as answer:
        outcome = answer.read().decode()
    os.waitpid(pid, 0)
    return outcome


def test_a_runtime_made_before_a_fork_refuses_the_childs_call_at_once() -> None:
    # A pre-fork server (gunicorn --preload) builds the app, and the runtime, in the parent.
    verifier = RequestVerifier(
        StaticKeyResolver({BUYER_ORIGIN: [buyer_key()]}),
        identity=SELLER_IDENTITY,
        nonces=MemoryNonceStore(),
        keys=CONFORMANCE_KEYS,
    )
    with SyncRuntime() as runtime:
        sync = SyncRequestVerifier(verifier, runtime=runtime)
        assert sync.verify(_signed()).origin == BUYER_ORIGIN
        outcome = _in_a_forked_child(lambda: sync.verify(_signed()))
        assert outcome.startswith("ConfigurationError: ") and "before a fork" in outcome
        assert sync.verify(_signed()).origin == BUYER_ORIGIN  # the parent's runtime still runs


def test_a_runtime_made_before_a_fork_and_never_used_refuses_the_child_too() -> None:
    verifier = RequestVerifier(
        StaticKeyResolver({BUYER_ORIGIN: [buyer_key()]}),
        identity=SELLER_IDENTITY,
        nonces=MemoryNonceStore(),
        keys=CONFORMANCE_KEYS,
    )
    with SyncRuntime() as runtime:
        sync = SyncRequestVerifier(verifier, runtime=runtime)

        def call_then_close() -> None:
            try:
                sync.verify(_signed())
            finally:
                runtime.close()  # closing in the child leaves the parent's loop alone

        outcome = _in_a_forked_child(call_then_close)
        assert outcome.startswith("ConfigurationError: ") and "before a fork" in outcome
        assert sync.verify(_signed()).origin == BUYER_ORIGIN


def test_a_sync_client_made_before_a_fork_refuses_the_childs_request_at_once() -> None:
    sellers_keys = StaticKeyResolver({SELLER_ORIGIN: [seller_key()]})
    seller = _seller(ResponseSigner.of(seller_key(), SELLER))
    with SyncRuntime() as runtime:
        auth = WebBotAuth(
            RequestSigner.of(buyer_key(), BUYER),
            ResponseVerifier(sellers_keys, keys=CONFORMANCE_KEYS),
            runtime=runtime,
        )
        with auth.client(transport=seller) as client:
            assert verified_response(client.post(COMMIT_URL, json={})) is not None
            outcome = _in_a_forked_child(lambda: client.post(COMMIT_URL, json={}))
            assert outcome.startswith("ConfigurationError: ") and "before a fork" in outcome
            assert verified_response(client.post(COMMIT_URL, json={})) is not None
