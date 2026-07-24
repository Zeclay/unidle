"""Generates the Unidle app icon.

Run this before building (build_windows.bat / build_macos.sh / build.yml
already do). Nothing here is committed to the repo — outputs are generated
files, listed in .gitignore, so this script is the source of truth.

Concept: a green presence dot (#6BB700, the same accent used in the
Settings window) on a dark circular badge, with a partial ring around it
suggesting an ongoing/continuous state ("staying online") rather than a
static dot. Kept to two flat shapes with no fine detail so it still reads
at 16px.

Outputs:
  assets/icon.ico        multi-size, for the Windows build (--icon)
  assets/icon_256.png    flat PNG, for the README and general use
  assets/icon.iconset/   macOS iconset source PNGs
  assets/icon.icns       macOS icon, assembled from icon.iconset via
                         `iconutil` — macOS only; skipped elsewhere (CI's
                         macOS job assembles it if this script couldn't)
"""

import os
import shutil
import subprocess
import sys

from PIL import Image, ImageDraw

ASSETS_DIR = os.path.dirname(os.path.abspath(__file__))

BG_COLOR = (28, 30, 34, 255)
ACCENT_COLOR = (107, 183, 0, 255)
RING_COLOR = (255, 255, 255, 235)

ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]

# Standard macOS .iconset naming: (filename, pixel size)
ICONSET_SIZES = [
    ("icon_16x16.png", 16),
    ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32),
    ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128),
    ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256),
    ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512),
    ("icon_512x512@2x.png", 1024),
]


def build_logo(size):
    """Renders the logo at ``size``x``size`` on a transparent canvas."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    cx = cy = size / 2

    bg_r = size * 0.48
    draw.ellipse([cx - bg_r, cy - bg_r, cx + bg_r, cy + bg_r], fill=BG_COLOR)

    ring_r = size * 0.36
    ring_width = max(1, round(size * 0.05))
    # 300-degree sweep (60 degree gap) reads as "in progress" / continuous,
    # rather than a closed static ring.
    draw.arc(
        [cx - ring_r, cy - ring_r, cx + ring_r, cy + ring_r],
        start=-190,
        end=110,
        fill=RING_COLOR,
        width=ring_width,
    )

    dot_r = size * 0.24
    draw.ellipse([cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r], fill=ACCENT_COLOR)

    return img


def write_ico(path):
    base = build_logo(256)
    base.save(path, format="ICO", sizes=[(s, s) for s in ICO_SIZES])


def write_png(path, size=256):
    build_logo(size).save(path, format="PNG")


def write_iconset(iconset_dir):
    if os.path.isdir(iconset_dir):
        shutil.rmtree(iconset_dir)
    os.makedirs(iconset_dir)
    for filename, size in ICONSET_SIZES:
        build_logo(size).save(os.path.join(iconset_dir, filename), format="PNG")


def try_build_icns(iconset_dir, icns_path):
    """Best-effort: only works on macOS, where `iconutil` ships with the OS."""
    if sys.platform != "darwin":
        return False
    if shutil.which("iconutil") is None:
        return False
    try:
        subprocess.run(
            ["iconutil", "-c", "icns", iconset_dir, "-o", icns_path],
            check=True,
            capture_output=True,
        )
        return True
    except Exception:
        return False


def main():
    ico_path = os.path.join(ASSETS_DIR, "icon.ico")
    png_path = os.path.join(ASSETS_DIR, "icon_256.png")
    iconset_dir = os.path.join(ASSETS_DIR, "icon.iconset")
    icns_path = os.path.join(ASSETS_DIR, "icon.icns")

    write_ico(ico_path)
    print(f"wrote {ico_path}")

    write_png(png_path, 256)
    print(f"wrote {png_path}")

    write_iconset(iconset_dir)
    print(f"wrote {iconset_dir}/ ({len(ICONSET_SIZES)} sizes)")

    if try_build_icns(iconset_dir, icns_path):
        print(f"wrote {icns_path}")
    else:
        print(
            "icon.icns not built here (needs macOS + iconutil) — "
            "the macOS CI job assembles it from icon.iconset/ instead."
        )


if __name__ == "__main__":
    main()
