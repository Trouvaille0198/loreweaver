"""Tests for the browser-facing WebSocket transport (`net.web_server.WebServer`):
the shared `SessionCore` over WebSocket PLUS the SPA static host on the same
port. A real `WebServer` is bound to an ephemeral localhost port and driven by
a real `websockets` WS client and an `aiohttp` HTTP client, so both halves of
the combined server are exercised over the actual wire.
"""

from __future__ import annotations

import asyncio
import json

import aiohttp
import websockets

from agent.services import build_services
from infra.config import Settings
from infra.embeddings import FakeEmbeddings
from infra.llm import FakeLLM
from net.keystore import Keystore
from net.web_server import WebServer


def _services():
    return build_services(
        Settings(locale="en"), llm=FakeLLM(script=[]), embeddings=FakeEmbeddings(64)
    )


def _index_html(tmp_path) -> str:
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><title>lw web</title>", encoding="utf-8")
    (static / "app.js").write_text("console.log('lw')", encoding="utf-8")
    (static / "asset-123.js").write_text("// hashed asset", encoding="utf-8")
    return str(static)


async def _start(server: WebServer) -> str:
    await server.start()
    return f"http://127.0.0.1:{server.bound_port}/"


async def test_web_server_serves_the_spa_and_handshakes_ws_on_one_port(tmp_path):
    """The combined server: plain HTTP GETs answer from the static dir, WS
    handshakes pass through to the shared SessionCore — one port, one origin."""
    services = _services()
    keystore = Keystore()
    key = keystore.add(room="r1", name="alice", role="player")
    static = _index_html(tmp_path)
    server = WebServer(services, keystore, host="127.0.0.1", port=0, static_dir=static)
    base = await _start(server)
    try:
        # HTTP half: the SPA shell, a hashed asset, and a 404 for traversal.
        async with aiohttp.ClientSession() as http:
            async with http.get(base) as resp:
                assert resp.status == 200
                assert resp.headers["Content-Type"].startswith("text/html")
                assert (await resp.text()).startswith("<!doctype html>")
            async with http.get(base + "asset-123.js") as resp:
                assert resp.status == 200
                assert resp.headers["Content-Type"] == "text/javascript"
                assert resp.headers["Cache-Control"] == "public, max-age=31536000, immutable"
            # Unknown paths fall back to the SPA shell, not a hard 404.
            async with http.get(base + "some/deep/link") as resp:
                assert resp.status == 200
                assert (await resp.text()).startswith("<!doctype html>")
            # Path traversal is refused.
            async with http.get(base + "..%2F..%2Fetc%2Fpasswd") as resp:
                assert resp.status == 404

        # WS half: the same server answers a `join` handshake.
        ws_base = base.replace("http://", "ws://")
        async with websockets.connect(ws_base) as ws:
            await ws.send(json.dumps({"type": "join", "key": key}))
            welcome = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert welcome["type"] == "welcome"
            assert welcome["room"] == "r1"
            assert welcome["you"]["name"] == "alice"
    finally:
        await server.close()
        services.store.close()


async def test_web_server_without_static_dir_still_serves_ws(tmp_path):
    """`--web` without a static dir is still a full WS endpoint (a reverse
    proxy can host the web client); HTTP GETs just get no static answer."""
    services = _services()
    keystore = Keystore()
    key = keystore.add(room="r2", name="bob", role="keeper")
    server = WebServer(services, keystore, host="127.0.0.1", port=0, static_dir=None)
    base = await _start(server)
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(base) as resp:
                # No static dir → the upgrade-less GET has no SPA answer; the
                # server refuses the non-upgrade request (426 Upgrade Required).
                assert resp.status == 426

        ws_base = base.replace("http://", "ws://")
        async with websockets.connect(ws_base) as ws:
            await ws.send(json.dumps({"type": "join", "key": key}))
            welcome = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert welcome["type"] == "welcome"
            assert welcome["you"]["role"] == "keeper"
    finally:
        await server.close()
        services.store.close()
