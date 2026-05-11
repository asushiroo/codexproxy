from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlparse

DEFAULT_CONFIG_PATH = Path("proxy-config.json")
DEFAULT_LISTEN_PORT = 7001
DEFAULT_CLIENT_COUNT = 2
DEFAULT_CLIENT_LIMIT = 300
DEFAULT_CLIENT_NAME_PREFIX = "client-"
DEFAULT_CLIENT_NAME_SUFFIX_LENGTH = 4
DEFAULT_CLIENT_API_KEY_PREFIX = "sk-client-"


@dataclass(slots=True)
class ClientConfig:
    name: str
    client_api_key: str
    limit: int = DEFAULT_CLIENT_LIMIT
    count: int | float = 0


@dataclass(slots=True)
class ProxyConfig:
    listen_host: str
    listen_port: int
    base_url: str
    upstream_api_key: str
    clients: list[ClientConfig]
    client_name_suffix_length: int = DEFAULT_CLIENT_NAME_SUFFIX_LENGTH
    advertise_host: str | None = None
    record: bool = False
    unlock_last: bool = False


def build_default_config(
    base_url: str,
    upstream_api_key: str,
    *,
    client_count: int = DEFAULT_CLIENT_COUNT,
    client_name_suffix_length: int = DEFAULT_CLIENT_NAME_SUFFIX_LENGTH,
    listen_port: int = DEFAULT_LISTEN_PORT,
    advertise_host: str | None = None,
    record: bool = False,
    unlock_last: bool = False,
) -> ProxyConfig:
    config = ProxyConfig(
        listen_host="0.0.0.0",
        listen_port=listen_port,
        base_url=base_url,
        upstream_api_key=upstream_api_key,
        clients=[],
        client_name_suffix_length=client_name_suffix_length,
        advertise_host=advertise_host,
        record=record,
        unlock_last=unlock_last,
    )
    for _ in range(client_count):
        add_new_client(config)
    return config


def load_config(path: Path) -> ProxyConfig:
    payload = json.loads(path.read_text(encoding="utf-8"))

    if "clients" not in payload:
        raise ValueError("Config must include clients.")

    if "base_url" in payload and "upstream_api_key" in payload:
        config = ProxyConfig(
            listen_host=payload.get("listen_host", "0.0.0.0"),
            listen_port=payload.get("listen_port", DEFAULT_LISTEN_PORT),
            base_url=payload["base_url"],
            upstream_api_key=payload["upstream_api_key"],
            clients=[ClientConfig(**item) for item in payload["clients"]],
            client_name_suffix_length=payload.get(
                "client_name_suffix_length",
                DEFAULT_CLIENT_NAME_SUFFIX_LENGTH,
            ),
            advertise_host=payload.get("advertise_host"),
            record=payload.get("record", False),
            unlock_last=payload.get("unlock_last", False),
        )
        validate_config(config)
        return config

    return _load_legacy_client_scoped_config(payload)


def save_config(path: Path, config: ProxyConfig) -> None:
    payload = {
        "listen_host": config.listen_host,
        "listen_port": config.listen_port,
        "base_url": config.base_url,
        "upstream_api_key": config.upstream_api_key,
        "client_name_suffix_length": config.client_name_suffix_length,
        "record": config.record,
        "unlock_last": config.unlock_last,
        "clients": [asdict(item) for item in config.clients],
    }
    if config.advertise_host:
        payload["advertise_host"] = config.advertise_host
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w",
        encoding="utf-8",
        delete=False,
        dir=path.parent,
        prefix=f"{path.name}.",
        suffix=".tmp",
    ) as handle:
        json.dump(payload, handle, ensure_ascii=True, indent=2)
        handle.write("\n")
        temp_path = Path(handle.name)
    temp_path.replace(path)


def validate_config(config: ProxyConfig) -> None:
    if config.listen_port < 1 or config.listen_port > 65535:
        raise ValueError("listen_port must be between 1 and 65535.")
    if config.advertise_host is not None and not config.advertise_host.strip():
        raise ValueError("advertise_host cannot be blank when provided.")
    if not isinstance(config.record, bool):
        raise ValueError("record must be a boolean.")
    if not isinstance(config.unlock_last, bool):
        raise ValueError("unlock_last must be a boolean.")
    if not config.upstream_api_key:
        raise ValueError("upstream_api_key must be non-empty.")
    if config.client_name_suffix_length < 1:
        raise ValueError("client_name_suffix_length must be >= 1.")

    parsed = urlparse(config.base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"base_url is invalid: {config.base_url!r}")

    seen_names: set[str] = set()
    seen_client_keys: set[str] = set()
    for index, client in enumerate(config.clients, start=1):
        if not client.name:
            raise ValueError(f"clients[{index}] must include a non-empty name.")
        if client.name in seen_names:
            raise ValueError(f"clients[{index}] name {client.name!r} is duplicated.")
        if not client.client_api_key:
            raise ValueError(f"clients[{index}] client_api_key must be non-empty.")
        if client.client_api_key in seen_client_keys:
            raise ValueError(f"clients[{index}] client_api_key is duplicated.")
        if client.limit < 0:
            raise ValueError(f"clients[{index}] limit must be >= 0.")
        if client.count < 0:
            raise ValueError(f"clients[{index}] count must be >= 0.")

        seen_names.add(client.name)
        seen_client_keys.add(client.client_api_key)


def add_new_client(config: ProxyConfig) -> ClientConfig:
    existing_names = {client.name for client in config.clients}
    existing_keys = {client.client_api_key for client in config.clients}
    client = ClientConfig(
        name=_generate_unique_client_name(existing_names, config.client_name_suffix_length),
        client_api_key=_generate_unique_client_api_key(existing_keys),
        limit=DEFAULT_CLIENT_LIMIT,
        count=0,
    )
    config.clients.append(client)
    validate_config(config)
    return client


def _generate_unique_client_name(existing_names: set[str], suffix_length: int) -> str:
    while True:
        candidate = f"{DEFAULT_CLIENT_NAME_PREFIX}{_generate_client_name_suffix(suffix_length)}"
        if candidate not in existing_names:
            return candidate


def _generate_client_name_suffix(length: int) -> str:
    return secrets.token_hex((length + 1) // 2)[:length]


def _generate_unique_client_api_key(existing_keys: set[str]) -> str:
    while True:
        candidate = f"{DEFAULT_CLIENT_API_KEY_PREFIX}{secrets.token_urlsafe(24)}"
        if candidate not in existing_keys:
            return candidate


def _load_legacy_client_scoped_config(payload: dict) -> ProxyConfig:
    client_payloads = payload["clients"]
    if not client_payloads:
        raise ValueError("At least one client must be configured.")

    base_urls = {item.get("base_url") for item in client_payloads}
    upstream_api_keys = {item.get("upstream_api_key") for item in client_payloads}
    if None in base_urls or None in upstream_api_keys:
        raise ValueError("Legacy client-scoped config is missing base_url or upstream_api_key.")
    if len(base_urls) != 1:
        raise ValueError(
            "Legacy client-scoped config contains multiple base_url values. "
            "Rewrite the config to use one global base_url."
        )
    if len(upstream_api_keys) != 1:
        raise ValueError(
            "Legacy client-scoped config contains multiple upstream_api_key values. "
            "Rewrite the config to use one global upstream_api_key."
        )

    config = ProxyConfig(
        listen_host=payload.get("listen_host", "0.0.0.0"),
        listen_port=payload.get("listen_port", DEFAULT_LISTEN_PORT),
        base_url=base_urls.pop(),
        upstream_api_key=upstream_api_keys.pop(),
        clients=[
            ClientConfig(
                name=item["name"],
                client_api_key=item["client_api_key"],
                limit=item["limit"],
                count=item.get("count", 0),
            )
            for item in client_payloads
        ],
        client_name_suffix_length=payload.get(
            "client_name_suffix_length",
            DEFAULT_CLIENT_NAME_SUFFIX_LENGTH,
        ),
        advertise_host=payload.get("advertise_host"),
        record=payload.get("record", False),
        unlock_last=payload.get("unlock_last", False),
    )
    validate_config(config)
    return config
