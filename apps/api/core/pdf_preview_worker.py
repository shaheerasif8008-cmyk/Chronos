"""Resource-bounded PDF metadata and raster worker.

This module is launched in a scrubbed subprocess by ``artifact_rendering`` so
native PDF parser faults cannot take down the API process or inherit provider
credentials. It accepts PDF bytes on stdin and emits either an ASCII page count
or one bounded PNG on stdout.
"""
from __future__ import annotations

from io import BytesIO
import math
import sys


_MAX_DIMENSION = 4_096
_MAX_PIXELS = 16_000_000
_MAX_PNG_BYTES = 24 * 1024 * 1024


def _fail(message: str) -> int:
    sys.stderr.write(message[:500])
    return 2


def main() -> int:
    if len(sys.argv) != 4 or sys.argv[1] not in {"count", "render"}:
        return _fail("invalid PDF preview worker request")
    action = sys.argv[1]
    try:
        page_number = int(sys.argv[2])
        max_pages = int(sys.argv[3])
    except ValueError:
        return _fail("invalid PDF preview worker bounds")
    content = sys.stdin.buffer.read()
    try:
        import pypdfium2 as pdfium

        document = pdfium.PdfDocument(content)
        page_count = len(document)
        if action == "count":
            document.close()
            sys.stdout.write(str(page_count))
            return 0
        if page_number < 0 or page_number >= min(page_count, max_pages):
            document.close()
            return _fail("PDF page not found")
        page = document[page_number]
        width, height = page.get_size()
        if (
            not math.isfinite(width)
            or not math.isfinite(height)
            or width <= 0
            or height <= 0
        ):
            page.close()
            document.close()
            return _fail("PDF page has invalid dimensions")
        scale = min(
            1.5,
            _MAX_DIMENSION / width,
            _MAX_DIMENSION / height,
            math.sqrt(_MAX_PIXELS / (width * height)),
        )
        if not math.isfinite(scale) or scale <= 0:
            page.close()
            document.close()
            return _fail("PDF page cannot be rendered within safe dimensions")
        bitmap = page.render(scale=scale)
        image = bitmap.to_pil()
        output = BytesIO()
        image.save(output, format="PNG", optimize=True)
        png = output.getvalue()
        bitmap.close()
        page.close()
        document.close()
        if len(png) > _MAX_PNG_BYTES:
            return _fail("rendered PDF page exceeds the output limit")
        sys.stdout.buffer.write(png)
        return 0
    except Exception:
        return _fail("PDF parser rejected the document")


if __name__ == "__main__":
    raise SystemExit(main())
