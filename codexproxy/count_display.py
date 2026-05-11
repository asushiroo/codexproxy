from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP


def round_count_for_display(value: int | float) -> int:
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
