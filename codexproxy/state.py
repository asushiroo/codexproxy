from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from codexproxy.config import ProxyConfig, add_new_client, load_config, save_config


class ClientApiKeyNotConfiguredError(Exception):
    """Raised when a request arrives with a client API key that is not configured."""


class ClientNameNotConfiguredError(Exception):
    """Raised when a reset references a client name that is not configured."""


class RequestLimitReachedError(Exception):
    """Raised when a configured client reaches its request limit."""

    def __init__(self, client_name: str, limit: int, count: int) -> None:
        super().__init__(f"Client {client_name} reached its request limit ({limit}).")
        self.client_name = client_name
        self.limit = limit
        self.count = count


@dataclass(frozen=True, slots=True)
class ClientBinding:
    name: str
    base_url: str
    upstream_api_key: str
    limit: int
    count: int
    client_api_key: str | None = None


class ConfigStore:
    def __init__(self, path: Path, config: ProxyConfig) -> None:
        self._path = path
        self._config = config
        self._lock = Lock()

    @classmethod
    def from_path(cls, path: Path) -> "ConfigStore":
        return cls(path, load_config(path))

    @property
    def listen_host(self) -> str:
        return self._config.listen_host

    @property
    def listen_port(self) -> int:
        return self._config.listen_port

    @property
    def base_url(self) -> str:
        return self._config.base_url

    @property
    def advertise_host(self) -> str | None:
        return self._config.advertise_host

    @property
    def record(self) -> bool:
        return self._config.record

    def list_clients(self) -> list[ClientBinding]:
        with self._lock:
            return [self._build_binding(client) for client in self._config.clients]

    def reserve_request(self, client_api_key: str) -> ClientBinding:
        with self._lock:
            index = self._client_api_key_to_index(client_api_key)
            client = self._config.clients[index]
            if client.count >= client.limit:
                raise RequestLimitReachedError(
                    client_name=client.name,
                    limit=client.limit,
                    count=client.count,
                )

            client.count += 1
            save_config(self._path, self._config)
            return self._build_binding(client)

    def add_new_client(self) -> ClientBinding:
        with self._lock:
            client = add_new_client(self._config)
            save_config(self._path, self._config)
            return self._build_binding(client, include_client_api_key=True)

    def reset_client(self, client_name: str) -> ClientBinding:
        with self._lock:
            index = self._client_name_to_index(client_name)
            client = self._config.clients[index]
            client.count = 0
            save_config(self._path, self._config)
            return self._build_binding(client)

    def reset_all(self) -> list[ClientBinding]:
        with self._lock:
            bindings: list[ClientBinding] = []
            for client in self._config.clients:
                client.count = 0
                bindings.append(self._build_binding(client))
            save_config(self._path, self._config)
            return bindings

    def _build_binding(self, client, *, include_client_api_key: bool = False) -> ClientBinding:
        return ClientBinding(
            name=client.name,
            base_url=self._config.base_url,
            upstream_api_key=self._config.upstream_api_key,
            limit=client.limit,
            count=client.count,
            client_api_key=client.client_api_key if include_client_api_key else None,
        )

    def _client_api_key_to_index(self, client_api_key: str) -> int:
        for index, client in enumerate(self._config.clients):
            if client.client_api_key == client_api_key:
                return index
        raise ClientApiKeyNotConfiguredError("Client API key is not configured.")

    def _client_name_to_index(self, client_name: str) -> int:
        for index, client in enumerate(self._config.clients):
            if client.name == client_name:
                return index
        raise ClientNameNotConfiguredError(f"Client {client_name!r} is not configured.")
