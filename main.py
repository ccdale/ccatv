import sys

import ccalogging
from ccaerrors import errorExit, errorNotify, errorRaise

import ccatv
from ccatv import __appname__, __version__

ccalogging.setDebug()
# ccalogging.setInfo()
ccalogging.setConsoleOut()
log = ccalogging.log


def main():
    try:
        log.info(f"{__appname__} v{__version__}")
    except Exception as e:
        errorExit(sys.exc_info()[2], e)


if __name__ == "__main__":
    main()
