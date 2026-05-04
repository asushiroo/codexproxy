from __future__ import annotations

from html import escape

from codexproxy.expiry_manager import ExpiryStatus
from codexproxy.spend_tracker import DailySpendStatus
from codexproxy.state import ClientBinding


def render_usage_page(
    binding: ClientBinding,
    expiry_status: ExpiryStatus | None = None,
    spend_status: DailySpendStatus | None = None,
) -> str:
    remaining = max(binding.limit - binding.count, 0)
    usage_percent = (
        0 if binding.limit == 0 else round((binding.count / binding.limit) * 100, 2)
    )
    client_name = escape(binding.name)
    expire_time_text = escape(expiry_status.expire_time_text or "Not set") if expiry_status else "Not set"
    auto_update_enabled = "enabled" if expiry_status and expiry_status.auto_update_enabled else "disabled"
    unlock_last_badge_html = _render_unlock_last_badge(expiry_status)
    today_client_usd = _format_usd(spend_status.client_usd if spend_status else None)
    today_total_usd = _format_usd(spend_status.total_usd if spend_status else None)
    today_date_text = escape(spend_status.date_text if spend_status else "Not available")
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
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 40px; background: #f7f7f8; color: #111827; }}
    .card {{ max-width: 720px; background: #ffffff; border: 1px solid #e5e7eb; border-radius: 16px; padding: 24px; box-shadow: 0 10px 30px rgba(0,0,0,0.05); }}
    h1 {{ margin-top: 0; font-size: 28px; }}
    .grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; margin-top: 24px; }}
    .item {{ background: #f9fafb; border-radius: 12px; padding: 16px; border: 1px solid #e5e7eb; }}
    .label {{ color: #6b7280; font-size: 14px; margin-bottom: 8px; }}
    .value {{ font-size: 28px; font-weight: 700; }}
    .meta {{ margin-top: 24px; color: #4b5563; line-height: 1.7; word-break: break-all; }}
    .notice {{ margin-top: 20px; padding: 14px 16px; border-radius: 12px; background: #fff7ed; color: #9a3412; border: 1px solid #fdba74; }}
    .unlock-last-badge {{ display: inline-block; margin-top: 16px; padding: 10px 14px; border-radius: 999px; background: #dc2626; color: #ffffff; font-size: 13px; font-weight: 800; letter-spacing: 0.04em; box-shadow: 0 8px 20px rgba(220,38,38,0.25); }}
  </style>
</head>
<body>
  <div class=\"card\">
    <h1>Client Usage</h1>
    {unlock_last_badge_html}
    <div class=\"meta\">
      <div><strong>client</strong>: {client_name}</div>
      <div><strong>expire_time</strong>: {expire_time_text}</div>
      <div><strong>auto_update</strong>: {auto_update_enabled}</div>
    </div>
    {notice_html}
    <div class=\"grid\">
      <div class=\"item\">
        <div class=\"label\">Remaining</div>
        <div class=\"value\">{remaining} / {binding.limit}</div>
      </div>
      <div class=\"item\">
        <div class=\"label\">Used</div>
        <div class=\"value\">{binding.count} / {binding.limit}</div>
      </div>
      <div class=\"item\">
        <div class=\"label\">Usage Percent</div>
        <div class=\"value\">{usage_percent}%</div>
      </div>
      <div class=\"item\">
        <div class=\"label\">Limit</div>
        <div class=\"value\">{binding.limit}</div>
      </div>
      <div class=\"item\">
        <div class=\"label\">Today USD ({today_date_text})</div>
        <div class=\"value\">{today_client_usd}</div>
      </div>
      <div class=\"item\">
        <div class=\"label\">Today Total USD ({today_date_text})</div>
        <div class=\"value\">{today_total_usd}</div>
      </div>
    </div>
  </div>
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
