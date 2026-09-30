"""Account safety lock. No runtime, timer, login or environment unlock exists.

Removing this policy requires a reviewed change and a separately approved plan.
Offline tests may patch this function only with a socket-level egress blocker.
"""
from .validation import AdapterFailure

SAFETY_REASON = "account_safety_user_instruction_2026_09_30"


def xhs_network_paused() -> bool:
    return True


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
