"""
Geotag + keyword-embed a photo in memory before it's posted to GBP.

Replicates what the campaign doc's GeoImgr step does, but programmatically —
no website. Writes into the JPEG's EXIF:
  - GPS latitude/longitude (Smooth Closing's Austin office, from the doc)
  - ImageDescription  = the post's alt text
  - XPKeywords/XPTitle = the post's SEO keywords/title (Windows UCS-2 tags)

HONEST CAVEAT: Google Business Profile re-encodes uploaded photos and strips
most metadata, so an embedded geotag usually does NOT survive to the copy
Google serves. We do it because the doc calls for it and it costs nothing —
just don't expect it to move the SEO needle on its own.
"""

from __future__ import annotations

import io

import piexif
from PIL import Image

# Register HEIC/HEIF support so Image.open() handles iPhone photos (the folder
# has ~18 .HEIC files that Google Business won't accept as-is).
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover — HEIC just won't open if this fails
    pass

# Smooth Closing — 210 Barton Springs Rd #215, Austin TX 78704 (from the doc).
DEFAULT_LAT = 30.2599
DEFAULT_LNG = -97.7498


def _dms_rational(value: float):
    """Decimal degrees -> EXIF ((deg,1),(min,1),(sec,100)) rationals."""
    value = abs(value)
    deg = int(value)
    minutes_f = (value - deg) * 60
    minutes = int(minutes_f)
    seconds = round((minutes_f - minutes) * 60 * 100)
    return ((deg, 1), (minutes, 1), (seconds, 100))


def _ucs2(text: str) -> bytes:
    """Windows XP* EXIF tags are UTF-16LE with a UCS-2 null terminator."""
    return text.encode("utf-16le") + b"\x00\x00"


def geotag_jpeg(image_bytes: bytes, *, keywords: str = "", description: str = "",
                lat: float = DEFAULT_LAT, lng: float = DEFAULT_LNG) -> bytes:
    """Return JPEG bytes with GPS + keyword/description EXIF embedded.

    Any input image is normalized to JPEG. Metadata problems never raise — we
    fall back to a clean JPEG so a post is never blocked by tagging.
    """
    img = Image.open(io.BytesIO(image_bytes))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    gps_ifd = {
        piexif.GPSIFD.GPSVersionID: (2, 3, 0, 0),
        piexif.GPSIFD.GPSLatitudeRef: "N" if lat >= 0 else "S",
        piexif.GPSIFD.GPSLatitude: _dms_rational(lat),
        piexif.GPSIFD.GPSLongitudeRef: "E" if lng >= 0 else "W",
        piexif.GPSIFD.GPSLongitude: _dms_rational(lng),
    }

    zeroth = {piexif.ImageIFD.Software: b"SmoothClosing GBP"}
    if description:
        zeroth[piexif.ImageIFD.ImageDescription] = description.encode("utf-8", "replace")
        zeroth[piexif.ImageIFD.XPTitle] = _ucs2(description)
    if keywords:
        zeroth[piexif.ImageIFD.XPKeywords] = _ucs2(keywords)

    try:
        exif_bytes = piexif.dump(
            {"0th": zeroth, "Exif": {}, "GPS": gps_ifd, "1st": {}, "thumbnail": None}
        )
    except Exception:
        exif_bytes = b""

    out = io.BytesIO()
    if exif_bytes:
        img.save(out, format="JPEG", quality=90, exif=exif_bytes)
    else:
        img.save(out, format="JPEG", quality=90)
    return out.getvalue()


def read_geotags(image_bytes: bytes) -> dict:
    """Read back the tags we write — for verification/debugging."""
    exif = piexif.load(image_bytes)
    gps = exif.get("GPS", {})
    zeroth = exif.get("0th", {})

    def _to_deg(dms, ref):
        if not dms:
            return None
        d = dms[0][0] / dms[0][1]
        m = dms[1][0] / dms[1][1]
        s = dms[2][0] / dms[2][1]
        val = d + m / 60 + s / 3600
        if ref in (b"S", b"W"):
            val = -val
        return round(val, 5)

    out = {
        "lat": _to_deg(gps.get(piexif.GPSIFD.GPSLatitude),
                       gps.get(piexif.GPSIFD.GPSLatitudeRef)),
        "lng": _to_deg(gps.get(piexif.GPSIFD.GPSLongitude),
                       gps.get(piexif.GPSIFD.GPSLongitudeRef)),
    }
    desc = zeroth.get(piexif.ImageIFD.ImageDescription)
    if desc:
        out["description"] = desc.decode("utf-8", "replace")
    kw = zeroth.get(piexif.ImageIFD.XPKeywords)
    if kw:
        out["keywords"] = bytes(kw).decode("utf-16le", "replace").rstrip("\x00")
    return out
