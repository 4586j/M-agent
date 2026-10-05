"""Explicit local bootstrap, standalone HTTP service and scheduler host commands."""
from __future__ import annotations

import argparse
from getpass import getpass
import httpx
from ipaddress import ip_address
import json
from pathlib import Path
import sqlite3

from .app import AppConfig, _wake, create_app
from .auth import AuthService, Unauthorized, Forbidden, RateLimited
from .backup import snapshot
from .budgets import BudgetService
from .database import Database
from .dispatch import DispatchService
from .host import SchedulerHost
from .instances import InstanceService
from .models import CollaborationError
from .planning import PlanService
from .reviews import ReviewService
from .scheduler import QueueService, Scheduler


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
    backup = commands.add_parser("backup",
                                 help="Write a verified snapshot of the collaboration database")
    backup.add_argument("--data-dir", type=Path, required=True)
    backup.add_argument("--out", type=Path, required=True)
    scheduler = commands.add_parser("scheduler",
                                    help="Keep running scheduling rounds until stopped (Ctrl-C)")
    scheduler.add_argument("--data-dir", type=Path, required=True)
    scheduler.add_argument("--interval", type=float, default=5.0)
    scheduler.add_argument("--iterations", type=int, default=None)
    scheduler.add_argument("--project", default=None)
    scheduler.add_argument("--auto-dispatch", action="store_true")
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

    if args.command == "backup":
        # The manifest is the part an operator reads years later, so it is printed in full and
        # carries no content: counts and digests only, never a requirement text or a name.
        try:
            report = snapshot(args.data_dir / "metadata.sqlite3", args.out)
        except (CollaborationError, Unauthorized, Forbidden, RateLimited, sqlite3.Error, OSError):
            parser.error("Backup failed")
        print(json.dumps(report, sort_keys=True))
        return 0

    if args.command == "scheduler":
        if not 0.1 <= args.interval <= 3600:
            parser.error("Interval must be between 0.1 and 3600 seconds")
        if args.iterations is not None and args.iterations < 1:
            parser.error("Iterations must be at least 1")
        # Same services and the same wake transport the HTTP entry assembles, so the host cannot
        # hand out a lease the serving process would not honour.
        config = AppConfig(args.data_dir)
        waker = httpx.Client(timeout=httpx.Timeout(10.0, connect=3.0), trust_env=False)
        try:
            database = Database(config.data_dir / "metadata.sqlite3")
            database.initialize()
            instances = InstanceService(database)
            budgets = BudgetService()
            host = SchedulerHost(
                Scheduler(database, planning=PlanService(database, budgets=budgets),
                          queue=QueueService(database,
                                             heartbeat_timeout=instances.heartbeat_timeout),
                          dispatch=DispatchService(database, instances=instances,
                                                   lease_ttl=config.lease_ttl,
                                                   push=lambda url, payload: _wake(
                                                       waker, url, payload)),
                          auto_dispatch=args.auto_dispatch),
                database, reviews=ReviewService(budgets=budgets))
            print(host.run(interval=args.interval, iterations=args.iterations,
                           project_id=args.project))
        except (CollaborationError, Unauthorized, Forbidden, RateLimited, sqlite3.Error):
            parser.error("Scheduler host failed")
        except KeyboardInterrupt:
            # Ctrl-C is how this command is meant to stop, so a clean exit is not a failure.
            pass
        finally:
            waker.close()
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
