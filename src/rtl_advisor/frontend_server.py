from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from rtl_advisor.config import ProjectConfig
from rtl_advisor.frontend_api import FrontendAPIError, FrontendDataStore


FRONTEND_ASSET_ROOT = Path(__file__).with_name("frontend")


class FrontendServerError(RuntimeError):
    """Raised when the local frontend cannot be served safely."""


class FrontendHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], config: ProjectConfig):
        self.data_store = FrontendDataStore(config)
        super().__init__(address, FrontendRequestHandler)


class FrontendRequestHandler(BaseHTTPRequestHandler):
    server: FrontendHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_bytes(
        self,
        status: HTTPStatus,
        payload: bytes,
        *,
        content_type: str,
        cache_control: str = "no-store",
    ) -> None:
        self.send_response(status.value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
        self._send_bytes(
            status,
            body,
            content_type="application/json; charset=utf-8",
        )

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._send_json(
            status,
            {
                "api_version": "v1",
                "error": {
                    "status": status.value,
                    "message": message,
                },
            },
        )

    @staticmethod
    def _single_query(query: dict[str, list[str]], name: str) -> str | None:
        values = query.get(name)
        if not values:
            return None
        return values[-1]

    def _api_get(self, parsed) -> None:
        path = parsed.path.rstrip("/") or "/"
        store = self.server.data_store
        try:
            if path == "/api/v1/health":
                self._send_json(HTTPStatus.OK, store.health())
                return
            if path == "/api/v1/contract":
                self._send_json(HTTPStatus.OK, store.contract())
                return
            if path == "/api/v1/overview":
                self._send_json(HTTPStatus.OK, store.overview())
                return
            if path == "/api/v1/cases":
                query = parse_qs(parsed.query, keep_blank_values=False)
                try:
                    limit = int(self._single_query(query, "limit") or "50")
                    offset = int(self._single_query(query, "offset") or "0")
                except ValueError as exc:
                    raise FrontendAPIError("limit and offset must be integers") from exc
                result = store.cases(
                    family=self._single_query(query, "family"),
                    category=self._single_query(query, "category"),
                    query=self._single_query(query, "q"),
                    limit=limit,
                    offset=offset,
                )
                self._send_json(HTTPStatus.OK, result)
                return
            prefix = "/api/v1/cases/"
            if path.startswith(prefix):
                case_id = unquote(path[len(prefix) :])
                self._send_json(HTTPStatus.OK, store.case_detail(case_id))
                return
            self._error(HTTPStatus.NOT_FOUND, "API route not found")
        except FrontendAPIError as exc:
            status = (
                HTTPStatus.NOT_FOUND
                if str(exc).startswith("unknown diagnostic case")
                else HTTPStatus.BAD_REQUEST
            )
            self._error(status, str(exc))

    def _static_get(self, path: str) -> None:
        route = path if path != "/" else "/index.html"
        relative = Path(unquote(route).lstrip("/"))
        if relative.is_absolute() or ".." in relative.parts:
            self._error(HTTPStatus.BAD_REQUEST, "invalid asset path")
            return
        asset_path = FRONTEND_ASSET_ROOT / relative
        if not asset_path.is_file():
            self._error(HTTPStatus.NOT_FOUND, "asset not found")
            return
        content_type = mimetypes.guess_type(asset_path.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {
            "application/javascript",
            "application/json",
        }:
            content_type += "; charset=utf-8"
        try:
            payload = asset_path.read_bytes()
        except OSError as exc:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"could not read asset: {exc}")
            return
        self._send_bytes(
            HTTPStatus.OK,
            payload,
            content_type=content_type,
            cache_control="no-cache",
        )

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self._api_get(parsed)
        else:
            self._static_get(parsed.path)

    def do_HEAD(self) -> None:
        self._error(HTTPStatus.METHOD_NOT_ALLOWED, "HEAD is not supported")

    def do_POST(self) -> None:
        self._error(
            HTTPStatus.METHOD_NOT_ALLOWED,
            "The V2.2 frontend is read-only; live analysis unlocks after V2.3 passes.",
        )


def create_frontend_server(
    config: ProjectConfig,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> FrontendHTTPServer:
    if not 0 <= port <= 65535:
        raise FrontendServerError("frontend port must be between 0 and 65535")
    if not FRONTEND_ASSET_ROOT.is_dir():
        raise FrontendServerError(f"frontend assets missing: {FRONTEND_ASSET_ROOT}")
    try:
        server = FrontendHTTPServer((host, port), config)
    except OSError as exc:
        raise FrontendServerError(
            f"could not bind frontend to {host}:{port}: {exc}"
        ) from exc
    try:
        server.data_store.health()
    except FrontendAPIError:
        server.server_close()
        raise
    return server


def serve_frontend(
    config: ProjectConfig,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> None:
    server = create_frontend_server(config, host=host, port=port)
    actual_host, actual_port = server.server_address[:2]
    print(f"RTL Advisor frontend: http://{actual_host}:{actual_port}")
    print("  mode              read-only V2.2 calibration evidence")
    print("  stop              Ctrl-C")
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("\nRTL Advisor frontend stopped")
    finally:
        server.server_close()
