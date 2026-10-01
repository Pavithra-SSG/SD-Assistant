"""Spec §11 scenarios 1–14 (one test each)."""
import pytest

from .scenarios import SCENARIOS


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.__name__ for s in SCENARIOS])
def test_scenario(h, scenario):
    print(scenario(h))
