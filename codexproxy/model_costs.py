from __future__ import annotations

import json
import math
from collections.abc import Mapping
from pathlib import Path

MODEL_COSTS_FILE_NAME = "model-costs.json"
DEFAULT_MODEL_COSTS_PATH = Path(__file__).resolve().parent.parent / MODEL_COSTS_FILE_NAME
FALLBACK_MODEL_NAME = "other"


def load_model_costs(config_dir: Path | None = None) -> dict[str, float]:
    path = resolve_model_costs_path(config_dir)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain one JSON object.")

    model_costs: dict[str, float] = {}
    for model_name, cost in payload.items():
        if not isinstance(model_name, str) or not model_name.strip():
            raise ValueError(f"{path} contains an invalid model name.")
        if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost <= 0:
            raise ValueError(f"{path} cost for {model_name!r} must be a finite number > 0.")
        model_costs[model_name.strip()] = float(cost)

    if FALLBACK_MODEL_NAME not in model_costs:
        raise ValueError(f"{path} must define {FALLBACK_MODEL_NAME!r}.")

    return model_costs


def resolve_model_costs_path(config_dir: Path | None = None) -> Path:
    if config_dir is not None:
        local_path = config_dir / MODEL_COSTS_FILE_NAME
        if local_path.exists():
            return local_path
    return DEFAULT_MODEL_COSTS_PATH


def get_model_cost(model_name: str | None, model_costs: Mapping[str, float]) -> float:
    if model_name is None:
        return model_costs[FALLBACK_MODEL_NAME]

    normalized = model_name.strip()
    if not normalized:
        return model_costs[FALLBACK_MODEL_NAME]

    return model_costs.get(normalized, model_costs[FALLBACK_MODEL_NAME])
