"""Normal manual HTTP access; browser capture remains disabled."""
from .validation import AdapterFailure

SAFETY_REASON = "account_safety_user_instruction_2026_09_30"


def xhs_network_paused() -> bool:
    return False


def require_xhs_network():
    """HTTP access is controlled by the normal user operation and source health."""


def require_browser_disabled():
    raise AdapterFailure("browser_automation_disabled")
