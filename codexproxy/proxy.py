from __future__ import annotations

import asyncio
import io
import json
import socket
from collections.abc import Mapping
from pathlib import Path

from aiohttp import ClientError, ClientSession, ClientTimeout, web
from yarl import URL

from codexproxy.count_display import round_count_for_display
from codexproxy.debug_record import (
    build_debug_record,
    build_http_message_snapshot,
    format_debug_record_summary,
    save_debug_record,
)
from codexproxy.expiry_manager import ExpiryManager
from codexproxy.spend_tracker import SpendTracker, TokenUsage
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
SPEND_TRACKER_KEY = web.AppKey("spend_tracker", SpendTracker)


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
    count: int | float | None = None,
    limit: int | float | None = None,
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
        parts.append(
            f"count={round_count_for_display(count)}/{round_count_for_display(limit)}"
        )
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


def create_app(
    store: ConfigStore,
    expiry_manager: ExpiryManager | None = None,
    spend_tracker: SpendTracker | None = None,
) -> web.Application:
    app = web.Application(client_max_size=0)
    app[CONFIG_STORE_KEY] = store
    app[SPEND_TRACKER_KEY] = spend_tracker or SpendTracker(store.config_path.parent)
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
            spend_status=request.app[SPEND_TRACKER_KEY].get_status(binding.name),
        ),
        content_type="text/html",
    )


async def handle_proxy_request(request: web.Request) -> web.StreamResponse:
    store = request.app[CONFIG_STORE_KEY]
    session = request.app[CLIENT_SESSION_KEY]
    local_port = _resolve_local_port(request)
    client_base_url = build_client_base_url(store.listen_host, local_port, store.advertise_host)
    client_api_key = _extract_client_api_key(request.headers)
    unlock_last_active = _is_unlock_last_active(request)

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

    has_request_body = request.can_read_body
    downstream_request_body = await request.read() if has_request_body else b""
    request_body = io.BytesIO(downstream_request_body) if has_request_body else None
    request_model = _extract_request_model_name(downstream_request_body, request.headers)
    request_cost = store.get_model_cost(request_model)

    try:
        binding = store.reserve_request(
            client_api_key,
            enforce_limit=not unlock_last_active,
            request_cost=request_cost,
        )
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
            {
                "error": "today's limit exceeded",
                "detail": "This client has exceeded today's usage limit.",
                "client": exc.client_name,
                "limit": exc.limit,
            },
            status=429,
            reason="Today's limit exceeded",
        )

    request_rolled_back = False

    def rollback_reserved_request() -> None:
        nonlocal request_rolled_back
        if request_rolled_back:
            return
        store.rollback_request(client_api_key, request_cost=request_cost)
        request_rolled_back = True

    target_url = build_target_url(binding.base_url, request.rel_url)
    forwarded_headers = _forward_request_headers(request.headers)
    _replace_upstream_auth_headers(forwarded_headers, binding.upstream_api_key)

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
                rollback_reserved_request()
                logged_binding = store.get_client_by_name(binding.name)
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
            response_headers = _forward_response_headers(upstream_response.headers)
            if (
                upstream_response.status >= 400
                and _should_normalize_text_error_response(upstream_response.headers)
            ):
                recorded_response_body = await upstream_response.read()

                if downstream_request_snapshot and upstream_request_snapshot:
                    upstream_response_snapshot = build_http_message_snapshot(
                        method=request.method,
                        url=str(target_url),
                        headers=upstream_response.headers,
                        body=recorded_response_body,
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

                downstream_body, downstream_headers = _normalize_text_error_response_for_downstream(
                    body=recorded_response_body,
                    headers=response_headers,
                )
                return web.Response(
                    status=upstream_response.status,
                    reason=upstream_response.reason,
                    headers=downstream_headers,
                    body=downstream_body,
                )

            downstream = web.StreamResponse(
                status=upstream_response.status,
                reason=upstream_response.reason,
                headers=response_headers,
            )
            await downstream.prepare(request)
            recorded_response_body = bytearray()
            async for chunk in upstream_response.content.iter_chunked(64 * 1024):
                if recorded_response_body is not None:
                    recorded_response_body.extend(chunk)
                await downstream.write(chunk)
            await downstream.write_eof()

            usage = _extract_usage_from_response(
                body=bytes(recorded_response_body),
                headers=upstream_response.headers,
            )
            if usage is not None and upstream_response.status < 400:
                request.app[SPEND_TRACKER_KEY].record_usage(
                    client_name=binding.name,
                    model_name=request_model,
                    usage=usage,
                )

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
    except asyncio.CancelledError:
        rollback_reserved_request()
        raise
    except ClientError as exc:
        rollback_reserved_request()
        rolled_back_binding = store.get_client_by_name(binding.name)
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
    except Exception:
        rollback_reserved_request()
        raise


async def run_proxy(config_path: Path, *, expire_time: str | None = None) -> None:
    store = ConfigStore.from_path(config_path)
    expiry_manager = ExpiryManager.from_runtime(
        config_path=config_path,
        expire_time_text=expire_time,
        on_update_success=store.reset_all,
        unlock_last=store.unlock_last,
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
            print(
                "Configured client: "
                f"name={client.name} "
                f"limit={round_count_for_display(client.limit)} "
                f"count={round_count_for_display(client.count)}"
            )
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


def _is_unlock_last_active(request: web.Request) -> bool:
    if EXPIRY_MANAGER_KEY not in request.app:
        return False
    store = request.app[CONFIG_STORE_KEY]
    if not store.unlock_last:
        return False
    return request.app[EXPIRY_MANAGER_KEY].is_last_hour_unlocked()


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


def _extract_request_model_name(body: bytes, headers: Mapping[str, str]) -> str | None:
    if not body:
        return None

    content_type = _extract_content_type(headers)
    if content_type is not None and not _is_json_content_type(content_type):
        return None

    charset = _extract_charset(headers)
    try:
        text = body.decode(charset)
    except (LookupError, UnicodeDecodeError):
        return None

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None

    if not isinstance(payload, dict):
        return None

    model_name = payload.get("model")
    if not isinstance(model_name, str) or not model_name.strip():
        return None

    return model_name.strip()


def _extract_usage_from_response(body: bytes, headers: Mapping[str, str]) -> TokenUsage | None:
    if not body:
        return None

    content_type = _extract_content_type(headers)
    if content_type is None:
        return None

    charset = _extract_charset(headers)
    try:
        text = body.decode(charset)
    except (LookupError, UnicodeDecodeError):
        return None

    if _is_json_content_type(content_type):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return None
        return _extract_usage_from_payload(payload)

    if content_type == "text/event-stream":
        return _extract_usage_from_sse_text(text)

    return None


def _extract_usage_from_payload(payload: object) -> TokenUsage | None:
    if not isinstance(payload, dict):
        return None

    usage = payload.get("usage")
    if isinstance(usage, dict):
        extracted = _usage_dict_to_token_usage(usage)
        if extracted is not None:
            return extracted

    response = payload.get("response")
    if isinstance(response, dict):
        usage = response.get("usage")
        if isinstance(usage, dict):
            return _usage_dict_to_token_usage(usage)

    return None


def _extract_usage_from_sse_text(text: str) -> TokenUsage | None:
    last_usage: TokenUsage | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        payload_text = line[5:].strip()
        if not payload_text or payload_text == "[DONE]":
            continue
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            continue
        usage = _extract_usage_from_payload(payload)
        if usage is not None:
            last_usage = usage
    return last_usage


def _usage_dict_to_token_usage(usage: Mapping[str, object]) -> TokenUsage | None:
    input_tokens = _extract_int(usage.get("input_tokens"))
    cached_input_tokens = 0
    output_tokens = _extract_int(usage.get("output_tokens"))

    input_details = usage.get("input_tokens_details")
    if isinstance(input_details, dict):
        cached_input_tokens = _extract_int(input_details.get("cached_tokens"))

    if input_tokens == 0 and output_tokens == 0:
        prompt_tokens = _extract_int(usage.get("prompt_tokens"))
        completion_tokens = _extract_int(usage.get("completion_tokens"))
        if prompt_tokens == 0 and completion_tokens == 0:
            return None
        input_tokens = prompt_tokens
        output_tokens = completion_tokens

        prompt_details = usage.get("prompt_tokens_details")
        if isinstance(prompt_details, dict):
            cached_input_tokens = _extract_int(prompt_details.get("cached_tokens"))

    cached_input_tokens = min(cached_input_tokens, input_tokens)
    return TokenUsage(
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        output_tokens=output_tokens,
    )


def _should_normalize_text_error_response(headers: Mapping[str, str]) -> bool:
    content_encoding = headers.get("Content-Encoding")
    if content_encoding and content_encoding.lower() != "identity":
        return False

    content_type = _extract_content_type(headers)
    return content_type is None or _is_text_content_type(content_type)


def _normalize_text_error_response_for_downstream(
    *,
    body: bytes,
    headers: Mapping[str, str],
) -> tuple[bytes, dict[str, str]]:
    if not body:
        return body, dict(headers.items())

    decoded = _decode_text_error_body(body, headers)
    if decoded is None:
        return body, dict(headers.items())

    text, decoded_charset = decoded
    declared_charset = _extract_declared_charset(headers)
    if decoded_charset.lower() == "utf-8" and (
        declared_charset is None or declared_charset.lower() == "utf-8"
    ):
        return body, dict(headers.items())

    normalized_headers = dict(headers.items())
    normalized_headers.pop("Content-Length", None)
    content_type = normalized_headers.get("Content-Type")
    if content_type:
        normalized_headers["Content-Type"] = _replace_charset_in_content_type(
            content_type,
            charset="utf-8",
        )

    return text.encode("utf-8"), normalized_headers


def _decode_text_error_body(body: bytes, headers: Mapping[str, str]) -> tuple[str, str] | None:
    declared_charset = _extract_declared_charset(headers)
    candidate_charsets = ["utf-8"]
    if declared_charset and declared_charset.lower() != "utf-8":
        candidate_charsets.append(declared_charset)
    if declared_charset is None or declared_charset.lower() == "utf-8":
        candidate_charsets.append("gb18030")

    for charset in candidate_charsets:
        try:
            return body.decode(charset), charset
        except (LookupError, UnicodeDecodeError):
            continue

    return None


def _extract_charset(headers: Mapping[str, str]) -> str:
    return _extract_declared_charset(headers) or "utf-8"


def _extract_declared_charset(headers: Mapping[str, str]) -> str | None:
    header_value = headers.get("Content-Type", "")
    for segment in header_value.split(";")[1:]:
        key, separator, value = segment.strip().partition("=")
        if separator and key.lower() == "charset" and value:
            return value.strip().strip('"')
    return None


def _replace_charset_in_content_type(content_type: str, *, charset: str) -> str:
    segments = [segment.strip() for segment in content_type.split(";") if segment.strip()]
    if not segments:
        return f"text/plain; charset={charset}"

    filtered_segments = [segments[0]]
    for segment in segments[1:]:
        key, separator, _ = segment.partition("=")
        if separator and key.strip().lower() == "charset":
            continue
        filtered_segments.append(segment)
    filtered_segments.append(f"charset={charset}")
    return "; ".join(filtered_segments)


def _extract_int(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return max(value, 0)
    return 0


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
