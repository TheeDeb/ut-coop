# ut-coop

Two-player online co-op for **Ultimate Trial**, a total-conversion ROM hack of
*The Legend of Zelda: Ocarina of Time* (USA 1.0).

This is a prototype. It runs two emulator instances, exchanges player state over UDP,
and renders the other player as a second Link (a puppet actor) driven by the received
position, rotation, appearance and skeleton pose. It also gates/hides the puppet when
the two players are in different scenes and makes the joining side follow the host's
scene changes.

## Requirements

- Your own Ocarina of Time (USA 1.0) dump, md5 `5bd1fe107bf8106b2ab6650abecd54d6`.
- The official Ultimate Trial v1.2.3 BPS patch, applied to that dump. This repo contains
  no ROMs, patches or game data.
- The matching **gopher64 fork** with the co-op memory bridge (`--coop-port`), built from
  <https://github.com/TheeDeb/gopher64> (GPL-3.0). A prebuilt Windows binary is attached
  to that repo's releases.
- A save in File 1. Create one once in the emulator (name entry), because the client's
  boot automation assumes it.

## Quick start

Download **`ut-coop-bundle-v0.1.1.zip`** from the releases page. It contains
`ut-coop.exe`, `gopher64.exe` and `START-HERE.txt`. Extract it into a folder, put your
patched `ultimate_trial.z64` beside them, create a File 1 save once, then run:

```
ut-coop.exe
```

With no flags it starts a setup wizard: choose host or join, it prints your IPs,
generates or asks for a session id, finds `gopher64.exe` and `ultimate_trial.z64` next
to itself, launches the emulator, boots the game and starts syncing. Share the printed
IP and session with your friend.

Manual host/join (advanced):

```
ut-coop.exe --emulator gopher64.exe --rom ultimate_trial.z64 ^
  --coop-port 47900 --bind 0.0.0.0:49001 --peer CLIENT_IP:49002 --session 0x1234
```

```
ut-coop.exe --emulator gopher64.exe --rom ultimate_trial.z64 ^
  --coop-port 47900 --bind 0.0.0.0:49002 --peer HOST_IP:49001 --session 0x1234
```

Both players must use the same nonzero `--session`; snapshots with another session are
dropped. Forward the UDP ports (49001/49002 by default) or use a VPN (Tailscale, ZeroTier,
Radmin VPN).

The client launches the emulator, waits for the bridge, taps through the menu and intro
once, then starts syncing. Use `--no-launch` to attach to an emulator you started
yourself, and `--no-boot` if the game is already in gameplay.

## Options

| Option | Meaning |
|---|---|
| *(no arguments)* | setup wizard: host/join, IP, session, file detection |
| `--wizard` | force the setup wizard even if `--peer` is set |
| `--emulator PATH` | gopher64 fork executable |
| `--rom PATH` | patched Ultimate Trial ROM |
| `--coop-port N` | local bridge port opened by the fork |
| `--bind ADDR:PORT` | local UDP address |
| `--peer ADDR:PORT` | peer UDP address |
| `--session N` | shared session id (nonzero recommended) |
| `--rate HZ` | snapshot rate, default 30 |
| `--no-puppet` | do not render the remote Link |
| `--no-follow` | do not follow the peer's scene changes |
| `--no-launch` | attach to a running emulator |
| `--no-boot` | skip menu/intro automation |
| `--seconds N` | exit after N seconds (testing) |

## Status and known issues

- Same-area co-op works, and the joining side follows the host between scenes 41 and 42
  in testing; broader trial-to-trial travel is still lightly tested.
- The puppet's animation is stepped at the snapshot rate (no interpolation).
- Scene changes are followed by replaying the host's entrance index; if a load fails,
  the two sides stay gated until the scene ids match again.
- The boot automation taps through the intro and assumes a File 1 save; it is slow
  (about a minute) and may need retries on a slow machine.
- No NAT traversal: use port forwarding or a VPN.
- Python-packaged prototype (PyInstaller); a Rust client is future work.

## Build

```
python -m pip install -r requirements.txt
powershell -ExecutionPolicy Bypass -File build.ps1
```

Output: `dist/ut-coop.exe`.

## License

GPL-3.0. The client and the gopher64 fork are separate GPL-3.0 derivatives. No game
assets are included or distributed.
