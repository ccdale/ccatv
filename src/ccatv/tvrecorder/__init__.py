"""TV recorder integrations and process control adapters."""

from ccatv.tvrecorder.orchestrator import (
    CaptureController,
    NoOpCaptureController,
    OrchestratorResult,
    PeriodicCheckPolicy,
    RecorderOrchestrator,
)
from ccatv.tvrecorder.preflight import (
    WritePreflightChecker,
    WritePreflightError,
    WritePreflightResult,
)

__all__ = [
    "CaptureController",
    "NoOpCaptureController",
    "OrchestratorResult",
    "PeriodicCheckPolicy",
    "RecorderOrchestrator",
    "WritePreflightChecker",
    "WritePreflightError",
    "WritePreflightResult",
]
