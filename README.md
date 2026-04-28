# MobMusic

Transcodes a music library to lossy formats for portable devices. Syncs Jellyfin playlists as M3U files. Auto-triggers when a registered USB drive is plugged into a headless Debian server.

## How it works

```
Music library ──> transcode (aac/opus/mp3) ──> USB drive
Jellyfin playlists ──> M3U files with rewritten paths ──> USB drive
```

Source library can contain FLAC, ALAC, APE, WAV, MP3, AAC, OGG, WMA, or Opus files — all are transcoded to the target codec.

Register a USB device once with `setup`. After that, plugging it in triggers a full sync automatically via udev + systemd: mount, transcode new/changed files (mtime-based — re-transcodes when source is updated), sync playlists, clean orphans, unmount, email notification.

Source library structure is `Artist/Album/tracks`. Album directories can use any of these year formats: `Album (1965)`, `Album [1965]`, `1965 - Album`, `[1965] Album`, `Album - 1965`, or no year at all. Artists prefixed with "The" (e.g., `The Beatles`) are automatically normalized to `Beatles, The` in the target.

## Requirements

- Locally mounted music library (any format ffmpeg can decode)
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

**Server** (`creds.conf`): Jellyfin URL/API key, SMTP credentials, default source path and FFmpeg location. Copy the template and fill in your values:

```bash
cp creds.conf.example creds.conf
chmod 600 creds.conf
# Edit creds.conf with your settings
```

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
