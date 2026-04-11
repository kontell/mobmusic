#!/opt/mobmusic/venv/bin/python3
"""MobMusic - Music library sync tool.

Transcodes a master FLAC library to lossy formats, syncs Jellyfin playlists,
and auto-triggers on USB device connection.

Subcommands:
    setup      - Interactive registration of a new USB device
    sync       - Sync music library to a target directory
    playlists  - Sync Jellyfin playlists to a target directory
    auto       - Full auto-sync workflow (mount, sync, playlists, unmount, email)
"""

import argparse
import configparser
import fcntl
import json
import logging
import os
import re
import shutil
import smtplib
import subprocess
import sys
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path

import ffmpeg

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
CREDS_FILE = SCRIPT_DIR / "creds.conf"
UDEV_RULES_FILE = SCRIPT_DIR / "99-mobmusic.rules"

CODECS = {
    "aac":  {"encoder": "libfdk_aac", "fallback": "aac", "ext": ".m4a"},
    "opus": {"encoder": "libopus",    "fallback": None,  "ext": ".opus"},
    "mp3":  {"encoder": "libmp3lame", "fallback": None,  "ext": ".mp3"},
}

AUDIO_EXTENSIONS = {"flac", "mp3", "m4a", "ogg", "wav", "wma", "opus", "aac"}
IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "bmp", "webp"}

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def load_server_config():
    """Load server-wide config from creds.conf."""
    if not CREDS_FILE.exists():
        print(f"Error: config file not found: {CREDS_FILE}", file=sys.stderr)
        sys.exit(1)
    cfg = configparser.ConfigParser()
    cfg.read(CREDS_FILE)
    return cfg


def load_device_config(mount_point):
    """Load per-device config from .mobmusic.conf on the mounted device."""
    conf_path = Path(mount_point) / ".mobmusic.conf"
    if not conf_path.exists():
        return None
    cfg = configparser.ConfigParser()
    cfg.read(conf_path)
    return cfg

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def setup_logging(log_file=None):
    """Configure logging to console and optionally a file."""
    handlers = [logging.StreamHandler()]
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=handlers,
    )

# ---------------------------------------------------------------------------
# FFmpeg helpers
# ---------------------------------------------------------------------------

def resolve_encoder(codec, ffmpeg_path):
    """Check if the preferred encoder is available, fall back if needed."""
    info = CODECS[codec]
    encoder = info["encoder"]
    try:
        result = subprocess.run(
            [ffmpeg_path, "-encoders"],
            capture_output=True, text=True, timeout=10,
        )
        if encoder in result.stdout:
            return encoder
    except (subprocess.SubprocessError, FileNotFoundError):
        pass

    if info["fallback"]:
        logging.warning(
            f"Encoder {encoder} not available, falling back to {info['fallback']}"
        )
        return info["fallback"]

    logging.error(f"Encoder {encoder} not available and no fallback exists.")
    sys.exit(1)


def verify_ffmpeg(ffmpeg_path):
    """Verify ffmpeg is installed at the given path."""
    try:
        subprocess.run([ffmpeg_path, "-version"], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        logging.error(f"FFmpeg not found at {ffmpeg_path}")
        sys.exit(1)

# ---------------------------------------------------------------------------
# Album / path helpers
# ---------------------------------------------------------------------------

def parse_album_name(album_name):
    """Extract year and title from album directory name.

    Handles:
        "Album Title (1965)"          -> ("1965", "Album Title")
        "Album Title (1965, UK)"      -> ("1965", "Album Title")
        "Blazing Saddles"             -> (None, "Blazing Saddles")
        "B-Sides (1)"                 -> (None, "B-Sides (1)")
    """
    match = re.match(r"(.+?)\s*\((\d{4})\b", album_name)
    if match:
        return match.group(2), match.group(1).rstrip()
    return None, album_name


def get_artist_display_name(artist_name):
    """Normalise 'Name, The' style artist names.

    Source dirs already use 'Name, The' format. This function is idempotent:
    it strips a trailing 'The' (with optional comma/space) and re-adds ', The'.
    """
    if artist_name.endswith("The") and len(artist_name) > 3:
        base = artist_name[:-3].rstrip(", ")
        if base:
            return f"{base}, The"
    return artist_name


def artist_initial(artist_name):
    """Return the single-character directory for an artist."""
    ch = artist_name[0].upper() if artist_name else "#"
    return ch if ch.isalpha() else "#"


def sanitize_exfat(name):
    """Strip trailing dots and spaces from names — exFAT silently removes them."""
    return name.rstrip(". ")


def build_target_album_path(target_root, artist_name, album_name, structure):
    """Build the target directory for an album based on the structure mode."""
    if structure == "initial":
        display = sanitize_exfat(get_artist_display_name(artist_name))
        initial = artist_initial(artist_name)
        year, title = parse_album_name(album_name)
        album_dir = sanitize_exfat(f"{year} - {title}" if year else title)
        return Path(target_root) / initial / display / album_dir
    else:  # mirror
        return Path(target_root) / sanitize_exfat(artist_name) / sanitize_exfat(album_name)


def transform_playlist_path(master_path, source_root, structure, codec_ext):
    """Convert a master library file path to the mobile library relative path.

    Returns the path relative to the target root, or None if transformation fails.
    """
    try:
        rel = os.path.relpath(master_path, source_root)
        parts = rel.split(os.sep)
        if len(parts) < 3:
            return None

        artist = parts[0]
        album = parts[1]
        rest = parts[2:]  # track file, possibly with disc subdir

        if structure == "initial":
            display = sanitize_exfat(get_artist_display_name(artist))
            initial = artist_initial(artist)
            year, title = parse_album_name(album)
            album_dir = sanitize_exfat(f"{year} - {title}" if year else title)
            mobile_parts = [initial, display, album_dir] + rest
        else:  # mirror
            mobile_parts = [sanitize_exfat(artist), sanitize_exfat(album)] + rest

        mobile_path = os.path.join(*mobile_parts)
        # Change audio extension
        mobile_path = re.sub(
            r"\.(" + "|".join(AUDIO_EXTENSIONS) + r")$",
            codec_ext,
            mobile_path,
            flags=re.IGNORECASE,
        )
        return mobile_path
    except Exception:
        return None

# ---------------------------------------------------------------------------
# Sync logic
# ---------------------------------------------------------------------------

class SyncResult:
    def __init__(self, source, target, codec, bitrate, structure):
        self.source = source
        self.target = target
        self.codec = codec
        self.bitrate = bitrate
        self.structure = structure
        self.transcoded = 0
        self.copied = 0
        self.skipped = 0
        self.deleted = 0
        self.errors = 0
        self.error_messages = []
        self.elapsed = ""


def process_file(src_file, target_file, ext, encoder, bitrate, ffmpeg_path):
    """Process a single file: copy images or transcode audio."""
    src_path = Path(src_file)
    target_path = Path(target_file)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    if target_path.exists():
        return "skipped"

    if not src_path.exists():
        logging.error(f"Source file does not exist: {src_file}")
        return "error"

    if ext in IMAGE_EXTENSIONS:
        try:
            shutil.copy2(str(src_file), str(target_file))
            logging.info(f"Copied: {target_file}")
            return "copied"
        except (shutil.Error, OSError) as e:
            logging.error(f"Failed to copy {src_file}: {e}")
            return "error"

    if ext in AUDIO_EXTENSIONS:
        try:
            stream = ffmpeg.input(str(src_file))
            stream = stream.audio  # select only audio, ignore embedded art
            stream = ffmpeg.output(
                stream,
                str(target_file),
                **{"c:a": encoder, "b:a": bitrate},
                map_metadata=0,
            )
            ffmpeg.run(stream, cmd=ffmpeg_path, overwrite_output=True, quiet=True)
            logging.info(f"Transcoded: {target_file}")
            return "transcoded"
        except ffmpeg.Error as e:
            error_msg = e.stderr.decode() if e.stderr else str(e)
            logging.error(f"Failed to transcode {src_file}: {error_msg}")
            return "error"
        except Exception as e:
            logging.error(f"Unexpected error transcoding {src_file}: {e}")
            return "error"

    logging.warning(f"Skipping unsupported file: {src_file}")
    return "skipped"


def collect_file_tasks(source_root, target_root, structure, codec_ext):
    """Walk the source library and build a list of (src, target, ext) tuples."""
    tasks = []
    source = Path(source_root)

    for artist_dir in sorted(source.iterdir()):
        if not artist_dir.is_dir():
            continue

        artist_name = artist_dir.name

        for album_dir in sorted(artist_dir.iterdir()):
            if not album_dir.is_dir():
                continue

            album_name = album_dir.name
            target_album = build_target_album_path(
                target_root, artist_name, album_name, structure
            )

            for src_file in album_dir.rglob("*"):
                if not src_file.is_file():
                    continue

                ext = src_file.suffix.lower().lstrip(".")
                if ext not in AUDIO_EXTENSIONS and ext not in IMAGE_EXTENSIONS:
                    continue

                rel_path = src_file.relative_to(album_dir)
                target_file = target_album / rel_path

                if ext in AUDIO_EXTENSIONS:
                    target_file = target_file.with_suffix(codec_ext)

                tasks.append((src_file, target_file, ext))

    return tasks


def clean_orphans(target_root, expected_targets, dry_run=False):
    """Remove files from target that aren't in the expected set. Returns count.

    expected_targets: set of str paths (from collect_file_tasks) that should exist.
    """
    deleted = 0
    target = Path(target_root)

    if not target.exists():
        return 0

    playlist_dir = str(target / "playlists")

    # Walk target and find orphans
    for f in sorted(target.rglob("*")):
        if not f.is_file():
            continue
        # Skip playlist directory
        if str(f).startswith(playlist_dir):
            continue
        ext = f.suffix.lower().lstrip(".")
        if ext not in AUDIO_EXTENSIONS and ext not in IMAGE_EXTENSIONS:
            continue
        if str(f) in expected_targets:
            continue

        # Orphan found
        if dry_run:
            logging.info(f"Would delete orphan: {f}")
        else:
            try:
                f.unlink()
                logging.info(f"Deleted orphan: {f}")
                deleted += 1
            except OSError as e:
                logging.error(f"Failed to delete orphan {f}: {e}")

    # Clean empty directories (bottom-up)
    if not dry_run:
        for dirpath, dirnames, filenames in os.walk(str(target), topdown=False):
            dp = Path(dirpath)
            if dp == target:
                continue
            if str(dp).startswith(playlist_dir):
                continue
            try:
                if not any(dp.iterdir()):
                    dp.rmdir()
                    logging.info(f"Removed empty directory: {dp}")
            except OSError:
                pass

    return deleted


def cmd_sync(args, server_cfg):
    """Execute the sync subcommand."""
    source = args.source or server_cfg.get("defaults", "source")
    target = args.target
    codec = args.codec or "aac"
    bitrate = args.bitrate or "128k"
    structure = args.structure or "initial"
    ffmpeg_path = args.ffmpeg or server_cfg.get("defaults", "ffmpeg_path")
    max_threads = args.threads or int(server_cfg.get("defaults", "max_threads", fallback="8"))
    dry_run = args.dry_run

    setup_logging(Path(target) / "mobmusic_sync.log" if not dry_run else None)

    if not Path(source).is_dir():
        logging.error(f"Source directory does not exist: {source}")
        sys.exit(1)

    Path(target).mkdir(parents=True, exist_ok=True)
    verify_ffmpeg(ffmpeg_path)
    encoder = resolve_encoder(codec, ffmpeg_path)
    codec_ext = CODECS[codec]["ext"]

    result = SyncResult(source, target, codec, bitrate, structure)
    start_time = datetime.now()

    logging.info(f"Sync: {source} -> {target}")
    logging.info(f"Codec: {codec} ({encoder}), Bitrate: {bitrate}, Structure: {structure}")

    file_tasks = collect_file_tasks(source, target, structure, codec_ext)
    total = len(file_tasks)
    logging.info(f"Found {total} files to process (threads: {max_threads})")

    # Build expected target set once — used for both skip checks and orphan cleanup
    expected_targets = {str(tgt) for _, tgt, _ in file_tasks}

    if dry_run:
        for src, tgt, ext in file_tasks:
            if tgt.exists():
                result.skipped += 1
            elif ext in AUDIO_EXTENSIONS:
                logging.info(f"Would transcode: {src} -> {tgt}")
                result.transcoded += 1
            elif ext in IMAGE_EXTENSIONS:
                logging.info(f"Would copy: {src} -> {tgt}")
                result.copied += 1
        orphans = clean_orphans(target, expected_targets, dry_run=True)
        logging.info(f"Dry run complete: {result.transcoded} to transcode, "
                     f"{result.copied} to copy, {result.skipped} already exist, "
                     f"{orphans} orphans to delete")
        return result

    processed = 0
    with ThreadPoolExecutor(max_workers=max_threads) as executor:
        futures = {
            executor.submit(
                process_file, str(src), str(tgt), ext, encoder, bitrate, ffmpeg_path
            ): (src, tgt)
            for src, tgt, ext in file_tasks
        }
        for future in as_completed(futures):
            try:
                status = future.result()
                if status == "transcoded":
                    result.transcoded += 1
                elif status == "copied":
                    result.copied += 1
                elif status == "skipped":
                    result.skipped += 1
                elif status == "error":
                    result.errors += 1
                    src, tgt = futures[future]
                    result.error_messages.append(str(src))
            except Exception as e:
                result.errors += 1
                logging.error(f"Thread error: {e}")
            processed += 1
            if processed % 100 == 0 or processed == total:
                logging.info(f"Progress: {processed}/{total}")

    # Orphan cleanup
    result.deleted = clean_orphans(target, expected_targets)

    elapsed = datetime.now() - start_time
    result.elapsed = str(elapsed).split(".")[0]
    logging.info(
        f"Sync complete in {result.elapsed}: "
        f"{result.transcoded} transcoded, {result.copied} copied, "
        f"{result.skipped} skipped, {result.deleted} orphans deleted, "
        f"{result.errors} errors"
    )
    return result

# ---------------------------------------------------------------------------
# Jellyfin API
# ---------------------------------------------------------------------------

def jellyfin_get(path, server_cfg):
    """Authenticated GET request to the Jellyfin API."""
    server = server_cfg.get("jellyfin", "server")
    api_key = server_cfg.get("jellyfin", "api_key")
    url = f"{server}{path}"
    req = urllib.request.Request(url)
    req.add_header("X-Emby-Token", api_key)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def resolve_jellyfin_user(username, server_cfg):
    """Resolve a Jellyfin username to a user ID."""
    users = jellyfin_get("/Users", server_cfg)
    for u in users:
        if u["Name"].lower() == username.lower():
            return u["Id"]
    return None


class PlaylistResult:
    def __init__(self):
        self.count = 0
        self.names = []
        self.errors = 0


def sync_playlists(target, source, structure, codec, user_id, server_cfg):
    """Fetch Jellyfin playlists and write M3U files."""
    codec_ext = CODECS[codec]["ext"]
    playlist_dir = Path(target) / "playlists"
    playlist_dir.mkdir(parents=True, exist_ok=True)

    result = PlaylistResult()

    # Get all audio playlists for this user
    try:
        data = jellyfin_get(
            f"/Users/{user_id}/Items?IncludeItemTypes=Playlist&Recursive=true",
            server_cfg,
        )
    except Exception as e:
        logging.error(f"Failed to fetch playlists: {e}")
        result.errors += 1
        return result

    playlists = [
        (item["Name"], item["Id"])
        for item in data.get("Items", [])
        if item.get("MediaType") == "Audio"
    ]

    logging.info(f"Found {len(playlists)} audio playlists")

    for name, playlist_id in playlists:
        try:
            tracks = jellyfin_get(
                f"/Playlists/{playlist_id}/Items?userId={user_id}&Fields=Path",
                server_cfg,
            )
        except Exception as e:
            logging.error(f"Failed to fetch playlist '{name}': {e}")
            result.errors += 1
            continue

        m3u_path = playlist_dir / f"{name}.m3u"
        track_count = 0

        with open(m3u_path, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
            for item in tracks.get("Items", []):
                master_path = item.get("Path")
                if not master_path:
                    continue

                mobile_rel = transform_playlist_path(
                    master_path, source, structure, codec_ext
                )
                if mobile_rel is None:
                    logging.warning(
                        f"Skipping track in '{name}': could not transform {master_path}"
                    )
                    continue

                # Make path relative to playlist directory
                abs_mobile = os.path.join(target, mobile_rel)
                rel_from_playlist = os.path.relpath(abs_mobile, str(playlist_dir))
                f.write(rel_from_playlist + "\n")
                track_count += 1

        logging.info(f"Playlist '{name}': {track_count} tracks -> {m3u_path}")
        result.count += 1
        result.names.append(f"{name} ({track_count} tracks)")

    return result


def cmd_playlists(args, server_cfg):
    """Execute the playlists subcommand."""
    target = args.target
    source = args.source or server_cfg.get("defaults", "source")
    codec = args.codec or "aac"
    structure = args.structure or "initial"
    username = args.user

    setup_logging()

    if not username:
        logging.error("--user is required for the playlists subcommand")
        sys.exit(1)

    user_id = resolve_jellyfin_user(username, server_cfg)
    if not user_id:
        logging.error(f"Jellyfin user '{username}' not found")
        sys.exit(1)

    logging.info(f"Resolved Jellyfin user '{username}' -> {user_id}")

    result = sync_playlists(target, source, structure, codec, user_id, server_cfg)
    logging.info(f"Playlists complete: {result.count} synced, {result.errors} errors")
    return result

# ---------------------------------------------------------------------------
# Email notification
# ---------------------------------------------------------------------------

def send_notification(sync_result, playlist_result, device_email, server_cfg):
    """Send an email summary of the sync."""
    cfg = server_cfg
    smtp_host = cfg.get("smtp", "host")
    smtp_port = int(cfg.get("smtp", "port"))
    smtp_user = cfg.get("smtp", "user", fallback="")
    smtp_pass = cfg.get("smtp", "pass", fallback="")
    from_addr = cfg.get("smtp", "from")

    subject = (
        f"MobMusic sync complete - "
        f"{sync_result.transcoded} transcoded, {sync_result.errors} errors"
    )

    body = f"""MobMusic sync completed at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

The drive has been unmounted and is safe to unplug.

Library sync:
  Source: {sync_result.source}
  Target: {sync_result.target}
  Codec: {sync_result.codec} @ {sync_result.bitrate}
  Structure: {sync_result.structure}
  Files transcoded: {sync_result.transcoded}
  Files copied (images): {sync_result.copied}
  Files skipped (existing): {sync_result.skipped}
  Orphans deleted: {sync_result.deleted}
  Errors: {sync_result.errors}
  Elapsed: {sync_result.elapsed}

Playlists synced: {playlist_result.count}
"""
    for name in playlist_result.names:
        body += f"  - {name}\n"

    if sync_result.error_messages:
        body += "\nFailed files:\n"
        for msg in sync_result.error_messages[:50]:
            body += f"  - {msg}\n"
        if len(sync_result.error_messages) > 50:
            body += f"  ... and {len(sync_result.error_messages) - 50} more\n"

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = device_email

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            if smtp_user:
                server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        logging.info(f"Notification sent to {device_email}")
    except Exception as e:
        logging.error(f"Failed to send notification: {e}")

# ---------------------------------------------------------------------------
# Setup subcommand
# ---------------------------------------------------------------------------

def list_usb_block_devices():
    """List USB block devices with their details."""
    try:
        result = subprocess.run(
            ["lsblk", "-o", "NAME,SIZE,FSTYPE,LABEL,UUID,MOUNTPOINT,TRAN", "-J"],
            capture_output=True, text=True, check=True,
        )
        data = json.loads(result.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError) as e:
        logging.error(f"Failed to list block devices: {e}")
        return []

    usb_devices = []
    for dev in data.get("blockdevices", []):
        if dev.get("tran") != "usb":
            continue
        # Include partitions of USB devices
        for child in dev.get("children", []):
            if child.get("fstype"):
                usb_devices.append({
                    "name": f"/dev/{child['name']}",
                    "size": child.get("size", "?"),
                    "fstype": child.get("fstype", "?"),
                    "label": child.get("label", ""),
                    "uuid": child.get("uuid", ""),
                    "mountpoint": child.get("mountpoint"),
                })
    return usb_devices


def update_udev_rules(uuid):
    """Add a udev rule for the given filesystem UUID."""
    rule_line = (
        f'ACTION=="add", SUBSYSTEM=="block", ENV{{ID_FS_UUID}}=="{uuid}", '
        f'TAG+="systemd", ENV{{SYSTEMD_WANTS}}="mobmusic-sync@{uuid}.service"'
    )

    existing_lines = []
    if UDEV_RULES_FILE.exists():
        existing_lines = UDEV_RULES_FILE.read_text().strip().splitlines()

    # Check if rule already exists for this UUID
    for line in existing_lines:
        if uuid in line:
            logging.info(f"udev rule for UUID {uuid} already exists")
            return

    existing_lines.append(rule_line)
    UDEV_RULES_FILE.write_text("\n".join(existing_lines) + "\n")
    logging.info(f"Added udev rule for UUID {uuid}")


def cmd_setup(args, server_cfg):
    """Interactive device setup."""
    setup_logging()

    print("\n=== MobMusic Device Setup ===\n")

    # List USB devices
    devices = list_usb_block_devices()
    if not devices:
        print("No USB storage devices found.")
        sys.exit(1)

    print("Available USB devices:")
    for i, dev in enumerate(devices, 1):
        mp = dev["mountpoint"] or "not mounted"
        label = dev["label"] or "no label"
        print(f"  {i}. {dev['name']} - {dev['size']} {dev['fstype']} "
              f"[{label}] (UUID: {dev['uuid']}, {mp})")

    # Select device
    while True:
        try:
            choice = int(input(f"\nSelect device [1-{len(devices)}]: "))
            if 1 <= choice <= len(devices):
                break
        except (ValueError, EOFError):
            pass
        print("Invalid selection.")

    device = devices[choice - 1]
    block_dev = device["name"]
    uuid = device["uuid"]

    if not uuid:
        print("Error: device has no filesystem UUID. Format it first.")
        sys.exit(1)

    # Mount if needed
    mount_point = device["mountpoint"]
    did_mount = False
    if not mount_point:
        print(f"Mounting {block_dev}...")
        result = subprocess.run(
            ["udisksctl", "mount", "--block-device", block_dev, "--no-user-interaction"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"Failed to mount: {result.stderr}")
            sys.exit(1)
        # Parse mount point from output like "Mounted /dev/sde1 at /run/media/conor/momus."
        mount_point = result.stdout.strip().split(" at ")[-1].rstrip(".")
        did_mount = True
        print(f"Mounted at {mount_point}")

    try:
        # Query Jellyfin username
        print()
        jf_username = input("Jellyfin username: ").strip()
        user_id = resolve_jellyfin_user(jf_username, server_cfg)
        if not user_id:
            print(f"Error: Jellyfin user '{jf_username}' not found.")
            sys.exit(1)
        print(f"  -> Resolved to user ID: {user_id}")

        # Email
        email = input("Email address for notifications: ").strip()

        # Codec
        print("\nCodec options: aac, opus, mp3")
        codec = input("Codec [aac]: ").strip().lower() or "aac"
        if codec not in CODECS:
            print(f"Invalid codec: {codec}")
            sys.exit(1)

        # Bitrate
        bitrate = input("Bitrate [128k]: ").strip() or "128k"

        # Structure
        print("\nStructure options:")
        print("  initial - Artist Initial / Artist / Year - Album")
        print("  mirror  - Artist / Album (preserves master layout)")
        structure = input("Structure [initial]: ").strip().lower() or "initial"
        if structure not in ("initial", "mirror"):
            print(f"Invalid structure: {structure}")
            sys.exit(1)

        # Music directory on device
        music_dir = input("Music directory on device (relative to root) [.]: ").strip() or "."

        # Write device config
        dev_cfg = configparser.ConfigParser()
        dev_cfg["device"] = {
            "jellyfin_user": jf_username,
            "jellyfin_user_id": user_id,
            "email": email,
            "codec": codec,
            "bitrate": bitrate,
            "structure": structure,
            "music_dir": music_dir,
        }

        conf_path = Path(mount_point) / ".mobmusic.conf"
        with open(conf_path, "w") as f:
            dev_cfg.write(f)
        print(f"\nDevice config written to {conf_path}")

        # Update udev rules
        update_udev_rules(uuid)

        print(f"\nSetup complete for {block_dev} (UUID: {uuid})")
        print(f"  Jellyfin user: {jf_username}")
        print(f"  Email: {email}")
        print(f"  Codec: {codec} @ {bitrate}")
        print(f"  Structure: {structure}")
        print(f"  Music dir: {music_dir}")

        # Check what still needs to be installed and only show relevant steps
        udev_link = Path("/etc/udev/rules.d/99-mobmusic.rules")
        service_link = Path("/etc/systemd/system/mobmusic-sync@.service")
        pending = []
        if not udev_link.exists():
            pending.append(f"  sudo ln -sf {UDEV_RULES_FILE} {udev_link}")
        if not service_link.exists():
            pending.append(f"  sudo ln -sf {SCRIPT_DIR}/mobmusic-sync@.service {service_link}")
            pending.append("  sudo systemctl daemon-reload")

        if pending:
            print("\nFirst-time setup — run these once:")
            for cmd in pending:
                print(cmd)

        # udev always needs a reload when a new device is added
        print("\nReload udev rules to activate this device:")
        print("  sudo udevadm control --reload-rules")

    finally:
        if did_mount:
            print(f"Unmounting {block_dev}...")
            subprocess.run(
                ["udisksctl", "unmount", "--block-device", block_dev,
                 "--no-user-interaction"],
                capture_output=True, text=True,
            )

# ---------------------------------------------------------------------------
# Auto subcommand
# ---------------------------------------------------------------------------

def cmd_auto(args, server_cfg):
    """Full auto-sync: mount, sync, playlists, unmount, email."""
    block_device = args.device
    setup_logging()

    # Derive a lock name from the device path
    lock_name = block_device.replace("/", "_")
    lock_path = f"/run/lock/mobmusic-{lock_name}.lock"

    try:
        lock_fd = open(lock_path, "w")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        logging.info("Another sync is already running for this device. Exiting.")
        sys.exit(0)

    mount_point = None
    did_mount = False

    try:
        # Check if already mounted
        result = subprocess.run(
            ["lsblk", "-o", "MOUNTPOINT", "-n", block_device],
            capture_output=True, text=True,
        )
        existing_mp = result.stdout.strip()

        if existing_mp:
            mount_point = existing_mp
            logging.info(f"Device already mounted at {mount_point}")
        else:
            # Mount the device
            logging.info(f"Mounting {block_device}...")
            result = subprocess.run(
                ["udisksctl", "mount", "--block-device", block_device,
                 "--no-user-interaction"],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                logging.error(f"Failed to mount {block_device}: {result.stderr}")
                sys.exit(1)
            mount_point = result.stdout.strip().split(" at ")[-1].rstrip(".")
            did_mount = True
            logging.info(f"Mounted at {mount_point}")

        # Read device config
        dev_cfg = load_device_config(mount_point)
        if not dev_cfg:
            logging.error(
                f"No .mobmusic.conf found on {mount_point}. "
                f"Run 'mobmusic.py setup' first."
            )
            sys.exit(1)

        device = dev_cfg["device"]
        codec = device.get("codec", "aac")
        bitrate = device.get("bitrate", "128k")
        structure = device.get("structure", "initial")
        music_dir = device.get("music_dir", ".")
        user_id = device.get("jellyfin_user_id")
        device_email = device.get("email")

        source = server_cfg.get("defaults", "source")
        ffmpeg_path = server_cfg.get("defaults", "ffmpeg_path")
        max_threads = int(server_cfg.get("defaults", "max_threads", fallback="8"))

        target = str(Path(mount_point) / music_dir)
        Path(target).mkdir(parents=True, exist_ok=True)

        setup_logging(Path(target) / "mobmusic_sync.log")
        verify_ffmpeg(ffmpeg_path)
        encoder = resolve_encoder(codec, ffmpeg_path)
        codec_ext = CODECS[codec]["ext"]

        # Sync library
        logging.info("=== Starting library sync ===")
        sync_result = SyncResult(source, target, codec, bitrate, structure)
        start_time = datetime.now()

        file_tasks = collect_file_tasks(source, target, structure, codec_ext)
        total = len(file_tasks)
        logging.info(f"Found {total} files to process")

        # Build expected set once — shared between sync and orphan cleanup
        expected_targets = {str(tgt) for _, tgt, _ in file_tasks}

        processed = 0
        with ThreadPoolExecutor(max_workers=max_threads) as executor:
            futures = {
                executor.submit(
                    process_file, str(src), str(tgt), ext,
                    encoder, bitrate, ffmpeg_path
                ): (src, tgt)
                for src, tgt, ext in file_tasks
            }
            for future in as_completed(futures):
                try:
                    status = future.result()
                    if status == "transcoded":
                        sync_result.transcoded += 1
                    elif status == "copied":
                        sync_result.copied += 1
                    elif status == "skipped":
                        sync_result.skipped += 1
                    elif status == "error":
                        sync_result.errors += 1
                        src, tgt = futures[future]
                        sync_result.error_messages.append(str(src))
                except Exception as e:
                    sync_result.errors += 1
                    logging.error(f"Thread error: {e}")
                processed += 1
                if processed % 100 == 0 or processed == total:
                    logging.info(f"Progress: {processed}/{total}")

        # Orphan cleanup
        logging.info("=== Cleaning orphans ===")
        sync_result.deleted = clean_orphans(target, expected_targets)

        elapsed = datetime.now() - start_time
        sync_result.elapsed = str(elapsed).split(".")[0]
        logging.info(f"Library sync complete in {sync_result.elapsed}")

        # Sync playlists
        logging.info("=== Syncing playlists ===")
        playlist_result = sync_playlists(
            target, source, structure, codec, user_id, server_cfg
        )
        logging.info(f"Playlists complete: {playlist_result.count} synced")

    finally:
        # Always unmount if we mounted it
        if did_mount and mount_point:
            logging.info(f"Unmounting {block_device}...")
            # sync filesystem buffers first
            subprocess.run(["sync"], capture_output=True)
            result = subprocess.run(
                ["udisksctl", "unmount", "--block-device", block_device,
                 "--no-user-interaction"],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                logging.info("Unmounted successfully")
            else:
                logging.error(f"Failed to unmount: {result.stderr}")

        # Release lock
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()
            os.unlink(lock_path)
        except OSError:
            pass

    # Send email notification (after unmount)
    if device_email:
        send_notification(sync_result, playlist_result, device_email, server_cfg)

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="mobmusic",
        description="Music library sync tool - transcode, playlist sync, USB auto-sync",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # -- setup --
    sub.add_parser("setup", help="Interactive setup for a new USB device")

    # -- sync --
    p_sync = sub.add_parser("sync", help="Sync music library to a target directory")
    p_sync.add_argument("target", help="Target directory")
    p_sync.add_argument("--source", help="Source music library path")
    p_sync.add_argument("--codec", choices=["aac", "opus", "mp3"],
                        help="Audio codec (default: aac)")
    p_sync.add_argument("--bitrate", help="Audio bitrate, e.g. 128k (default: 128k)")
    p_sync.add_argument("--structure", choices=["initial", "mirror"],
                        help="Directory structure (default: initial)")
    p_sync.add_argument("--ffmpeg", help="Path to ffmpeg binary")
    p_sync.add_argument("--threads", type=int, help="Max transcoding threads")
    p_sync.add_argument("--dry-run", action="store_true",
                        help="Show what would be done without doing it")

    # -- playlists --
    p_pl = sub.add_parser("playlists", help="Sync Jellyfin playlists to a target directory")
    p_pl.add_argument("target", help="Target directory")
    p_pl.add_argument("--source", help="Source music library path")
    p_pl.add_argument("--codec", choices=["aac", "opus", "mp3"],
                        help="Audio codec (default: aac)")
    p_pl.add_argument("--structure", choices=["initial", "mirror"],
                        help="Directory structure (default: initial)")
    p_pl.add_argument("--user", help="Jellyfin username (required)")

    # -- auto --
    p_auto = sub.add_parser("auto", help="Auto-sync: mount, sync, playlists, unmount, email")
    p_auto.add_argument("device", help="Block device path, e.g. /dev/disk/by-uuid/XXXX")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    server_cfg = load_server_config()

    if args.command == "setup":
        cmd_setup(args, server_cfg)
    elif args.command == "sync":
        cmd_sync(args, server_cfg)
    elif args.command == "playlists":
        cmd_playlists(args, server_cfg)
    elif args.command == "auto":
        cmd_auto(args, server_cfg)


if __name__ == "__main__":
    main()
