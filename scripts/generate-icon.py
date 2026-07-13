"""Generuje wielorozmiarową ikonę Windows zgodną z ikoną Mówika."""

from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
SIZE = 1024


def rounded_gradient(size: int) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gradient = Image.new("RGBA", (size, size))
    pixels = gradient.load()
    start = (23, 58, 99)
    end = (11, 27, 49)
    for y in range(size):
        for x in range(size):
            ratio = (x + y) / (2 * (size - 1))
            pixels[x, y] = tuple(
                round(a + (b - a) * ratio) for a, b in zip(start, end)
            ) + (255,)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((56, 56, 968, 968), radius=248, fill=255)
    image.paste(gradient, (0, 0), mask)
    return image


def build_icon() -> Image.Image:
    image = rounded_gradient(SIZE)
    draw = ImageDraw.Draw(image)

    # Dwukolorowa obwódka daje czytelny znak również na małej ikonie paska.
    draw.rounded_rectangle(
        (84, 84, 940, 940), radius=222, outline=(79, 188, 255, 255), width=40
    )
    white = (248, 250, 252, 255)
    dark = (11, 27, 49, 255)
    green = (34, 197, 94, 255)

    draw.rounded_rectangle((376, 206, 648, 596), radius=136, fill=white)
    draw.arc((280, 306, 744, 720), start=0, end=180, fill=white, width=58)
    draw.line((512, 718, 512, 822), fill=white, width=58)
    draw.line((390, 824, 634, 824), fill=white, width=58)

    draw.ellipse((666, 646, 914, 894), fill=dark)
    draw.ellipse((694, 674, 886, 866), fill=green)
    draw.line((744, 770, 775, 802, 839, 732), fill=white, width=28, joint="curve")
    return image


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    icon = build_icon()
    icon.save(ASSETS / "Mowik.png", format="PNG", optimize=True)
    icon.save(
        ASSETS / "Mowik.ico",
        format="ICO",
        sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)],
    )


if __name__ == "__main__":
    main()
