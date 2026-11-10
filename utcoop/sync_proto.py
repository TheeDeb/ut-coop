"""Prototype co-op state synchronization between two gopher64 instances.

Each side reads its own player actor through the emulator's co-op bridge and
sends a snapshot to its peer over UDP. Snapshots carry position, rotation,
appearance and the 24-limb joint table, so the receiver can drive a puppet actor
that both moves and animates like the remote player.

The node also owns a watchdog that re-injects the puppet if the actor lists are
rebuilt (scene transition) and can mirror the latest snapshot into a reserved
RDRAM buffer for game-side consumers.

Snapshot v2 (little-endian, 210 bytes):
  u32 magic, u16 version, u16 flags, u32 seq, u64 frame,
  f32 x,y,z, i16 rx,ry,rz, f32 speed, u32 scene,
  20s appearance (tunic..mask), 144s jointTable (24 limbs x Vec3s)
"""

from __future__ import annotations

import argparse
import os
import socket
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from . import player_state  # noqa: E402
from . import puppet as puppet_mod  # noqa: E402
from .coop_bridge import OP_READ, OP_WRITE, CoopClient  # noqa: E402

MAGIC = 0x55544350  # "UTC P"
VERSION = 4
FMT = "<IHHIIQfffhhhfIH20s144s"
SIZE = struct.calcsize(FMT)
DEFAULT_REMOTE_BUF = 0x807FF000


def pack_snapshot(
    seq: int,
    frame: int,
    state: dict,
    scene: int = 0,
    entrance: int = 0,
    flags: int = 0,
    session: int = 0,
) -> bytes:
    return struct.pack(
        FMT,
        MAGIC,
        VERSION,
        flags,
        session,
        seq,
        frame,
        state["pos"][0],
        state["pos"][1],
        state["pos"][2],
        state["rot"][0],
        state["rot"][1],
        state["rot"][2],
        state["speed"],
        scene,
        entrance,
        state["appearance"],
        state["joint"],
    )


def unpack_snapshot(data: bytes) -> dict:
    if len(data) != SIZE:
        raise ValueError(f"bad snapshot size {len(data)}")
    (
        magic,
        version,
        flags,
        session,
        seq,
        frame,
        x,
        y,
        z,
        rx,
        ry,
        rz,
        speed,
        scene,
        entrance,
        appearance,
        joint,
    ) = struct.unpack(FMT, data)
    if magic != MAGIC or version != VERSION:
        raise ValueError("bad snapshot magic/version")
    return {
        "seq": seq,
        "frame": frame,
        "flags": flags,
        "session": session,
        "pos": (x, y, z),
        "rot": (rx, ry, rz),
        "speed": speed,
        "scene": scene,
        "entrance": entrance,
        "appearance": appearance,
        "joint": joint,
    }


def connect_emulator(port: int, timeout: float = 30.0) -> CoopClient:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        client = None
        try:
            client = CoopClient(port=port, timeout=3.0)
            client.stat()
            return client
        except Exception as exc:  # the listener opens before the first frame
            last = exc
            if client is not None:
                client.close()
            time.sleep(0.25)
    raise RuntimeError(f"no bridge answer on port {port}: {last}")


class SyncNode:
    def __init__(
        self,
        coop_port: int,
        udp_bind: tuple[str, int],
        peer: tuple[str, int],
        rate: float = 30.0,
        remote_buf: int | None = None,
        puppet: int | None = None,
        auto_follow: bool = False,
        session: int = 0,
        animate: bool = True,
    ):
        self.client = connect_emulator(coop_port)
        self.coop_port = coop_port
        self.actor = player_state.PLAYER_ACTOR
        if not player_state.is_actor(self.client, self.actor):
            found = player_state.find_player(self.client)
            self.actor = found or self.actor

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(udp_bind)
        self.sock.setblocking(False)
        self.peer = peer
        self.rate = rate
        self.remote_buf = remote_buf
        self.puppet = puppet
        self.lists: int | None = None
        self.reinjects = 0
        self.scene_id: int | None = None
        self.scene_override: int | None = None
        self.puppet_active = False
        self.scene_gates = 0
        self.auto_follow = auto_follow
        self.session = session
        self.animate = animate
        self.follow_attempts = 0
        self.max_follow_attempts = 5
        self.follow_gave_up = False
        self._last_scene: int | None = None
        self._scene_changed_at = 0.0
        self.scene_stable_delay = 6.0
        self.follow_count = 0
        self.following_scene: int | None = None
        self._follow_ready = 0.0

        self.seq = 0
        self.local: dict | None = None
        self.remote: dict | None = None
        self.remote_at = 0.0
        self.received = 0
        self.gaps = 0
        self.errors = 0
        self.reconnects = 0
        self.buf_ok = 0
        self.buf_bad = 0
        self._buf_check = 10

        self.puppet_pos: tuple[float, float, float] | None = None
        self.puppet_applied: tuple[float, float, float] | None = None
        self.puppet_pos_written: tuple[float, float, float] | None = None
        self.puppet_joint: bytes | None = None
        self.puppet_joint_applied: bytes | None = None
        self.puppet_joint_written: bytes | None = None
        self.puppet_check: dict | None = None
        self.joint_changes = 0
        self._last_joint_read: bytes | None = None
        self.puppet_drawn: int | None = None

        self._last_remote_seq: int | None = None
        self._input_lock = threading.Lock()
        self._pending_input: tuple[int, bool] | None = None
        self._teleport: tuple[float, float, float] | None = None
        self._unlink = False
        self._stop = threading.Event()

    def set_input(self, data: int, active: bool = True) -> None:
        with self._input_lock:
            self._pending_input = (int(data), bool(active))

    def set_teleport(self, pos: tuple[float, float, float] | None) -> None:
        with self._input_lock:
            self._teleport = tuple(pos) if pos is not None else None

    def force_unlink(self) -> None:
        """Simulate an actor-list rebuild by detaching the puppet."""
        with self._input_lock:
            self._unlink = True

    def set_scene_override(self, scene: int | None) -> None:
        with self._input_lock:
            self.scene_override = scene

    def _reconnect(self) -> None:
        try:
            self.client.close()
        except Exception:
            pass
        self.client = connect_emulator(self.coop_port)
        if not player_state.is_actor(self.client, self.actor):
            self.actor = player_state.find_player(self.client) or player_state.PLAYER_ACTOR
        self.lists = None
        self.reconnects += 1

    def _drain_udp(self) -> None:
        while True:
            try:
                data, _addr = self.sock.recvfrom(2048)
            except BlockingIOError:
                return
            try:
                snap = unpack_snapshot(data)
            except ValueError:
                continue
            if self.session and snap["session"] != self.session:
                continue
            self.received += 1
            if self._last_remote_seq is not None:
                expected = (self._last_remote_seq + 1) & 0xFFFFFFFF
                if snap["seq"] != expected:
                    self.gaps += 1
            self._last_remote_seq = snap["seq"]
            self.remote = snap
            self.remote_at = time.time()

            if self.remote_buf is not None:
                try:
                    self.client.write(self.remote_buf, data)
                    self._buf_check -= 1
                    if self._buf_check <= 0:
                        self._buf_check = 10
                        if self.client.read(self.remote_buf, len(data)) == data:
                            self.buf_ok += 1
                        else:
                            self.buf_bad += 1
                except Exception:
                    pass

    def _apply_teleport(self) -> None:
        with self._input_lock:
            teleport = self._teleport
        if teleport is None:
            return
        x, y, z = teleport
        self.client.batch(
            [
                (OP_WRITE, self.actor + 0x24, struct.pack(">fff", x, y, z)),
                (OP_WRITE, self.actor + 0x38, struct.pack(">fff", x, y + 56.0, z)),
                (OP_WRITE, self.actor + 0x100, struct.pack(">fff", x, y, z)),
            ]
        )

    def _try_reinject(self) -> None:
        # Never touch the actor list while a scene transition is in flight.
        if self._last_scene is not None:
            if time.monotonic() - self._scene_changed_at < self.scene_stable_delay:
                return
        if self.lists is not None:
            scene = puppet_mod.scene_state(self.client, self.lists)
            if scene is not None and scene.get("trigger", 0) != 0:
                return
        if not player_state.is_actor(self.client, self.actor):
            found = player_state.find_player(self.client)
            if found is not None:
                self.actor = found
        self.lists, reinjected = puppet_mod.ensure(self.client, self.lists, self.actor)
        if reinjected:
            self.reinjects += 1

    def step(self) -> None:
        self._drain_udp()
        with self._input_lock:
            pending = self._pending_input
            self._pending_input = None
            unlink = self._unlink
            self._unlink = False
            override = self.scene_override

        if pending is not None:
            self.client.set_input(0, pending[0], pending[1])
        self._apply_teleport()

        if unlink and self.lists is not None:
            puppet_mod.unlink(self.client, self.lists, self.actor)
            self.lists, reinjected = puppet_mod.ensure(self.client, self.lists, self.actor)
            if reinjected:
                self.reinjects += 1

        state = player_state.read_actor(self.client, self.actor, frame=True, with_anim=True)
        self.local = state

        local_scene = None
        local_entrance = 0
        if self.puppet is not None:
            if self.lists is None:
                self._try_reinject()
            if self.lists is not None:
                scene = puppet_mod.scene_state(self.client, self.lists)
                if scene is not None:
                    local_scene = scene["id"]
                    local_entrance = scene["entrance"]
            if override is not None:
                local_scene = override
            self.scene_id = local_scene
            if local_scene is not None:
                if self._last_scene is not None and local_scene != self._last_scene:
                    self._scene_changed_at = time.monotonic()
                self._last_scene = local_scene
            remote_scene = self.remote["scene"] if self.remote is not None else None
            same = remote_scene is None or (
                local_scene is not None and remote_scene == local_scene
            )
            if not same:
                if (
                    self.auto_follow
                    and not self.follow_gave_up
                    and self.lists is not None
                    and self.remote is not None
                    and 0 < self.remote.get("entrance", 0) < 0x1000
                    and time.monotonic() >= self._follow_ready
                ):
                    entrance = self.remote["entrance"]
                    self.client.batch(
                        [
                            (
                                OP_WRITE,
                                self.lists + puppet_mod.ENTRANCE_DELTA,
                                struct.pack(">H", entrance),
                            ),
                            (
                                OP_WRITE,
                                self.lists + puppet_mod.TRIGGER_DELTA,
                                struct.pack(">b", 20),
                            ),
                        ]
                    )
                    self.follow_count += 1
                    self.follow_attempts += 1
                    self.following_scene = self.remote["scene"]
                    self._follow_ready = time.monotonic() + 4.0
                    if self.follow_attempts >= self.max_follow_attempts:
                        self.follow_gave_up = True
                        print(
                            "scene follow gave up after "
                            f"{self.follow_attempts} attempts; staying put"
                        )
                if self.puppet_active and self.lists is not None:
                    puppet_mod.unlink(self.client, self.lists, self.actor)
                    self.puppet_active = False
                    self.scene_gates += 1
            else:
                self.puppet_active = True

        if self.puppet is not None and self.puppet_active and self.remote is not None:
            pos = self.remote["pos"]
            rot = self.remote["rot"]
            writes = [
                (OP_WRITE, self.puppet + 0x24, struct.pack(">fffhhh", *pos, *rot)),
                (OP_WRITE, self.puppet + puppet_mod.OFF_SHAPE_ROT, struct.pack(">hhh", *rot)),
                (OP_WRITE, self.puppet + puppet_mod.OFF_APPEARANCE, self.remote["appearance"]),
            ]
            if self.animate:
                writes.append(
                    (OP_WRITE, self.puppet + puppet_mod.OFF_JOINT, self.remote["joint"])
                )
            if self.seq % 5 == 0:
                x, y, z = pos
                writes += [
                    (OP_WRITE, self.puppet + 0x38, struct.pack(">fff", x, y + 56.0, z)),
                    (OP_WRITE, self.puppet + 0x100, struct.pack(">fff", x, y, z)),
                ]
            self.client.batch(writes)
            self.puppet_applied = pos
            self.puppet_joint_applied = self.remote["joint"]

            if self.seq % 10 == 0:
                self.puppet_joint_written = self.remote["joint"]
                self.puppet_pos_written = self.puppet_applied
                results = self.client.batch(
                    [
                        (OP_READ, self.puppet + 0x24, 12),
                        (OP_READ, self.puppet + 0x115, 1),
                        (OP_READ, self.puppet + puppet_mod.OFF_JOINT, puppet_mod.JOINT_BYTES),
                    ]
                )
                self.puppet_pos = struct.unpack(">fff", results[0])
                self.puppet_drawn = results[1][0]
                self.puppet_joint = results[2]
                if self._last_joint_read is not None and self.puppet_joint != self._last_joint_read:
                    self.joint_changes += 1
                self._last_joint_read = self.puppet_joint
                self.puppet_check = {
                    "pos": self.puppet_pos,
                    "joint": self.puppet_joint,
                    "drawn": self.puppet_drawn,
                    "pos_written": self.puppet_pos_written,
                    "joint_written": self.puppet_joint_written,
                    "seq": self.seq,
                }

        if self.puppet is not None and (
            self.lists is None or (self.puppet_active and self.seq % 30 == 0)
        ):
            self._try_reinject()

        packet = pack_snapshot(
            self.seq,
            state["frame"],
            state,
            scene=local_scene if local_scene is not None else 0,
            entrance=local_entrance if 0 < local_entrance < 0x1000 else 0,
            session=self.session,
        )
        self.sock.sendto(packet, self.peer)
        self.seq += 1

    def run(self) -> None:
        period = 1.0 / self.rate
        next_tick = time.monotonic()
        failures = 0
        while not self._stop.is_set():
            try:
                self.step()
                failures = 0
            except Exception:
                self.errors += 1
                failures += 1
                if failures >= 3:
                    try:
                        self._reconnect()
                        failures = 0
                    except Exception:
                        time.sleep(0.5)
            next_tick += period
            delay = next_tick - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_tick = time.monotonic()

    def close(self) -> None:
        self.stop()
        try:
            self.client.close()
        finally:
            self.sock.close()

    def stop(self) -> None:
        self._stop.set()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coop-port", type=int, required=True)
    parser.add_argument("--bind", default="127.0.0.1:49001")
    parser.add_argument("--peer", default="127.0.0.1:49002")
    parser.add_argument("--rate", type=float, default=30.0)
    parser.add_argument("--remote-buf", type=lambda x: int(x, 0), default=None)
    parser.add_argument("--puppet", type=lambda x: int(x, 0), default=None)
    parser.add_argument("--follow", action="store_true", help="follow the peer's scene changes")
    parser.add_argument("--session", type=lambda x: int(x, 0), default=0, help="session id shared by both peers")
    parser.add_argument("--seconds", type=float, default=0, help="0 = run until Ctrl+C")
    args = parser.parse_args()

    def parse_addr(text: str) -> tuple[str, int]:
        host, port = text.rsplit(":", 1)
        return host, int(port)

    node = SyncNode(
        args.coop_port,
        parse_addr(args.bind),
        parse_addr(args.peer),
        args.rate,
        args.remote_buf,
        args.puppet,
        args.follow,
        args.session,
    )
    thread = threading.Thread(target=node.run, daemon=True)
    thread.start()
    deadline = time.time() + args.seconds if args.seconds else None
    try:
        while deadline is None or time.time() < deadline:
            time.sleep(1.0)
            local = player_state.read_actor(node.client, node.actor)
            remote = node.remote
            print(
                f"seq {node.seq} local {tuple(round(v, 1) for v in local['pos'])} "
                f"remote {tuple(round(v, 1) for v in remote['pos']) if remote else None} "
                f"recv {node.received} gaps {node.gaps} reinjects {node.reinjects}"
            )
    except KeyboardInterrupt:
        pass
    finally:
        node.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
