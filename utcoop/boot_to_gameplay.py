"""Drive a running gopher64 instance from boot to controllable gameplay.

Preconditions:
  - emulator started with --coop-port PORT and the UT ROM,
  - save slot 1 already exists (name entry + Options + Start creates it once;
    see games/ultimate-trial/NOTES.md).

The sequence is timing based, not menu-state based. It finishes by printing the
player actor position before and after a short forward walk, which is the real
pass/fail check: if the position changes, the intro is over and input works.
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from .coop_bridge import CoopClient  # noqa: E402

A = 1 << 7
START = 1 << 4
UP = 0x50 << 24
PLAYER = 0x80289550
POS = PLAYER + 0x24


def tap(client: CoopClient, word: int, hold: float = 0.25, wait: float = 0.9) -> None:
    client.set_input(0, word, True)
    time.sleep(hold)
    client.set_input(0, 0, False)
    time.sleep(wait)


def tap_all(client: CoopClient, word: int, hold: float = 0.5, wait: float = 4.0) -> None:
    for channel in range(4):
        client.set_input(channel, word, True)
    time.sleep(hold)
    for channel in range(4):
        client.set_input(channel, 0, False)
    time.sleep(wait)


def pos(client: CoopClient) -> tuple[float, float, float]:
    return struct.unpack(">fff", client.read(POS, 12))


def moved_since(client: CoopClient, before: tuple[float, float, float], hold: float = 0.5) -> float:
    client.set_input(0, UP, True)
    time.sleep(hold)
    client.set_input(0, 0, False)
    time.sleep(0.2)
    after = pos(client)
    return sum((after[i] - before[i]) ** 2 for i in range(3)) ** 0.5


def drive_to_gameplay(client: CoopClient, extra_taps: int = 12) -> bool:
    """Menu navigation plus intro skipping, with a control check between attempts.

    Returns True once the player moves under scripted input. Handles instances
    whose intro runs slower (e.g. two emulators on one GPU).
    """
    tap_all(client, A | START)          # title -> select file
    tap(client, A, wait=2.0)            # open file 1
    tap(client, A, wait=2.0)            # confirm "Yes"
    tap(client, A, wait=1.0)            # first intro prompt
    for _ in range(extra_taps):
        if moved_since(client, pos(client)) > 2.0:
            return True
        tap(client, A, wait=0.8)
    return moved_since(client, pos(client)) > 2.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=47921)
    parser.add_argument("--boot-wait", type=float, default=12.0)
    args = parser.parse_args()

    # The listener opens before the first frame, so wait until STAT answers.
    deadline = time.time() + 60
    client = None
    while time.time() < deadline:
        try:
            client = CoopClient(port=args.port, timeout=3.0)
            client.stat()
            break
        except Exception:
            if client:
                client.close()
            client = None
            time.sleep(0.5)
    if client is None:
        print("bridge never answered", file=sys.stderr)
        return 1

    try:
        print("connected", client.stat())
        time.sleep(args.boot_wait)          # let the title screen finish loading
        before = pos(client)
        ok = drive_to_gameplay(client)
        after = pos(client)
        print(f"player {before} -> {after}; controllable: {ok}")
        return 0 if ok else 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
