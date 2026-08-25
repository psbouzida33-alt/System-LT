"""Render a kawkab card locally so the boxes can be tuned without deploying.

Usage (mel racine mte3 el projet):

    python tools/preview_kawkab_card.py male "LT --> LOST"
    python tools/preview_kawkab_card.py female "Hisoka yt"

Ya9ra el template mel assets/, y7ott fi blaset et-taswira wa7da d'essai,
w yektheb l-isem — ba3d ysajjel el natija fi tools/preview_<gender>.png.
Ken t3addi `--boxes`, yerssem el cadres bel loun asfar/azra9 bech tchouf
win bel dhabt te7bat el taswira w l-isem.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from config import (  # noqa: E402
    KAWKAB_CARD_FEMALE_PATH,
    KAWKAB_CARD_LAYOUT,
    KAWKAB_CARD_MALE_PATH,
    KAWKAB_FONT_PATH,
)

_FONT_FALLBACKS = (
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)


def load_font(size: int):
    candidates = []
    if KAWKAB_FONT_PATH.is_file():
        candidates.append(str(KAWKAB_FONT_PATH))
    candidates.extend(_FONT_FALLBACKS)
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


def shape_arabic(text: str) -> str:
    if not any("\u0600" <= ch <= "\u06ff" for ch in text):
        return text
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display

        return get_display(arabic_reshaper.reshape(text))
    except Exception:
        return text


def paste_avatar(card: Image.Image, avatar: Image.Image, box) -> None:
    width, height = card.size
    left, top = int(box[0] * width), int(box[1] * height)
    right, bottom = int(box[2] * width), int(box[3] * height)
    target_w, target_h = max(right - left, 1), max(bottom - top, 1)

    scale = max(target_w / avatar.width, target_h / avatar.height)
    resized = avatar.resize(
        (max(int(avatar.width * scale), target_w), max(int(avatar.height * scale), target_h)),
        Image.Resampling.LANCZOS,
    )
    crop_left = (resized.width - target_w) // 2
    crop_top = (resized.height - target_h) // 2
    cropped = resized.crop((crop_left, crop_top, crop_left + target_w, crop_top + target_h))

    radius = max(int(min(target_w, target_h) * 0.08), 1)
    mask = Image.new("L", (target_w, target_h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([(0, 0), (target_w - 1, target_h - 1)], radius=radius, fill=255)
    card.paste(cropped, (left, top), mask)


def draw_name(card: Image.Image, name: str, box, color) -> int:
    width, height = card.size
    left, top = int(box[0] * width), int(box[1] * height)
    right, bottom = int(box[2] * width), int(box[3] * height)
    max_w = max(int((right - left) * 0.92), 1)
    max_h = max(int((bottom - top) * 0.80), 1)

    text = shape_arabic(name.strip()) or "?"
    draw = ImageDraw.Draw(card)

    size = max_h
    font = load_font(size)
    while size > 10:
        font = load_font(size)
        bbox = draw.textbbox((0, 0), text, font=font)
        if (bbox[2] - bbox[0]) <= max_w and (bbox[3] - bbox[1]) <= max_h:
            break
        size -= 2

    outline = (0, 0, 0) if sum(color) > 380 else (255, 255, 255)
    draw.text(
        ((left + right) // 2, (top + bottom) // 2),
        text,
        font=font,
        fill=tuple(color),
        anchor="mm",
        stroke_width=2,
        stroke_fill=outline,
    )
    return size


def sample_avatar() -> Image.Image:
    """A stand-in avatar so the preview works without a Discord download."""
    avatar = Image.new("RGBA", (512, 512), (245, 245, 245, 255))
    draw = ImageDraw.Draw(avatar)
    draw.ellipse([(150, 90), (362, 302)], fill=(90, 110, 160, 255))
    draw.ellipse([(70, 320), (442, 700)], fill=(90, 110, 160, 255))
    return avatar


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--boxes"]
    show_boxes = "--boxes" in sys.argv

    gender = (args[0] if args else "male").lower()
    name = args[1] if len(args) > 1 else "LT --> LOST"

    if gender not in KAWKAB_CARD_LAYOUT:
        print(f"Gender lazem ykoun male wala female — mch {gender!r}")
        return 1

    path = KAWKAB_CARD_FEMALE_PATH if gender == "female" else KAWKAB_CARD_MALE_PATH
    full_path = REPO_ROOT / path
    if not full_path.is_file():
        print(f"Ma lqitech el template: {full_path}")
        print("Sajjel et-taswira b hedha l-isem bel dhabt, ba3d 3awed jarreb.")
        return 1

    template = Image.open(full_path).convert("RGBA")
    layout = KAWKAB_CARD_LAYOUT[gender]
    card = template.copy()

    if show_boxes:
        draw = ImageDraw.Draw(card)
        width, height = card.size
        for box, color in ((layout["avatar_box"], (255, 255, 0)), (layout["name_box"], (0, 255, 255))):
            draw.rectangle(
                [(box[0] * width, box[1] * height), (box[2] * width, box[3] * height)],
                outline=color,
                width=4,
            )

    paste_avatar(card, sample_avatar(), layout["avatar_box"])
    font_size = draw_name(card, name, layout["name_box"], layout["name_color"])

    out = Path(__file__).parent / f"preview_{gender}.png"
    card.convert("RGB").save(out)
    print(f"Template: {full_path.name}  ({template.width}x{template.height})")
    print(f"Font size: {font_size}")
    print(f"Preview -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
