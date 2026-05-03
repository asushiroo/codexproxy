from contextlib import redirect_stdout
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import unittest
from unittest.mock import patch

from codexproxy.cli import main
from codexproxy.config import load_config, save_config, build_default_config


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
            self.assertEqual(config.clients[1].name, "client-2")
            self.assertEqual(config.clients[1].limit, 300)
            self.assertEqual(config.clients[1].count, 0)
            self.assertNotEqual(config.clients[0].client_api_key, config.clients[1].client_api_key)
            self.assertIn("Added client client-2", output)
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
            self.assertEqual(config.clients[0].name, "client-1")
            self.assertEqual(config.clients[0].limit, 300)
            self.assertEqual(config.clients[0].count, 0)
            self.assertIn("Added client client-1", output)
            self.assertIn(config.clients[0].client_api_key, output)
