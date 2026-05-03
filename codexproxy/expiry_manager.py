from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

EXPIRY_TIME_FORMAT = "%Y/%m/%d %H:%M:%S"
CACHE_DIR_NAME = "cache"
CACHE_FILE_NAME = "expire-time.json"
ERROR_LOG_PATH = Path("logs/error/update.log")


@dataclass(frozen=True, slots=True)
class ExpiryStatus:
    expire_time_text: str | None
    auto_update_enabled: bool
    notice: str | None = None


class ExpiryManager:
    def __init__(
        self,
        *,
        expire_time: datetime,
        cache_path: Path,
        error_log_path: Path,
        working_dir: Path,
        on_update_success: Callable[[], object] | None = None,
    ) -> None:
        self._expire_time = expire_time
        self._cache_path = cache_path
        self._error_log_path = error_log_path
        self._working_dir = working_dir
        self._on_update_success = on_update_success
        self._notice: str | None = None
        self._auto_update_enabled = True
        self._task: asyncio.Task | None = None

    @classmethod
    def from_runtime(
        cls,
        *,
        config_path: Path,
        expire_time_text: str | None,
        on_update_success: Callable[[], object] | None = None,
    ) -> "ExpiryManager":
        cache_path = config_path.parent / CACHE_DIR_NAME / CACHE_FILE_NAME
        error_log_path = config_path.parent / ERROR_LOG_PATH
        working_dir = config_path.parent

        if expire_time_text is not None:
            expire_time = parse_expire_time(expire_time_text)
            manager = cls(
                expire_time=expire_time,
                cache_path=cache_path,
                error_log_path=error_log_path,
                working_dir=working_dir,
                on_update_success=on_update_success,
            )
            manager._save_cache()
            return manager

        cached_expire_time = _load_cached_expire_time(cache_path)
        if cached_expire_time is None:
            raise SystemExit(
                "expire-time is required on first startup. "
                "Example: uv run codexproxy --expire-time \"2026/5/3 21:32:39\""
            )

        return cls(
            expire_time=cached_expire_time,
            cache_path=cache_path,
            error_log_path=error_log_path,
            working_dir=working_dir,
            on_update_success=on_update_success,
        )

    @property
    def expire_time_text(self) -> str:
        return format_expire_time(self._expire_time)

    def get_status(self) -> ExpiryStatus:
        return ExpiryStatus(
            expire_time_text=(format_expire_time(self._expire_time) if self._expire_time is not None else None),
            auto_update_enabled=self._auto_update_enabled,
            notice=self._notice,
        )

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run_auto_update_loop())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run_auto_update_loop(self) -> None:
        while self._auto_update_enabled:
            delay = max((self._expire_time - datetime.now()).total_seconds(), 0)
            await asyncio.sleep(delay)
            if not await self._run_update_once():
                return

    async def _run_update_once(self) -> bool:
        started_expire_time = format_expire_time(self._expire_time)
        try:
            process = await asyncio.create_subprocess_exec(
                "codex",
                "exec",
                "hello",
                "--skip-git-repo-check",
                cwd=str(self._working_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            self._handle_update_failure(
                f"[{datetime.now().isoformat(sep=' ', timespec='seconds')}] "
                f"expire_time={started_expire_time} process_error={type(exc).__name__}: {exc}\n"
            )
            return False
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            self._handle_update_failure(
                f"[{datetime.now().isoformat(sep=' ', timespec='seconds')}] "
                f"expire_time={started_expire_time} returncode={process.returncode}\n"
                f"stdout:\n{stdout.decode('utf-8', errors='replace')}\n"
                f"stderr:\n{stderr.decode('utf-8', errors='replace')}\n"
            )
            return False

        finished_at = datetime.now()
        self._expire_time = finished_at + timedelta(days=1)
        self._notice = None
        self._auto_update_enabled = True
        if self._on_update_success is not None:
            try:
                self._on_update_success()
            except Exception as exc:
                self._handle_update_failure(
                    f"[{datetime.now().isoformat(sep=' ', timespec='seconds')}] "
                    f"expire_time={started_expire_time} post_update_error={type(exc).__name__}: {exc}\n"
                )
                return False
        self._save_cache()
        print(f"Expire time updated: {format_expire_time(self._expire_time)}")
        return True

    def _handle_update_failure(self, log_message: str) -> None:
        self._auto_update_enabled = False
        self._notice = (
            "Auto update failed. Restart manually with --expire-time to set a new expire time."
        )
        self._delete_cache()
        self._error_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._error_log_path.open("a", encoding="utf-8") as handle:
            handle.write(log_message)
            if not log_message.endswith("\n"):
                handle.write("\n")
        print(f"Expire time auto update failed. See {self._error_log_path}")

    def _save_cache(self) -> None:
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"expire_time": format_expire_time(self._expire_time)}
        self._cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _delete_cache(self) -> None:
        if self._cache_path.exists():
            self._cache_path.unlink()


def parse_expire_time(value: str) -> datetime:
    normalized = value.strip()
    try:
        return datetime.strptime(normalized, EXPIRY_TIME_FORMAT)
    except ValueError as exc:
        raise SystemExit(
            f'expire-time must match format {EXPIRY_TIME_FORMAT!r}, got: {value!r}'
        ) from exc


def format_expire_time(value: datetime) -> str:
    year = value.year
    month = value.month
    day = value.day
    return f"{year}/{month}/{day} {value.strftime('%H:%M:%S')}"


def _load_cached_expire_time(cache_path: Path) -> datetime | None:
    if not cache_path.exists():
        return None

    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    expire_time_text = payload.get("expire_time")
    if not isinstance(expire_time_text, str) or not expire_time_text.strip():
        return None

    try:
        return parse_expire_time(expire_time_text)
    except SystemExit:
        return None
