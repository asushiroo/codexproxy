from contextlib import redirect_stdout
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import unittest
from unittest.mock import patch

from codexproxy.cli import main
from codexproxy.config import (
    DEFAULT_CLIENT_NAME_PREFIX,
    load_config,
    save_config,
    build_default_config,
)


class CliTests(unittest.TestCase):
    def test_new_client_appends_incremented_client_and_prints_api_key(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "proxy-config.json"
            save_config(
                config_path,
                build_default_config(
                    "https://example.invalid/v1",
                    "shared-upstream-key",
                    client_count=1,
                ),
            )

            stdout = io.StringIO()
            with patch.object(sys, "argv", ["codexproxy", "new-client", "--config", str(config_path)]):
                with redirect_stdout(stdout):
                    main()

            output = stdout.getvalue()
            config = load_config(config_path)

            self.assertEqual(len(config.clients), 2)
            self.assertTrue(config.clients[1].name.startswith(DEFAULT_CLIENT_NAME_PREFIX))
            self.assertNotEqual(config.clients[0].name, config.clients[1].name)
            self.assertEqual(
                len(config.clients[1].name.removeprefix(DEFAULT_CLIENT_NAME_PREFIX)),
                config.client_name_suffix_length,
            )
            self.assertEqual(config.clients[1].limit, 300)
            self.assertEqual(config.clients[1].count, 0)
            self.assertNotEqual(config.clients[0].client_api_key, config.clients[1].client_api_key)
            self.assertIn(f"Added client {config.clients[1].name}", output)
            self.assertIn(config.clients[1].client_api_key, output)

    def test_new_client_works_when_clients_list_is_empty(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "proxy-config.json"
            save_config(
                config_path,
                build_default_config(
                    "https://example.invalid/v1",
                    "shared-upstream-key",
                    client_count=0,
                ),
            )

            stdout = io.StringIO()
            with patch.object(sys, "argv", ["codexproxy", "new-client", "--config", str(config_path)]):
                with redirect_stdout(stdout):
                    main()

            output = stdout.getvalue()
            config = load_config(config_path)

            self.assertEqual(len(config.clients), 1)
            self.assertTrue(config.clients[0].name.startswith(DEFAULT_CLIENT_NAME_PREFIX))
            self.assertEqual(
                len(config.clients[0].name.removeprefix(DEFAULT_CLIENT_NAME_PREFIX)),
                config.client_name_suffix_length,
            )
            self.assertEqual(config.clients[0].limit, 300)
            self.assertEqual(config.clients[0].count, 0)
            self.assertIn(f"Added client {config.clients[0].name}", output)
            self.assertIn(config.clients[0].client_api_key, output)

    def test_init_config_supports_custom_client_name_suffix_length(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "proxy-config.json"

            with patch.object(
                sys,
                "argv",
                [
                    "codexproxy",
                    "init-config",
                    "--config",
                    str(config_path),
                    "--base-url",
                    "https://example.invalid/v1",
                    "--upstream-api-key",
                    "shared-upstream-key",
                    "--client-count",
                    "1",
                    "--client-name-suffix-length",
                    "6",
                ],
            ):
                main()

            config = load_config(config_path)
            self.assertEqual(config.client_name_suffix_length, 6)
            self.assertEqual(
                len(config.clients[0].name.removeprefix(DEFAULT_CLIENT_NAME_PREFIX)),
                6,
            )

    def test_init_config_supports_unlock_last(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "proxy-config.json"

            with patch.object(
                sys,
                "argv",
                [
                    "codexproxy",
                    "init-config",
                    "--config",
                    str(config_path),
                    "--base-url",
                    "https://example.invalid/v1",
                    "--upstream-api-key",
                    "shared-upstream-key",
                    "--unlock-last",
                ],
            ):
                main()

            config = load_config(config_path)
            self.assertTrue(config.unlock_last)

    def test_default_run_passes_expire_time_argument(self) -> None:
        with patch("codexproxy.cli._run_command") as mocked_run_command:
            with patch.object(
                sys,
                "argv",
                ["codexproxy", "--expire-time", "2026/5/3 21:32:39"],
            ):
                main()

        mocked_run_command.assert_called_once()
        _, kwargs = mocked_run_command.call_args
        self.assertEqual(kwargs["expire_time"], "2026/5/3 21:32:39")
