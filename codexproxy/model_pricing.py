from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

MODEL_PRICING_FILE_NAME = "model-pricing.json"
DEFAULT_MODEL_PRICING_PATH = Path(__file__).resolve().parent.parent / MODEL_PRICING_FILE_NAME
FALLBACK_MODEL_NAME = "other"


@dataclass(frozen=True, slots=True)
class ModelPricing:
    input_per_million_usd: Decimal
    cached_input_per_million_usd: Decimal
    output_per_million_usd: Decimal


def load_model_pricing(config_dir: Path | None = None) -> dict[str, ModelPricing]:
    path = resolve_model_pricing_path(config_dir)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain one JSON object.")

    raw_models = payload.get("models")
    if not isinstance(raw_models, dict):
        raise ValueError(f"{path} must contain a top-level 'models' object.")

    pricing_map: dict[str, ModelPricing] = {}
    for model_name, raw_pricing in raw_models.items():
        if not isinstance(model_name, str) or not model_name.strip():
            raise ValueError(f"{path} contains an invalid model name.")
        pricing_map[model_name.strip()] = _parse_model_pricing(path, model_name, raw_pricing)

    if FALLBACK_MODEL_NAME not in pricing_map:
        raise ValueError(f"{path} must define {FALLBACK_MODEL_NAME!r}.")

    return pricing_map


def resolve_model_pricing_path(config_dir: Path | None = None) -> Path:
    if config_dir is not None:
        local_path = config_dir / MODEL_PRICING_FILE_NAME
        if local_path.exists():
            return local_path
    return DEFAULT_MODEL_PRICING_PATH


def get_model_pricing(model_name: str | None, pricing_map: dict[str, ModelPricing]) -> ModelPricing:
    if model_name is None:
        return pricing_map[FALLBACK_MODEL_NAME]

    normalized = model_name.strip()
    if not normalized:
        return pricing_map[FALLBACK_MODEL_NAME]

    return pricing_map.get(normalized, pricing_map[FALLBACK_MODEL_NAME])


def _parse_model_pricing(path: Path, model_name: str, raw_pricing: object) -> ModelPricing:
    if not isinstance(raw_pricing, dict):
        raise ValueError(f"{path} pricing for {model_name!r} must be an object.")

    input_price = _parse_decimal_field(path, model_name, raw_pricing, "input_per_million_usd")
    cached_input_price = _parse_decimal_field(
        path,
        model_name,
        raw_pricing,
        "cached_input_per_million_usd",
    )
    output_price = _parse_decimal_field(path, model_name, raw_pricing, "output_per_million_usd")
    return ModelPricing(
        input_per_million_usd=input_price,
        cached_input_per_million_usd=cached_input_price,
        output_per_million_usd=output_price,
    )


def _parse_decimal_field(
    path: Path,
    model_name: str,
    raw_pricing: dict[str, object],
    field_name: str,
) -> Decimal:
    raw_value = raw_pricing.get(field_name)
    if not isinstance(raw_value, (int, float, str)) or isinstance(raw_value, bool):
        raise ValueError(f"{path} pricing for {model_name!r}.{field_name} must be numeric.")
    value = Decimal(str(raw_value))
    if value < 0:
        raise ValueError(f"{path} pricing for {model_name!r}.{field_name} must be >= 0.")
    return value
