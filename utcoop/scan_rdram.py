"""Dump or search live RDRAM through the gopher64 co-op bridge.

Examples:
    python local/scan_rdram.py --out local/dumps/rdram.bin
    python local/scan_rdram.py --find "Ultimate" --find "Trial"

The output dump is private (local/ is gitignored). Reading 8 MiB takes roughly
a second or two over the socket because reads are batched per frame.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from .coop_bridge import OP_READ, CoopClient  # noqa: E402


def read_range(client: CoopClient, addr: int, length: int, chunk: int = 0x8000, per_batch: int = 16) -> bytes:
    out = bytearray()
    while len(out) < length:
        requests = []
        added = 0
        while len(requests) < per_batch and len(out) + added < length:
            size = min(chunk, length - len(out) - added)
            requests.append((OP_READ, (addr + len(out) + added) & 0xFFFFFFFF, size))
            added += size
        for part in client.batch(requests):
            out += part
    return bytes(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=47900)
    parser.add_argument("--base", type=lambda x: int(x, 0), default=0x80000000)
    parser.add_argument("--size", type=lambda x: int(x, 0), default=0x800000)
    parser.add_argument("--out", help="write the dump here")
    parser.add_argument("--find", action="append", default=[], help="ASCII substring to locate (repeatable)")
    args = parser.parse_args()

    with CoopClient(args.host, args.port, timeout=60.0) as client:
        print(client.stat())
        data = read_range(client, args.base, args.size)

    nonzero = sum(1 for byte in data if byte)
    print(f"read {len(data)} bytes; nonzero {nonzero} ({nonzero / max(1, len(data)):.1%})")

    if args.out:
        parent = os.path.dirname(args.out)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(args.out, "wb") as handle:
            handle.write(data)
        print(f"wrote {args.out}")

    for needle in args.find:
        encoded = needle.encode()
        hits = []
        start = 0
        while True:
            index = data.find(encoded, start)
            if index < 0:
                break
            hits.append(args.base + index)
            start = index + 1
        print(f"{needle!r}: {len(hits)} hit(s)", " ".join(hex(hit) for hit in hits[:10]))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
