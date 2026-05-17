from __future__ import annotations

import functools
import logging
import sys
import traceback
from collections.abc import Callable
from typing import Any, TypeVar

LOG = logging.getLogger(__name__)
F = TypeVar("F", bound=Callable[..., Any])


def error_notify(exci: object, exc: Exception, fname: str | None = None) -> None:
    """Emit a contextual error message and traceback without changing control flow."""
    tb = exci if exci is not None else exc.__traceback__
    if tb is not None:
        lineno = tb.tb_lineno
        if fname is None:
            fname = tb.tb_frame.f_code.co_name
    else:
        lineno = -1
        fname = fname or "<unknown>"

    msg = f"{type(exc).__name__} at line {lineno} in function {fname}: {exc}"
    LOG.error(msg)
    traceback.print_exception(type(exc), exc, tb)


def error_raise(exci: object, exc: Exception, fname: str | None = None) -> None:
    """Notify and re-raise the current exception preserving traceback."""
    error_notify(exci, exc, fname)
    raise


def error_exit(exci: object, exc: Exception, fname: str | None = None) -> None:
    """Notify and terminate process with a non-zero exit code."""
    error_notify(exci, exc, fname)
    raise SystemExit(1)


def _decorate_with(
    handler: Callable[[object, Exception, str | None], None], func: F
) -> F:
    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            handler(sys.exc_info()[2], exc, func.__name__)
            return None

    return wrapper  # type: ignore[return-value]


def on_error_notify(func: F) -> F:
    """Decorator for boundary handlers that should notify and continue."""
    return _decorate_with(error_notify, func)


def on_error_raise(func: F) -> F:
    """Decorator for boundary handlers that should notify and re-raise."""
    return _decorate_with(error_raise, func)


def on_error_exit(func: F) -> F:
    """Decorator for top-level handlers that should notify and exit."""
    return _decorate_with(error_exit, func)


# Compatibility aliases for legacy call sites.
errorNotify = error_notify
errorRaise = error_raise
errorExit = error_exit
onErrorNotify = on_error_notify
onErrorRaise = on_error_raise
onErrorExit = on_error_exit

__all__ = [
    "errorExit",
    "errorNotify",
    "errorRaise",
    "error_exit",
    "error_notify",
    "error_raise",
    "onErrorExit",
    "onErrorNotify",
    "onErrorRaise",
    "on_error_exit",
    "on_error_notify",
    "on_error_raise",
]
