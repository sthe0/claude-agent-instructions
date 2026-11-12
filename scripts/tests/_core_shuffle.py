"""``--core-shuffle-seed=N``: collect the tests in a seeded random order.

Difficulty removed: a test that shares state with another (a cache, a fixed temp path, a port,
an environment variable left set) passes whenever the order happens to suit it, so one run in the
usual order proves nothing about isolation. Under xdist the shuffled list is what is distributed
to the workers, so a seed changes both the order and which worker runs which test. Every worker
shuffles the same collected list with the same seed, so they still agree on the ids. Without the
option the collection order is untouched, and the suite gate never passes it: the option is for the
isolation check (``gate_speedup_control.py shuffle-pair``) and for developers.

Registered from conftest.py; ``pytest -p _core_shuffle`` loads it where that conftest does not apply.
"""
from __future__ import annotations

import random
from typing import Sequence

import pytest

SEED_OPTION = "--core-shuffle-seed"


def pytest_addoption(parser):
    parser.addoption(SEED_OPTION, action="store", type=int, default=None, metavar="N",
                     help="collect the tests in the order random.Random(N) gives, to expose shared state")


def shuffled(items: Sequence, seed: int) -> list:
    """``items`` in the order ``random.Random(seed)`` gives; the input is not changed."""
    order = list(items)
    random.Random(seed).shuffle(order)
    return order


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    seed = config.getoption(SEED_OPTION)
    if seed is not None:
        items[:] = shuffled(items, seed)
