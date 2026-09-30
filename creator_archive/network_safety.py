"""Default account pause; a bounded explicit local action may own one scope.

There is no global, environment, timer or browser unlock. Offline tests always
install socket interception before exercising scoped permissions.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from .validation import AdapterFailure, Page

SAFETY_REASON = "account_safety_user_instruction_2026_09_30"
_MANUAL_SCOPE = ContextVar("xhs_manual_validation", default=None)


def xhs_network_paused() -> bool:
    scope = _MANUAL_SCOPE.get()
    return scope is None or scope.stopped


def manual_scope():
    return _MANUAL_SCOPE.get()


@contextmanager
def manual_action(scope):
    token = _MANUAL_SCOPE.set(scope)
    try:
        yield
    finally:
        _MANUAL_SCOPE.reset(token)


def require_manual_author(author_id):
    scope = manual_scope()
    if scope is not None and scope.author_id != author_id:
        scope.stop("identity_mismatch")
        raise AdapterFailure("identity_mismatch")


def consume_manual_request(host, path):
    scope = manual_scope()
    if scope is not None:
        scope.consume(host, path)


def observe_manual_window(author_id, item_ids):
    require_manual_author(author_id)
    scope = manual_scope()
    if scope is not None:
        scope.observe(item_ids)


def bound_manual_page(author_id, page):
    require_manual_author(author_id)
    scope = manual_scope()
    if scope is None:
        return page
    if scope.counts["list"] != 1 or not page.items or len(page.items) > 30:
        raise AdapterFailure("invalid_page")
    selected = tuple(page.items[:3])
    if any(item.author_id != author_id for item in selected) or len({item.item_id for item in selected}) != len(selected):
        raise AdapterFailure("identity_mismatch")
    scope.observe(item.item_id for item in selected)
    scope.page = Page(selected, None, False, None)
    return scope.page


def require_xhs_network():
    if xhs_network_paused():
        raise AdapterFailure("network_paused")


def require_browser_disabled():
    # Browser automation remains forbidden even in an approved HTTP test scope.
    raise AdapterFailure("network_paused")


def require_paused_local_runtime():
    """Fail closed before launching local pages if either safety guard is absent."""
    if xhs_network_paused() is not True:
        raise RuntimeError("XHS network policy must remain paused")
    for guard in (require_xhs_network, require_browser_disabled):
        try:
            guard()
        except AdapterFailure as error:
            if error.category != "network_paused":
                raise RuntimeError("Unexpected local runtime safety policy") from error
        else:
            raise RuntimeError("Local runtime safety guard is not blocking")
