"""Run standard unittest in writable, retained fixtures with bounded network scope."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
import uuid


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("suite", choices=("core", "engine"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    fixtures = root / "state" / "test-runs" / uuid.uuid4().hex
    fixtures.mkdir(parents=True)  # Immediate permission error, no tempfile retry loop.
    (fixtures / "writable-check.txt").write_text("synthetic test workspace\n", encoding="utf-8")
    os.environ["TEMP"] = os.environ["TMP"] = str(fixtures)
    tempfile.tempdir = str(fixtures)
    target = root / "engine" if args.suite == "engine" else root
    os.chdir(target)
    sys.path.insert(0, str(target))
    original_connect = socket.socket.connect
    original_lookup = socket.getaddrinfo

    def permitted(address):
        return args.suite == "core" and isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1", "localhost"}

    def connect(sock, address):
        if not permitted(address):
            raise AssertionError("offline test: external connection forbidden")
        return original_connect(sock, address)

    def lookup(host, port, *positional, **keywords):
        if not permitted((host, port)):
            raise AssertionError("offline test: external DNS forbidden")
        return original_lookup(host, port, *positional, **keywords)

    socket.socket.connect = connect
    socket.getaddrinfo = lookup
    try:
        suite = unittest.defaultTestLoader.discover(str(target), pattern="test_*.py")
        result = unittest.TextTestRunner(verbosity=1).run(suite)
        return 0 if result.testsRun > len(result.skipped) and result.wasSuccessful() else 1
    finally:
        socket.socket.connect = original_connect
        socket.getaddrinfo = original_lookup


if __name__ == "__main__":
    raise SystemExit(main())
