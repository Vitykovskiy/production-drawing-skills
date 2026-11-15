#!/usr/bin/env python3
"""Limited PDF sheet preflight; exit 1 for errors/incomplete checks, 0 otherwise."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

MM_PER_PT = 25.4 / 72
EPSILON_PT = 0.5
LIMITATIONS = [
    "Checks extracted non-whitespace character bounds, not actual painted glyphs. "
    "Invisible/OCR text, clipping, optional layers and transparency require visual review.",
    "Font size is an orientation-aware character-em estimate, not cap height or ink height. "
    "Skewed/arbitrarily rotated text and vertical writing may need manual measurement.",
    "No text/line collision, dimension consistency, geometry, manufacturing feasibility, "
    "standards compliance or production-release validation is performed.",
    "Raster-only or outlined text is not assessed. Renders are aids for manual review, "
    "not proof that a drawing is readable or complete.",
]


def issue(code: str, message: str, **details) -> dict:
    return {"code": code, "message": message, **details}


def normalized_box(box) -> tuple:
    x0, y0, x1, y1 = map(float, box)
    return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)


def display_crop(page) -> tuple:
    """Apply pdfminer's page CTM, then convert to a top-left display origin."""
    x0, y0, x1, y1 = normalized_box(page.page_obj.mediabox)
    matrices = {0: (1, 0, 0, 1, -x0, -y0), 90: (0, -1, 1, 0, -y0, x1),
                180: (-1, 0, 0, -1, x1, y1), 270: (0, 1, -1, 0, y1, -x0)}
    if page.rotation not in matrices:
        raise ValueError("Page rotation is not a multiple of 90 degrees")
    a, b, c, d, e, f = matrices[page.rotation]
    cx0, cy0, cx1, cy1 = normalized_box(page.page_obj.cropbox)
    points = [(a*x+c*y+e, b*x+d*y+f) for x in (cx0, cx1) for y in (cy0, cy1)]
    xs, ys = zip(*points)
    return (max(0, min(xs)), max(0, page.height-max(ys)),
            min(page.width, max(xs)), min(page.height, page.height-min(ys)))


def inspect_page(page, threshold: float) -> dict:
    from pdfplumber.utils import resolve_all

    unit = float(resolve_all(page.page_obj.attrs.get("UserUnit", 1)))
    if not math.isfinite(unit) or unit <= 0:
        raise ValueError("Invalid PDF UserUnit")
    crop = tuple(v*unit for v in display_crop(page))
    if crop[2] <= crop[0] or crop[3] <= crop[1]:
        raise ValueError("CropBox has no area inside MediaBox")
    result = {"page": page.page_number, "rotation_deg": page.rotation,
              "user_unit": unit,
              "media_size_mm": [round(v*unit*MM_PER_PT, 3) for v in (page.width, page.height)],
              "visible_size_mm": [round(v*MM_PER_PT, 3) for v in (crop[2]-crop[0], crop[3]-crop[1])],
              "visible_box_pt": list(crop), "errors": [], "warnings": [],
              "checks_complete": True, "text_bounds_checked": False}
    chars = page.chars
    result["char_count"] = len(chars)
    printable = [ch for ch in chars if ch.get("text", "").strip()]
    result["non_whitespace_char_count"] = len(printable)
    if not printable:
        result["min_font_size_pt"] = None
        result["checks_complete"] = False
        result["warnings"].append(issue("NO_EXTRACTABLE_TEXT",
            "No non-whitespace text extracted; this does not prove that the page is blank. "
            "Automatic text checks are unavailable; inspect the rendered page."))
        return result
    small, outside, unknown_sizes, sizes, suspect = [], [], 0, [], {}
    origin_x, origin_top = page.mediabox[:2]
    for char in printable:
        bounds = [(char["x0"]-origin_x)*unit, (char["top"]-origin_top)*unit,
                  (char["x1"]-origin_x)*unit, (char["bottom"]-origin_top)*unit]
        if not all(math.isfinite(v) for v in bounds):
            raise ValueError("Non-finite character bounds")
        if (bounds[0] < crop[0]-EPSILON_PT or bounds[1] < crop[1]-EPSILON_PT or
                bounds[2] > crop[2]+EPSILON_PT or bounds[3] > crop[3]+EPSILON_PT):
            outside.append([round(v, 3) for v in bounds])
        matrix = char.get("matrix", (1, 0, 0, 1, 0, 0))
        a, b, c, d = matrix[:4]
        if abs(b) < 1e-8 and abs(c) < 1e-8:
            size = bounds[3]-bounds[1]
        elif abs(a) < 1e-8 and abs(d) < 1e-8:
            size = bounds[2]-bounds[0]
        else:
            size = None
            unknown_sizes += 1
        if size is not None:
            sizes.append(size)
            if size < threshold-1e-6:
                small.append(size)
        text = char.get("text", "")
        for symbol in text:
            if symbol in "\ufffd\ufffe\uffff\x00\u25a1\u25a0" or 0xD800 <= ord(symbol) <= 0xDFFF:
                key = f"U+{ord(symbol):04X}"
                suspect[key] = suspect.get(key, 0)+1
        if "(cid:" in text or ".notdef" in text:
            suspect["unmapped_glyph"] = suspect.get("unmapped_glyph", 0)+1
    result.update(min_font_size_pt=round(min(sizes), 3) if sizes else None,
                  font_size_checked_chars=len(sizes), text_bounds_checked=True,
                  outside_char_count=len(outside), small_char_count=len(small))
    if outside:
        result["errors"].append(issue("TEXT_BOUNDS_OUTSIDE_VISIBLE_PAGE",
            "Extracted character bounds exceed CropBox/MediaBox intersection. "
            "Review render to distinguish unintended clipping from hidden text.",
            count=len(outside), example_bounds_pt=outside[:5], epsilon_pt=EPSILON_PT))
    if small:
        result["warnings"].append(issue("SMALL_TEXT", "Extracted font-size estimate is below threshold.",
            count=len(small), threshold_pt=threshold, minimum_pt=round(min(small), 3)))
    if unknown_sizes:
        result["checks_complete"] = False
        result["warnings"].append(issue("FONT_SIZE_CHECK_PARTIAL",
            "Arbitrary rotation/skew prevents this script's font-size estimate.", count=unknown_sizes))
    if suspect:
        result["warnings"].append(issue("SUSPECT_GLYPHS",
            "Potential replacement/unmapped glyphs; square symbols may be intentional.", counts=suspect))
    return result


def render_pages(source: Path, target: Path, count: int, dpi: int) -> list:
    executable = shutil.which("pdftoppm")
    if not executable:
        raise RuntimeError("pdftoppm is unavailable; requested rendering could not run")
    target.mkdir(parents=True, exist_ok=True)
    outputs = []
    with tempfile.TemporaryDirectory(prefix=".preflight-", dir=target) as temporary:
        for mode, flags in (("rgb", []), ("gray", ["-gray"])):
            prefix = Path(temporary)/mode
            command = [executable, "-cropbox", "-r", str(dpi), "-png", *flags, str(source), str(prefix)]
            proc = subprocess.run(command, capture_output=True, text=True, timeout=600, check=False)
            if proc.returncode:
                raise RuntimeError(f"pdftoppm {mode} failed (exit {proc.returncode}): {proc.stderr[-600:]}")
            produced = sorted(Path(temporary).glob(f"{mode}-*.png"), key=lambda p: int(p.stem.rsplit("-", 1)[1]))
            if len(produced) != count:
                raise RuntimeError(f"Expected {count} {mode} pages, received {len(produced)}")
            for number, path in enumerate(produced, 1):
                final = target/f"page-{number:03d}-{mode}.png"
                if final.resolve() == source:
                    raise RuntimeError("Render output would overwrite the source PDF")
                path.replace(final)
                outputs.append({"page": number, "mode": mode, "path": str(final)})
    return outputs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--json", required=True, type=Path, dest="report")
    parser.add_argument("--render-dir", type=Path)
    parser.add_argument("--dpi", type=int, default=150)
    parser.add_argument("--warn-below-pt", type=float, default=9)
    args = parser.parse_args()
    if args.dpi <= 0 or not math.isfinite(args.warn_below_pt) or args.warn_below_pt <= 0:
        parser.error("dpi and warn-below-pt must be positive finite numbers")
    source, destination = args.input.resolve(), args.report.resolve()
    if source == destination:
        parser.error("JSON report must not overwrite the source PDF")
    report = {"schema_version": 1, "source": str(source), "source_sha256": None,
              "scope": "limited_extracted_text_preflight", "pages": [], "errors": [], "warnings": [],
              "limitations": LIMITATIONS, "rendered_pages": [], "checks_complete": True,
              "settings": {"warn_below_pt": args.warn_below_pt, "bounds_epsilon_pt": EPSILON_PT,
                           "render_dpi": args.dpi if args.render_dir else None}}
    try:
        import pdfplumber

        with source.open("rb") as stream:
            digest = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024*1024), b""):
                digest.update(chunk)
        report["source_sha256"] = digest.hexdigest()
        report["pdfplumber_version"] = pdfplumber.__version__
        with pdfplumber.open(source) as pdf:
            report["page_count"] = len(pdf.pages)
            if not pdf.pages:
                raise ValueError("PDF contains no pages")
            for page in pdf.pages:
                try:
                    result = inspect_page(page, args.warn_below_pt)
                except Exception as exc:
                    result = {"page": page.page_number, "checks_complete": False, "warnings": [],
                              "errors": [issue("PAGE_CHECK_FAILED", str(exc)[:600])]}
                report["pages"].append(result)
                report["checks_complete"] &= result["checks_complete"]
                for level in ("errors", "warnings"):
                    report[level].extend({"page": page.page_number, **item} for item in result[level])
        if args.render_dir:
            report["rendered_pages"] = render_pages(source, args.render_dir.resolve(), report["page_count"], args.dpi)
    except Exception as exc:
        report["checks_complete"] = False
        report["errors"].append(issue("PREFLIGHT_FAILED", str(exc)[:600]))
    code = int(bool(report["errors"]) or not report["checks_complete"])
    report.update(exit_code=code, status="failed_or_incomplete" if code else
                  ("completed_with_warnings" if report["warnings"] else "completed"))
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    except OSError as exc:
        print(f"Cannot write report: {exc}", file=sys.stderr)
        return 1
    print(f"{report['status']}: {len(report['errors'])} errors, {len(report['warnings'])} warnings; {destination}")
    return code


if __name__ == "__main__":
    sys.exit(main())
