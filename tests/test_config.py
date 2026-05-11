from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
from unittest.mock import patch

from codexproxy.config import (
    ClientConfig,
    DEFAULT_CLIENT_LIMIT,
    DEFAULT_CLIENT_NAME_PREFIX,
    DEFAULT_CLIENT_NAME_SUFFIX_LENGTH,
    DEFAULT_LISTEN_PORT,
    add_new_client,
    build_default_config,
    load_config,
    save_config,
)
from codexproxy.state import ConfigStore


class ConfigTests(unittest.TestCase):
    def test_default_config_uses_shared_upstream_and_default_limit(self) -> None:
        config = build_default_config(
            "https://example.invalid/v1",
            "shared-upstream-key",
            client_count=2,
        )

        self.assertEqual(config.listen_port, DEFAULT_LISTEN_PORT)
        self.assertEqual(config.base_url, "https://example.invalid/v1")
        self.assertEqual(config.upstream_api_key, "shared-upstream-key")
        self.assertEqual(len(config.clients), 2)
        self.assertTrue(config.clients[0].name.startswith(DEFAULT_CLIENT_NAME_PREFIX))
        self.assertTrue(config.clients[1].name.startswith(DEFAULT_CLIENT_NAME_PREFIX))
        self.assertEqual(config.client_name_suffix_length, DEFAULT_CLIENT_NAME_SUFFIX_LENGTH)
        self.assertEqual(
            len(config.clients[0].name.removeprefix(DEFAULT_CLIENT_NAME_PREFIX)),
            DEFAULT_CLIENT_NAME_SUFFIX_LENGTH,
        )
        self.assertEqual(
            len(config.clients[1].name.removeprefix(DEFAULT_CLIENT_NAME_PREFIX)),
            DEFAULT_CLIENT_NAME_SUFFIX_LENGTH,
        )
        self.assertNotEqual(config.clients[0].name, config.clients[1].name)
        self.assertEqual(config.clients[0].limit, DEFAULT_CLIENT_LIMIT)
        self.assertEqual(config.clients[1].limit, DEFAULT_CLIENT_LIMIT)
        self.assertEqual(config.clients[0].count, 0)
        self.assertEqual(config.clients[1].count, 0)
        self.assertNotEqual(config.clients[0].client_api_key, config.clients[1].client_api_key)
        self.assertFalse(config.record)
        self.assertFalse(config.unlock_last)

    def test_add_new_client_generates_random_name_and_unique_key(self) -> None:
        config = build_default_config(
            "https://example.invalid/v1",
            "shared-upstream-key",
            client_count=2,
        )
        existing_names = {client.name for client in config.clients}
        existing_keys = {client.client_api_key for client in config.clients}

        new_client = add_new_client(config)

        self.assertTrue(new_client.name.startswith(DEFAULT_CLIENT_NAME_PREFIX))
        self.assertNotIn(new_client.name, existing_names)
        self.assertEqual(
            len(new_client.name.removeprefix(DEFAULT_CLIENT_NAME_PREFIX)),
            DEFAULT_CLIENT_NAME_SUFFIX_LENGTH,
        )
        self.assertEqual(new_client.limit, DEFAULT_CLIENT_LIMIT)
        self.assertEqual(new_client.count, 0)
        self.assertNotIn(new_client.client_api_key, existing_keys)

    def test_add_new_client_retries_when_generated_name_is_duplicated(self) -> None:
        config = build_default_config(
            "https://example.invalid/v1",
            "shared-upstream-key",
            client_count=0,
        )
        config.clients.append(
            ClientConfig(name="client-aaaa", client_api_key="client-key-a")
        )

        with patch("codexproxy.config.secrets.token_hex", side_effect=["aaaa", "cccc"]):
            new_client = add_new_client(config)

        self.assertEqual(new_client.name, "client-cccc")

    def test_save_and_load_client_name_suffix_length(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "proxy-config.json"
            save_config(
                config_path,
                build_default_config(
                    "https://example.invalid/v1",
                    "shared-upstream-key",
                    client_count=1,
                    client_name_suffix_length=6,
                ),
            )

            reloaded = load_config(config_path)

            self.assertEqual(reloaded.client_name_suffix_length, 6)
            self.assertEqual(
                len(reloaded.clients[0].name.removeprefix(DEFAULT_CLIENT_NAME_PREFIX)),
                6,
            )

    def test_reserve_request_persists_count(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "proxy-config.json"
            config = build_default_config(
                "https://example.invalid/v1",
                "shared-upstream-key",
                client_count=1,
            )
            save_config(config_path, config)

            store = ConfigStore.from_path(config_path)
            binding = store.reserve_request(config.clients[0].client_api_key)

            self.assertEqual(binding.name, config.clients[0].name)
            self.assertEqual(binding.count, 1)
            reloaded = load_config(config_path)
            self.assertEqual(reloaded.clients[0].count, 1)
            self.assertEqual(reloaded.base_url, "https://example.invalid/v1")
            self.assertEqual(reloaded.upstream_api_key, "shared-upstream-key")

    def test_reserve_request_persists_decimal_count(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "proxy-config.json"
            config = build_default_config(
                "https://example.invalid/v1",
                "shared-upstream-key",
                client_count=1,
            )
            save_config(config_path, config)

            store = ConfigStore.from_path(config_path)
            binding = store.reserve_request(config.clients[0].client_api_key, request_cost=1.6)

            self.assertEqual(binding.count, 1.6)
            reloaded = load_config(config_path)
            self.assertEqual(reloaded.clients[0].count, 1.6)

    def test_save_and_load_record_flag(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "proxy-config.json"
            save_config(
                config_path,
                build_default_config(
                    "https://example.invalid/v1",
                    "shared-upstream-key",
                    client_count=1,
                    record=True,
                ),
            )

            reloaded = load_config(config_path)

            self.assertTrue(reloaded.record)

    def test_save_and_load_unlock_last_flag(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "proxy-config.json"
            save_config(
                config_path,
                build_default_config(
                    "https://example.invalid/v1",
                    "shared-upstream-key",
                    client_count=1,
                    unlock_last=True,
                ),
            )

            reloaded = load_config(config_path)

            self.assertTrue(reloaded.unlock_last)

    def test_load_config_supports_legacy_client_scoped_upstream_when_values_match(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "proxy-config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "listen_host": "0.0.0.0",
                        "listen_port": 7001,
                        "record": True,
                        "clients": [
                            {
                                "name": "client-1",
                                "client_api_key": "client-key-a",
                                "upstream_api_key": "shared-upstream-key",
                                "base_url": "https://example.invalid/v1",
                                "limit": 300,
                                "count": 1,
                            },
                            {
                                "name": "client-2",
                                "client_api_key": "client-key-b",
                                "upstream_api_key": "shared-upstream-key",
                                "base_url": "https://example.invalid/v1",
                                "limit": 300,
                                "count": 2,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            config = load_config(config_path)

            self.assertEqual(config.base_url, "https://example.invalid/v1")
            self.assertEqual(config.upstream_api_key, "shared-upstream-key")
            self.assertEqual(config.clients[0].count, 1)
            self.assertEqual(config.clients[1].count, 2)
            self.assertTrue(config.record)
            self.assertFalse(config.unlock_last)
