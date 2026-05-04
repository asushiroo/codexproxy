from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import Mapping
from pathlib import Path

from aiohttp import ClientError, ClientSession, ClientTimeout, web
from yarl import URL

from codexproxy.debug_record import (
    build_debug_record,
    build_http_message_snapshot,
    format_debug_record_summary,
    save_debug_record,
)
from codexproxy.expiry_manager import ExpiryManager
from codexproxy.state import (
    ClientApiKeyNotConfiguredError,
    ClientNameNotConfiguredError,
    ConfigStore,
    RequestLimitReachedError,
)
from codexproxy.usage_page import render_usage_page

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
CONFIG_STORE_KEY = web.AppKey("config_store", ConfigStore)
CLIENT_SESSION_KEY = web.AppKey("client_session", ClientSession)
EXPIRY_MANAGER_KEY = web.AppKey("expiry_manager", ExpiryManager)


def build_target_url(base_url: str, request_url: URL) -> URL:
    upstream = URL(base_url)
    upstream_path = upstream.path.rstrip("/")
    request_path = request_url.path

    if upstream_path and request_path:
        merged_path = f"{upstream_path}{request_path}"
    else:
        merged_path = upstream_path or request_path or "/"

    return upstream.with_path(merged_path).with_query(request_url.query)


def get_current_host_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as candidate:
            candidate.connect(("8.8.8.8", 80))
            detected_ip = candidate.getsockname()[0]
            if detected_ip:
                return str(detected_ip)
    except OSError:
        pass

    try:
        detected_ip = socket.gethostbyname(socket.gethostname())
        if detected_ip:
            return str(detected_ip)
    except OSError:
        pass

    return "127.0.0.1"


def build_client_base_url(listen_host: str, port: int, advertise_host: str | None = None) -> str:
    host = advertise_host or listen_host
    if host in {"0.0.0.0", "::"}:
        host = get_current_host_ip()
    return f"http://{host}:{port}"


def format_request_log_line(
    *,
    method: str,
    path: str,
    port: int,
    client_base_url: str,
    status: int,
    name: str | None = None,
    count: int | None = None,
    limit: int | None = None,
    detail: str | None = None,
) -> str:
    parts = [
        "REQUEST",
        f"method={method}",
        f"path={path}",
        f"port={port}",
    ]
    if name:
        parts.append(f"name={name}")
    parts.append(f"status={status}")
    if count is not None and limit is not None:
        parts.append(f"count={count}/{limit}")
    parts.append(f"client_base_url={client_base_url}")
    if detail:
        parts.append(f"detail={detail}")
    return " ".join(parts)


def format_record_log_line(
    *,
    direction: str,
    method: str,
    path: str,
    port: int,
    body: str,
    status: int | None = None,
    content_type: str | None = None,
) -> str:
    parts = [
        "RECORD",
        f"direction={direction}",
        f"method={method}",
        f"path={path}",
        f"port={port}",
    ]
    if status is not None:
        parts.append(f"status={status}")
    if content_type:
        parts.append(f"content_type={content_type}")
    parts.append(f"body={body}")
    return " ".join(parts)


def create_app(store: ConfigStore, expiry_manager: ExpiryManager | None = None) -> web.Application:
    app = web.Application()
    app[CONFIG_STORE_KEY] = store
    if expiry_manager is not None:
        app[EXPIRY_MANAGER_KEY] = expiry_manager

    async def on_startup(application: web.Application) -> None:
        application[CLIENT_SESSION_KEY] = ClientSession(timeout=ClientTimeout(total=None))
        if expiry_manager is not None:
            expiry_manager.start()

    async def on_cleanup(application: web.Application) -> None:
        if expiry_manager is not None:
            await expiry_manager.stop()
        await application[CLIENT_SESSION_KEY].close()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_get("/{client_name}/usage", handle_usage_request)
    app.router.add_route("*", "/{tail:.*}", handle_proxy_request)
    return app


async def handle_usage_request(request: web.Request) -> web.Response:
    store = request.app[CONFIG_STORE_KEY]
    client_name = request.match_info["client_name"]

    try:
        binding = store.get_client_by_name(client_name)
    except ClientNameNotConfiguredError as exc:
        raise web.HTTPNotFound(text="client name is not configured") from exc

    return web.Response(
        text=render_usage_page(
            binding,
            expiry_status=(
                request.app[EXPIRY_MANAGER_KEY].get_status()
                if EXPIRY_MANAGER_KEY in request.app
                else None
            ),
        ),
        content_type="text/html",
    )


async def handle_proxy_request(request: web.Request) -> web.StreamResponse:
    store = request.app[CONFIG_STORE_KEY]
    session = request.app[CLIENT_SESSION_KEY]
    local_port = _resolve_local_port(request)
    client_base_url = build_client_base_url(store.listen_host, local_port, store.advertise_host)
    client_api_key = _extract_client_api_key(request.headers)

    if client_api_key is None:
        print(
            format_request_log_line(
                method=request.method,
                path=request.path_qs,
                port=local_port,
                client_base_url=client_base_url,
                status=401,
                detail="missing-api-key",
            )
        )
        return web.json_response(
            {
                "error": "missing api key",
                "detail": "Provide Authorization: Bearer <key>, api-key, or x-api-key.",
            },
            status=401,
        )

    try:
        binding = store.reserve_request(client_api_key)
    except ClientApiKeyNotConfiguredError:
        print(
            format_request_log_line(
                method=request.method,
                path=request.path_qs,
                port=local_port,
                client_base_url=client_base_url,
                status=403,
                detail="api-key-not-configured",
            )
        )
        return web.json_response(
            {"error": "client api key is not configured"},
            status=403,
        )
    except RequestLimitReachedError as exc:
        print(
            format_request_log_line(
                method=request.method,
                path=request.path_qs,
                port=local_port,
                name=exc.client_name,
                status=429,
                count=exc.count,
                limit=exc.limit,
                client_base_url=client_base_url,
                detail="request-limit-reached",
            )
        )
        return web.json_response(
            {"error": "request limit reached", "client": exc.client_name, "limit": exc.limit},
            status=429,
        )

    target_url = build_target_url(binding.base_url, request.rel_url)
    forwarded_headers = _forward_request_headers(request.headers)
    _replace_upstream_auth_headers(forwarded_headers, binding.upstream_api_key)

    has_request_body = request.can_read_body
    downstream_request_body = b""
    request_body = request.content.iter_chunked(64 * 1024) if has_request_body else None
    if store.record:
        downstream_request_body = await request.read() if has_request_body else b""
        request_body = downstream_request_body if has_request_body else None

    downstream_request_snapshot = None
    upstream_request_snapshot = None
    if store.record:
        downstream_request_snapshot = build_http_message_snapshot(
            method=request.method,
            url=str(request.url),
            headers=request.headers,
            body=downstream_request_body,
        )
        upstream_request_snapshot = build_http_message_snapshot(
            method=request.method,
            url=str(target_url),
            headers=forwarded_headers,
            body=downstream_request_body,
        )

    try:
        async with session.request(
            method=request.method,
            url=target_url,
            headers=forwarded_headers,
            data=request_body,
            allow_redirects=False,
            auto_decompress=False,
        ) as upstream_response:
            logged_binding = binding
            log_detail = None
            if upstream_response.status >= 400:
                logged_binding = store.rollback_request(client_api_key)
                log_detail = "upstream-status-error"
            print(
                format_request_log_line(
                    method=request.method,
                    path=request.path_qs,
                    port=local_port,
                    name=binding.name,
                    status=upstream_response.status,
                    count=logged_binding.count,
                    limit=logged_binding.limit,
                    client_base_url=client_base_url,
                    detail=log_detail,
                )
            )
            downstream = web.StreamResponse(
                status=upstream_response.status,
                reason=upstream_response.reason,
                headers=_forward_response_headers(upstream_response.headers),
            )
            await downstream.prepare(request)
            recorded_response_body = bytearray() if store.record else None
            async for chunk in upstream_response.content.iter_chunked(64 * 1024):
                if recorded_response_body is not None:
                    recorded_response_body.extend(chunk)
                await downstream.write(chunk)
            await downstream.write_eof()

            if recorded_response_body is not None and downstream_request_snapshot and upstream_request_snapshot:
                upstream_response_snapshot = build_http_message_snapshot(
                    method=request.method,
                    url=str(target_url),
                    headers=upstream_response.headers,
                    body=bytes(recorded_response_body),
                    status=upstream_response.status,
                    reason=upstream_response.reason,
                )
                _record_debug_artifacts(
                    client_name=binding.name,
                    port=local_port,
                    downstream_request=downstream_request_snapshot,
                    upstream_request=upstream_request_snapshot,
                    upstream_response=upstream_response_snapshot,
                )
            return downstream
    except ClientError as exc:
        rolled_back_binding = store.rollback_request(client_api_key)
        print(
            format_request_log_line(
                method=request.method,
                path=request.path_qs,
                port=local_port,
                name=binding.name,
                status=502,
                count=rolled_back_binding.count,
                limit=rolled_back_binding.limit,
                client_base_url=client_base_url,
                detail="upstream-request-failed",
            )
        )
        if store.record and downstream_request_snapshot and upstream_request_snapshot:
            _record_debug_artifacts(
                client_name=binding.name,
                port=local_port,
                downstream_request=downstream_request_snapshot,
                upstream_request=upstream_request_snapshot,
                upstream_error={
                    "type": type(exc).__name__,
                    "message": str(exc),
                },
            )
        return web.json_response(
            {
                "error": "upstream request failed",
                "client": binding.name,
                "base_url": binding.base_url,
                "detail": "upstream-request-failed",
            },
            status=502,
        )


async def run_proxy(config_path: Path, *, expire_time: str | None = None) -> None:
    store = ConfigStore.from_path(config_path)
    expiry_manager = ExpiryManager.from_runtime(
        config_path=config_path,
        expire_time_text=expire_time,
        on_update_success=store.reset_all,
    )
    print(f"Expire time: {expiry_manager.expire_time_text}")
    print(f"Codex executable: {expiry_manager.codex_executable}")

    app = create_app(store, expiry_manager=expiry_manager)
    runner = web.AppRunner(app)
    await runner.setup()

    client_base_url = build_client_base_url(
        listen_host=store.listen_host,
        port=store.listen_port,
        advertise_host=store.advertise_host,
    )
    site = web.TCPSite(runner, host=store.listen_host, port=store.listen_port)
    try:
        await site.start()
        clients = store.list_clients()
        print(
            f"Client base_url: {client_base_url} "
            f"(clients={len(clients)}, upstream_base_url={store.base_url})"
        )
        for client in clients:
            print(f"Configured client: name={client.name} limit={client.limit} count={client.count}")
        await asyncio.Event().wait()
    finally:
        await site.stop()
        await runner.cleanup()


def _record_debug_artifacts(
    *,
    client_name: str,
    port: int,
    downstream_request: dict,
    upstream_request: dict,
    upstream_response: dict | None = None,
    upstream_error: dict | None = None,
) -> None:
    record = build_debug_record(
        client_name=client_name,
        port=port,
        downstream_request=downstream_request,
        upstream_request=upstream_request,
        upstream_response=upstream_response,
        upstream_error=upstream_error,
    )
    file_path = save_debug_record(record)
    print(format_debug_record_summary(record, file_path=file_path))


def _resolve_local_port(request: web.Request) -> int:
    sockname = request.transport.get_extra_info("sockname")
    if not sockname:
        raise RuntimeError("Could not resolve the local listening port for this request.")
    return int(sockname[1])


def _extract_client_api_key(headers: Mapping[str, str]) -> str | None:
    authorization = headers.get("Authorization")
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token.strip():
            return token.strip()

    for header_name in ("x-api-key", "api-key"):
        api_key = headers.get(header_name)
        if api_key and api_key.strip():
            return api_key.strip()

    return None


def _forward_request_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() not in {"host", "content-length"}
    }


def _forward_response_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {key: value for key, value in headers.items() if key.lower() not in HOP_BY_HOP_HEADERS}


def _replace_upstream_auth_headers(headers: dict[str, str], upstream_api_key: str) -> None:
    for header_name in list(headers):
        lowered = header_name.lower()
        if lowered == "authorization":
            headers[header_name] = f"Bearer {upstream_api_key}"
        elif lowered in {"api-key", "x-api-key"}:
            headers[header_name] = upstream_api_key


def _extract_content_type(headers: Mapping[str, str]) -> str | None:
    header_value = headers.get("Content-Type", "")
    if not header_value:
        return None
    return header_value.split(";", 1)[0].strip().lower() or None


def _extract_charset(headers: Mapping[str, str]) -> str:
    header_value = headers.get("Content-Type", "")
    for segment in header_value.split(";")[1:]:
        key, separator, value = segment.strip().partition("=")
        if separator and key.lower() == "charset" and value:
            return value.strip().strip('"')
    return "utf-8"


def _render_record_body(body: bytes, headers: Mapping[str, str]) -> str:
    if not body:
        return "<empty>"

    content_encoding = headers.get("Content-Encoding")
    if content_encoding and content_encoding.lower() != "identity":
        return (
            "<encoded body omitted "
            f"content_encoding={content_encoding} bytes={len(body)}>"
        )

    content_type = _extract_content_type(headers)
    charset = _extract_charset(headers)

    if content_type and not _is_text_content_type(content_type):
        return f"<non-text body omitted content_type={content_type} bytes={len(body)}>"

    try:
        text = body.decode(charset)
    except (LookupError, UnicodeDecodeError):
        return f"<binary body omitted bytes={len(body)}>"

    if content_type and _is_json_content_type(content_type):
        try:
            return json.dumps(json.loads(text), ensure_ascii=False, separators=(",", ":"))
        except json.JSONDecodeError:
            return text

    return text


def _is_json_content_type(content_type: str) -> bool:
    return content_type == "application/json" or content_type.endswith("+json")


def _is_text_content_type(content_type: str) -> bool:
    return (
        content_type.startswith("text/")
        or _is_json_content_type(content_type)
        or content_type in {"application/xml", "application/x-ndjson"}
    )
