"""Emulator-side puppet actor: inject, validate, re-inject.

The puppet is a copy of the live Player struct placed in reserved RDRAM
(0x807FF100), with its skeleton tables rebased, a no-op update function and the
real Player draw function, appended to the player actor list tail. The real
player stays at the head so GET_PLAYER() is unaffected.

This module is imported by the sync nodes and the QA harness.
"""

from __future__ import annotations

import struct

from . import player_state
from .coop_bridge import OP_WRITE
from .scan_rdram import read_range

PLAYER_SIZE = 0xA94
PUPPET = 0x807FF100
NOOP = 0x80001CC0
CAT_PLAYER = 2
OFF_PREV = 0x120
OFF_NEXT = 0x124
OFF_UPDATE = 0x130
OFF_DRAW = 0x134
OFF_OVERLAY = 0x138
OFF_JOINT = 0x1F8
JOINT_BYTES = 144            # PLAYER_LIMB_BUF_COUNT (24) * 6
OFF_APPEARANCE = 0x14C
APPEARANCE_BYTES = 20        # tunic..currentMask
OFF_SHAPE_ROT = 0xB4
TABLE = 0x800FA7F0
TABLE_COUNT = 513
TABLE_ENTRY = 0x20
# Validated in this build: with the actor-list array at L, the PlayState
# fields are L-0x1B88 (sceneId u16) and L-0x1B7C (sceneSegment u32).
SCENE_ID_DELTA = 0x1B88
SCENE_SEGMENT_DELTA = 0x1B7C
TRIGGER_DELTA = 0x10215
ENTRANCE_DELTA = 0x1021A


def scene_state(client, lists: int) -> dict | None:
    """Read scene id / draw config / segment / transition fields, validated."""
    try:
        results = client.batch(
            [
                (0x01, lists - SCENE_ID_DELTA, 3),
                (0x01, lists - SCENE_SEGMENT_DELTA, 4),
                (0x01, lists + TRIGGER_DELTA, 0x0A),
            ]
        )
    except Exception:
        return None
    sid, draw = struct.unpack(">HB", results[0])
    segment = struct.unpack(">I", results[1])[0]
    trigger = struct.unpack(">b", results[2][0:1])[0]
    entrance = struct.unpack(">H", results[2][5:7])[0]
    if not (0 <= sid < 0x200):
        return None
    if not (0x80100000 <= segment < 0x80800000):
        return None
    return {
        "id": sid,
        "draw_config": draw,
        "segment": segment,
        "trigger": trigger,
        "entrance": entrance,
    }


def u32(data: bytes, off: int) -> int:
    return struct.unpack_from(">I", data, off)[0]


def _actor_at(data: bytes, addr: int) -> tuple[int, int] | None:
    off = addr - 0x80000000
    if off < 0 or off + 0x140 > len(data):
        return None
    ident, category = struct.unpack_from(">hB", data, off)
    overlay = u32(data, off + OFF_OVERLAY)
    if (
        0 <= ident < TABLE_COUNT
        and TABLE <= overlay < TABLE + TABLE_COUNT * TABLE_ENTRY
        and (overlay - TABLE) % TABLE_ENTRY == 0
        and (overlay - TABLE) // TABLE_ENTRY == ident
    ):
        return ident, category
    return None


def find_actor_lists(client, player: int) -> int:
    """Find the 12-entry actorLists array by shape and by the player head."""
    data = read_range(client, 0x80000000, 0x800000)
    for start in range(0, len(data) - 0x60, 4):
        heads = []
        good = True
        for category in range(12):
            entry = start + category * 8
            length = struct.unpack_from(">i", data, entry)[0]
            head = u32(data, entry + 4)
            if head == 0:
                if length != 0:
                    good = False
                    break
            else:
                actor = _actor_at(data, head)
                if length <= 0 or actor is None or actor[1] != category:
                    good = False
                    break
                heads.append(head)
        if not good or len(heads) < 2:
            continue
        if u32(data, start + CAT_PLAYER * 8 + 4) == player:
            return 0x80000000 + start
    raise RuntimeError("actor list array not found")


def list_entry(client, lists: int) -> tuple[int, int]:
    return struct.unpack(">iI", client.read(lists + CAT_PLAYER * 8, 8))


def is_linked(client, lists: int, puppet: int = PUPPET) -> bool:
    length, head = list_entry(client, lists)
    node = head
    for _ in range(max(2, min(length, 64)) + 2):
        if node == puppet:
            return True
        if node == 0:
            return False
        node = struct.unpack(">I", client.read(node + OFF_NEXT, 4))[0]
    return False


def inject(client, player: int, lists: int | None = None,
           offset: tuple[float, float, float] = (35.0, 0.0, 0.0)) -> int:
    live = client.read(player, PLAYER_SIZE)
    buf = bytearray(live)

    px, py, pz = struct.unpack_from(">fff", live, 0x24)
    draw = u32(live, OFF_DRAW)
    overlay = u32(live, OFF_OVERLAY)

    nx, ny, nz = px + offset[0], py + offset[1], pz + offset[2]
    struct.pack_into(">fff", buf, 0x24, nx, ny, nz)          # world.pos
    struct.pack_into(">fff", buf, 0x38, nx, ny + 56.0, nz)   # focus.pos
    struct.pack_into(">fff", buf, 0x100, nx, ny, nz)         # prevPos

    struct.pack_into(">II", buf, 0x118, 0, 0)                # parent, child
    struct.pack_into(">I", buf, OFF_PREV, 0)
    struct.pack_into(">I", buf, OFF_NEXT, 0)
    struct.pack_into(">I", buf, 0x128, NOOP)                 # init
    struct.pack_into(">I", buf, 0x12C, NOOP)                 # destroy
    struct.pack_into(">I", buf, OFF_UPDATE, NOOP)
    struct.pack_into(">I", buf, OFF_DRAW, draw)
    struct.pack_into(">I", buf, OFF_OVERLAY, overlay)

    struct.pack_into(">I", buf, 0x1B0, 0)                    # giObjectSegment
    struct.pack_into(">I", buf, 0x1D4, PUPPET + OFF_JOINT)   # skelAnime.jointTable
    struct.pack_into(">I", buf, 0x1D8, PUPPET + 0x288)       # skelAnime.morphTable
    struct.pack_into(">I", buf, 0x3AC, 0)                    # heldActor
    struct.pack_into(">I", buf, 0x3C4, 0)
    struct.pack_into(">I", buf, 0x430, 0)                    # doorActor

    client.write(PUPPET, bytes(buf))

    if lists is None:
        lists = find_actor_lists(client, player)
    entry = lists + CAT_PLAYER * 8
    length, head = struct.unpack(">iI", client.read(entry, 8))
    if head != player:
        raise RuntimeError(f"unexpected player list head {head:#x}")

    client.batch(
        [
            (OP_WRITE, head + OFF_NEXT, struct.pack(">I", PUPPET)),
            (OP_WRITE, PUPPET + OFF_PREV, struct.pack(">I", head)),
            (OP_WRITE, entry, struct.pack(">i", length + 1)),
        ]
    )
    return lists


def unlink(client, lists: int, player: int, puppet: int = PUPPET) -> None:
    """Detach the puppet, simulating an actor-list rebuild after a scene load."""
    entry = lists + CAT_PLAYER * 8
    length, head = struct.unpack(">iI", client.read(entry, 8))
    if head == player:
        client.batch(
            [
                (OP_WRITE, head + OFF_NEXT, struct.pack(">I", 0)),
                (OP_WRITE, puppet + OFF_PREV, struct.pack(">I", 0)),
                (OP_WRITE, entry, struct.pack(">i", 1)),
            ]
        )


def ensure(client, lists: int | None, player: int) -> tuple[int, bool]:
    """Re-inject the puppet if it is no longer linked. Returns (lists, reinjected)."""
    if lists is not None:
        try:
            length, head = list_entry(client, lists)
        except Exception:
            length, head = -1, 0
        if head == player and 1 <= length < 64:
            if is_linked(client, lists):
                return lists, False
            inject(client, player, lists)
            return lists, True
    lists = find_actor_lists(client, player)
    if is_linked(client, lists):
        return lists, False
    inject(client, player, lists)
    return lists, True
