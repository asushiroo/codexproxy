import json
import io
import socket
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import codexproxy.debug_record as debug_record_module
import codexproxy.proxy as proxy_module
from aiohttp import ClientSession, web
from yarl import URL

from codexproxy.config import ClientConfig, ProxyConfig, save_config
from codexproxy.expiry_manager import ExpiryStatus
from codexproxy.proxy import (
    build_client_base_url,
    build_target_url,
    create_app,
    format_record_log_line,
    format_request_log_line,
)
from codexproxy.state import ConfigStore


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


class _FakeExpiryManager:
    def __init__(
        self,
        *,
        unlock_last_enabled: bool = False,
        last_hour_unlocked: bool = False,
        auto_update_enabled: bool = False,
        notice: str | None = "Auto update failed. Restart manually with --expire-time to set a new expire time.",
    ) -> None:
        self._unlock_last_enabled = unlock_last_enabled
        self._last_hour_unlocked = last_hour_unlocked
        self._auto_update_enabled = auto_update_enabled
        self._notice = notice

    def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def is_last_hour_unlocked(self) -> bool:
        return self._last_hour_unlocked

    def get_status(self) -> ExpiryStatus:
        return ExpiryStatus(
            expire_time_text="2026/5/3 21:32:39",
            auto_update_enabled=self._auto_update_enabled,
            notice=self._notice,
            unlock_last_enabled=self._unlock_last_enabled,
            unlock_last_active=self._last_hour_unlocked,
        )


class ProxyTests(unittest.IsolatedAsyncioTestCase):
    async def test_build_target_url_keeps_base_path(self) -> None:
        target = build_target_url("https://example.invalid/v1", URL("/chat?mode=debug"))

        self.assertEqual(str(target), "https://example.invalid/v1/chat?mode=debug")

    async def test_build_client_base_url_uses_advertise_host(self) -> None:
        base_url = build_client_base_url("0.0.0.0", 7001, advertise_host="proxy.example.com")

        self.assertEqual(base_url, "http://proxy.example.com:7001")

    async def test_build_client_base_url_uses_detected_host_ip_for_wildcard(self) -> None:
        original = proxy_module.get_current_host_ip
        proxy_module.get_current_host_ip = lambda: "10.20.30.40"
        try:
            base_url = build_client_base_url("0.0.0.0", 7001)
        finally:
            proxy_module.get_current_host_ip = original

        self.assertEqual(base_url, "http://10.20.30.40:7001")

    async def test_format_request_log_line_contains_latest_count(self) -> None:
        line = format_request_log_line(
            method="POST",
            path="/v1/chat?stream=true",
            port=7001,
            name="client-a",
            status=200,
            count=3,
            limit=10,
            client_base_url="http://proxy.example.com:7001",
        )

        self.assertEqual(
            line,
            "REQUEST method=POST path=/v1/chat?stream=true port=7001 name=client-a status=200 count=3/10 client_base_url=http://proxy.example.com:7001",
        )

    async def test_format_request_log_line_rounds_decimal_count_for_display(self) -> None:
        line = format_request_log_line(
            method="POST",
            path="/v1/chat",
            port=7001,
            name="client-a",
            status=200,
            count=1.6,
            limit=300,
            client_base_url="http://proxy.example.com:7001",
        )

        self.assertEqual(
            line,
            "REQUEST method=POST path=/v1/chat port=7001 name=client-a status=200 count=2/300 client_base_url=http://proxy.example.com:7001",
        )

    async def test_format_record_log_line_contains_parsed_body(self) -> None:
        line = format_record_log_line(
            direction="request",
            method="POST",
            path="/v1/chat",
            port=7001,
            content_type="application/json",
            body='{"message":"hello"}',
        )

        self.assertEqual(
            line,
            "RECORD direction=request method=POST path=/v1/chat port=7001 content_type=application/json body={\"message\":\"hello\"}",
        )

    async def test_same_port_tracks_different_clients_against_shared_upstream(self) -> None:
        upstream_port = _find_free_port()
        proxy_port = _find_free_port()

        async def upstream_handler(request: web.Request) -> web.Response:
            return web.json_response(
                {
                    "authorization": request.headers.get("Authorization"),
                    "api_key": request.headers.get("api-key"),
                    "x_api_key": request.headers.get("x-api-key"),
                    "path": request.path,
                }
            )

        upstream_app = web.Application(client_max_size=0)
        upstream_app.router.add_get("/{tail:.*}", upstream_handler)
        upstream_runner = web.AppRunner(upstream_app)
        await upstream_runner.setup()
        upstream_site = web.TCPSite(upstream_runner, "127.0.0.1", upstream_port)
        await upstream_site.start()

        try:
            with TemporaryDirectory() as temp_dir:
                config_path = Path(temp_dir) / "proxy-config.json"
                config = ProxyConfig(
                    listen_host="127.0.0.1",
                    listen_port=proxy_port,
                    base_url=f"http://127.0.0.1:{upstream_port}/v1",
                    upstream_api_key="shared-upstream-key",
                    clients=[
                        ClientConfig(name="client-1", client_api_key="client-key-a", limit=300, count=0),
                        ClientConfig(name="client-2", client_api_key="client-key-b", limit=300, count=0),
                    ],
                    advertise_host="proxy.example.com",
                )
                save_config(config_path, config)
                store = ConfigStore.from_path(config_path)

                proxy_runner = web.AppRunner(create_app(store))
                await proxy_runner.setup()
                proxy_site = web.TCPSite(proxy_runner, "127.0.0.1", proxy_port)
                await proxy_site.start()

                try:
                    async with ClientSession() as session:
                        async with session.get(
                            f"http://127.0.0.1:{proxy_port}/chat",
                            headers={"Authorization": "Bearer client-key-a"},
                        ) as response_one:
                            payload_one = await response_one.json()

                        async with session.get(
                            f"http://127.0.0.1:{proxy_port}/chat",
                            headers={"x-api-key": "client-key-b"},
                        ) as response_two:
                            payload_two = await response_two.json()

                        async with session.get(
                            f"http://127.0.0.1:{proxy_port}/chat",
                            headers={"api-key": "client-key-a"},
                        ) as response_three:
                            payload_three = await response_three.json()

                    self.assertEqual(
                        payload_one,
                        {
                            "authorization": "Bearer shared-upstream-key",
                            "api_key": None,
                            "x_api_key": None,
                            "path": "/v1/chat",
                        },
                    )
                    self.assertEqual(
                        payload_two,
                        {
                            "authorization": None,
                            "api_key": None,
                            "x_api_key": "shared-upstream-key",
                            "path": "/v1/chat",
                        },
                    )
                    self.assertEqual(
                        payload_three,
                        {
                            "authorization": None,
                            "api_key": "shared-upstream-key",
                            "x_api_key": None,
                            "path": "/v1/chat",
                        },
                    )

                    reloaded = ConfigStore.from_path(config_path)
                    clients = reloaded.list_clients()
                    self.assertEqual(clients[0].count, 2)
                    self.assertEqual(clients[1].count, 1)
                    self.assertEqual(clients[0].base_url, f"http://127.0.0.1:{upstream_port}/v1")
                    self.assertEqual(clients[1].upstream_api_key, "shared-upstream-key")
                finally:
                    await proxy_runner.cleanup()
        finally:
            await upstream_site.stop()
            await upstream_runner.cleanup()

    async def test_missing_api_key_returns_401(self) -> None:
        proxy_port = _find_free_port()

        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "proxy-config.json"
            config = ProxyConfig(
                listen_host="127.0.0.1",
                listen_port=proxy_port,
                base_url="http://127.0.0.1:1/v1",
                upstream_api_key="shared-upstream-key",
                clients=[
                    ClientConfig(
                        name="client-1",
                        client_api_key="client-key-a",
                        limit=300,
                        count=0,
                    )
                ],
            )
            save_config(config_path, config)
            store = ConfigStore.from_path(config_path)

            proxy_runner = web.AppRunner(create_app(store, expiry_manager=_FakeExpiryManager()))
            await proxy_runner.setup()
            proxy_site = web.TCPSite(proxy_runner, "127.0.0.1", proxy_port)
            await proxy_site.start()

            try:
                async with ClientSession() as session:
                    async with session.get(f"http://127.0.0.1:{proxy_port}/chat") as response:
                        payload = await response.json()

                self.assertEqual(response.status, 401)
                self.assertEqual(payload["error"], "missing api key")
            finally:
                await proxy_runner.cleanup()

    async def test_large_request_body_is_not_rejected_by_proxy_layer(self) -> None:
        upstream_port = _find_free_port()
        proxy_port = _find_free_port()
        large_body = b"x" * 1_100_000

        async def upstream_handler(request: web.Request) -> web.Response:
            size = 0
            async for chunk in request.content.iter_chunked(64 * 1024):
                size += len(chunk)
            return web.json_response({"size": size})

        upstream_app = web.Application()
        upstream_app.router.add_post("/{tail:.*}", upstream_handler)
        upstream_runner = web.AppRunner(upstream_app)
        await upstream_runner.setup()
        upstream_site = web.TCPSite(upstream_runner, "127.0.0.1", upstream_port)
        await upstream_site.start()

        try:
            with TemporaryDirectory() as temp_dir:
                config_path = Path(temp_dir) / "proxy-config.json"
                config = ProxyConfig(
                    listen_host="127.0.0.1",
                    listen_port=proxy_port,
                    base_url=f"http://127.0.0.1:{upstream_port}/v1",
                    upstream_api_key="shared-upstream-key",
                    clients=[
                        ClientConfig(
                            name="client-1",
                            client_api_key="client-key-a",
                            limit=300,
                            count=0,
                        )
                    ],
                )
                save_config(config_path, config)
                store = ConfigStore.from_path(config_path)

                proxy_runner = web.AppRunner(create_app(store))
                await proxy_runner.setup()
                proxy_site = web.TCPSite(proxy_runner, "127.0.0.1", proxy_port)
                await proxy_site.start()

                try:
                    async with ClientSession() as session:
                        async with session.post(
                            f"http://127.0.0.1:{proxy_port}/responses",
                            headers={
                                "Authorization": "Bearer client-key-a",
                                "Content-Type": "application/octet-stream",
                            },
                            data=io.BytesIO(large_body),
                        ) as response:
                            payload = await response.json()

                    self.assertEqual(response.status, 200)
                    self.assertEqual(payload, {"size": len(large_body)})
                finally:
                    await proxy_runner.cleanup()
        finally:
            await upstream_site.stop()
            await upstream_runner.cleanup()

    async def test_gpt_5_5_request_consumes_three_counts(self) -> None:
        upstream_port = _find_free_port()
        proxy_port = _find_free_port()

        async def upstream_handler(request: web.Request) -> web.Response:
            payload = await request.json()
            return web.json_response({"model": payload["model"]})

        upstream_app = web.Application()
        upstream_app.router.add_post("/{tail:.*}", upstream_handler)
        upstream_runner = web.AppRunner(upstream_app)
        await upstream_runner.setup()
        upstream_site = web.TCPSite(upstream_runner, "127.0.0.1", upstream_port)
        await upstream_site.start()

        try:
            with TemporaryDirectory() as temp_dir:
                config_path = Path(temp_dir) / "proxy-config.json"
                config = ProxyConfig(
                    listen_host="127.0.0.1",
                    listen_port=proxy_port,
                    base_url=f"http://127.0.0.1:{upstream_port}/v1",
                    upstream_api_key="shared-upstream-key",
                    clients=[
                        ClientConfig(
                            name="client-1",
                            client_api_key="client-key-a",
                            limit=300,
                            count=0,
                        )
                    ],
                )
                save_config(config_path, config)
                store = ConfigStore.from_path(config_path)

                proxy_runner = web.AppRunner(create_app(store))
                await proxy_runner.setup()
                proxy_site = web.TCPSite(proxy_runner, "127.0.0.1", proxy_port)
                await proxy_site.start()

                try:
                    async with ClientSession() as session:
                        async with session.post(
                            f"http://127.0.0.1:{proxy_port}/chat",
                            headers={"Authorization": "Bearer client-key-a"},
                            json={"model": "gpt-5.5", "message": "hello"},
                        ) as response:
                            payload = await response.json()

                    self.assertEqual(response.status, 200)
                    self.assertEqual(payload["model"], "gpt-5.5")
                    reloaded = ConfigStore.from_path(config_path)
                    self.assertEqual(reloaded.list_clients()[0].count, 3)
                finally:
                    await proxy_runner.cleanup()
        finally:
            await upstream_site.stop()
            await upstream_runner.cleanup()

    async def test_unknown_model_uses_other_cost_of_one(self) -> None:
        upstream_port = _find_free_port()
        proxy_port = _find_free_port()

        async def upstream_handler(request: web.Request) -> web.Response:
            return web.json_response({"ok": True})

        upstream_app = web.Application()
        upstream_app.router.add_post("/{tail:.*}", upstream_handler)
        upstream_runner = web.AppRunner(upstream_app)
        await upstream_runner.setup()
        upstream_site = web.TCPSite(upstream_runner, "127.0.0.1", upstream_port)
        await upstream_site.start()

        try:
            with TemporaryDirectory() as temp_dir:
                config_path = Path(temp_dir) / "proxy-config.json"
                config = ProxyConfig(
                    listen_host="127.0.0.1",
                    listen_port=proxy_port,
                    base_url=f"http://127.0.0.1:{upstream_port}/v1",
                    upstream_api_key="shared-upstream-key",
                    clients=[
                        ClientConfig(
                            name="client-1",
                            client_api_key="client-key-a",
                            limit=300,
                            count=0,
                        )
                    ],
                )
                save_config(config_path, config)
                store = ConfigStore.from_path(config_path)

                proxy_runner = web.AppRunner(create_app(store))
                await proxy_runner.setup()
                proxy_site = web.TCPSite(proxy_runner, "127.0.0.1", proxy_port)
                await proxy_site.start()

                try:
                    async with ClientSession() as session:
                        async with session.post(
                            f"http://127.0.0.1:{proxy_port}/chat",
                            headers={"Authorization": "Bearer client-key-a"},
                            json={"model": "gpt-4.1", "message": "hello"},
                        ) as response:
                            payload = await response.json()

                    self.assertEqual(response.status, 200)
                    self.assertEqual(payload, {"ok": True})
                    reloaded = ConfigStore.from_path(config_path)
                    self.assertEqual(reloaded.list_clients()[0].count, 1)
                finally:
                    await proxy_runner.cleanup()
        finally:
            await upstream_site.stop()
            await upstream_runner.cleanup()

    async def test_successful_json_response_updates_today_usd_spend(self) -> None:
        upstream_port = _find_free_port()
        proxy_port = _find_free_port()

        async def upstream_handler(request: web.Request) -> web.Response:
            return web.json_response(
                {
                    "ok": True,
                    "usage": {
                        "input_tokens": 1000,
                        "input_tokens_details": {"cached_tokens": 200},
                        "output_tokens": 500,
                    },
                }
            )

        upstream_app = web.Application()
        upstream_app.router.add_post("/{tail:.*}", upstream_handler)
        upstream_runner = web.AppRunner(upstream_app)
        await upstream_runner.setup()
        upstream_site = web.TCPSite(upstream_runner, "127.0.0.1", upstream_port)
        await upstream_site.start()

        try:
            with TemporaryDirectory() as temp_dir:
                config_path = Path(temp_dir) / "proxy-config.json"
                config = ProxyConfig(
                    listen_host="127.0.0.1",
                    listen_port=proxy_port,
                    base_url=f"http://127.0.0.1:{upstream_port}/v1",
                    upstream_api_key="shared-upstream-key",
                    clients=[
                        ClientConfig(
                            name="client-1",
                            client_api_key="client-key-a",
                            limit=300,
                            count=0,
                        )
                    ],
                )
                save_config(config_path, config)
                store = ConfigStore.from_path(config_path)

                proxy_runner = web.AppRunner(create_app(store))
                await proxy_runner.setup()
                proxy_site = web.TCPSite(proxy_runner, "127.0.0.1", proxy_port)
                await proxy_site.start()

                try:
                    async with ClientSession() as session:
                        async with session.post(
                            f"http://127.0.0.1:{proxy_port}/chat",
                            headers={"Authorization": "Bearer client-key-a"},
                            json={"model": "gpt-5.5", "message": "hello"},
                        ) as response:
                            payload = await response.json()

                        async with session.get(
                            f"http://127.0.0.1:{proxy_port}/client-1/usage"
                        ) as usage_response:
                            usage_html = await usage_response.text()

                    self.assertEqual(response.status, 200)
                    self.assertTrue(payload["ok"])
                    self.assertIn("$0.019100", usage_html)
                    spend_payload = json.loads(
                        (Path(temp_dir) / "cache" / "daily-spend.json").read_text(encoding="utf-8")
                    )
                    self.assertEqual(spend_payload["date"], date.today().isoformat())
                    self.assertEqual(spend_payload["total_usd"], "0.019100")
                    self.assertEqual(spend_payload["clients"]["client-1"], "0.019100")
                finally:
                    await proxy_runner.cleanup()
        finally:
            await upstream_site.stop()
            await upstream_runner.cleanup()

    async def test_successful_sse_response_updates_today_usd_spend(self) -> None:
        upstream_port = _find_free_port()
        proxy_port = _find_free_port()

        async def upstream_handler(request: web.Request) -> web.StreamResponse:
            response = web.StreamResponse(
                status=200,
                headers={"Content-Type": "text/event-stream"},
            )
            await response.prepare(request)
            await response.write(b'data: {"type":"response.started"}\n\n')
            await response.write(
                b'data: {"type":"response.completed","response":{"usage":{"input_tokens":1000,"input_tokens_details":{"cached_tokens":0},"output_tokens":500}}}\n\n'
            )
            await response.write(b"data: [DONE]\n\n")
            await response.write_eof()
            return response

        upstream_app = web.Application()
        upstream_app.router.add_post("/{tail:.*}", upstream_handler)
        upstream_runner = web.AppRunner(upstream_app)
        await upstream_runner.setup()
        upstream_site = web.TCPSite(upstream_runner, "127.0.0.1", upstream_port)
        await upstream_site.start()

        try:
            with TemporaryDirectory() as temp_dir:
                config_path = Path(temp_dir) / "proxy-config.json"
                config = ProxyConfig(
                    listen_host="127.0.0.1",
                    listen_port=proxy_port,
                    base_url=f"http://127.0.0.1:{upstream_port}/v1",
                    upstream_api_key="shared-upstream-key",
                    clients=[
                        ClientConfig(
                            name="client-1",
                            client_api_key="client-key-a",
                            limit=300,
                            count=0,
                        )
                    ],
                )
                save_config(config_path, config)
                store = ConfigStore.from_path(config_path)

                proxy_runner = web.AppRunner(create_app(store))
                await proxy_runner.setup()
                proxy_site = web.TCPSite(proxy_runner, "127.0.0.1", proxy_port)
                await proxy_site.start()

                try:
                    async with ClientSession() as session:
                        async with session.post(
                            f"http://127.0.0.1:{proxy_port}/chat",
                            headers={"Authorization": "Bearer client-key-a"},
                            json={"model": "gpt-5.5", "message": "hello"},
                        ) as response:
                            sse_text = await response.text()

                    self.assertEqual(response.status, 200)
                    self.assertIn("response.completed", sse_text)
                    spend_payload = json.loads(
                        (Path(temp_dir) / "cache" / "daily-spend.json").read_text(encoding="utf-8")
                    )
                    self.assertEqual(spend_payload["total_usd"], "0.020000")
                finally:
                    await proxy_runner.cleanup()
        finally:
            await upstream_site.stop()
            await upstream_runner.cleanup()

    async def test_usage_page_shows_current_client_usage_without_incrementing_count(self) -> None:
        proxy_port = _find_free_port()

        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "proxy-config.json"
            config = ProxyConfig(
                listen_host="127.0.0.1",
                listen_port=proxy_port,
                base_url="http://127.0.0.1:1/v1",
                upstream_api_key="shared-upstream-key",
                clients=[
                    ClientConfig(
                        name="client-1",
                        client_api_key="client-key-a",
                        limit=300,
                        count=12,
                    )
                ],
            )
            save_config(config_path, config)
            store = ConfigStore.from_path(config_path)

            proxy_runner = web.AppRunner(create_app(store, expiry_manager=_FakeExpiryManager()))
            await proxy_runner.setup()
            proxy_site = web.TCPSite(proxy_runner, "127.0.0.1", proxy_port)
            await proxy_site.start()

            try:
                async with ClientSession() as session:
                    async with session.get(
                        f"http://127.0.0.1:{proxy_port}/client-1/usage"
                    ) as response:
                        html = await response.text()

                self.assertEqual(response.status, 200)
                self.assertEqual(response.content_type, "text/html")
                self.assertIn("Client Usage", html)
                self.assertIn("288 / 300", html)
                self.assertIn("12 / 300", html)
                self.assertIn("client-1", html)
                self.assertIn("2026/5/3 21:32:39", html)
                self.assertIn("Auto update failed", html)
                self.assertIn(f"Today Total USD ({date.today().isoformat()})", html)
                self.assertIn("$0.000000", html)
                self.assertNotIn(f"Today USD ({date.today().isoformat()})", html)
                self.assertIn('data-progress="4.0"', html)
                self.assertIn("#16a34a", html)
                self.assertNotIn("unlock_last", html)
                self.assertNotIn("UNLOCK LAST ACTIVE", html)
                self.assertNotIn("client-key-a", html)

                reloaded = ConfigStore.from_path(config_path)
                self.assertEqual(reloaded.list_clients()[0].count, 12)
            finally:
                await proxy_runner.cleanup()

    async def test_gpt_5_5_request_is_rejected_when_only_two_counts_remain(self) -> None:
        upstream_port = _find_free_port()
        proxy_port = _find_free_port()

        async def upstream_handler(request: web.Request) -> web.Response:
            return web.json_response({"ok": True})

        upstream_app = web.Application()
        upstream_app.router.add_post("/{tail:.*}", upstream_handler)
        upstream_runner = web.AppRunner(upstream_app)
        await upstream_runner.setup()
        upstream_site = web.TCPSite(upstream_runner, "127.0.0.1", upstream_port)
        await upstream_site.start()

        try:
            with TemporaryDirectory() as temp_dir:
                config_path = Path(temp_dir) / "proxy-config.json"
                config = ProxyConfig(
                    listen_host="127.0.0.1",
                    listen_port=proxy_port,
                    base_url=f"http://127.0.0.1:{upstream_port}/v1",
                    upstream_api_key="shared-upstream-key",
                    clients=[
                        ClientConfig(
                            name="client-1",
                            client_api_key="client-key-a",
                            limit=300,
                            count=298,
                        )
                    ],
                )
                save_config(config_path, config)
                store = ConfigStore.from_path(config_path)

                proxy_runner = web.AppRunner(create_app(store))
                await proxy_runner.setup()
                proxy_site = web.TCPSite(proxy_runner, "127.0.0.1", proxy_port)
                await proxy_site.start()

                try:
                    async with ClientSession() as session:
                        async with session.post(
                            f"http://127.0.0.1:{proxy_port}/chat",
                            headers={"Authorization": "Bearer client-key-a"},
                            json={"model": "gpt-5.5", "message": "hello"},
                        ) as response:
                            payload = await response.json()

                    self.assertEqual(response.status, 429)
                    self.assertEqual(response.reason, "Today's limit exceeded")
                    self.assertEqual(payload["error"], "today's limit exceeded")
                    self.assertEqual(
                        payload["detail"],
                        "This client has exceeded today's usage limit.",
                    )
                    reloaded = ConfigStore.from_path(config_path)
                    self.assertEqual(reloaded.list_clients()[0].count, 298)
                finally:
                    await proxy_runner.cleanup()
        finally:
            await upstream_site.stop()
            await upstream_runner.cleanup()

    async def test_usage_page_shows_active_unlock_last_window(self) -> None:
        proxy_port = _find_free_port()

        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "proxy-config.json"
            config = ProxyConfig(
                listen_host="127.0.0.1",
                listen_port=proxy_port,
                base_url="http://127.0.0.1:1/v1",
                upstream_api_key="shared-upstream-key",
                clients=[
                    ClientConfig(
                        name="client-1",
                        client_api_key="client-key-a",
                        limit=300,
                        count=12,
                    )
                ],
                unlock_last=True,
            )
            save_config(config_path, config)
            store = ConfigStore.from_path(config_path)

            proxy_runner = web.AppRunner(
                create_app(
                    store,
                    expiry_manager=_FakeExpiryManager(
                        unlock_last_enabled=True,
                        last_hour_unlocked=True,
                        auto_update_enabled=True,
                        notice=None,
                    ),
                )
            )
            await proxy_runner.setup()
            proxy_site = web.TCPSite(proxy_runner, "127.0.0.1", proxy_port)
            await proxy_site.start()

            try:
                async with ClientSession() as session:
                    async with session.get(
                        f"http://127.0.0.1:{proxy_port}/client-1/usage"
                    ) as response:
                        html = await response.text()

                self.assertEqual(response.status, 200)
                self.assertIn("UNLOCK LAST ACTIVE", html)
                self.assertIn('data-progress="4.0"', html)
            finally:
                await proxy_runner.cleanup()

    async def test_usage_page_returns_404_for_unknown_client_name(self) -> None:
        proxy_port = _find_free_port()

        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "proxy-config.json"
            config = ProxyConfig(
                listen_host="127.0.0.1",
                listen_port=proxy_port,
                base_url="http://127.0.0.1:1/v1",
                upstream_api_key="shared-upstream-key",
                clients=[
                    ClientConfig(
                        name="client-1",
                        client_api_key="client-key-a",
                        limit=300,
                        count=0,
                    )
                ],
            )
            save_config(config_path, config)
            store = ConfigStore.from_path(config_path)

            proxy_runner = web.AppRunner(create_app(store))
            await proxy_runner.setup()
            proxy_site = web.TCPSite(proxy_runner, "127.0.0.1", proxy_port)
            await proxy_site.start()

            try:
                async with ClientSession() as session:
                    async with session.get(
                        f"http://127.0.0.1:{proxy_port}/client-missing/usage"
                    ) as response:
                        body = await response.text()

                self.assertEqual(response.status, 404)
                self.assertIn("client name is not configured", body)
            finally:
                await proxy_runner.cleanup()

    async def test_upstream_error_response_does_not_increment_count(self) -> None:
        upstream_port = _find_free_port()
        proxy_port = _find_free_port()

        async def upstream_handler(request: web.Request) -> web.Response:
            return web.json_response({"error": "bad request"}, status=500)

        upstream_app = web.Application()
        upstream_app.router.add_get("/{tail:.*}", upstream_handler)
        upstream_runner = web.AppRunner(upstream_app)
        await upstream_runner.setup()
        upstream_site = web.TCPSite(upstream_runner, "127.0.0.1", upstream_port)
        await upstream_site.start()

        try:
            with TemporaryDirectory() as temp_dir:
                config_path = Path(temp_dir) / "proxy-config.json"
                config = ProxyConfig(
                    listen_host="127.0.0.1",
                    listen_port=proxy_port,
                    base_url=f"http://127.0.0.1:{upstream_port}/v1",
                    upstream_api_key="shared-upstream-key",
                    clients=[
                        ClientConfig(
                            name="client-1",
                            client_api_key="client-key-a",
                            limit=300,
                            count=0,
                        )
                    ],
                )
                save_config(config_path, config)
                store = ConfigStore.from_path(config_path)

                proxy_runner = web.AppRunner(create_app(store))
                await proxy_runner.setup()
                proxy_site = web.TCPSite(proxy_runner, "127.0.0.1", proxy_port)
                await proxy_site.start()

                try:
                    async with ClientSession() as session:
                        async with session.get(
                            f"http://127.0.0.1:{proxy_port}/chat",
                            headers={"Authorization": "Bearer client-key-a"},
                        ) as response:
                            payload = await response.json()

                    self.assertEqual(response.status, 500)
                    self.assertEqual(payload, {"error": "bad request"})
                    reloaded = ConfigStore.from_path(config_path)
                    self.assertEqual(reloaded.list_clients()[0].count, 0)
                finally:
                    await proxy_runner.cleanup()
        finally:
            await upstream_site.stop()
            await upstream_runner.cleanup()

    async def test_upstream_text_error_without_charset_is_normalized_to_utf_8(self) -> None:
        upstream_port = _find_free_port()
        proxy_port = _find_free_port()
        upstream_message = "上游返回中文错误"

        async def upstream_handler(request: web.Request) -> web.Response:
            return web.Response(
                status=403,
                body=upstream_message.encode("gb18030"),
                headers={"Content-Type": "text/plain"},
            )

        upstream_app = web.Application()
        upstream_app.router.add_get("/{tail:.*}", upstream_handler)
        upstream_runner = web.AppRunner(upstream_app)
        await upstream_runner.setup()
        upstream_site = web.TCPSite(upstream_runner, "127.0.0.1", upstream_port)
        await upstream_site.start()

        try:
            with TemporaryDirectory() as temp_dir:
                config_path = Path(temp_dir) / "proxy-config.json"
                config = ProxyConfig(
                    listen_host="127.0.0.1",
                    listen_port=proxy_port,
                    base_url=f"http://127.0.0.1:{upstream_port}/v1",
                    upstream_api_key="shared-upstream-key",
                    clients=[
                        ClientConfig(
                            name="client-1",
                            client_api_key="client-key-a",
                            limit=300,
                            count=0,
                        )
                    ],
                )
                save_config(config_path, config)
                store = ConfigStore.from_path(config_path)

                proxy_runner = web.AppRunner(create_app(store))
                await proxy_runner.setup()
                proxy_site = web.TCPSite(proxy_runner, "127.0.0.1", proxy_port)
                await proxy_site.start()

                try:
                    async with ClientSession() as session:
                        async with session.get(
                            f"http://127.0.0.1:{proxy_port}/chat",
                            headers={"Authorization": "Bearer client-key-a"},
                        ) as response:
                            body = await response.text()

                    self.assertEqual(response.status, 403)
                    self.assertEqual(response.charset, "utf-8")
                    self.assertEqual(body, upstream_message)
                    reloaded = ConfigStore.from_path(config_path)
                    self.assertEqual(reloaded.list_clients()[0].count, 0)
                finally:
                    await proxy_runner.cleanup()
        finally:
            await upstream_site.stop()
            await upstream_runner.cleanup()

    async def test_failed_gpt_5_5_request_rolls_back_three_counts(self) -> None:
        upstream_port = _find_free_port()
        proxy_port = _find_free_port()

        async def upstream_handler(request: web.Request) -> web.Response:
            return web.json_response({"error": "bad request"}, status=500)

        upstream_app = web.Application()
        upstream_app.router.add_post("/{tail:.*}", upstream_handler)
        upstream_runner = web.AppRunner(upstream_app)
        await upstream_runner.setup()
        upstream_site = web.TCPSite(upstream_runner, "127.0.0.1", upstream_port)
        await upstream_site.start()

        try:
            with TemporaryDirectory() as temp_dir:
                config_path = Path(temp_dir) / "proxy-config.json"
                config = ProxyConfig(
                    listen_host="127.0.0.1",
                    listen_port=proxy_port,
                    base_url=f"http://127.0.0.1:{upstream_port}/v1",
                    upstream_api_key="shared-upstream-key",
                    clients=[
                        ClientConfig(
                            name="client-1",
                            client_api_key="client-key-a",
                            limit=300,
                            count=0,
                        )
                    ],
                )
                save_config(config_path, config)
                store = ConfigStore.from_path(config_path)

                proxy_runner = web.AppRunner(create_app(store))
                await proxy_runner.setup()
                proxy_site = web.TCPSite(proxy_runner, "127.0.0.1", proxy_port)
                await proxy_site.start()

                try:
                    async with ClientSession() as session:
                        async with session.post(
                            f"http://127.0.0.1:{proxy_port}/chat",
                            headers={"Authorization": "Bearer client-key-a"},
                            json={"model": "gpt-5.5", "message": "hello"},
                        ) as response:
                            payload = await response.json()

                    self.assertEqual(response.status, 500)
                    self.assertEqual(payload, {"error": "bad request"})
                    reloaded = ConfigStore.from_path(config_path)
                    self.assertEqual(reloaded.list_clients()[0].count, 0)
                finally:
                    await proxy_runner.cleanup()
        finally:
            await upstream_site.stop()
            await upstream_runner.cleanup()

    async def test_upstream_transport_error_does_not_increment_count(self) -> None:
        proxy_port = _find_free_port()

        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "proxy-config.json"
            config = ProxyConfig(
                listen_host="127.0.0.1",
                listen_port=proxy_port,
                base_url="http://127.0.0.1:1/v1",
                upstream_api_key="shared-upstream-key",
                clients=[
                    ClientConfig(
                        name="client-1",
                        client_api_key="client-key-a",
                        limit=300,
                        count=0,
                    )
                ],
            )
            save_config(config_path, config)
            store = ConfigStore.from_path(config_path)

            proxy_runner = web.AppRunner(create_app(store))
            await proxy_runner.setup()
            proxy_site = web.TCPSite(proxy_runner, "127.0.0.1", proxy_port)
            await proxy_site.start()

            try:
                async with ClientSession() as session:
                    async with session.get(
                        f"http://127.0.0.1:{proxy_port}/chat",
                        headers={"Authorization": "Bearer client-key-a"},
                    ) as response:
                        payload = await response.json()

                self.assertEqual(response.status, 502)
                self.assertEqual(payload["error"], "upstream request failed")
                reloaded = ConfigStore.from_path(config_path)
                self.assertEqual(reloaded.list_clients()[0].count, 0)
            finally:
                await proxy_runner.cleanup()

    async def test_unlock_last_allows_over_limit_requests_and_continues_incrementing_count(self) -> None:
        upstream_port = _find_free_port()
        proxy_port = _find_free_port()

        async def upstream_handler(request: web.Request) -> web.Response:
            return web.json_response({"ok": True})

        upstream_app = web.Application()
        upstream_app.router.add_get("/{tail:.*}", upstream_handler)
        upstream_runner = web.AppRunner(upstream_app)
        await upstream_runner.setup()
        upstream_site = web.TCPSite(upstream_runner, "127.0.0.1", upstream_port)
        await upstream_site.start()

        try:
            with TemporaryDirectory() as temp_dir:
                config_path = Path(temp_dir) / "proxy-config.json"
                config = ProxyConfig(
                    listen_host="127.0.0.1",
                    listen_port=proxy_port,
                    base_url=f"http://127.0.0.1:{upstream_port}/v1",
                    upstream_api_key="shared-upstream-key",
                    clients=[
                        ClientConfig(
                            name="client-1",
                            client_api_key="client-key-a",
                            limit=1,
                            count=1,
                        )
                    ],
                    unlock_last=True,
                )
                save_config(config_path, config)
                store = ConfigStore.from_path(config_path)

                proxy_runner = web.AppRunner(
                    create_app(store, expiry_manager=_FakeExpiryManager(last_hour_unlocked=True))
                )
                await proxy_runner.setup()
                proxy_site = web.TCPSite(proxy_runner, "127.0.0.1", proxy_port)
                await proxy_site.start()

                try:
                    async with ClientSession() as session:
                        async with session.get(
                            f"http://127.0.0.1:{proxy_port}/chat",
                            headers={"Authorization": "Bearer client-key-a"},
                        ) as response:
                            payload = await response.json()

                    self.assertEqual(response.status, 200)
                    self.assertEqual(payload, {"ok": True})
                    reloaded = ConfigStore.from_path(config_path)
                    self.assertEqual(reloaded.list_clients()[0].count, 2)
                finally:
                    await proxy_runner.cleanup()
        finally:
            await upstream_site.stop()
            await upstream_runner.cleanup()

    async def test_record_true_saves_full_debug_record_and_prints_summary(self) -> None:
        upstream_port = _find_free_port()
        proxy_port = _find_free_port()

        async def upstream_handler(request: web.Request) -> web.Response:
            payload = await request.json()
            return web.json_response({"echo": payload["message"]})

        upstream_app = web.Application()
        upstream_app.router.add_post("/{tail:.*}", upstream_handler)
        upstream_runner = web.AppRunner(upstream_app)
        await upstream_runner.setup()
        upstream_site = web.TCPSite(upstream_runner, "127.0.0.1", upstream_port)
        await upstream_site.start()

        try:
            with TemporaryDirectory() as temp_dir:
                config_path = Path(temp_dir) / "proxy-config.json"
                debug_dir = Path(temp_dir) / "records"
                config = ProxyConfig(
                    listen_host="127.0.0.1",
                    listen_port=proxy_port,
                    base_url=f"http://127.0.0.1:{upstream_port}/v1",
                    upstream_api_key="shared-upstream-key",
                    clients=[
                        ClientConfig(
                            name="client-1",
                            client_api_key="client-key-a",
                            limit=300,
                            count=0,
                        )
                    ],
                    record=True,
                )
                save_config(config_path, config)
                store = ConfigStore.from_path(config_path)

                proxy_runner = web.AppRunner(create_app(store))
                await proxy_runner.setup()
                proxy_site = web.TCPSite(proxy_runner, "127.0.0.1", proxy_port)
                await proxy_site.start()

                try:
                    with patch.object(debug_record_module, "DEBUG_RECORD_DIR", debug_dir):
                        with patch("builtins.print") as mocked_print:
                            async with ClientSession() as session:
                                async with session.post(
                                    f"http://127.0.0.1:{proxy_port}/chat",
                                    headers={"Authorization": "Bearer client-key-a"},
                                    json={"message": "hello"},
                                ) as response:
                                    payload = await response.json()

                    self.assertEqual(payload, {"echo": "hello"})
                    files = list(debug_dir.glob("*.json"))
                    self.assertEqual(len(files), 1)
                    record = json.loads(files[0].read_text(encoding="utf-8"))
                    self.assertEqual(record["client_name"], "client-1")
                    self.assertEqual(record["downstream_request"]["headers"]["Authorization"], "Bearer client-key-a")
                    self.assertEqual(record["upstream_request"]["headers"]["Authorization"], "Bearer shared-upstream-key")
                    self.assertEqual(record["downstream_request"]["body"], {"message": "hello"})
                    self.assertEqual(record["upstream_request"]["body"], {"message": "hello"})
                    self.assertEqual(record["upstream_response"]["body"], {"echo": "hello"})

                    messages = [" ".join(str(part) for part in call.args) for call in mocked_print.call_args_list]
                    self.assertTrue(any(message.startswith("RECORD {") for message in messages))
                    self.assertTrue(any(str(files[0]) in message for message in messages))
                    self.assertTrue(any("downstream_request" in message for message in messages))
                    self.assertTrue(any("upstream_request" in message for message in messages))
                finally:
                    await proxy_runner.cleanup()
        finally:
            await upstream_site.stop()
            await upstream_runner.cleanup()
