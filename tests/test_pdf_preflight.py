"""Regression cases for page coordinates and honest incomplete-check reporting."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, NumberObject
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parents[1]
SKILL = Path(os.environ.get("DRAWING_SKILL_DIR", ROOT / "skills" / "prepare-production-drawings"))
SCRIPT = SKILL / "scripts" / "pdf_preflight.py"


def page(text="Readable", x=50, y=90, size=12, rotation=0, crop=None, media=None, unit=1):
    stream = io.BytesIO()
    drawing = canvas.Canvas(stream, pagesize=(200, 150))
    if text is None:
        drawing.rect(40, 40, 80, 60)
    else:
        drawing.setFillColorRGB(0.8, 0.1, 0.2)
        drawing.setFont("Helvetica", size)
        drawing.drawString(x, y, text)
    drawing.showPage()
    drawing.save()
    result = PdfReader(stream).pages[0]
    if media:
        result.mediabox.lower_left, result.mediabox.upper_right = media[:2], media[2:]
    if crop:
        result.cropbox.lower_left, result.cropbox.upper_right = crop[:2], crop[2:]
    result.rotate(rotation)
    result[NameObject("/UserUnit")] = NumberObject(unit)
    return result


class PDFPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="drawing-test-", dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def run_pdf(self, pages, render=False, env=None):
        source = self.directory / "drawing.pdf"
        writer = PdfWriter()
        for item in pages:
            writer.add_page(item)
        writer.write(source)
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        report = self.directory / "report.json"
        command = [sys.executable, str(SCRIPT), str(source), "--json", str(report)]
        if render:
            command += ["--render-dir", str(self.directory / "renders"), "--dpi", "72"]
        result = subprocess.run(command, capture_output=True, text=True, env=env, timeout=30)
        self.assertTrue(report.exists(), (result.stdout, result.stderr))
        self.assertEqual(digest, hashlib.sha256(source.read_bytes()).hexdigest())
        data = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(data["source_sha256"], digest)
        return result.returncode, data

    def test_rotations_cropbox_and_shifted_mediabox(self):
        pages = [page(rotation=r, crop=(40, 40, 180, 130)) for r in (0, 90, 180, 270)]
        pages += [page(rotation=90, media=(10, 20, 210, 170), crop=(20, 30, 200, 160))]
        code, report = self.run_pdf(pages)
        self.assertEqual(code, 0, report)
        self.assertFalse(report["errors"])
        self.assertFalse(report["warnings"])
        self.assertTrue(all(p["min_font_size_pt"] == 12 for p in report["pages"]))
        self.assertEqual(report["pages"][0]["visible_size_mm"], [49.389, 31.75])
        self.assertEqual(report["pages"][1]["visible_size_mm"], [31.75, 49.389])

    def test_outside_media_and_cropbox(self):
        code, report = self.run_pdf([
            page("Outside media", x=195),
            page("Outside crop", x=145, y=50, rotation=90, crop=(40, 30, 160, 120)),
        ])
        self.assertEqual(code, 1)
        for result in report["pages"]:
            self.assertIn("TEXT_BOUNDS_OUTSIDE_VISIBLE_PAGE", [i["code"] for i in result["errors"]])

    def test_textless_page_is_incomplete_not_blank(self):
        code, report = self.run_pdf([page(text=None)])
        self.assertEqual(code, 1)
        self.assertFalse(report["checks_complete"])
        self.assertFalse(report["errors"])
        self.assertEqual(report["warnings"][0]["code"], "NO_EXTRACTABLE_TEXT")

    def test_small_text_is_warning(self):
        code, report = self.run_pdf([page(size=6)])
        self.assertEqual(code, 0)
        self.assertTrue(report["checks_complete"])
        self.assertFalse(report["errors"])
        self.assertEqual(report["pages"][0]["min_font_size_pt"], 6)
        self.assertEqual(report["warnings"][0]["code"], "SMALL_TEXT")

    def test_user_unit_is_applied(self):
        code, report = self.run_pdf([page(size=6, unit=2)])
        self.assertEqual(code, 0, report)
        self.assertEqual(report["pages"][0]["min_font_size_pt"], 12)
        self.assertEqual(report["pages"][0]["media_size_mm"], [141.111, 105.833])

    @unittest.skipUnless(shutil.which("pdftoppm"), "Poppler is needed for render verification")
    def test_all_pages_render_in_rgb_and_gray(self):
        code, report = self.run_pdf([page(), page(rotation=90)], render=True)
        self.assertEqual(code, 0, report)
        self.assertEqual(len(report["rendered_pages"]), 4)
        for output in report["rendered_pages"]:
            with Image.open(output["path"]) as image:
                raw = image.convert("RGB").tobytes()
                pixels = zip(raw[0::3], raw[1::3], raw[2::3])
            if output["mode"] == "gray":
                self.assertTrue(all(r == g == b for r, g, b in pixels))
            else:
                self.assertTrue(any(r > g + 20 for r, g, b in pixels))

    def test_missing_renderer_is_failure(self):
        code, report = self.run_pdf([page()], render=True, env={**os.environ, "PATH": ""})
        self.assertEqual(code, 1)
        self.assertFalse(report["checks_complete"])
        self.assertTrue(any("pdftoppm is unavailable" in e["message"] for e in report["errors"]))


if __name__ == "__main__":
    unittest.main()
