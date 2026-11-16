#!/usr/bin/env python3
import argparse
import os
import sys

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.units import mm
from xml.sax.saxutils import escape

PAGE_WIDTH, PAGE_HEIGHT = A4

# Huawei MatePad 11.5 / Android fonts found on the device.
FONT_CANDIDATES = [
    "/system/fonts/HarmonyOS_Sans.ttf",
    "/system/fonts/Roboto-Regular.ttf",
    "/system/fonts/RobotoStatic-Regular.ttf",
    "/system/fonts/NotoSans-Regular.ttf",
    "/system/fonts/NotoSansCJK-Regular.ttc",
    "/system/product/fonts/HarmonyOS_Sans.ttf",
    "/system_ext/fonts/HarmonyOS_Sans.ttf",
]

def choose_font():
    for path in FONT_CANDIDATES:
        if os.path.isfile(path):
            try:
                pdfmetrics.registerFont(TTFont("HuaweiUnicode", path))
                return "HuaweiUnicode", path
            except Exception as e:
                print(f"Font skipped: {path} ({e})", file=sys.stderr)
    return "Helvetica", None

def page_number(canvas, doc):
    canvas.saveState()
    canvas.setFont(doc.font_name, 8)
    canvas.setFillColor(colors.grey)
    canvas.drawCentredString(PAGE_WIDTH / 2, 10 * mm, f"Page {doc.page}")
    canvas.restoreState()

def convert(input_file, output_file, font_size, spacing, margin):
    font, font_path = choose_font()

    doc = SimpleDocTemplate(
        output_file,
        pagesize=A4,
        leftMargin=margin * mm,
        rightMargin=margin * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=os.path.basename(input_file),
        author="txt2pdf",
    )
    doc.font_name = font

    leading = font_size + spacing
    normal = ParagraphStyle(
        "NormalTXT",
        fontName=font,
        fontSize=font_size,
        leading=leading,
        textColor=colors.black,
        spaceAfter=0,
    )
    heading = ParagraphStyle(
        "HeadingTXT",
        parent=normal,
        fontSize=font_size + 1,
        leading=leading + 1,
        spaceBefore=5,
        spaceAfter=3,
    )

    story = []

    with open(input_file, "r", encoding="utf-8-sig", errors="replace") as f:
        for raw in f:
            line = raw.rstrip("\r\n")

            if not line.strip():
                story.append(Spacer(1, leading))
                continue

            # ReportLab Paragraph interprets <, > and & as markup.
            safe = escape(line)

            # Preserve leading spaces.
            n = len(line) - len(line.lstrip(" "))
            if n:
                safe = "&nbsp;" * n + safe.lstrip()

            stripped = line.strip()
            looks_heading = (
                (stripped.endswith(":") and len(stripped) <= 100)
                or (stripped.isupper() and len(stripped) <= 100)
            )

            story.append(Paragraph(
                safe,
                heading if looks_heading else normal
            ))

    if not story:
        story.append(Paragraph("", normal))

    doc.build(story, onFirstPage=page_number, onLaterPages=page_number)

    print(f"Created: {output_file}")
    if font_path:
        print(f"Font used: {font_path}")
    else:
        print("WARNING: using Helvetica fallback; Unicode coverage may be limited.")

def main():
    ap = argparse.ArgumentParser(description="Convert UTF-8 TXT to A4 PDF on Termux.")
    ap.add_argument("input")
    ap.add_argument("output", nargs="?")
    ap.add_argument("--font-size", type=float, default=10.5)
    ap.add_argument("--line-spacing", type=float, default=3.0)
    ap.add_argument("--margin", type=float, default=20)
    args = ap.parse_args()

    if not os.path.isfile(args.input):
        print(f"ERROR: file not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    output = args.output
    if not output:
        output = os.path.splitext(args.input)[0] + ".pdf"
    if not output.lower().endswith(".pdf"):
        output += ".pdf"

    try:
        convert(args.input, output, args.font_size, args.line_spacing, args.margin)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
