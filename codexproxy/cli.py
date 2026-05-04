from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from codexproxy.config import (
    DEFAULT_CLIENT_COUNT,
    DEFAULT_CLIENT_NAME_SUFFIX_LENGTH,
    DEFAULT_CONFIG_PATH,
    DEFAULT_LISTEN_PORT,
    build_default_config,
    save_config,
)
from codexproxy.proxy import run_proxy
from codexproxy.state import ClientNameNotConfiguredError, ConfigStore


def _add_config_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to the proxy config file. Default: {DEFAULT_CONFIG_PATH}",
    )


def _add_expire_time_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--expire-time",
        help='Expire time for today, format: "YYYY/M/D HH:MM:SS". Required on first startup when no cache exists.',
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Single-port reverse proxy with shared upstream config.")
    _add_config_argument(parser)
    _add_expire_time_argument(parser)

    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Run the proxy server.")
    _add_config_argument(run_parser)
    _add_expire_time_argument(run_parser)

    reset_parser = subparsers.add_parser("reset", help="Reset request counts.")
    _add_config_argument(reset_parser)
    reset_target = reset_parser.add_mutually_exclusive_group(required=True)
    reset_target.add_argument("--client", help="Reset one configured client by name.")
    reset_target.add_argument("--all", action="store_true", help="Reset all configured clients.")

    init_parser = subparsers.add_parser("init-config", help="Create a starter config file.")
    _add_config_argument(init_parser)
    init_parser.add_argument(
        "--base-url",
        required=True,
        help="Shared upstream base URL used by every generated client.",
    )
    init_parser.add_argument(
        "--upstream-api-key",
        required=True,
        help="Shared upstream API key used by every generated client.",
    )
    init_parser.add_argument(
        "--client-count",
        type=int,
        default=DEFAULT_CLIENT_COUNT,
        help=f"How many client entries to create. Default: {DEFAULT_CLIENT_COUNT}",
    )
    init_parser.add_argument(
        "--client-name-suffix-length",
        type=int,
        default=DEFAULT_CLIENT_NAME_SUFFIX_LENGTH,
        help=(
            "How many random characters to generate after the client- prefix. "
            f"Default: {DEFAULT_CLIENT_NAME_SUFFIX_LENGTH}"
        ),
    )
    init_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the target config file if it already exists.",
    )
    init_parser.add_argument(
        "--unlock-last",
        action="store_true",
        help="Disable all client limits during the last hour before expire-time.",
    )

    new_client_parser = subparsers.add_parser("new-client", help="Append one generated client to the config.")
    _add_config_argument(new_client_parser)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    command = args.command or "run"

    if command == "run":
        _run_command(args.config, expire_time=args.expire_time)
        return

    if command == "reset":
        _reset_command(config_path=args.config, client_name=args.client, reset_all=args.all)
        return

    if command == "init-config":
        _init_config_command(
            config_path=args.config,
            base_url=args.base_url,
            upstream_api_key=args.upstream_api_key,
            client_count=args.client_count,
            client_name_suffix_length=args.client_name_suffix_length,
            force=args.force,
            unlock_last=args.unlock_last,
        )
        return

    if command == "new-client":
        _new_client_command(args.config)
        return

    parser.error(f"Unknown command: {command}")


def _run_command(config_path: Path, *, expire_time: str | None) -> None:
    try:
        asyncio.run(run_proxy(config_path, expire_time=expire_time))
    except KeyboardInterrupt:
        print("Proxy stopped.")


def _reset_command(config_path: Path, *, client_name: str | None, reset_all: bool) -> None:
    store = ConfigStore.from_path(config_path)
    if reset_all:
        bindings = store.reset_all()
        print(f"Reset {len(bindings)} configured clients.")
        return

    if client_name is None:
        raise ValueError("client_name must be provided when --all is not used.")

    try:
        binding = store.reset_client(client_name)
    except ClientNameNotConfiguredError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Reset client {binding.name} to count={binding.count}.")


def _init_config_command(
    config_path: Path,
    *,
    base_url: str,
    upstream_api_key: str,
    client_count: int,
    client_name_suffix_length: int,
    force: bool,
    unlock_last: bool,
) -> None:
    if config_path.exists() and not force:
        raise SystemExit(f"{config_path} already exists. Use --force to overwrite it.")
    if client_count < 0:
        raise SystemExit("client-count must be >= 0.")
    if client_name_suffix_length < 1:
        raise SystemExit("client-name-suffix-length must be >= 1.")

    config = build_default_config(
        base_url,
        upstream_api_key,
        client_count=client_count,
        listen_port=DEFAULT_LISTEN_PORT,
        client_name_suffix_length=client_name_suffix_length,
        unlock_last=unlock_last,
    )
    save_config(config_path, config)
    print(
        f"Created {config_path} with listen_port={DEFAULT_LISTEN_PORT} "
        f"and client_count={client_count}."
    )


def _new_client_command(config_path: Path) -> None:
    store = ConfigStore.from_path(config_path)
    binding = store.add_new_client()
    print(
        f"Added client {binding.name} with client_api_key={binding.client_api_key} "
        f"limit={binding.limit} count={binding.count}."
    )
