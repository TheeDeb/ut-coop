"""Client for the gopher64 co-op memory bridge (gopher64 fork, --coop-port).

The bridge is a local TCP socket served by the emulator. One round trip per
batch; the emulator executes the whole batch at a single frame boundary, so a
read/write pair in one batch is atomic with respect to the frame.

Wire format (little-endian):
  request  : u32 payload_len, then requests concatenated:
             0x01 READ   u32 addr, u16 len
             0x02 WRITE  u32 addr, u16 len, bytes
             0x03 INPUT  u8 channel, u8 active, u32 data
             0x04 STAT
  response : u32 payload_len, then one response per request, same order:
             0x81 READ   u8 status, u16 len, bytes
             0x82 WRITE  u8 status
             0x83 INPUT  u8 status
             0x84 STAT   u8 status, u64 frame, u32 rdram_size

Addresses are N64 game addresses as the game sees them (KSEG0, e.g.
0x80100000). The bridge converts to host RDRAM storage.
"""

from __future__ import annotations

import argparse
import socket
import struct

OP_READ = 0x01
OP_WRITE = 0x02
OP_INPUT = 0x03
OP_STAT = 0x04

RESP_READ = 0x81
RESP_WRITE = 0x82
RESP_INPUT = 0x83
RESP_STAT = 0x84

STATUS_OK = 0

MAX_BATCH = 4 * 1024 * 1024


class BridgeError(RuntimeError):
    pass


class CoopClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 47900, timeout: float = 5.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def close(self) -> None:
        self.sock.close()

    def __enter__(self) -> "CoopClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _recv_exact(self, length: int) -> bytes:
        chunks = []
        remaining = length
        while remaining:
            chunk = self.sock.recv(remaining)
            if not chunk:
                raise BridgeError("bridge closed the connection")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _exchange(self, payload: bytes) -> bytes:
        if len(payload) > MAX_BATCH:
            raise BridgeError("batch too large")
        self.sock.sendall(struct.pack("<I", len(payload)) + payload)
        (length,) = struct.unpack("<I", self._recv_exact(4))
        if length > MAX_BATCH:
            raise BridgeError(f"response too large: {length}")
        return self._recv_exact(length)

    @staticmethod
    def _encode(requests) -> tuple[bytes, list[str]]:
        out = bytearray()
        kinds = []
        for req in requests:
            kind = req[0]
            if kind == OP_READ:
                _, addr, length = req
                out += struct.pack("<BIH", OP_READ, addr, length)
                kinds.append("read")
            elif kind == OP_WRITE:
                _, addr, data = req
                out += struct.pack("<BIH", OP_WRITE, addr, len(data))
                out += bytes(data)
                kinds.append("write")
            elif kind == OP_INPUT:
                _, channel, data = req
                out += struct.pack("<BBBI", OP_INPUT, channel, 1, data)
                kinds.append("input")
            elif kind == OP_STAT:
                out += bytes([OP_STAT])
                kinds.append("stat")
            else:
                raise BridgeError(f"unknown request opcode {kind}")
        return bytes(out), kinds

    @staticmethod
    def _decode(resp: bytes, kinds) -> list:
        results = []
        offset = 0
        for kind in kinds:
            op = resp[offset]
            status = resp[offset + 1]
            if status != STATUS_OK:
                raise BridgeError(f"{kind} failed with status {status}")
            offset += 2
            if kind == "read":
                if op != RESP_READ:
                    raise BridgeError(f"expected READ response, got 0x{op:02x}")
                (length,) = struct.unpack_from("<H", resp, offset)
                offset += 2
                results.append(resp[offset : offset + length])
                offset += length
            elif kind == "write":
                if op != RESP_WRITE:
                    raise BridgeError(f"expected WRITE response, got 0x{op:02x}")
                results.append(None)
            elif kind == "input":
                if op != RESP_INPUT:
                    raise BridgeError(f"expected INPUT response, got 0x{op:02x}")
                results.append(None)
            else:
                if op != RESP_STAT:
                    raise BridgeError(f"expected STAT response, got 0x{op:02x}")
                frame, rdram_size = struct.unpack_from("<QI", resp, offset)
                offset += 12
                results.append({"frame": frame, "rdram_size": rdram_size})
        if offset != len(resp):
            raise BridgeError(f"trailing response bytes: {len(resp) - offset}")
        return results

    def batch(self, requests) -> list:
        payload, kinds = self._encode(requests)
        return self._decode(self._exchange(payload), kinds)

    def read(self, addr: int, length: int) -> bytes:
        return self.batch([(OP_READ, addr, length)])[0]

    def write(self, addr: int, data: bytes) -> None:
        self.batch([(OP_WRITE, addr, bytes(data))])

    def set_input(self, channel: int, data: int, active: bool = True) -> None:
        payload, kinds = CoopClient._encode([(OP_INPUT, channel, data)])
        # _encode always sets active; patch the active byte for the rare
        # "release input" case.
        if not active:
            payload = bytearray(payload)
            payload[2] = 0
            payload = bytes(payload)
        self._decode(self._exchange(payload), kinds)

    def stat(self) -> dict:
        return self.batch([(OP_STAT,)])[0]


def u32(data: bytes) -> int:
    return struct.unpack(">I", data)[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=47900)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("stat", help="print frame counter and RDRAM size")
    p_read = sub.add_parser("read", help="read bytes at a game address")
    p_read.add_argument("addr", type=lambda x: int(x, 0))
    p_read.add_argument("len", type=lambda x: int(x, 0))
    p_write = sub.add_parser("write", help="write hex bytes at a game address")
    p_write.add_argument("addr", type=lambda x: int(x, 0))
    p_write.add_argument("hex")

    args = parser.parse_args()
    with CoopClient(args.host, args.port) as client:
        if args.cmd == "stat":
            print(client.stat())
        elif args.cmd == "read":
            print(client.read(args.addr, args.len).hex())
        else:
            client.write(args.addr, bytes.fromhex(args.hex))
            print("ok")


if __name__ == "__main__":
    main()
