from aic.tools.base import NoArgs, Tool
from aic.tools.client import (
    InfrastructureClient,
    SourceUnavailableError,
    UnknownResourceError,
)
from aic.tools.environment import SimulatedEnvironment, demo_environment
from aic.tools.inspection import build_inspection_tools
from aic.tools.registry import ToolRegistry
from aic.tools.simulated import SimulatedInfrastructure

__all__ = [
    "InfrastructureClient",
    "NoArgs",
    "SimulatedEnvironment",
    "SimulatedInfrastructure",
    "SourceUnavailableError",
    "Tool",
    "ToolRegistry",
    "UnknownResourceError",
    "build_inspection_tools",
    "demo_environment",
]

# NOTE: ``aic.tools.aws`` is intentionally NOT re-exported. It imports boto3,
# which is an optional extra — importing this package must not require it.
