from ccatv import __appname__, __version__


def test_ccatv():
    assert __appname__ == "ccatv"


def test_version():
    assert __version__ == "0.1.1"
