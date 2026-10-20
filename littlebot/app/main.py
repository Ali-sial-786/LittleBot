"""LittleBot command-line entry point.

    python -m littlebot                       # desktop simulation (default)
    python -m littlebot --mode offline        # offline demo, no keys / network / devices
    python -m littlebot --mode pi             # Raspberry Pi with real USB devices
    python -m littlebot --headless            # no window (dashboard only)
    python -m littlebot --fullscreen --scale 1
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from typing import Any, Sequence

from littlebot import __version__


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="littlebot", description="LittleBot companion robot")
    p.add_argument("--mode", choices=["simulation", "pi", "offline"], default=None,
                   help="operating mode (default: LITTLEBOT_MODE env or 'simulation')")
    p.add_argument("--config", help="extra YAML config file to layer on top")
    p.add_argument("--headless", action="store_true", help="run without a window")
    p.add_argument("--fullscreen", action="store_true", help="fullscreen face window")
    p.add_argument("--scale", type=float, help="window scale factor (desktop)")
    p.add_argument("--no-dashboard", action="store_true", help="do not start the parent dashboard")
    p.add_argument("--port", type=int, help="dashboard port")
    p.add_argument("--lan", action="store_true", help="allow dashboard access from the local network (opt-in)")
    p.add_argument("--data-dir", help="where to keep the settings database and logs")
    p.add_argument("--debug", action="store_true", help="debug logging + face debug overlay")
    p.add_argument("--demo", action="store_true", help="loop the face demo animation sequence")
    p.add_argument("--run-for", type=float, default=None, help="exit after N seconds (smoke tests)")
    p.add_argument("--version", action="version", version=f"LittleBot {__version__}")
    return p


def overrides_from_args(args: argparse.Namespace) -> dict[str, Any]:
    o: dict[str, Any] = {}
    display: dict[str, Any] = {}
    if args.headless:
        display["headless"] = True
    if args.fullscreen:
        display["fullscreen"] = True
    if args.scale:
        display["scale"] = args.scale
    if args.debug:
        display["show_debug"] = True
        o["logging"] = {"level": "DEBUG"}
    if display:
        o["display"] = display
    dash: dict[str, Any] = {}
    if args.no_dashboard:
        dash["enabled"] = False
    if args.port:
        dash["port"] = args.port
    if args.lan:
        dash["lan_access"] = True
    if dash:
        o["dashboard"] = dash
    if args.data_dir:
        o["data_dir"] = args.data_dir
    return o


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # The database (password hash, sessions, child profile) and logs are private to this user.
    os.umask(0o077)

    from littlebot.core.config import load_config
    from littlebot.core.logging_setup import setup_logging

    cfg = load_config(mode=args.mode, config_file=args.config, overrides=overrides_from_args(args))
    if cfg.display.headless:
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    setup_logging(cfg.logging, cfg.data_path)
    log = logging.getLogger("littlebot")
    log.info("LittleBot %s starting (mode=%s, python=%s)", __version__, cfg.mode.value, sys.version.split()[0])

    from littlebot.app.robot import LittleBot

    try:
        robot = LittleBot.create(cfg)
    except Exception:  # noqa: BLE001
        log.exception("could not build LittleBot")
        return 2

    def _signal(signum: int, _frame: Any) -> None:
        robot.request_shutdown(f"signal {signum}")

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _signal)
        except (ValueError, OSError):  # not on main thread / unsupported
            pass

    if args.run_for:
        threading.Timer(args.run_for, robot.request_shutdown, args=("--run-for elapsed",)).start()

    exit_code = 0
    try:
        robot.start()
        if cfg.display.headless:
            robot.shutdown_event.wait()
        else:
            from littlebot.face.display import run_display

            run_display(robot, demo=args.demo)  # blocks on the main thread until quit
    except KeyboardInterrupt:
        pass
    except Exception:  # noqa: BLE001
        log.exception("fatal error")
        exit_code = 1
    finally:
        robot.shutdown("exit")
    if exit_code == 0 and robot.restart_requested:
        from littlebot.app.robot import RESTART_EXIT_CODE

        log.info("exiting with code %d so the service manager restarts LittleBot", RESTART_EXIT_CODE)
        return RESTART_EXIT_CODE
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
