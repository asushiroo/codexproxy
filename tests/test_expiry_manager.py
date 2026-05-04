from __future__ import annotations

from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import codexproxy.expiry_manager as expiry_manager_module
from codexproxy.expiry_manager import ExpiryManager


class _FakeProcess:
    def __init__(self, returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self):
        return self._stdout, self._stderr


class ExpiryManagerTests(unittest.IsolatedAsyncioTestCase):
    def test_startup_resolves_codex_to_absolute_path(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "proxy-config.json"

            with patch.object(expiry_manager_module.shutil, "which", return_value="/opt/bin/codex"):
                manager = ExpiryManager.from_runtime(
                    config_path=config_path,
                    expire_time_text="2026/5/3 21:32:39",
                )

            self.assertEqual(manager.codex_executable, "/opt/bin/codex")

    def test_first_startup_requires_expire_time_when_cache_missing(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "proxy-config.json"

            with self.assertRaises(SystemExit) as context:
                ExpiryManager.from_runtime(config_path=config_path, expire_time_text=None)

            self.assertIn("expire-time is required on first startup", str(context.exception))

    def test_cached_expire_time_can_be_reused_on_later_startup(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "proxy-config.json"
            first = ExpiryManager.from_runtime(
                config_path=config_path,
                expire_time_text="2026/5/3 21:32:39",
            )

            second = ExpiryManager.from_runtime(config_path=config_path, expire_time_text=None)

            self.assertEqual(first.expire_time_text, "2026/5/3 21:32:39")
            self.assertEqual(second.expire_time_text, "2026/5/3 21:32:39")

    async def test_update_failure_deletes_cache_and_writes_error_log(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "proxy-config.json"
            manager = ExpiryManager.from_runtime(
                config_path=config_path,
                expire_time_text="2026/5/3 21:32:39",
            )

            async def fake_create_subprocess_exec(*args, **kwargs):
                return _FakeProcess(returncode=1, stdout=b"oops", stderr=b"bad")

            with patch.object(expiry_manager_module.asyncio, "create_subprocess_exec", side_effect=fake_create_subprocess_exec):
                updated = await manager._run_update_once()

            self.assertFalse(updated)
            self.assertFalse((Path(temp_dir) / "cache" / "expire-time.json").exists())
            self.assertTrue((Path(temp_dir) / "logs" / "error" / "update.log").exists())
            self.assertFalse(manager.get_status().auto_update_enabled)
            self.assertIn("Restart manually with --expire-time", manager.get_status().notice or "")

    async def test_update_success_sets_next_expire_time_to_finish_time_plus_one_day(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "proxy-config.json"
            callback_calls: list[str] = []
            called_args: list[tuple] = []
            manager = ExpiryManager.from_runtime(
                config_path=config_path,
                expire_time_text="2026/5/3 21:32:39",
                on_update_success=lambda: callback_calls.append("reset"),
            )
            manager._codex_executable = "/opt/bin/codex"

            async def fake_create_subprocess_exec(*args, **kwargs):
                called_args.append(args)
                return _FakeProcess(returncode=0, stdout=b"ok", stderr=b"")

            real_datetime = expiry_manager_module.datetime

            class FakeDateTime(real_datetime):
                @classmethod
                def now(cls, tz=None):
                    return cls(2026, 5, 3, 22, 0, 0)

            with patch.object(expiry_manager_module.asyncio, "create_subprocess_exec", side_effect=fake_create_subprocess_exec):
                with patch.object(expiry_manager_module, "datetime", FakeDateTime):
                    updated = await manager._run_update_once()

            self.assertTrue(updated)
            self.assertEqual(manager.expire_time_text, "2026/5/4 22:00:00")
            self.assertEqual(callback_calls, ["reset"])
            self.assertEqual(called_args[0][0], "/opt/bin/codex")
            cached_text = (Path(temp_dir) / "cache" / "expire-time.json").read_text(encoding="utf-8")
            self.assertIn("2026/5/4 22:00:00", cached_text)
