import pytest

from reelpilot import cli
from reelpilot.cli import build_parser


def test_default_cli_values() -> None:
    args = build_parser().parse_args([])
    assert args.controller_profile == "auto"
    assert not args.manual_cast
    assert not args.no_auto_hook
    assert args.rod_slot == 1
    assert args.food_slot == 2
    assert not args.no_auto_eat


def test_modes_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--manual-cast", "--no-auto-hook"])


def test_same_reserved_slot_is_rejected_when_auto_eat_is_enabled() -> None:
    with pytest.raises(SystemExit):
        cli.main(["--rod-slot", "4", "--food-slot", "4"])


def test_same_reserved_slot_is_allowed_when_auto_eat_is_disabled(monkeypatch) -> None:
    captured = None

    class Application:
        def __init__(self, settings) -> None:
            nonlocal captured
            captured = settings

        def run(self) -> int:
            return 0

    monkeypatch.setattr(cli, "ReelPilotApplication", Application)

    assert cli.main(["--rod-slot", "4", "--food-slot", "4", "--no-auto-eat"]) == 0
    assert captured is not None
    assert not captured.auto_eat
