"""Custom logo stored in /config/branding.

Only PNG, JPEG and WebP are accepted, identified by their magic bytes (never by
the client's Content-Type). SVG is refused on purpose: it can carry script.
"""

from __future__ import annotations

from pathlib import Path

MAX_BYTES = 512 * 1024
_SIGNATURES: tuple[tuple[bytes, str, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
)
MEDIA_TYPES = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}


class LogoError(ValueError):
    pass


def sniff(data: bytes) -> str:
    for magic, ext, _ in _SIGNATURES:
        if data.startswith(magic):
            return ext
    if len(data) > 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    raise LogoError("The logo must be a PNG, JPEG or WebP image.")


def branding_dir(config_dir: Path) -> Path:
    return config_dir / "branding"


def current_logo(config_dir: Path) -> Path | None:
    d = branding_dir(config_dir)
    for ext in MEDIA_TYPES:
        p = d / f"logo.{ext}"
        if p.is_file():
            return p
    return None


def save_logo(config_dir: Path, data: bytes) -> str:
    if not data:
        raise LogoError("No image was uploaded.")
    if len(data) > MAX_BYTES:
        raise LogoError(f"The logo must be under {MAX_BYTES // 1024} KB.")
    ext = sniff(data)
    d = branding_dir(config_dir)
    d.mkdir(parents=True, exist_ok=True)
    remove_logo(config_dir)
    tmp = d / f".logo.{ext}.tmp"
    tmp.write_bytes(data)
    tmp.replace(d / f"logo.{ext}")
    return ext


def remove_logo(config_dir: Path) -> bool:
    removed = False
    for ext in MEDIA_TYPES:
        p = branding_dir(config_dir) / f"logo.{ext}"
        if p.exists():
            p.unlink()
            removed = True
    return removed
