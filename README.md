# MobMusic

Transcodes a FLAC library to lossy formats for portable devices. Syncs Jellyfin playlists as M3U files. Auto-triggers when a registered USB drive is plugged into a headless Debian server.

## How it works

```
Master FLAC library ──> transcode (aac/opus/mp3) ──> USB drive
Jellyfin playlists  ──> M3U files with rewritten paths ──> USB drive
```

Register a USB device once with `setup`. After that, plugging it in triggers a full sync automatically via udev + systemd: mount, transcode new/changed files, sync playlists, clean orphans, unmount, email notification.

## Requirements

- Python 3.13+ with `ffmpeg-python`
- FFmpeg with `libfdk_aac` (falls back to built-in `aac` if unavailable)
- Jellyfin server (for playlist sync)
- Linux with udev + systemd (for auto-sync)

## Usage

```bash
# Register a USB device interactively
./mobmusic.py setup

# Manual sync
./mobmusic.py sync /path/to/target --codec opus --bitrate 90k

# Dry run
./mobmusic.py sync /path/to/target --dry-run

# Sync playlists only
./mobmusic.py playlists /path/to/target --user conor

# Watch auto-sync logs
journalctl -t mobmusic -f
```

## Configuration

**Server** (`creds.conf`): Jellyfin URL/API key, SMTP credentials, default source path and FFmpeg location.

**Per-device** (`.mobmusic.conf` on USB root): Jellyfin user, email, codec, bitrate, directory structure. Written by `setup`, travels with the device.

## Codecs

| Option | Encoder | Extension |
|--------|---------|-----------|
| aac | libfdk_aac | .m4a |
| opus | libopus | .opus |
| mp3 | libmp3lame | .mp3 |

## Directory structures

- **initial** — `A/Artist Name/2024 - Album Title/track.m4a`
- **mirror** — preserves source layout as-is

## System integration

The `setup` command writes udev rules and guides you through symlinking the systemd service and polkit rule. After first-time setup:

```bash
sudo ln -sf /opt/mobmusic/99-mobmusic.rules /etc/udev/rules.d/
sudo ln -sf /opt/mobmusic/mobmusic-sync@.service /etc/systemd/system/
sudo cp /opt/mobmusic/10-mobmusic-mount.rules /etc/polkit-1/rules.d/
sudo udevadm control --reload-rules
sudo systemctl daemon-reload
```
