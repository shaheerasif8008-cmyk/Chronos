"""Credential-scrubbed, resource-bounded image validation worker."""
from __future__ import annotations

import json
import sys
import warnings


_MAX_PIXELS = 16_000_000
_MAX_ANIMATION_PIXELS = 32_000_000
_MAX_FRAMES = 200
_BROWSER_FORMATS = {"PNG", "JPEG", "GIF", "WEBP"}


def _fail(message: str) -> int:
    sys.stderr.write(message[:500])
    return 2


def main() -> int:
    try:
        from PIL import Image

        Image.MAX_IMAGE_PIXELS = _MAX_PIXELS
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        image = Image.open(sys.stdin.buffer)
        width, height = image.size
        image_format = str(image.format or "").upper()
        frames = int(getattr(image, "n_frames", 1) or 1)
        if image_format not in _BROWSER_FORMATS:
            image.close()
            return _fail("image format does not have a safe browser preview")
        if width <= 0 or height <= 0 or width * height > _MAX_PIXELS:
            image.close()
            return _fail("image dimensions exceed the safe preview limit")
        if frames > _MAX_FRAMES or width * height * frames > _MAX_ANIMATION_PIXELS:
            image.close()
            return _fail("animated image exceeds the safe preview limit")
        image.verify()
        image.close()
        sys.stdout.write(
            json.dumps(
                {
                    "width": width,
                    "height": height,
                    "frames": frames,
                    "image_format": image_format.lower(),
                },
                separators=(",", ":"),
            )
        )
        return 0
    except Exception:
        return _fail("image parser rejected the document")


if __name__ == "__main__":
    raise SystemExit(main())
