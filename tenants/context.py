"""The club the current request (or background job) is working for.

Set by TenantMiddleware for web requests and by `use_club()` in jobs and tests. Club-owned
models read it to scope every query, so one club can never see another club's data.
"""
from contextlib import contextmanager
from contextvars import ContextVar

_current_club = ContextVar("current_club", default=None)


def get_current_club():
    return _current_club.get()


def set_current_club(club):
    return _current_club.set(club)


def reset_current_club(token):
    _current_club.reset(token)


@contextmanager
def use_club(club):
    token = _current_club.set(club)
    try:
        yield club
    finally:
        _current_club.reset(token)
