import pytest

import main
from ccatv import __appname__, __version__


def test_main(caplog):
    main.main()
    assert f"{__appname__} v{__version__}" in caplog.text
