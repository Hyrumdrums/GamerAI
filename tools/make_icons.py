#!/usr/bin/env python3
"""Generate placeholder PWA icons for GamerAI using only stdlib.

These are placeholders — solid color rounded squares with a centered
"G" glyph drawn pixel-by-pixel. Replace with proper branding before
public launch. Bump filenames (e.g., icon-192-v2.png) when redesigning
because iOS caches home-screen icons stickily (per PWA_REFERENCE §8).
"""
import struct
import zlib
import os
from pathlib import Path

OUT = Path("/home/dallin/site/GamerAI/client/static/icons")
OUT.mkdir(parents=True, exist_ok=True)

# Brand palette: deep blue background, white glyph.
BG = (29, 78, 216, 255)        # tailwind blue-700
FG = (255, 255, 255, 255)
TRANSPARENT = (0, 0, 0, 0)


def write_png(path: Path, pixels: list[list[tuple[int, int, int, int]]]):
    """Write RGBA pixels as a PNG using stdlib only."""
    h = len(pixels)
    w = len(pixels[0])

    def chunk(typ: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + typ + data + struct.pack(
            ">I", zlib.crc32(typ + data) & 0xFFFFFFFF
        )

    # IHDR: width, height, 8-bit depth, color type 6 (RGBA),
    # compression 0, filter 0, interlace 0.
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)

    # IDAT: each scanline prefixed with filter byte 0 (none).
    raw = bytearray()
    for row in pixels:
        raw.append(0)
        for r, g, b, a in row:
            raw.extend((r, g, b, a))
    idat = zlib.compress(bytes(raw), level=9)

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", idat)
        + chunk(b"IEND", b"")
    )
    path.write_bytes(png)


def rounded_square(size: int, radius_frac: float, bg, fg_glyph: bool = True,
                   safe_inset_frac: float = 0.0):
    """Return a pixel grid: rounded square in bg, optional centered 'G' in white.

    safe_inset_frac > 0 keeps the glyph inside the maskable "safe zone"
    (per the W3C spec, mask-able icons get clipped to a circle inscribed
    in an 80% center square — use 0.10 inset for 80% safe zone).
    """
    grid = [[TRANSPARENT for _ in range(size)] for _ in range(size)]
    r = size * radius_frac

    # Fill background as a rounded square.
    for y in range(size):
        for x in range(size):
            if _in_rounded_square(x, y, size, r):
                grid[y][x] = bg

    # Draw a stylized "G" centered. Bounding box shrunk by safe_inset.
    inset = int(size * safe_inset_frac)
    gx0, gy0 = inset, inset
    gw, gh = size - 2 * inset, size - 2 * inset
    if fg_glyph:
        _draw_g(grid, gx0, gy0, gw, gh)
    return grid


def _in_rounded_square(x, y, size, r):
    """True if (x,y) is inside a square of side `size` with corner radius r."""
    if r <= 0:
        return 0 <= x < size and 0 <= y < size
    if x < r and y < r:
        return ((x - r) ** 2 + (y - r) ** 2) <= r ** 2
    if x >= size - r and y < r:
        return ((x - (size - r - 1)) ** 2 + (y - r) ** 2) <= r ** 2
    if x < r and y >= size - r:
        return ((x - r) ** 2 + (y - (size - r - 1)) ** 2) <= r ** 2
    if x >= size - r and y >= size - r:
        return (
            (x - (size - r - 1)) ** 2 + (y - (size - r - 1)) ** 2
        ) <= r ** 2
    return 0 <= x < size and 0 <= y < size


def _draw_g(grid, x0, y0, w, h):
    """Draw a chunky 'G' (the closed-style font 'G') into the bounding box.

    Built from primitives: outer ring + inner bite + horizontal stub.
    All measurements expressed as fractions of the bounding box so the
    glyph scales cleanly to any size.
    """
    cx = x0 + w / 2
    cy = y0 + h / 2
    R_out = min(w, h) * 0.42
    R_in = R_out - max(w, h) * 0.10  # ring thickness ~10% of box
    bite_start_angle = -25  # degrees, measured from east, ccw positive
    bite_end_angle = 25
    import math
    bsa = math.radians(bite_start_angle)
    bea = math.radians(bite_end_angle)

    for y in range(y0, y0 + h):
        for x in range(x0, x0 + w):
            if x < 0 or y < 0 or x >= len(grid[0]) or y >= len(grid):
                continue
            dx = x - cx
            dy = y - cy
            d2 = dx * dx + dy * dy
            # Outer ring (annulus).
            if R_in * R_in <= d2 <= R_out * R_out:
                angle = math.atan2(-dy, dx)  # screen y inverted
                # Cut out the "bite" on the east side of the G.
                if not (bsa <= angle <= bea):
                    grid[y][x] = FG
            # Horizontal stub: middle bar at y ~ cy, extending from
            # the right edge of the ring inward to ~mid-bite.
            stub_y_thick = max(2, int(min(w, h) * 0.08))
            stub_x_start = int(cx + R_in * 0.50)
            stub_x_end = int(cx + R_out * 1.02)
            if (stub_x_start <= x <= stub_x_end
                    and abs(y - cy) <= stub_y_thick // 2):
                grid[y][x] = FG


def write_ico(path: Path, pixels_32):
    """Write a 32x32 ICO containing one 32x32 BMP image."""
    h = len(pixels_32)
    w = len(pixels_32[0])
    assert w == 32 and h == 32
    # BMP within ICO: BITMAPINFOHEADER + XOR mask + AND mask
    bmih_size = 40
    image_data = bytearray()
    # XOR (RGBA, but BMP stores BGRA bottom-up)
    for y in range(h - 1, -1, -1):
        for x in range(w):
            r, g, b, a = pixels_32[y][x]
            image_data.extend((b, g, r, a))
    # AND mask: 1 bit per pixel, rows padded to 32 bits.
    and_row_bytes = (w + 31) // 32 * 4
    and_mask = bytearray(and_row_bytes * h)  # all zeros = opaque

    bmih = struct.pack(
        "<IIIHHIIIIII",
        bmih_size,
        w,
        h * 2,        # ICO uses double height
        1,             # planes
        32,            # bits per pixel
        0, 0, 0, 0, 0, 0,
    )
    bmp = bmih + bytes(image_data) + bytes(and_mask)

    iconheader = struct.pack("<HHH", 0, 1, 1)
    iconentry = struct.pack(
        "<BBBBHHII",
        w if w < 256 else 0,
        h if h < 256 else 0,
        0,    # colors in palette
        0,    # reserved
        1,    # planes
        32,   # bit count
        len(bmp),
        6 + 16,  # offset
    )
    path.write_bytes(iconheader + iconentry + bmp)


# --- Generate the five required files ---
# Standard square icons: 10% corner radius.
sizes = {
    "icon-192.png":           (192, 0.18, 0.0),
    "icon-512.png":           (512, 0.18, 0.0),
    # Maskable: full-bleed background, glyph in safe zone (80%).
    "icon-512-maskable.png":  (512, 0.0, 0.10),
    # Apple touch icon: iOS rounds corners itself, so use a soft radius.
    "apple-touch-icon.png":   (180, 0.08, 0.0),
}
for fname, (size, radius_frac, safe) in sizes.items():
    grid = rounded_square(size, radius_frac, BG, fg_glyph=True,
                          safe_inset_frac=safe)
    write_png(OUT / fname, grid)
    print(f"  wrote {fname:30s} {size}x{size}  ({(OUT / fname).stat().st_size:>6} bytes)")

# Favicon: 32x32 ICO with the same glyph (no rounded corners — browsers
# usually render favicons in a fixed shape).
grid32 = rounded_square(32, 0.0, BG, fg_glyph=True)
write_ico(OUT / "favicon.ico", grid32)
print(f"  wrote {'favicon.ico':30s} 32x32    ({(OUT / 'favicon.ico').stat().st_size:>6} bytes)")
