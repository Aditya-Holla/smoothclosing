"""
Photo sourcing for the GBP campaign.

For a given post, this fetches its assigned photo from the (public) Google Drive
folder, normalizes it to JPEG, geotags + keyword-embeds it, stages it at a
public URL, and hands that URL back to the scheduler to attach to the post.

Google downloads and rehosts the image the moment the post publishes, so the
staged URL only needs to live for a few seconds — no permanent hosting.

The post→photo assignment is fixed in gbp_photos.json (post_number -> file_id),
built once from the folder listing. Nothing here needs the Drive connector or a
service account: the folder is shared "anyone with the link", so the files are
fetched over plain HTTPS.

Every failure is swallowed and returns None — a photo problem must never block a
post from going out (it just goes text-only that day).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import requests

import gbp_image

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR)).resolve()
# Mapping ships with the code; allow a DATA_DIR override if ever needed.
MAP_PATH = (DATA_DIR / "gbp_photos.json") if (DATA_DIR / "gbp_photos.json").exists() \
    else (BASE_DIR / "gbp_photos.json")

DRIVE_DOWNLOAD = "https://drive.usercontent.google.com/download"
# Staging hosts tried in order — first one that returns a fetchable URL wins.
STAGING_HOSTS = ("catbox", "0x0")


def _load_map() -> dict:
    try:
        return json.loads(MAP_PATH.read_text())
    except Exception as e:
        logger.warning("No photo map (%s): %s", MAP_PATH, e)
        return {}


def download_drive_image(file_id: str, timeout: int = 60) -> bytes | None:
    """Download a publicly-shared Drive file's bytes."""
    try:
        r = requests.get(DRIVE_DOWNLOAD,
                         params={"id": file_id, "export": "download"},
                         timeout=timeout)
        r.raise_for_status()
        if not r.content:
            return None
        return r.content
    except Exception as e:
        logger.warning("Drive download failed for %s: %s", file_id, e)
        return None


def stage_public_image(jpeg_bytes: bytes, timeout: int = 90) -> str | None:
    """Upload JPEG bytes to a public host; return a fetchable URL or None."""
    for host in STAGING_HOSTS:
        try:
            if host == "catbox":
                r = requests.post(
                    "https://catbox.moe/user/api.php",
                    data={"reqtype": "fileupload"},
                    files={"fileToUpload": ("photo.jpg", jpeg_bytes, "image/jpeg")},
                    timeout=timeout,
                )
                r.raise_for_status()
                url = r.text.strip()
            elif host == "0x0":
                r = requests.post(
                    "https://0x0.st",
                    files={"file": ("photo.jpg", jpeg_bytes, "image/jpeg")},
                    headers={"User-Agent": "SmoothClosing/1.0"},
                    timeout=timeout,
                )
                r.raise_for_status()
                url = r.text.strip()
            else:
                continue
            if url.startswith("http"):
                return url
            logger.warning("%s returned unexpected body: %s", host, url[:120])
        except Exception as e:
            logger.warning("Staging via %s failed: %s", host, e)
    return None


def photo_url_for_post(post: dict) -> str | None:
    """Full pipeline: resolve → download → geotag → stage. None on any failure.

    `post` is a gmb_posts.json entry (needs number, keywords, alt_text).
    """
    mapping = _load_map()
    entry = mapping.get(str(post.get("number")))
    if not entry:
        return None

    raw = download_drive_image(entry["file_id"])
    if not raw:
        return None

    try:
        jpeg = gbp_image.geotag_jpeg(
            raw,
            keywords=post.get("keywords", ""),
            description=post.get("alt_text", ""),
        )
    except Exception as e:
        logger.warning("Geotag failed for post %s: %s", post.get("number"), e)
        return None

    url = stage_public_image(jpeg)
    if url:
        logger.info("Post %s photo staged: %s (%s)",
                    post.get("number"), url, entry["name"])
    return url
