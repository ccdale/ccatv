from __future__ import annotations

import argparse
import logging
import os

from ccatv import __version__
from ccatv.web.app import create_app

logger = logging.getLogger("ccatv.web")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ccatv-web",
        description=(
            "Run the ccatv Flask web app as a remote desktop frontend for "
            "scheduling and status workflows."
        ),
    )
    parser.add_argument(
        "--listen-host",
        default="127.0.0.1",
        help=(
            "Flask bind host for the web app "
            "(use 0.0.0.0 to allow LAN access)"
        ),
    )
    parser.add_argument(
        "--listen-port",
        type=int,
        default=5000,
        help="Flask bind port for the web app",
    )
    parser.add_argument(
        "--service-host",
        default="127.0.0.1",
        help="Host where ccatv-service HTTP transport is listening",
    )
    parser.add_argument(
        "--service-port",
        type=int,
        default=8787,
        help="Port where ccatv-service HTTP transport is listening",
    )
    parser.add_argument(
        "--service-auth-token",
        default=None,
        help=(
            "Bearer token for ccatv-service HTTP transport. If omitted, uses "
            "CCATV_SERVICE_AUTH_TOKEN from environment."
        ),
    )
    parser.add_argument(
        "--web-auth-token",
        default=None,
        help=(
            "Optional bearer token required by this Flask app for /api/* routes. "
            "If omitted, uses CCATV_WEB_AUTH_TOKEN from environment."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.listen_port < 1 or args.listen_port > 65535:
        parser.error("--listen-port must be in range 1..65535")
    if args.service_port < 1 or args.service_port > 65535:
        parser.error("--service-port must be in range 1..65535")

    service_auth_token = args.service_auth_token or os.getenv("CCATV_SERVICE_AUTH_TOKEN")
    if not service_auth_token:
        parser.error(
            "--service-auth-token is required (or set CCATV_SERVICE_AUTH_TOKEN)"
        )

    web_auth_token = args.web_auth_token or os.getenv("CCATV_WEB_AUTH_TOKEN")

    app = create_app(
        service_host=args.service_host,
        service_port=args.service_port,
        service_auth_token=service_auth_token,
        web_auth_token=web_auth_token,
    )
    logger.info(
        "ccatv-web starting (version=%s, listen=%s:%d, service=%s:%d)",
        __version__,
        args.listen_host,
        args.listen_port,
        args.service_host,
        args.service_port,
    )
    app.run(host=args.listen_host, port=args.listen_port, debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
