"""Shared fixtures.

Everything here is offline. No fixture reaches the network or a database.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from aic.guardrails.policy import ActionPolicy


@pytest.fixture
def policy() -> ActionPolicy:
    """The default policy: nothing above read-only is auto-approved."""
    return ActionPolicy()


@pytest.fixture(autouse=True)
def _quiet_logging() -> Iterator[None]:
    """Keep test output readable; log configuration is exercised separately."""
    import logging

    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)
