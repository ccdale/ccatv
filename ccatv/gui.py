import sys

import ccalogging
from ccaerrors import errorExit, errorNotify, errorRaise

log = ccalogging.log


def fullScreenWindow():
    try:
        pass
    except Exception as e:
        errorRaise(sys.exc_info()[2], e)
