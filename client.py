"""ut-coop: Ultimate Trial two-player online co-op client (prototype).

Talks to the gopher64 fork's co-op bridge (--coop-port), launches/boots the game
if asked, and exchanges player snapshots with a peer over UDP.

Run it with no arguments for a setup wizard (host/join, IP, session), or use the
flags directly:

Host:
  ut-coop --emulator gopher64.exe --rom ultimate_trial.z64 --coop-port 47900 \
          --bind 0.0.0.0:49001 --peer CLIENT_IP:49002 --session 0x1234

Join:
  ut-coop --emulator gopher64.exe --rom ultimate_trial.z64 --coop-port 47900 \
          --bind 0.0.0.0:49002 --peer HOST_IP:49001 --session 0x1234

Both players need a save in File 1 (create one once in the emulator). Use
--no-launch to attach to an emulator that is already running, and --no-boot if
the game is already in gameplay.
"""

from __future__ import annotations

import argparse
import os
import secrets
import socket
import subprocess
import sys
import threading
import time

from utcoop import boot_to_gameplay as boot
from utcoop import puppet as puppet_mod
from utcoop import sync_proto


def parse_addr(text: str) -> tuple[str, int]:
    host, port = text.rsplit(":", 1)
    return host, int(port)


def app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def default_path(name: str) -> str:
    path = os.path.join(app_dir(), name)
    return path if os.path.exists(path) else ""


def local_ips() -> list[str]:
    found: list[str] = []
    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            found += [line.strip() for line in result.stdout.splitlines() if line.strip()]
    except Exception:
        pass
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        found.append(sock.getsockname()[0])
        sock.close()
    except Exception:
        pass
    unique = []
    for ip in found:
        if ip not in unique and not ip.startswith("127."):
            unique.append(ip)
    return unique


def wait_bridge(port: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def run_wizard(args) -> argparse.Namespace:
    print("=== ut-coop setup ===")
    ips = local_ips()
    if ips:
        print("Your addresses:", ", ".join(ips))
    print("(For Radmin VPN, use the 26.x address shown in Radmin.)")

    while True:
        role = input("Host or join? [h/j]: ").strip().lower()
        if role.startswith("h") or role.startswith("j"):
            break
    host = role.startswith("h")
    bind_port = 49001 if host else 49002
    peer_port = 49002 if host else 49001

    if not args.no_launch:
        emulator = args.emulator or default_path("gopher64.exe")
        rom = args.rom or default_path("ultimate_trial.z64")
        if not emulator or not os.path.exists(emulator):
            emulator = input("Path to gopher64.exe: ").strip().strip('"')
        if not rom or not os.path.exists(rom):
            rom = input("Path to ultimate_trial.z64: ").strip().strip('"')
        args.emulator = emulator
        args.rom = rom

    if host:
        session = args.session or (secrets.randbits(16) | 1)
        print(f"Session: {session:#06x}  (share this with your friend)")
    else:
        while True:
            text = input("Session from the host (e.g. 0x1a2b): ").strip()
            if text:
                session = int(text, 0)
                break
        if args.session:
            session = args.session
    args.session = session

    if host:
        friend = input("Friend's IP: ").strip()
        args.bind = f"0.0.0.0:{bind_port}"
        args.peer = f"{friend}:{peer_port}"
        if ips:
            print(f"Tell your friend to join {ips[0]}:{bind_port} with session {session:#06x}")
    else:
        host_ip = input("Host's IP: ").strip()
        args.bind = f"0.0.0.0:{bind_port}"
        args.peer = f"{host_ip}:{peer_port}"

    print(f"bind {args.bind} -> peer {args.peer}, session {args.session:#06x}")
    print("(You need a File 1 save; the client will tap through the menu and intro.)")
    return args


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--emulator", help="path to the gopher64 fork executable")
    parser.add_argument("--rom", help="path to the player's patched Ultimate Trial ROM")
    parser.add_argument("--coop-port", type=int, default=47900)
    parser.add_argument("--bind", default="0.0.0.0:49001")
    parser.add_argument("--peer", default=None, help="peer address HOST:PORT")
    parser.add_argument("--rate", type=float, default=30.0)
    parser.add_argument("--session", type=lambda x: int(x, 0), default=0, help="session id shared by both peers")
    parser.add_argument("--wizard", action="store_true", help="ask for host/join, IP and session")
    parser.add_argument("--no-puppet", action="store_true")
    parser.add_argument("--no-follow", action="store_true")
    parser.add_argument("--no-launch", action="store_true", help="attach to a running emulator")
    parser.add_argument("--no-boot", action="store_true", help="skip menu automation")
    parser.add_argument("--seconds", type=float, default=0, help="0 = run until Ctrl+C")
    args = parser.parse_args()

    if args.wizard or not args.peer:
        args = run_wizard(args)

    emulator = None
    try:
        if not args.no_launch:
            if not args.emulator or not args.rom:
                print("--emulator and --rom are required unless --no-launch is set", file=sys.stderr)
                return 2
            print(f"launching {args.emulator}")
            emulator = subprocess.Popen(
                [args.emulator, args.rom, "--coop-port", str(args.coop_port)]
            )
            if not wait_bridge(args.coop_port, 60):
                print("emulator bridge did not open", file=sys.stderr)
                return 1
        if not args.no_boot:
            print("booting the game to gameplay (this takes about a minute)")
            client = sync_proto.connect_emulator(args.coop_port, timeout=60)
            try:
                time.sleep(12)
                ok = boot.drive_to_gameplay(client)
                print("boot controllable:", ok)
            finally:
                client.close()

        node = sync_proto.SyncNode(
            args.coop_port,
            parse_addr(args.bind),
            parse_addr(args.peer),
            rate=args.rate,
            puppet=None if args.no_puppet else puppet_mod.PUPPET,
            auto_follow=not args.no_follow,
            session=args.session,
        )
        print(
            f"connected: scene {node.scene_id}, local bridge {args.coop_port}, "
            f"udp {args.bind} -> {args.peer}, session {args.session:#06x}"
        )
        threading.Thread(target=node.run, daemon=True).start()

        deadline = time.time() + args.seconds if args.seconds else None
        try:
            while deadline is None or time.time() < deadline:
                time.sleep(1.0)
                remote = node.remote
                print(
                    f"scene {node.scene_id} follows {node.follow_count} "
                    f"puppet {node.puppet_active} recv {node.received} gaps {node.gaps} "
                    f"remote {tuple(round(v, 1) for v in remote['pos']) if remote else None}"
                )
                sys.stdout.flush()
        except KeyboardInterrupt:
            pass
        node.close()
        return 0
    finally:
        if emulator is not None and emulator.poll() is None:
            emulator.terminate()
            try:
                emulator.wait(timeout=10)
            except subprocess.TimeoutExpired:
                emulator.kill()


if __name__ == "__main__":
    raise SystemExit(main())
