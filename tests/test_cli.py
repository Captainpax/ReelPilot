import pytest

from reelpilot.cli import build_parser


def test_default_cli_values() -> None:
    args = build_parser().parse_args([])
    assert args.controller_profile == "auto"
    assert not args.manual_cast
    assert not args.no_auto_hook


def test_modes_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--manual-cast", "--no-auto-hook"])
