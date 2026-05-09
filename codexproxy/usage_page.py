from __future__ import annotations

from html import escape
from math import pi

from codexproxy.expiry_manager import ExpiryStatus
from codexproxy.spend_tracker import DailySpendStatus
from codexproxy.state import ClientBinding


def render_usage_page(
    binding: ClientBinding,
    expiry_status: ExpiryStatus | None = None,
    spend_status: DailySpendStatus | None = None,
) -> str:
    remaining = max(binding.limit - binding.count, 0)
    usage_percent = 0 if binding.limit == 0 else round((binding.count / binding.limit) * 100, 2)
    ring_percent = min(max(usage_percent, 0), 100)
    ring_size = 176
    ring_center = ring_size / 2
    ring_radius = 73.6
    ring_circumference = 2 * pi * ring_radius
    ring_offset = ring_circumference * (1 - (ring_percent / 100))
    client_name = escape(binding.name)
    expire_time_text = escape(expiry_status.expire_time_text or "Not set") if expiry_status else "Not set"
    auto_update_enabled = "enabled" if expiry_status and expiry_status.auto_update_enabled else "disabled"
    unlock_last_badge_html = _render_unlock_last_badge(expiry_status)
    today_total_usd = _format_usd(spend_status.total_usd if spend_status else None)
    today_date_text = escape(spend_status.date_text if spend_status else "Not available")
    usage_percent_text = _format_percent(usage_percent)
    ring_color = _get_ring_color(ring_percent)
    notice_html = ""
    if expiry_status and expiry_status.notice:
        notice_html = (
            '<div class="notice">'
            f"{escape(expiry_status.notice)}"
            "</div>"
        )

    return f"""<!DOCTYPE html>
<html lang=\"zh-CN\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>{client_name} usage</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 40px; background: linear-gradient(180deg, #f8fafc 0%, #eef2ff 100%); color: #111827; }}
    .card {{ max-width: 920px; background: rgba(255,255,255,0.94); border: 1px solid #e5e7eb; border-radius: 24px; padding: 28px; box-shadow: 0 18px 50px rgba(15,23,42,0.08); backdrop-filter: blur(10px); }}
    h1 {{ margin: 0; font-size: 28px; }}
    .hero {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }}
    .content {{ display: grid; grid-template-columns: 220px minmax(0, 1fr); gap: 24px; margin-top: 24px; align-items: center; }}
    .stats-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }}
    .item {{ background: linear-gradient(180deg, #ffffff 0%, #f8fafc 100%); border-radius: 18px; padding: 18px; border: 1px solid #e5e7eb; min-height: 112px; display: flex; flex-direction: column; justify-content: center; }}
    .label {{ color: #6b7280; font-size: 14px; margin-bottom: 8px; }}
    .value {{ font-size: 28px; font-weight: 700; line-height: 1.2; }}
    .meta {{ margin-top: 24px; color: #4b5563; line-height: 1.8; word-break: break-all; padding-top: 20px; border-top: 1px solid #e5e7eb; }}
    .notice {{ margin-top: 20px; padding: 14px 16px; border-radius: 12px; background: #fff7ed; color: #9a3412; border: 1px solid #fdba74; }}
    .unlock-last-badge {{ display: inline-block; padding: 10px 14px; border-radius: 999px; background: #dc2626; color: #ffffff; font-size: 13px; font-weight: 800; letter-spacing: 0.04em; box-shadow: 0 8px 20px rgba(220,38,38,0.25); }}
    .ring-panel {{ display: flex; justify-content: center; }}
    .usage-ring {{ position: relative; width: {ring_size}px; aspect-ratio: 1; display: grid; place-items: center; }}
    .ring-svg {{ width: {ring_size}px; height: {ring_size}px; transform: rotate(-90deg); filter: drop-shadow(0 14px 30px rgba(15,23,42,0.08)); }}
    .ring-track {{ fill: none; stroke: #e5e7eb; stroke-width: 14.4; }}
    .ring-progress {{ fill: none; stroke: {ring_color}; stroke-width: 14.4; stroke-linecap: round; stroke-dasharray: {ring_circumference:.2f}; stroke-dashoffset: {ring_circumference:.2f}; transition: stroke-dashoffset 1.1s ease-out; }}
    .ring-inner {{ position: absolute; width: 125px; aspect-ratio: 1; border-radius: 50%; background: #ffffff; display: flex; align-items: center; justify-content: center; box-shadow: inset 0 0 0 1px #eef2f7; }}
    .ring-value {{ font-size: 36px; font-weight: 800; color: {ring_color}; line-height: 1; }}
    @media (max-width: 760px) {{
      body {{ margin: 16px; }}
      .card {{ padding: 20px; border-radius: 20px; }}
      .hero {{ flex-direction: column; }}
      .content {{ grid-template-columns: 1fr; }}
      .stats-grid {{ grid-template-columns: 1fr; }}
      .usage-ring {{ width: {ring_size}px; }}
      .ring-inner {{ width: 125px; }}
    }}
  </style>
</head>
<body>
  <div class=\"card\">
    <div class=\"hero\">
      <h1>Client Usage</h1>
      {unlock_last_badge_html}
    </div>
    {notice_html}
    <div class=\"content\">
      <div class=\"ring-panel\">
        <div class=\"usage-ring\" data-progress=\"{ring_percent}\">
          <svg class=\"ring-svg\" viewBox=\"0 0 {ring_size} {ring_size}\" aria-hidden=\"true\">
            <circle class=\"ring-track\" cx=\"{ring_center}\" cy=\"{ring_center}\" r=\"{ring_radius}\" />
            <circle
              class=\"ring-progress\"
              cx=\"{ring_center}\"
              cy=\"{ring_center}\"
              r=\"{ring_radius}\"
              data-offset=\"{ring_offset:.2f}\"
            />
          </svg>
          <div class=\"ring-inner\">
            <div class=\"ring-value\">{usage_percent_text}</div>
          </div>
        </div>
      </div>
      <div class=\"stats-grid\">
        <div class=\"item\">
          <div class=\"label\">Remaining</div>
          <div class=\"value\">{remaining} / {binding.limit}</div>
        </div>
        <div class=\"item\">
          <div class=\"label\">Used</div>
          <div class=\"value\">{binding.count} / {binding.limit}</div>
        </div>
        <div class=\"item\">
          <div class=\"label\">Limit</div>
          <div class=\"value\">{binding.limit}</div>
        </div>
        <div class=\"item\">
          <div class=\"label\">Today Total USD ({today_date_text})</div>
          <div class=\"value\">{today_total_usd}</div>
        </div>
      </div>
    </div>
    <div class=\"meta\">
      <div><strong>client</strong>: {client_name}</div>
      <div><strong>expire_time</strong>: {expire_time_text}</div>
      <div><strong>auto_update</strong>: {auto_update_enabled}</div>
    </div>
  </div>
  <script>
    const ring = document.querySelector('.usage-ring');
    const progressCircle = document.querySelector('.ring-progress');
    if (ring && progressCircle) {{
      requestAnimationFrame(() => {{
        progressCircle.style.strokeDashoffset = progressCircle.dataset.offset || '0';
      }});
    }}
  </script>
</body>
</html>
"""


def _render_unlock_last_badge(expiry_status: ExpiryStatus | None) -> str:
    if expiry_status is None:
        return ""
    if not expiry_status.unlock_last_enabled or not expiry_status.unlock_last_active:
        return ""
    return '<div class="unlock-last-badge">UNLOCK LAST ACTIVE</div>'


def _format_usd(value) -> str:
    if value is None:
        return "$0.000000"
    return f"${value:.6f}"


def _format_percent(value: float) -> str:
    numeric_value = float(value)
    if numeric_value.is_integer():
        return f"{int(numeric_value)}%"
    return f"{numeric_value:.2f}".rstrip("0").rstrip(".") + "%"


def _get_ring_color(percent: float) -> str:
    if percent < 50:
        return "#16a34a"
    if percent < 90:
        return "#f59e0b"
    return "#dc2626"
