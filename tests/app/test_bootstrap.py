from __future__ import annotations

from ccatv.app.bootstrap import bootstrap_app
from ccatv.settings import AppSettings


def test_bootstrap_uses_dvbctrl_without_inline_credentials(monkeypatch) -> None:
    monkeypatch.setattr(
        AppSettings,
        "from_env",
        classmethod(
            lambda cls: AppSettings(
                dvbctrl_path="dvbctrl",
            )
        ),
    )

    context = bootstrap_app()

    assert context.dvbctrl.executable_path == "dvbctrl"
