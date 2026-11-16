#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Convert local PDFs, online PDF URLs, or folders of PDFs to UTF-8 TXT.

Designed for Termux: uses pdfplumber (not PyMuPDF), detects tables, and
optionally performs OCR automatically when Tesseract and pdftoppm are installed.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import urllib.parse
import urllib.request

try:
    import pdfplumber
except ImportError:
    print("ERROR: falta pdfplumber. Instálalo con: python -m pip install pdfplumber", file=sys.stderr)
    sys.exit(1)


def norm(text):
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text).replace("\u00a0", " ").replace("\u200b", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return re.sub(r"\n{4,}", "\n\n\n", text).strip()


def cell(value):
    if value is None:
        return ""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", str(value)).replace("\u00a0", " ")).strip()


def table_to_text(table):
    rows = [[cell(value) for value in row] for row in (table or []) if row and any(cell(v) for v in row)]
    if not rows:
        return ""
    columns = max(len(row) for row in rows)
    for row in rows:
        row.extend([""] * (columns - len(row)))
    widths = [min(max(len(row[i]) for row in rows), 60) for i in range(columns)]
    rendered = []
    for row_index, row in enumerate(rows):
        values = []
        for i, value in enumerate(row):
            width = widths[i]
            if len(value) > width:
                value = value[:max(0, width - 1)] + "…"
            values.append(value.ljust(width))
        rendered.append(" | ".join(values))
        if row_index == 0 and len(rows) > 1:
            rendered.append("-+-".join("-" * width for width in widths))
    return "\n".join(rendered)


def extract_tables(page):
    found = []
    settings_list = [
        {"vertical_strategy": "lines", "horizontal_strategy": "lines",
         "snap_tolerance": 3, "join_tolerance": 3, "edge_min_length": 3,
         "intersection_tolerance": 3},
        {"vertical_strategy": "text", "horizontal_strategy": "text",
         "min_words_vertical": 2, "min_words_horizontal": 1, "snap_tolerance": 3},
    ]
    for settings in settings_list:
        try:
            tables = page.extract_tables(table_settings=settings) or []
        except Exception:
            continue
        for table in tables:
            text = table_to_text(table)
            if text and text not in found:
                found.append(text)
        if found:
            break
    return found


def ocr_page(pdf_path, page_number, language="spa"):
    """OCR a page if poppler's pdftoppm and Tesseract are installed."""
    tesseract = shutil.which("tesseract")
    pdftoppm = shutil.which("pdftoppm")
    if not tesseract or not pdftoppm:
        return None
    with tempfile.TemporaryDirectory(prefix="pdf2txt_ocr_") as temp_dir:
        base = os.path.join(temp_dir, "page")
        try:
            subprocess.run(
                [pdftoppm, "-f", str(page_number + 1), "-l", str(page_number + 1),
                 "-singlefile", "-r", "200", "-png", pdf_path, base],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True
            )
            image_path = base + ".png"
            if not os.path.isfile(image_path):
                return None
            result = subprocess.run(
                [tesseract, image_path, "stdout", "-l", language, "--psm", "3"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                encoding="utf-8", errors="replace"
            )
            return norm(result.stdout) if result.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            return None


def convert_pdf(pdf_path, output_path=None, tables=True, layout=False, ocr=True, ocr_language="spa"):
    if not os.path.isfile(pdf_path):
        raise FileNotFoundError(pdf_path)
    if output_path is None:
        output_path = os.path.splitext(pdf_path)[0] + ".txt"
    page_blocks = []
    with pdfplumber.open(pdf_path) as pdf:
        total = len(pdf.pages)
        for index, page in enumerate(pdf.pages):
            print(f"Procesando página {index + 1}/{total}...", end="\r", flush=True)
            try:
                text = norm(page.extract_text(layout=layout) or "")
            except Exception as exc:
                print(f"\nAVISO: no se pudo extraer el texto de la página {index + 1}: {exc}", file=sys.stderr)
                text = ""
            # Attempt OCR automatically only for pages with no meaningful extracted text.
            if ocr and sum(char.isalnum() for char in text) < 20:
                scanned_text = ocr_page(pdf_path, index, ocr_language)
                if scanned_text and sum(char.isalnum() for char in scanned_text) > sum(char.isalnum() for char in text):
                    text = scanned_text
            if not text:
                text = "[SIN TEXTO EXTRAÍBLE: puede ser una página escaneada o una imagen]"
            parts = [f"===== PÁGINA {index + 1} =====", text]
            if tables:
                try:
                    detected = extract_tables(page)
                except Exception as exc:
                    print(f"\nAVISO: fallo al detectar tablas en la página {index + 1}: {exc}", file=sys.stderr)
                    detected = []
                if detected:
                    parts.extend(["", "===== TABLA(S) DETECTADA(S) ====="])
                    for table_index, table in enumerate(detected, 1):
                        parts.extend([f"--- TABLA {table_index} ---", table])
            page_blocks.append("\n".join(parts))

    output_path = os.path.abspath(output_path)
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8", newline="\n") as output:
        output.write(norm("\n\n".join(page_blocks)) + "\n")
    print(f"\nTXT creado: {output_path}\nPáginas procesadas: {total}")
    return output_path


def download_pdf(url):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        raise ValueError("La URL debe comenzar por http:// o https://")
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; pdf2txt-termux/2.0)"})
    temp = tempfile.NamedTemporaryFile(prefix="pdf2txt_download_", suffix=".pdf", delete=False)
    temp_path = temp.name
    try:
        with temp:
            with urllib.request.urlopen(request, timeout=90) as response:
                content_type = response.headers.get("Content-Type", "")
                first = response.read(1024)
                if b"%PDF-" not in first:
                    raise ValueError(
                        "La URL no parece devolver un PDF válido "
                        f"(Content-Type: {content_type or 'desconocido'}). Puede requerir iniciar sesión o usar otro enlace."
                    )
                temp.write(first)
                while True:
                    chunk = response.read(128 * 1024)
                    if not chunk:
                        break
                    temp.write(chunk)
        return temp_path
    except Exception:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise


def default_txt_name(source):
    parsed = urllib.parse.urlparse(source)
    if parsed.scheme.lower() in ("http", "https"):
        base = os.path.basename(urllib.parse.unquote(parsed.path))
    else:
        base = os.path.basename(source)
    if base.lower().endswith(".pdf"):
        base = base[:-4]
    base = re.sub(r"[^\w.-]+", "_", base, flags=re.UNICODE).strip("._")
    return (base or "documento") + ".txt"


def process_source(source, output=None, tables=True, layout=False, ocr=True, ocr_language="spa"):
    is_url = urllib.parse.urlparse(source).scheme.lower() in ("http", "https")
    if is_url:
        temporary_pdf = None
        try:
            target = output or default_txt_name(source)
            print(f"Descargando PDF: {source}")
            temporary_pdf = download_pdf(source)
            return convert_pdf(temporary_pdf, target, tables, layout, ocr, ocr_language)
        finally:
            if temporary_pdf and os.path.exists(temporary_pdf):
                try:
                    os.remove(temporary_pdf)
                except OSError:
                    pass

    source = os.path.abspath(os.path.expanduser(source))
    if os.path.isfile(source):
        if not source.lower().endswith(".pdf"):
            raise ValueError("La entrada debe ser un PDF, una carpeta o una URL http(s) de PDF.")
        return convert_pdf(source, output, tables, layout, ocr, ocr_language)
    if os.path.isdir(source):
        if output:
            raise ValueError("No se puede indicar un único archivo de salida al convertir una carpeta.")
        pdfs = sorted(os.path.join(source, name) for name in os.listdir(source) if name.lower().endswith(".pdf"))
        if not pdfs:
            print("No se encontraron archivos PDF en esa carpeta.")
            return
        for pdf_path in pdfs:
            try:
                convert_pdf(pdf_path, None, tables, layout, ocr, ocr_language)
            except Exception as exc:
                print(f"\nERROR procesando {pdf_path}: {exc}", file=sys.stderr)
        return
    raise FileNotFoundError(f"No se encuentra el archivo o carpeta: {source}")


def main():
    parser = argparse.ArgumentParser(
        description="Convierte PDF locales, URL de PDF o carpetas de PDF a TXT UTF-8; detecta tablas y usa OCR si está disponible."
    )
    parser.add_argument("input", help="Ruta a un PDF, carpeta con PDF o URL http(s) de un PDF")
    parser.add_argument("output", nargs="?", help="Ruta de salida TXT (opcional; para un solo PDF o URL)")
    parser.add_argument("--no-tables", action="store_true", help="No intentar detectar tablas")
    parser.add_argument("--layout", action="store_true", help="Intentar conservar la distribución visual del texto")
    parser.add_argument("--no-ocr", action="store_true", help="Desactivar OCR automático")
    parser.add_argument("--ocr-lang", default="spa", help="Idioma de Tesseract para OCR (por defecto: spa)")
    args = parser.parse_args()
    try:
        process_source(args.input, args.output, not args.no_tables, args.layout,
                       not args.no_ocr, args.ocr_lang)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
