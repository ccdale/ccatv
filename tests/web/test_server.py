from __future__ import annotations

import pytest

from ccatv.web.server import main


def test_main_requires_service_auth_token() -> None:
    with pytest.raises(SystemExit):
        main([
            "--listen-host",
            "127.0.0.1",
            "--listen-port",
            "5001",
            "--service-host",
            "127.0.0.1",
            "--service-port",
            "8787",
        ])
