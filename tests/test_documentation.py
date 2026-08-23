import doctest

from reelpilot import domain
from reelpilot.control import motion
from reelpilot.stats import models


def test_safe_code_examples() -> None:
    for module in (domain, motion, models):
        result = doctest.testmod(module, raise_on_error=False)
        assert result.failed == 0
