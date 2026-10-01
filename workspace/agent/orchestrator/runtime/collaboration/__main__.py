"""Explicit local bootstrap and standalone HTTP service commands."""
from __future__ import annotations

import argparse
from getpass import getpass
from ipaddress import ip_address
from pathlib import Path
import sqlite3

from .app import AppConfig, create_app
from .auth import AuthService, Unauthorized, Forbidden, RateLimited
from .database import Database
from .instances import InstanceService
from .models import CollaborationError


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse otherwise echoes unknown arguments, which can contain a password.
        super().error("Invalid arguments or operation rejected")


def main(argv=None) -> int:
    parser = _Parser(description="Standalone collaboration service")
    commands = parser.add_subparsers(dest="command", required=True)
    bootstrap = commands.add_parser("bootstrap", help="Create the initial administrator locally")
    bootstrap.add_argument("--data-dir", type=Path, required=True)
    bootstrap.add_argument("--username", required=True)
    bootstrap.add_argument("--display-name", required=True)
    serve = commands.add_parser("serve", help="Serve the independent collaboration API")
    serve.add_argument("--data-dir", type=Path, required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=7400)
    serve.add_argument("--origin", default="http://127.0.0.1:7400")
    enroll = commands.add_parser("node-enroll", help="Register an execution node; prints its token once")
    enroll.add_argument("--data-dir", type=Path, required=True)
    enroll.add_argument("--name", required=True)
    disable = commands.add_parser("node-disable", help="Disable an execution node and its live instances")
    disable.add_argument("--data-dir", type=Path, required=True)
    disable.add_argument("--node-id", required=True)
    args = parser.parse_args(argv)

    if args.command == "bootstrap":
        password = getpass("Password: ")
        confirmation = getpass("Confirm password: ")
        if password != confirmation:
            parser.error("Passwords do not match")
        if not 12 <= len(password) <= 1024:
            parser.error("Password must contain 12 to 1024 characters")
        try:
            database = Database(args.data_dir / "metadata.sqlite3")
            database.initialize()
            user = AuthService(database).bootstrap(args.username, args.display_name, password)
        except (CollaborationError, Unauthorized, Forbidden, RateLimited, sqlite3.Error):
            parser.error("Bootstrap failed")
        print(user.user_id)
        return 0

    if args.command == "node-enroll":
        try:
            database = Database(args.data_dir / "metadata.sqlite3")
            database.initialize()
            node, token = InstanceService(database).enroll_node(args.name)
        except (CollaborationError, Unauthorized, Forbidden, RateLimited, sqlite3.Error):
            parser.error("Node operation failed")
        print(node.node_id)
        print(token)
        return 0

    if args.command == "node-disable":
        try:
            database = Database(args.data_dir / "metadata.sqlite3")
            database.initialize()
            node, _ = InstanceService(database).disable_node(args.node_id)
        except (CollaborationError, Unauthorized, Forbidden, RateLimited, sqlite3.Error):
            parser.error("Node operation failed")
        print(node.node_id)
        return 0

    if not 1 <= args.port <= 65535:
        parser.error("Port must be between 1 and 65535")
    try:
        config = AppConfig(args.data_dir, public_origin=args.origin)
    except ValueError:
        parser.error("Invalid public origin")
    if config.public_origin.startswith("http://"):
        try:
            local = ip_address(args.host).is_loopback
        except ValueError:
            local = args.host == "localhost"
        if not local:
            parser.error("Plain HTTP must bind to loopback")
    import uvicorn

    uvicorn.run(create_app(config), host=args.host, port=args.port, proxy_headers=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
