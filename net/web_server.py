"""The browser-facing WebSocket transport: `net.tui_server.TuiServer` plus an
optional static host for the built web client.

Browsers cannot run Iroh QUIC (a custom-ALPN QUIC client only exists natively),
so the web client (`loreweaver-web`, a separate repo) speaks the SAME wire
protocol (`docs/protocol.md`) over WebSocket. This is `TuiServer` with a
`process_request` hook: plain HTTP GETs are answered from a static directory —
the built SPA — while WebSocket handshakes pass through untouched. One port,
one origin, no CORS.

Run it: `python -m app --web [--static-dir <path-to-web-dist>]`.
Without `--static-dir` the server still serves the WS endpoint (a reverse
proxy can host the web client); the operator is told so.
"""

from __future__ import annotations

import mimetypes
import re
import urllib.parse
from pathlib import Path

import websockets
from websockets.datastructures import Headers
from websockets.http11 import Response

from net.tui_server import (
    _WS_MEDIA_HEADER_SLACK,
    TuiServer,
    _build_ssl_context,
)

# The static directory's index document. Served for `/` and as the SPA
# fallback for any unknown path.
_INDEX = "index.html"
_MODULE_DOWNLOAD_PREFIX = "/__module-download/"
_MODULE_DOWNLOAD_TOKEN = re.compile(r"^[A-Za-z0-9_-]{40,}$")


class WebServer(TuiServer):
    """`TuiServer` + SPA static hosting. Same wire protocol, same keystore
    auth, same media channel — the browser carrier of the shared `SessionCore`."""

    def __init__(self, services, keystore, *, static_dir: str | Path | None = None, **kwargs):
        super().__init__(services, keystore, **kwargs)
        self.static_dir = Path(static_dir) if static_dir else None

    async def start(self) -> None:
        """Bind and start accepting connections (idempotent) — the `TuiServer`
        accept loop plus a `process_request` hook for static SPA files."""
        if self._server is None:
            ssl_context = _build_ssl_context(self.services.settings)
            tui = self.services.settings.tui
            max_size = max(tui.media_max_file_bytes, tui.audio_max_file_bytes) + _WS_MEDIA_HEADER_SLACK
            self._server = await websockets.serve(
                self.handle,
                self.host,
                self.port,
                ssl=ssl_context,
                max_size=max_size,
                process_request=self._process_request,
                # Same interop rule as `TuiServer.start`: no permessage-deflate
                # (the library's 12-bit window is rejected by strict browsers).
                compression=None,
            )

    async def _process_request(self, connection, request):
        """Answer plain HTTP GETs from the SPA; return `None` (the default
        `TuiServer` path) to keep the WebSocket handshake.

        The discriminator is the `Upgrade` header: a WS client sends
        `Upgrade: websocket`, a browser fetching an asset does not.
        """
        if self.static_dir is None:
            return None
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return None
        download = self._module_download_response(request.path)
        if download is not None:
            return download
        return self._static_response(request.path)

    def _module_download_response(self, raw_path: str) -> Response | None:
        """Serve one authenticated-by-capability pack export and consume it."""
        path_only = urllib.parse.urlsplit(raw_path).path
        if not path_only.startswith(_MODULE_DOWNLOAD_PREFIX):
            return None
        parts = path_only[len(_MODULE_DOWNLOAD_PREFIX) :].split("/", 1)
        if len(parts) != 2:
            return self._response(404, "Not Found", b"", "text/plain")
        token, raw_filename = parts
        filename = urllib.parse.unquote(raw_filename)
        if not _MODULE_DOWNLOAD_TOKEN.fullmatch(token):
            return self._response(404, "Not Found", b"", "text/plain")
        if not filename or Path(filename).name != filename or Path(filename).suffix.casefold() != ".lwpack":
            return self._response(404, "Not Found", b"", "text/plain")
        source = Path(self.services.settings.data_dir).resolve() / "module_exports" / f"{token}.lwpack"
        try:
            body = source.read_bytes()
            source.unlink(missing_ok=True)
        except OSError:
            return self._response(404, "Not Found", b"", "text/plain")
        safe_filename = filename.replace('"', "")
        return self._response(
            200,
            "OK",
            body,
            "application/zip",
            cache="no-store",
            extra_headers={"Content-Disposition": f'attachment; filename="{safe_filename}"'},
        )

    def _static_response(self, raw_path: str) -> Response | None:
        """Resolve one HTTP path to a `Response` under the static root,
        traversal-safe.

        The SPA fallback: any unknown path serves `index.html`, so deep links
        render the app shell rather than a 404 — this client has no server-side
        routes of its own. Returns `None` only for a genuinely unresolvable
        path (missing root), which the caller turns into a 404.
        """
        root = self.static_dir.resolve()
        path_only = raw_path.split("?", 1)[0]
        rel = urllib.parse.unquote(path_only).lstrip("/")
        try:
            if rel == "":
                candidate = root / _INDEX
            else:
                candidate = (root / rel).resolve()
                if candidate != root and root not in candidate.parents:
                    return self._response(404, "Not Found", b"", "text/plain")
                if candidate.is_dir():
                    candidate = candidate / _INDEX
                if not candidate.is_file():
                    # SPA fallback (unknown path, or a dir without an index).
                    candidate = root / _INDEX
                if not candidate.is_file():
                    return self._response(404, "Not Found", b"", "text/plain")
            body = candidate.read_bytes()
            content_type = mimetypes.guess_type(candidate.name)[0]
            # Hashed build assets are immutable; the shell is not. Asset
            # filenames carry a content hash (`index-<hash>.js`), so a long
            # cache for everything except the shell is safe and cheap.
            if candidate.name == _INDEX:
                cache = "no-cache"
            else:
                cache = "public, max-age=31536000, immutable"
            return self._response(200, "OK", body, content_type, cache=cache)
        except OSError:
            return self._response(404, "Not Found", b"", "text/plain")

    @staticmethod
    def _response(
        status: int,
        reason: str,
        body: bytes,
        content_type: str | None,
        *,
        cache: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Response:
        headers = Headers()
        if content_type:
            headers["Content-Type"] = content_type
        if cache:
            headers["Cache-Control"] = cache
        if extra_headers:
            for name, value in extra_headers.items():
                headers[name] = value
        return Response(status, reason, headers, body)
