import json
import socket
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import codexproxy.debug_record as debug_record_module
import codexproxy.proxy as proxy_module
from aiohttp import ClientSession, web
from yarl import URL

from codexproxy.config import ClientConfig, ProxyConfig, save_config
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

            proxy_runner = web.AppRunner(create_app(store))
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
