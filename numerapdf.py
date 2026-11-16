#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Añade números consecutivos visibles a las páginas de un PDF.
Compatible con Termux.

Instalación:
    python -m pip install pypdf reportlab

Uso:
    python numera_pdf.py documento.pdf
    python numera_pdf.py documento.pdf resultado.pdf
"""

import argparse
import io
import sys
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas


def main():
    parser = argparse.ArgumentParser(
        description="Añade numeración consecutiva visible a un PDF."
    )
    parser.add_argument("entrada", help="PDF de entrada")
    parser.add_argument(
        "salida", nargs="?",
        help="PDF de salida (por defecto: nombre_numerado.pdf)"
    )
    parser.add_argument(
        "--position", choices=["left", "center", "right"],
        default="right",
        help="Posición horizontal: left, center o right (por defecto: right)"
    )
    parser.add_argument(
        "--start", type=int, default=1,
        help="Número de la primera página (por defecto: 1)"
    )
    parser.add_argument(
        "--size", type=float, default=10,
        help="Tamaño de letra en puntos (por defecto: 10)"
    )
    parser.add_argument(
        "--margin", type=float, default=24,
        help="Distancia desde el borde inferior en puntos (por defecto: 24)"
    )
    parser.add_argument(
        "--format", choices=["number", "page-total"],
        default="number",
        help="Formato: número o número/total (por defecto: number)"
    )
    args = parser.parse_args()

    source = Path(args.entrada).expanduser().resolve()

    if not source.is_file():
        parser.error(f"No existe el archivo: {source}")

    if source.suffix.lower() != ".pdf":
        parser.error("El archivo de entrada debe tener extensión .pdf")

    if args.start < 0:
        parser.error("--start no puede ser negativo")
    if args.size <= 0:
        parser.error("--size debe ser mayor que cero")
    if args.margin < 0:
        parser.error("--margin no puede ser negativo")

    if args.salida:
        target = Path(args.salida).expanduser().resolve()
    else:
        target = source.with_name(source.stem + "_numerado.pdf")

    if target == source:
        parser.error(
            "El archivo de salida debe ser distinto del original."
        )

    if target.suffix.lower() != ".pdf":
        parser.error("El archivo de salida debe tener extensión .pdf")

    try:
        reader = PdfReader(str(source), strict=False)

        if reader.is_encrypted:
            try:
                result = reader.decrypt("")
            except Exception:
                result = 0

            if not result:
                sys.exit(
                    "Error: el PDF está protegido con contraseña. "
                    "No se ha podido abrir."
                )

        total = len(reader.pages)
        if total == 0:
            sys.exit("Error: el PDF no contiene páginas.")

        writer = PdfWriter()

        for index, page in enumerate(reader.pages):
            width = float(page.mediabox.width)
            height = float(page.mediabox.height)

            buffer = io.BytesIO()
            pdf_canvas = canvas.Canvas(
                buffer, pagesize=(width, height)
            )
            pdf_canvas.setFont("Helvetica", args.size)

            number = args.start + index

            if args.format == "page-total":
                label = f"{number} / {args.start + total - 1}"
            else:
                label = str(number)

            # Evita situar el texto por debajo del borde de la página.
            y = min(args.margin, max(0, height - args.size - 2))

            if args.position == "left":
                pdf_canvas.drawString(24, y, label)
            elif args.position == "center":
                pdf_canvas.drawCentredString(width / 2, y, label)
            else:
                pdf_canvas.drawRightString(width - 24, y, label)

            pdf_canvas.save()
            buffer.seek(0)

            overlay = PdfReader(buffer).pages[0]
            page.merge_page(overlay)
            writer.add_page(page)

        target.parent.mkdir(parents=True, exist_ok=True)

        with target.open("wb") as output_file:
            writer.write(output_file)

        print(f"Correcto: {total} páginas numeradas.")
        print(f"Archivo creado: {target}")

    except Exception as exc:
        sys.exit(f"Error al procesar el PDF: {exc}")


if __name__ == "__main__":
    main()
