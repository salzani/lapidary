"""Icon generation for the Lapidary desktop entry.

Rescales the designed mark to every size the desktop needs, plus a
multi-size Windows `.ico`. Uses PySide6 directly — it is already a
dependency of the `ui` extra this build requires, so no imaging library is
added just for this.

Must work headless (no display), including in CI: `QT_QPA_PLATFORM` is
forced to `offscreen` before any Qt class is touched, unless the caller
already set something explicit.

The source is `src/notemcp/ui/web/logo-mark.png` — the same file the running
app shows in its header. One asset drives the icon and the interface, so they
cannot drift; a designed mark that only reaches the dock, while the app draws
something else, is the ordinary way branding ends up inconsistent.

The asset is pre-cropped to its own edges. A small transparent margin is added
back here, because a rounded tile rendered edge-to-edge reads as visually
larger than its neighbours in a dock that expects some breathing room.
"""

from __future__ import annotations

import os
import struct
from pathlib import Path

MARK_PATH = Path(__file__).resolve().parent.parent / "src" / "notemcp" / "ui" / "web" / "logo-mark.png"

_MARGIN_FRACTION = 0.06
"""Transparent padding on each side, as a fraction of the icon edge."""

PNG_SIZES: tuple[int, ...] = (16, 32, 48, 64, 128, 256)
ICO_SIZES: tuple[int, ...] = (16, 32, 48, 64, 128, 256)


def _ensure_offscreen_platform() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def render_icon(size: int):
    """Return a `QImage` of the Lapidary mark at `size` x `size`."""
    _ensure_offscreen_platform()
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage, QPainter

    if not MARK_PATH.is_file():
        raise FileNotFoundError(
            f"icon source not found at {MARK_PATH}. It ships with the repository and is "
            "also served to the running UI — if it is missing, the app has no mark "
            "either, and generating a placeholder here would only hide that."
        )

    source = QImage(str(MARK_PATH))
    if source.isNull():
        raise ValueError(f"could not decode {MARK_PATH} as an image")

    inner = max(1, round(size * (1 - 2 * _MARGIN_FRACTION)))
    scaled = source.scaled(
        inner,
        inner,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )

    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)

    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    painter.drawImage((size - scaled.width()) // 2, (size - scaled.height()) // 2, scaled)
    painter.end()
    return image


def _png_bytes(image) -> bytes:
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice

    buffer = QByteArray()
    device = QBuffer(buffer)
    device.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(device, "PNG")
    device.close()
    return bytes(buffer)


def render_png_bytes(size: int) -> bytes:
    """Render `size` and return its PNG-encoded bytes, without touching disk."""
    return _png_bytes(render_icon(size))


def write_pngs(target_dir: Path, sizes: tuple[int, ...] = PNG_SIZES) -> dict[int, Path]:
    """Render every size in `sizes` and save it as `<target_dir>/<size>.png`.

    Returns a `{size: path}` map — the shape `desktop_entry.install` expects
    for `icon_sources`.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    written: dict[int, Path] = {}
    for size in sizes:
        path = target_dir / f"{size}.png"
        path.write_bytes(render_png_bytes(size))
        written[size] = path
    return written


def build_ico_bytes(sizes: tuple[int, ...] = ICO_SIZES) -> bytes:
    """Build a multi-size Windows `.ico` container from PNG-encoded frames.

    Windows has accepted PNG-compressed entries inside `.ico` files since
    Vista — using them means no bitmap/BMP conversion or Qt `.ico` writer
    is needed, only the small binary `ICONDIR`/`ICONDIRENTRY` header format
    documented in the Microsoft icon file spec, built by hand with `struct`.
    """
    frames = [(size, render_png_bytes(size)) for size in sizes]

    header = struct.pack("<HHH", 0, 1, len(frames))
    entries = bytearray()
    data = bytearray()
    offset = 6 + len(frames) * 16

    for size, png in frames:
        # 256 is encoded as 0 in a single byte, per the icon file spec.
        dim = size if size < 256 else 0
        entries += struct.pack(
            "<BBBBHHII",
            dim,
            dim,
            0,
            0,
            1,
            32,
            len(png),
            offset,
        )
        data += png
        offset += len(png)

    return header + bytes(entries) + bytes(data)


def write_ico(target_path: Path, sizes: tuple[int, ...] = ICO_SIZES) -> Path:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(build_ico_bytes(sizes))
    return target_path
