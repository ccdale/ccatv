from __future__ import annotations

from ccatv.app.bootstrap import bootstrap_app
from ccatv.settings import AppSettings


def test_bootstrap_threads_dvbctrl_credentials(monkeypatch) -> None:
    monkeypatch.setattr(
        AppSettings,
        "from_env",
        classmethod(
            lambda cls: AppSettings(
                dvbctrl_password="secret",
                dvbctrl_username="alice",
            )
        ),
    )

    context = bootstrap_app()

    assert context.dvbctrl.username == "alice"
    assert context.dvbctrl.password == "secret"
