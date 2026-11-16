#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""txt2pdf 2.3 - Convert UTF-8 TXT to PDF, rendering Markdown and ASCII box tables.

Usage:
  python txt2pdf.py input.txt
  python txt2pdf.py input.txt output.pdf
  python txt2pdf.py input.txt --landscape
  python txt2pdf.py input.txt --font-size 10 --margin 18 --debug

Recognizes Markdown pipe tables and ASCII box tables such as +---+---+.
  | Column A | Column B |
  | -------- | -------- |
  | Value 1  | Value 2  |

The separator row is used to detect a table and is not printed as content.
ANSI terminal escape sequences accidentally saved in a TXT are removed.
"""

import argparse
import os
import re
import sys
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, LongTable, TableStyle
)

VERSION = "2.3"
FINDS="CONTEXT"
#FINDS="SHADOW"

def register_unicode_font():
    candidates = [
        ("/system/fonts/HarmonyOS_Sans.ttf", "Txt2PdfUnicode"),
        ("/system/fonts/Roboto-Regular.ttf", "Txt2PdfUnicode"),
        ("/system/fonts/NotoSans-Regular.ttf", "Txt2PdfUnicode"),
        ("/system/fonts/RobotoStatic-Regular.ttf", "Txt2PdfUnicode"),
        ("/data/data/com.termux/files/usr/share/fonts/TTF/DejaVuSans.ttf", "Txt2PdfUnicode"),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "Txt2PdfUnicode"),
        ("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf", "Txt2PdfUnicode"),
    ]
    for path, name in candidates:
        if os.path.isfile(path):
            try:
                if name not in pdfmetrics.getRegisteredFontNames():
                    pdfmetrics.registerFont(TTFont(name, path))
                return name, path
            except Exception:
                continue
    return "Helvetica", None


def strip_ansi(text):
    """Remove ANSI/terminal control sequences such as ESC[H and ESC[J."""
    text = re.sub(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|[@-_])", "", text)
    return text.replace("\x00", "")


def split_pipe_row(line):
    """Split a pipe-delimited row, supporting optional outer pipes and an escaped pipe."""
    line = line.strip()
    if "|" not in line:
        return None
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    cells, current = [], []
    escaped_pipe = False
    findsnot=False
    for ch in line:
        if ch == "|" and not escaped_pipe:
            cells.append("".join(current).strip())
            current = []
        else:
            #if findsnot:
            #    print(f"Anadiendo {ch}")
            current.append(ch)
        if ch == "\\" and not escaped_pipe:
            escaped_pipe = True
        else:
            escaped_pipe = False
    cells.append("".join(current).strip())
    return cells


def is_separator_row(line):
    cells = split_pipe_row(line)
    if not cells:
        return False
    return all(bool(re.fullmatch(r":?-{3,}:?", re.sub(r"\s+", "", cell)))
               for cell in cells)


def looks_like_table_start(lines, index):
    """A Markdown table is a pipe row followed immediately by a dash separator."""
    if index + 1 >= len(lines):
        return False
    header = split_pipe_row(lines[index])
    separator = split_pipe_row(lines[index + 1])
    return (
        header is not None
        and len(header) >= 2
        and separator is not None
        and is_separator_row(lines[index + 1])
        and len(separator) == len(header)
    )


def is_ascii_border(line):
    """Recognize box-table borders such as +------+--------+."""
    return bool(re.fullmatch(r"\s*\+(?:[-=]+\+)+\s*", line))


def looks_like_ascii_table_start(lines, index):
    """An ASCII table starts with a +---+ border followed by a pipe row."""
    return (index + 1 < len(lines)
            and is_ascii_border(lines[index])
            and "|" in lines[index + 1]
            and index + 2 < len(lines)
            and (is_ascii_border(lines[index + 2]) or "|" in lines[index + 2]))


def parse_ascii_table(lines, start):
    """Parse bordered text tables; border lines are discarded, wrapped rows retained."""
    rows = []
    i = start
    while i < len(lines):
        line = lines[i]
        if is_ascii_border(line):
            # A border closes the current row/table; skip it and continue if another row follows.
            i += 1
            if i >= len(lines) or not ("|" in lines[i]):
                break
            continue
        if "|" not in line:
            break
        cells = split_pipe_row(line)
        if cells is None:
            break
        rows.append(cells)
        i += 1
    if rows:
        ncols = max(len(row) for row in rows)
        normalized = []
        for row in rows:
            if len(row) < ncols:
                row += [""] * (ncols - len(row))
            elif len(row) > ncols:
                row = row[:ncols - 1] + [" | ".join(row[ncols - 1:])]
            normalized.append(row)
            print(f"{row}")
        return normalized, i
    return [], start


def parse_table(lines, start):
    """Return normalized table rows and index of first line after the table."""
    header = split_pipe_row(lines[start])
    rows = [header]
    i = start + 2  # skip the Markdown separator row
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            break
        cells = split_pipe_row(line)
        if cells is None or is_separator_row(line):
            break
        if len(cells) < len(header):
            cells.extend([""] * (len(header) - len(cells)))
        elif len(cells) > len(header):
            # Preserve extra pipes as part of the final cell rather than losing text.
            cells = cells[:len(header) - 1] + [" | ".join(cells[len(header) - 1:])]
        rows.append(cells)
        i += 1
    return rows, i


def para_text(value):
    # ReportLab Paragraph accepts a small XML-like markup language.
    return escape(str(value)).replace("\n", "<br/>")


def add_page_number(canvas, doc):
    canvas.saveState()
    canvas.setFont(doc.font_name, 8)
    canvas.setFillColor(colors.grey)
    canvas.drawCentredString(doc.pagesize[0] / 2, 6 * mm, f"Page {doc.page}")
    canvas.restoreState()


def create_pdf(input_path, output_path, font_size=10.5, line_spacing=1.15,
               margin=10, landscape_mode=False, debug=False):
    with open(input_path, "r", encoding="utf-8-sig", errors="replace") as f:
        content = strip_ansi(f.read())
    lines = content.splitlines()

    font_name, font_path = register_unicode_font()
    pagesize = landscape(A4) if landscape_mode else A4
    # A4 print layout: 10 mm lateral/top margins and 14 mm bottom
    # clearance so the page number remains inside the printable area.
    margin_pt = margin * mm
    bottom_margin_pt = max(margin, 14) * mm
    doc = SimpleDocTemplate(
        output_path, pagesize=pagesize,
        leftMargin=margin_pt, rightMargin=margin_pt,
        topMargin=margin_pt, bottomMargin=bottom_margin_pt,
        title=os.path.basename(input_path), author="txt2pdf",
        allowSplitting=1,
    )
    doc.font_name = font_name
    doc.pagesize = pagesize

    body_style = ParagraphStyle(
        "TxtBody", fontName=font_name, fontSize=font_size,
        leading=font_size * line_spacing, spaceAfter=2.0,
        wordWrap="CJK", splitLongWords=1,
    )
    heading_style = ParagraphStyle(
        "TxtHeading", parent=body_style, fontSize=font_size + 1,
        leading=(font_size + 1) * line_spacing,
        spaceBefore=3, spaceAfter=2,
    )
    cell_size = max(7, font_size - 1)
    cell_style = ParagraphStyle(
        "TxtTableCell", fontName=font_name, fontSize=cell_size,
        leading=cell_size * 1.2, spaceAfter=0, spaceBefore=0,
        wordWrap="CJK", splitLongWords=1,
    )
    header_style = ParagraphStyle(
        "TxtTableHeader", parent=cell_style,
        textColor=colors.white, alignment=TA_LEFT,
    )

    story = []
    tables_found = 0
    i = 0
    while i < len(lines):
        line = lines[i]
        #print (f"{line}")

        if looks_like_ascii_table_start(lines, i):
            rows, next_i = parse_ascii_table(lines, i + 1)
            if rows:
                ncols = len(rows[0])
                usable_width = pagesize[0] - doc.leftMargin - doc.rightMargin
                maxlens = [max(1, max(len(row[col]) if col < len(row) else 0
                                      for row in rows)) for col in range(ncols)]
                weights = [max(5, min(80, length)) ** 0.72 for length in maxlens]
                weight_sum = sum(weights)
                widths = [usable_width * weight / weight_sum for weight in weights]
                data = []
                for row_index, row in enumerate(rows):
                    style = header_style if row_index == 0 else cell_style
                    #print(f"{row}")
                    data.append([Paragraph(para_text(row[col] if col < len(row) else ""), style)
                                 for col in range(ncols)])
                #print (f"{data}")
                table = LongTable(data, colWidths=widths, repeatRows=1,
                                  hAlign="LEFT", splitByRow=1)
                table.setStyle(TableStyle([
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#35506b")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#7d8790")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 3),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                    ("TOPPADDING", (0, 0), (-1, -1), 2.5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1),
                     [colors.white, colors.HexColor("#f1f4f7")]),
                ]))
                story.append(table)
                story.append(Spacer(1, font_size * 0.45))
                tables_found += 1
                if debug:
                    print(f"DEBUG: ASCII table detected at line {i + 1}: {len(rows)} rows, {ncols} columns")
                i = next_i
                continue

        if looks_like_table_start(lines, i):
            rows, next_i = parse_table(lines, i)
            ncols = len(rows[0])
            usable_width = pagesize[0] - doc.leftMargin - doc.rightMargin

            # Calculate widths from actual cell contents; keep every column usable.
            maxlens = []
            for col in range(ncols):
                maxlens.append(max(1, max(len(row[col]) if col < len(row) else 0
                                          for row in rows)))
            weights = [max(5, min(80, length)) ** 0.72 for length in maxlens]
            weight_sum = sum(weights)
            widths = [usable_width * weight / weight_sum for weight in weights]

            data = []
            for row_index, row in enumerate(rows):
                style = header_style if row_index == 0 else cell_style
                data.append([Paragraph(para_text(row[col] if col < len(row) else ""), style)
                             for col in range(ncols)])
                #print(f"{row}")

            #print (f"{data}")
            table = LongTable(data, colWidths=widths, repeatRows=1,
                              hAlign="LEFT", splitByRow=1)
            table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#35506b")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#7d8790")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 2.5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1),
                 [colors.white, colors.HexColor("#f1f4f7")]),
            ]))
            story.append(table)
            story.append(Spacer(1, font_size * 0.45))
            tables_found += 1
            if debug:
                print(f"DEBUG: table detected at line {i + 1}: {len(rows)} rows, {ncols} columns")
            i = next_i
            continue

        stripped = line.strip()
        if not stripped:
            story.append(Spacer(1, font_size * 0.30))
        else:
            safe = para_text(line)
            # Keep ordinary text's leading indentation approximately.
            leading_spaces = len(line) - len(line.lstrip(" "))
            if leading_spaces:
                safe = ("&nbsp;" * leading_spaces) + para_text(line.lstrip(" "))
            is_heading = ((len(stripped) <= 100 and stripped.endswith(":"))
                          or (stripped.isupper() and len(stripped) <= 100)
                          or (len(stripped) <= 100 and re.fullmatch(r"=+|-{3,}", stripped)))
            story.append(Paragraph(safe, heading_style if is_heading else body_style))
        i += 1

    #print(f"{story}")
    if not story:
        story.append(Paragraph("", body_style))
    doc.build(story, onFirstPage=add_page_number, onLaterPages=add_page_number)
    return font_path, tables_found


def main():
    parser = argparse.ArgumentParser(
        description="Convert TXT to PDF; Markdown pipe tables and ASCII box tables become real bordered tables."
    )
    parser.add_argument("input", help="Input TXT file")
    parser.add_argument("output", nargs="?", help="Output PDF (default: same base name)")
    parser.add_argument("--font-size", type=float, default=10.5,
                        help="Body font size in points (default: 10.5)")
    parser.add_argument("--line-spacing", type=float, default=1.15,
                        help="Line spacing multiplier (default: 1.15)")
    parser.add_argument("--margin", type=float, default=10,
                        help="Left, right and top margins in millimetres (default: 10; bottom is at least 14 mm for page number)")
    parser.add_argument("--landscape", action="store_true",
                        help="Use landscape A4 orientation")
    parser.add_argument("--debug", action="store_true",
                        help="Print table detection and conversion diagnostics")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    args = parser.parse_args()

    input_path = os.path.abspath(args.input)
    if not os.path.isfile(input_path):
        print(f"ERROR: input file not found: {input_path}", file=sys.stderr)
        return 2
    if args.font_size <= 0 or args.line_spacing <= 0 or args.margin <= 0:
        print("ERROR: font size, line spacing and margin must be greater than zero.", file=sys.stderr)
        return 2
    output_path = os.path.abspath(args.output or os.path.splitext(input_path)[0] + ".pdf")
    if input_path == output_path:
        print("ERROR: output PDF must not overwrite the input TXT file.", file=sys.stderr)
        return 2
    if not output_path.lower().endswith(".pdf"):
        output_path += ".pdf"

    try:
        font_path, tables_found = create_pdf(
            input_path, output_path, font_size=args.font_size,
            line_spacing=args.line_spacing, margin=args.margin,
            landscape_mode=args.landscape, debug=args.debug,
        )
    except Exception as exc:
        print(f"ERROR: PDF creation failed: {exc}", file=sys.stderr)
        return 1

    print(f"OK: PDF created: {output_path}")
    print(f"Tables rendered: {tables_found}")
    print(f"Font: {font_path or 'Helvetica (fallback)'}")
    if tables_found == 0:
        print("WARNING: no Markdown pipe tables detected; check that each table has a separator row like | --- | --- |.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
