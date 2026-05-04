from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from threading import Lock

from codexproxy.model_pricing import get_model_pricing, load_model_pricing

CACHE_DIR_NAME = "cache"
DAILY_SPEND_FILE_NAME = "daily-spend.json"
USD_PRECISION = Decimal("0.000001")
TOKENS_PER_MILLION = Decimal("1000000")


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class DailySpendStatus:
    date_text: str
    client_usd: Decimal
    total_usd: Decimal


class SpendTracker:
    def __init__(self, config_dir: Path) -> None:
        self._config_dir = config_dir
        self._path = config_dir / CACHE_DIR_NAME / DAILY_SPEND_FILE_NAME
        self._pricing_map = load_model_pricing(config_dir)
        self._lock = Lock()

    def get_status(self, client_name: str) -> DailySpendStatus:
        with self._lock:
            payload = self._load_payload_locked()
            today_text = _today_text()
            if payload.get("date") != today_text:
                return DailySpendStatus(
                    date_text=today_text,
                    client_usd=Decimal("0"),
                    total_usd=Decimal("0"),
                )

            clients = payload.get("clients", {})
            if not isinstance(clients, dict):
                clients = {}
            return DailySpendStatus(
                date_text=today_text,
                client_usd=_parse_decimal(clients.get(client_name, "0")),
                total_usd=_parse_decimal(payload.get("total_usd", "0")),
            )

    def record_usage(self, client_name: str, model_name: str | None, usage: TokenUsage) -> Decimal:
        pricing = get_model_pricing(model_name, self._pricing_map)
        cost = _calculate_cost_usd(pricing, usage)
        if cost <= 0:
            return Decimal("0")

        with self._lock:
            payload = self._load_payload_locked()
            today_text = _today_text()
            if payload.get("date") != today_text:
                payload = {
                    "date": today_text,
                    "total_usd": "0.000000",
                    "clients": {},
                }

            clients = payload.setdefault("clients", {})
            if not isinstance(clients, dict):
                clients = {}
                payload["clients"] = clients

            total_usd = _parse_decimal(payload.get("total_usd", "0")) + cost
            client_usd = _parse_decimal(clients.get(client_name, "0")) + cost
            payload["total_usd"] = _format_decimal(total_usd)
            clients[client_name] = _format_decimal(client_usd)
            self._save_payload_locked(payload)
        return cost

    def _load_payload_locked(self) -> dict:
        if not self._path.exists():
            return {"date": _today_text(), "total_usd": "0.000000", "clients": {}}

        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"date": _today_text(), "total_usd": "0.000000", "clients": {}}

        if not isinstance(payload, dict):
            return {"date": _today_text(), "total_usd": "0.000000", "clients": {}}
        return payload

    def _save_payload_locked(self, payload: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def _calculate_cost_usd(pricing, usage: TokenUsage) -> Decimal:
    non_cached_input_tokens = max(usage.input_tokens - usage.cached_input_tokens, 0)
    cost = (
        (Decimal(non_cached_input_tokens) * pricing.input_per_million_usd / TOKENS_PER_MILLION)
        + (
            Decimal(usage.cached_input_tokens)
            * pricing.cached_input_per_million_usd
            / TOKENS_PER_MILLION
        )
        + (Decimal(usage.output_tokens) * pricing.output_per_million_usd / TOKENS_PER_MILLION)
    )
    return cost.quantize(USD_PRECISION, rounding=ROUND_HALF_UP)


def _format_decimal(value: Decimal) -> str:
    return str(value.quantize(USD_PRECISION, rounding=ROUND_HALF_UP))


def _parse_decimal(value: object) -> Decimal:
    if isinstance(value, str) and value.strip():
        return Decimal(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return Decimal(str(value))
    return Decimal("0")


def _today_text() -> str:
    return date.today().isoformat()
