from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from .app import ReelPilotApplication
from .domain import AutomationMode, ControllerProfile, ReelPilotSettings
from .recording import ReplayRunner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reelpilot",
        description="Safely automate Stardew Valley fishing on Windows.",
    )
    parser.add_argument("--version", action="version", version="ReelPilot 0.1.0-beta.1")
    parser.add_argument("--fishing-level", type=int, choices=range(0, 11), metavar="0..10")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--manual-cast",
        action="store_true",
        help="you cast; ReelPilot hooks and controls the minigame",
    )
    mode.add_argument(
        "--no-auto-hook",
        action="store_true",
        help="control the fishing minigame only",
    )
    parser.add_argument(
        "--cast-hold-seconds",
        type=float,
        default=None,
        metavar="SECONDS",
        help="visual-meter fallback duration from 0.1 to 2.0 seconds",
    )
    parser.add_argument(
        "--controller-profile",
        choices=[profile.value for profile in ControllerProfile],
        default=ControllerProfile.AUTO.value,
    )
    parser.add_argument("--record-session", type=Path, metavar="PATH")
    parser.add_argument("--replay-session", type=Path, metavar="PATH")
    parser.add_argument("--no-stats", action="store_true")
    parser.add_argument("--stats-dir", type=Path, metavar="PATH")
    parser.add_argument("--plain", action="store_true", help="disable the live dashboard")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.replay_session is not None:
        report = ReplayRunner().run(
            args.replay_session,
            ControllerProfile(args.controller_profile),
        )
        print(json.dumps(report, indent=2))
        return 0
    if args.no_auto_hook:
        mode = AutomationMode.MINIGAME_ONLY
    elif args.manual_cast:
        mode = AutomationMode.HOOK_ONLY
    else:
        mode = AutomationMode.CONTINUOUS
    if args.stats_dir is not None and args.no_stats:
        parser.error("--stats-dir cannot be combined with --no-stats")
    if args.cast_hold_seconds is not None and mode is not AutomationMode.CONTINUOUS:
        parser.error("--cast-hold-seconds is only valid in continuous mode")
    cast_hold_seconds = args.cast_hold_seconds if args.cast_hold_seconds is not None else 1.5
    if not 0.1 <= cast_hold_seconds <= 2.0:
        parser.error("--cast-hold-seconds must be between 0.1 and 2.0")
    settings = ReelPilotSettings(
        mode,
        ControllerProfile(args.controller_profile),
        args.fishing_level,
        cast_hold_seconds,
        args.plain,
        not args.no_stats,
        args.stats_dir,
        args.record_session,
    )
    return ReelPilotApplication(settings).run()
