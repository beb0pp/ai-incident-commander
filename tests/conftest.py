"""Shared fixtures.

Everything here is offline: a scripted model and the bundled scenario. No
fixture reaches the network or a database.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from aic.domain.models import Incident, Signal
from aic.guardrails.policy import ActionPolicy
from aic.llm.fake import ScriptedLLMClient
from aic.orchestration.state import InvestigationState
from aic.scenario import demo_incident, demo_signals


@pytest.fixture
def policy() -> ActionPolicy:
    """The default policy: nothing above read-only is auto-approved."""
    return ActionPolicy()


@pytest.fixture
def scripted_llm() -> ScriptedLLMClient:
    """A bare scripted client. Register the handlers each test needs."""
    return ScriptedLLMClient()


@pytest.fixture
def incident() -> Incident:
    return demo_incident()


@pytest.fixture
def signals() -> list[Signal]:
    return demo_signals()


@pytest.fixture
def state(incident: Incident, signals: list[Signal]) -> InvestigationState:
    """A fresh investigation over the bundled scenario."""
    return InvestigationState(run_id="test-run", incident=incident, signals=signals)


@pytest.fixture(autouse=True)
def _quiet_logging() -> Iterator[None]:
    """Keep test output readable; log configuration is exercised separately."""
    import logging

    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)
