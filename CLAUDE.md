# MobMusic

Music library sync tool for a headless Debian 13 server. Transcodes a master FLAC library to lossy formats, syncs Jellyfin playlists via API, and auto-triggers when a registered USB drive is plugged in.

## Architecture

Single-file Python CLI (`mobmusic.py`) with four subcommands: `setup`, `sync`, `playlists`, `auto`.

### Config layers

- **Server-wide** (`creds.conf`): Jellyfin server URL/API key, SMTP credentials, default settings. Lives on the server, `chmod 600`, gitignored.
- **Per-device** (`.mobmusic.conf` on USB root): Jellyfin username, email recipient, codec, bitrate, structure, music directory. Written by `setup`, travels with the device.

### USB auto-sync flow

1. udev rule (`99-mobmusic.rules`) matches registered devices by `ID_FS_UUID`
2. Triggers systemd template service `mobmusic-sync@{UUID}.service`
3. Service runs `mobmusic.py auto /dev/disk/by-uuid/{UUID}`
4. Auto subcommand: mount (udisksctl) -> sync library -> sync playlists -> orphan cleanup -> unmount -> email notification

### Key design decisions

- **exFAT sanitization**: Directory names are stripped of trailing dots/spaces before path construction. exFAT silently strips these, causing path mismatches between constructed and on-disk paths if not handled.
- **Album year parsing**: Regex `r'(.+?)\s*\((\d{4})\b'` handles extra text after year (e.g., "Album (1965, UK Edition)"). Albums without years are included as-is rather than skipped.
- **Embedded art**: ffmpeg streams use `.audio` selector to ignore embedded cover art (video streams in FLAC), which would otherwise fail transcoding to m4a containers.
- **Orphan cleanup**: The expected target path set is built once from `collect_file_tasks()` and shared between sync and orphan cleanup. Never recomputed independently -- avoids path representation mismatches.
- **Polkit**: `10-mobmusic-mount.rules` grants the `conor` user udisks2 mount/unmount permission without a desktop session (required for headless operation).

## File layout

```
mobmusic.py              Main CLI (all logic in one file)
creds.conf               Server credentials (gitignored)
99-mobmusic.rules         udev rules (managed by setup subcommand)
mobmusic-sync@.service    systemd template unit
10-mobmusic-mount.rules   polkit rule for udisks2
```

## Dependencies

Python 3.13 venv at `/opt/mobmusic/venv`. Only non-stdlib package is `ffmpeg-python`. Everything else uses stdlib: `urllib.request` (Jellyfin API), `configparser` (config), `smtplib` (email), `concurrent.futures` (parallel transcoding).

FFmpeg: `/usr/lib/jellyfin-ffmpeg/ffmpeg` (has `libfdk_aac`). Falls back to system ffmpeg's built-in `aac` encoder if unavailable.

## System integration

Symlinks installed to system paths (requires sudo):
- `/etc/udev/rules.d/99-mobmusic.rules` -> `./99-mobmusic.rules`
- `/etc/systemd/system/mobmusic-sync@.service` -> `./mobmusic-sync@.service`
- `/etc/polkit-1/rules.d/10-mobmusic-mount.rules` (copied, not symlinked)

After changes to udev rules: `sudo udevadm control --reload-rules`
After changes to service file: `sudo systemctl daemon-reload`

## Common commands

```bash
# Register a new USB device interactively
./mobmusic.py setup

# Manual sync to any directory
./mobmusic.py sync /path/to/target --codec opus --bitrate 90k --structure initial

# Dry run (show what would happen)
./mobmusic.py sync /path/to/target --dry-run

# Sync Jellyfin playlists only
./mobmusic.py playlists /path/to/target --user conor

# Watch auto-sync logs
journalctl -t mobmusic -f
```

## Master library

Source: `/media/bluecon/music` (~293 artists, mostly FLAC).
Structure: `Artist Name/Album Title (Year)/Track.flac`
Artists with "The" stored as: `Name, The` (e.g., `Beatles, The`).

## Codec options

| Option | Encoder      | Extension |
|--------|-------------|-----------|
| aac    | libfdk_aac  | .m4a      |
| opus   | libopus     | .opus     |
| mp3    | libmp3lame  | .mp3      |
