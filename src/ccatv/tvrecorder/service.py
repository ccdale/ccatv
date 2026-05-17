from __future__ import annotations

import shlex

from ccatv.tvrecorder.dvbctrl import DvbCtrlClient, DvbCtrlResult


class TvRecorderService:
    """Thin service facade over DvbCtrlClient command execution."""

    def __init__(self, dvbctrl: DvbCtrlClient) -> None:
        self._dvbctrl = dvbctrl

    def run_raw(self, command: str) -> DvbCtrlResult:
        """Run a raw dvbctrl command string."""
        return self._dvbctrl.run_command(command)

    def select_service(self, service_name: str) -> DvbCtrlResult:
        """Select a primary service by name."""
        quoted = shlex.quote(service_name)
        return self._dvbctrl.run_command(f"select {quoted}")

    def current(self) -> DvbCtrlResult:
        """Return currently selected service output."""
        return self._dvbctrl.run_command("current")

    def stats(self) -> DvbCtrlResult:
        """Return current dvbstreamer statistics output."""
        return self._dvbctrl.run_command("stats")
