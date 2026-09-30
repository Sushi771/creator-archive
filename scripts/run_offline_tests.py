"""Run synthetic unittest cases with process-wide, loopback-only network access.

This harness never authorises platform traffic. Legacy cases exercise fake
transports with the application policy patched only for that case; safety cases
retain the production lock. A Python child inherits the same socket guard via
an ephemeral sitecustomize. Uninstrumented shell/browser children are refused.
"""
from __future__ import annotations

from contextlib import ExitStack
import ipaddress
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
_ACTIVE_GUARDS = []
_AUDIT_INSTALLED = False
_CHILD_GUARD = None
_DENIED_TOTAL = 0


def _loopback(host):
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="replace")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except (ValueError, TypeError):
        return False


class OfflineEgressBlocked(OSError):
    pass


class EgressGuard:
    """Block outbound TCP, UDP and DNS, including unrelated downloaders."""

    def __init__(self):
        self.stack = ExitStack()
        self.denied = []

    def check_host(self, host, operation):
        if not _loopback(host):
            global _DENIED_TOTAL
            _DENIED_TOTAL += 1
            self.denied.append(operation)
            # Deliberately omit URLs, hostnames and any credential material.
            raise OfflineEgressBlocked("offline test egress denied: " + operation)

    def check_address(self, sock, address, operation):
        if sock.family == getattr(socket, "AF_UNIX", None):
            return
        self.check_host(address[0] if isinstance(address, tuple) else None, operation)

    def __enter__(self):
        global _AUDIT_INSTALLED
        _ACTIVE_GUARDS.append(self)
        self.stack.callback(lambda: _ACTIVE_GUARDS.remove(self))
        if not _AUDIT_INSTALLED:
            sys.addaudithook(_audit)
            _AUDIT_INSTALLED = True
        original_connect = socket.socket.connect
        original_connect_ex = socket.socket.connect_ex
        original_sendto = socket.socket.sendto
        original_getaddrinfo = socket.getaddrinfo
        original_getnameinfo = socket.getnameinfo

        def connect(sock, address):
            self.check_address(sock, address, "connect")
            return original_connect(sock, address)

        def connect_ex(sock, address):
            self.check_address(sock, address, "connect_ex")
            return original_connect_ex(sock, address)

        def sendto(sock, data, *args):
            self.check_address(sock, args[-1], "sendto")
            return original_sendto(sock, data, *args)

        def getaddrinfo(host, *args, **kwargs):
            self.check_host(host, "getaddrinfo")
            # Resolve localhost numerically so even permitted use does not query DNS.
            if len(args) >= 5:
                args = (*args[:4], args[4] | socket.AI_NUMERICHOST, *args[5:])
            else:
                kwargs["flags"] = kwargs.get("flags", 0) | socket.AI_NUMERICHOST
            return original_getaddrinfo("127.0.0.1" if host == "localhost" else host, *args, **kwargs)

        def lookup(operation, extended=False):
            def guarded(host):
                self.check_host(host, operation)
                numeric = "127.0.0.1" if host == "localhost" else host
                # Never ask the OS resolver for a loopback reverse name either.
                return (str(host), [], [numeric]) if extended else numeric
            return guarded

        def getnameinfo(address, flags):
            self.check_host(address[0], "getnameinfo")
            return original_getnameinfo(address, flags | socket.NI_NUMERICHOST | socket.NI_NUMERICSERV)

        for target, value in (
            ("socket.socket.connect", connect), ("socket.socket.connect_ex", connect_ex),
            ("socket.socket.sendto", sendto), ("socket.getaddrinfo", getaddrinfo),
            ("socket.gethostbyname", lookup("gethostbyname")),
            ("socket.gethostbyname_ex", lookup("gethostbyname_ex", extended=True)),
            ("socket.gethostbyaddr", lookup("gethostbyaddr", extended=True)),
            ("socket.getnameinfo", getnameinfo),
        ):
            self.stack.enter_context(patch(target, value))
        return self

    def __exit__(self, *args):
        return self.stack.__exit__(*args)


def _audit(event, args):
    if not _ACTIVE_GUARDS:
        return
    guard = _ACTIVE_GUARDS[-1]
    if event in {"socket.connect", "socket.sendto"}:
        guard.check_address(args[0], args[1], event)
    elif event in {"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"}:
        guard.check_host(args[0], event)
    elif event == "socket.getnameinfo":
        guard.check_host(args[0][0], event)
    elif event == "subprocess.Popen":
        _check_child(args[0], args[1])
    elif event in {"os.system", "os.startfile", "os.startfile/2", "os.posix_spawn"}:
        raise OfflineEgressBlocked("offline test uninstrumented process denied")


def _check_child(executable, args):
    if isinstance(args, str):
        # Windows emits the effective command line and may leave executable=None.
        # Inspect only interpreter options; code after -c is arbitrary local data.
        first = re.match(r'^\s*(?:"([^\"]+)"|(\S+))', args)
        if not first:
            raise OfflineEgressBlocked("offline test uninstrumented child denied")
        executable = executable or first.group(1) or first.group(2)
        remainder = args[first.end():]
        option_prefix = re.split(r'\s-(?:c|m)\s', remainder, maxsplit=1)[0]
        args = [executable, *option_prefix.split()]
    name = Path(os.fsdecode(executable)).name.lower()
    if name in {"git", "git.exe"} and isinstance(args, (tuple, list)):
        arguments = list(args[1:])
        if arguments[:1] == ["-C"]:
            arguments = arguments[2:]
        # Other nominally read-only commands can invoke ext-diff/textconv helpers.
        if arguments == ["rev-parse", "HEAD"]:
            return
    if Path(os.fsdecode(executable)).resolve() == Path(sys.executable).resolve():
        if isinstance(args, (tuple, list)) and not _ignores_site(args[1:]):
            if os.environ.get("CREATOR_ARCHIVE_OFFLINE_CHILD_SITE"):
                return
    raise OfflineEgressBlocked("offline test uninstrumented child denied")


def _ignores_site(arguments):
    """Parse interpreter options only, never inspect -c code or script arguments."""
    iterator = iter(arguments)
    for argument in iterator:
        if not isinstance(argument, str):
            return True
        if argument in {"-c", "-m", "-"} or not argument.startswith("-"):
            break
        if argument in {"-X", "-W", "--check-hash-based-pycs"}:
            next(iterator, None)
            continue
        if argument.startswith(("-X", "-W", "--")):
            continue
        if any(option in argument[1:] for option in "SIE"):
            return True
    return False


def install_child_guard():
    """Called only by the temporary sitecustomize in instrumented children."""
    global _CHILD_GUARD
    _CHILD_GUARD = EgressGuard()
    _CHILD_GUARD.__enter__()
    if os.environ.get("CREATOR_ARCHIVE_OFFLINE_SYNTHETIC") == "1":
        from creator_archive import network_safety
        network_safety.xhs_network_paused = lambda: False
        network_safety.require_browser_disabled = lambda: None


def _cases(suite):
    for case in suite:
        if isinstance(case, unittest.TestSuite):
            yield from _cases(case)
        else:
            yield case


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pattern", default="test_*.py")
    parser.add_argument("--verbosity", type=int, default=1)
    args = parser.parse_args(argv)
    sys.path.insert(0, str(ROOT))
    with tempfile.TemporaryDirectory(prefix="creator-offline-suite-") as temporary, ExitStack() as stack:
        isolated = Path(temporary)
        child_site = isolated / "child-site"
        child_site.mkdir()
        (child_site / "sitecustomize.py").write_text(
            "from scripts.run_offline_tests import install_child_guard\ninstall_child_guard()\n", encoding="utf-8")
        environment = {name: value for name, value in os.environ.items()
                       if not any(secret in name.upper() for secret in ("COOKIE", "TOKEN", "SECRET", "CREDENTIAL", "SESSION"))
                       and not name.startswith("CREATOR_ARCHIVE_")}
        environment.update({"LOCALAPPDATA": str(isolated / "local"), "APPDATA": str(isolated / "roaming"), "PYTHONUTF8": "1",
                            "USERPROFILE": str(isolated / "user"), "HOME": str(isolated / "user"),
                            "CREATOR_ARCHIVE_OFFLINE_CHILD_SITE": str(child_site),
                            "PYTHONPATH": os.pathsep.join((str(child_site), str(ROOT)))})
        for name in ("local", "roaming", "user"):
            (isolated / name).mkdir()
        stack.enter_context(patch.dict(os.environ, environment, clear=True))
        egress = stack.enter_context(EgressGuard())
        original_popen = subprocess.Popen

        def instrumented_popen(command, *popen_args, **kwargs):
            _check_child(kwargs.get("executable") or command[0], command)
            env = dict(kwargs.get("env") or os.environ)
            env["CREATOR_ARCHIVE_OFFLINE_CHILD_SITE"] = str(child_site)
            env["PYTHONPATH"] = os.pathsep.join((str(child_site), str(ROOT)))
            if os.environ.get("CREATOR_ARCHIVE_OFFLINE_SYNTHETIC"):
                env["CREATOR_ARCHIVE_OFFLINE_SYNTHETIC"] = "1"
            kwargs["env"] = env
            return original_popen(command, *popen_args, **kwargs)

        stack.enter_context(patch("subprocess.Popen", instrumented_popen))
        suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern=args.pattern)

        class GuardedResult(unittest.TextTestResult):
            def startTest(self, test):
                self.case_stack = ExitStack()
                if test.__class__.__module__.rsplit(".", 1)[-1] != "test_network_safety":
                    self.case_stack.enter_context(patch("creator_archive.network_safety.xhs_network_paused", return_value=False))
                    self.case_stack.enter_context(patch("creator_archive.network_safety.require_browser_disabled", return_value=None))
                    self.case_stack.enter_context(patch.dict(os.environ, {"CREATOR_ARCHIVE_OFFLINE_SYNTHETIC": "1"}))
                return super().startTest(test)

            def stopTest(self, test):
                self.case_stack.close()
                return super().stopTest(test)

        # Shell children cannot inherit Python socket interception. Do not run
        # installation/deployment paths merely to make the suite look complete.
        for case in _cases(suite):
            if (case.__class__.__module__.rsplit(".", 1)[-1] == "test_windows_install"
                    or case._testMethodName == "test_windows_configuration_entry_from_stopped_workspace"):
                def shell_skipped():
                    raise unittest.SkipTest("offline harness refuses uninstrumented Windows shell children")
                setattr(case, case._testMethodName, shell_skipped)
        result = unittest.TextTestRunner(verbosity=args.verbosity, resultclass=GuardedResult).run(suite)
        print(f"Offline socket guard active; denied operations in parent test process: {_DENIED_TOTAL}; all data directories temporary.")
        return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    # Tests and temporary child hooks import this module by its canonical name.
    # Share one guard registry and exception class with the executable runner.
    sys.modules["scripts.run_offline_tests"] = sys.modules[__name__]
    raise SystemExit(main())
