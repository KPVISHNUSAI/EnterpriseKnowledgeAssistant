#!/usr/bin/env python3
"""Report whether the host ports this project uses are free, by trying to bind them.

    python3 scripts/check_ports.py            # the defaults (or values exported in env)
    python3 scripts/check_ports.py 8081 3000  # specific ports

A bind test is reliable where `lsof` is not (it misses other users' and VM-forwarded
sockets). A port held by this project's own containers also shows as busy.
"""

from __future__ import annotations

import os
import socket
import sys

DEFAULTS = {
    "EKA_POSTGRES_PORT": 5436,
    "EKA_REDIS_PORT": 6381,
    "EKA_API_PORT": 8081,
    "EKA_FRONTEND_PORT": 3000,
    "WORKER_METRICS_PORT": 9101,
}


def is_free(port: int) -> bool:
    for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET, "0.0.0.0")):  # noqa: S104
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((host, port))
            except OSError:
                return False
    return True


def main(argv: list[str]) -> int:
    if argv:
        ports = {f"port {p}": int(p) for p in argv}
    else:
        ports = {name: int(os.environ.get(name, default)) for name, default in DEFAULTS.items()}
    busy = 0
    for name, port in ports.items():
        free = is_free(port)
        busy += not free
        print(f"{name:<22} {port:<6} {'free' if free else 'IN USE'}")
    return 1 if busy else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
