"""TV recorder integrations and process control adapters."""

from ccatv.tvrecorder.orchestrator import (
    CaptureController,
    DvbCtrlCaptureController,
    NoOpCaptureController,
    OrchestratorResult,
    PeriodicCheckPolicy,
    RecorderOrchestrator,
    SchedulerWorker,
    build_recording_output_path,
)
from ccatv.tvrecorder.preflight import (
    WritePreflightChecker,
    WritePreflightError,
    WritePreflightResult,
)

__all__ = [
    "DvbCtrlCaptureController",
    "CaptureController",
    "NoOpCaptureController",
    "OrchestratorResult",
    "PeriodicCheckPolicy",
    "RecorderOrchestrator",
    "SchedulerWorker",
    "build_recording_output_path",
    "WritePreflightChecker",
    "WritePreflightError",
    "WritePreflightResult",
]
