"""Read and validate Ultimate Trial player-actor state over the co-op bridge.

The actor address is stable across boots in the builds tested so far, but it is
not a contract: every read validates id/category/overlayEntry, and `find_player`
rescans RDRAM if the cached address is stale.
"""

from __future__ import annotations

import struct

from .coop_bridge import OP_READ, OP_STAT, CoopClient
from .scan_rdram import read_range

TABLE_BASE = 0x800FA7F0
TABLE_COUNT = 513
ENTRY_SIZE = 0x20
PLAYER_ACTOR = 0x80289550

OFF_ID = 0x00
OFF_CATEGORY = 0x02
OFF_ROOM = 0x03
OFF_WORLD = 0x24
OFF_ROT = 0x30
OFF_FOCUS = 0x38
OFF_SPEED = 0x68
OFF_PREV = 0x100
OFF_NEXT = 0x124
OFF_UPDATE = 0x130
OFF_DRAW = 0x134
OFF_OVERLAY = 0x138
OFF_JOINT = 0x1F8
OFF_APPEARANCE = 0x14C
APPEARANCE_BYTES = 20
JOINT_BYTES = 144
ANIM_SPAN = OFF_JOINT + JOINT_BYTES - OFF_APPEARANCE


def _u32(client: CoopClient, addr: int) -> int:
    return struct.unpack(">I", client.read(addr, 4))[0]


def is_actor(client: CoopClient, base: int) -> bool:
    try:
        ident, category = struct.unpack(">hB", client.read(base, 3))
        overlay = _u32(client, base + OFF_OVERLAY)
    except Exception:
        return False
    return (
        ident == 0
        and category == 2
        and TABLE_BASE <= overlay < TABLE_BASE + TABLE_COUNT * ENTRY_SIZE
        and (overlay - TABLE_BASE) % ENTRY_SIZE == 0
    )


def read_actor(
    client: CoopClient, base: int = PLAYER_ACTOR, frame: bool = False, with_anim: bool = False
) -> dict:
    """One batch for the actor header, optionally + appearance/joint + STAT."""
    requests = [(OP_READ, base, 0x140)]
    if with_anim:
        requests.append((OP_READ, base + OFF_APPEARANCE, ANIM_SPAN))
    if frame:
        requests.append((OP_STAT,))
    results = client.batch(requests)
    data = results[0]
    ident, category, room = struct.unpack_from(">hBb", data, 0)
    pos = struct.unpack_from(">fff", data, OFF_WORLD)
    rot = struct.unpack_from(">hhh", data, OFF_ROT)
    focus = struct.unpack_from(">fff", data, OFF_FOCUS)
    speed = struct.unpack_from(">f", data, OFF_SPEED)[0]
    state = {
        "base": base,
        "id": ident,
        "category": category,
        "room": room,
        "pos": pos,
        "rot": rot,
        "focus": focus,
        "speed": speed,
    }
    index = 1
    if with_anim:
        anim = results[index]
        index += 1
        state["appearance"] = anim[0:APPEARANCE_BYTES]
        state["joint"] = anim[OFF_JOINT - OFF_APPEARANCE : OFF_JOINT - OFF_APPEARANCE + JOINT_BYTES]
    if frame:
        state["frame"] = results[index]["frame"]
    return state


def read_player(client: CoopClient, base: int = PLAYER_ACTOR) -> dict:
    return read_actor(client, base)


def find_player(client: CoopClient) -> int | None:
    data = read_range(client, 0x80000000, 0x800000)
    hits = []
    for offset in range(0, len(data) - 4, 4):
        value = struct.unpack_from(">I", data, offset)[0]
        if not (TABLE_BASE <= value < TABLE_BASE + TABLE_COUNT * ENTRY_SIZE):
            continue
        if (value - TABLE_BASE) % ENTRY_SIZE != 0:
            continue
        index = (value - TABLE_BASE) // ENTRY_SIZE
        base_offset = offset - OFF_OVERLAY
        if base_offset < 0:
            continue
        ident, category = struct.unpack_from(">hB", data, base_offset)
        if ident == 0 and category == 2 and index == 0:
            hits.append(0x80000000 + base_offset)
    if len(hits) == 1:
        return hits[0]
    return None
