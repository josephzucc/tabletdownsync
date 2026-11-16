#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Crea un índice de páginas iniciales para una futura unión de PDF.

En Termux:
    pkg install qpdf
    python -m pip install pypdf

Uso:
    python indice_pdf.py /ruta/a/la/carpeta
    python indice_pdf.py archivo1.pdf archivo2.pdf
    python indice_pdf.py /ruta/a/la/carpeta --merge

Crea indice_pdf.txt, lista_pdf.txt y unir_pdf.sh.
"""
import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

def natural_key(path):
    return [int(x) if x.isdigit() else x.casefold()
            for x in re.split(r"(\d+)", Path(path).name)]

def page_count(pdf):
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(pdf), strict=False)
        if reader.is_encrypted:
            try:
                if not reader.decrypt(""):
                    raise RuntimeError("PDF cifrado con contraseña")
            except Exception as e:
                raise RuntimeError(f"PDF cifrado o no legible: {e}")
        return len(reader.pages)
    except ImportError:
        pass
    except Exception as exc:
        pypdf_error = exc
    else:
        pypdf_error = None

    qpdf = shutil.which("qpdf")
    if qpdf:
        result = subprocess.run([qpdf, "--show-npages", str(pdf)],
                                capture_output=True, text=True)
        if result.returncode == 0:
            try:
                return int(result.stdout.strip())
            except ValueError:
                pass
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(detail or "qpdf no pudo contar las páginas")
    if 'pypdf_error' in locals() and pypdf_error:
        raise RuntimeError(str(pypdf_error))
    raise RuntimeError("Instala pypdf con 'python -m pip install pypdf' o qpdf con 'pkg install qpdf'.")

def gather(inputs, recursive=False):
    files = []
    for item in inputs:
        p = Path(item).expanduser()
        if p.is_dir():
            it = p.rglob("*") if recursive else p.iterdir()
            files.extend(x.resolve() for x in it if x.is_file() and x.suffix.lower() == ".pdf")
        elif p.is_file() and p.suffix.lower() == ".pdf":
            files.append(p.resolve())
        else:
            print(f"Aviso: no existe o no es PDF: {p}", file=sys.stderr)
    # Quitar duplicados y ordenar de forma natural por nombre.
    return sorted(set(files), key=natural_key)

def main():
    ap = argparse.ArgumentParser(description="Crea índice con página inicial de cada PDF.")
    ap.add_argument("inputs", nargs="+", help="Carpeta(s) y/o archivo(s) PDF")
    ap.add_argument("--recursive", action="store_true", help="Incluir subcarpetas")
    ap.add_argument("--merge", action="store_true", help="Unir también los PDF (requiere qpdf)")
    ap.add_argument("--output", help="Nombre/ruta del PDF unido (con --merge)")
    args = ap.parse_args()

    pdfs = gather(args.inputs, args.recursive)
    if not pdfs:
        print("No se encontraron PDF.", file=sys.stderr)
        return 2

    first = Path(args.inputs[0]).expanduser()
    outdir = (first if first.is_dir() else pdfs[0].parent).resolve()
    index_path = outdir / "indice_pdf.txt"
    list_path = outdir / "lista_pdf.txt"
    sh_path = outdir / "unir_pdf.sh"
    output_pdf = Path(args.output).expanduser().resolve() if args.output else outdir / "combinado.pdf"

    entries, start, errors = [], 1, []
    for pdf in pdfs:
        try:
            n = page_count(pdf)
            entries.append((pdf, n, start))
            print(f"{pdf.name}: {n} páginas; empieza en la página {start}")
            start += n
        except Exception as e:
            errors.append((pdf, str(e)))
            print(f"ERROR {pdf.name}: {e}", file=sys.stderr)

    if errors:
        print("No se generó un índice, porque uno o más PDF no se pudieron leer.", file=sys.stderr)
        return 1

    with list_path.open("w", encoding="utf-8", newline="\n") as f:
        for pdf, n, first_page in entries:
            f.write(f"{pdf}\t{n}\t{first_page}\n")

    with index_path.open("w", encoding="utf-8", newline="\n") as f:
        f.write("ÍNDICE DE DOCUMENTOS PDF\n=========================\n\n")
        for pdf, n, first_page in entries:
            last_page = first_page + n - 1
            f.write(f"Página {first_page:>6} | {pdf.name} (páginas {first_page}-{last_page}; {n} páginas)\n")
        f.write(f"\nTotal: {len(entries)} PDF; {start - 1} páginas.\n")

    def quote(s):
        return "'" + str(s).replace("'", "'\\''") + "'"

    shell = "#!/data/data/com.termux/files/usr/bin/bash\nset -e\n"
    shell += "command -v qpdf >/dev/null 2>&1 || { echo 'Instala qpdf con: pkg install qpdf' >&2; exit 1; }\n"
    shell += "qpdf --empty --pages " + " ".join(quote(p) for p, _, _ in entries) + " -- " + quote(outdir / "combinado.pdf") + "\n"
    shell += "echo 'PDF creado: combinado.pdf'\n"
    sh_path.write_text(shell, encoding="utf-8")
    sh_path.chmod(0o755)

    print(f"\nÍndice: {index_path}")
    print(f"Lista para verificar el orden: {list_path}")
    print(f"Script para unir después: {sh_path}")

    if args.merge:
        qpdf = shutil.which("qpdf")
        if not qpdf:
            print("Falta qpdf. Instálalo con: pkg install qpdf", file=sys.stderr)
            return 1
        if output_pdf in pdfs:
            print("La salida no puede sobrescribir un PDF de entrada.", file=sys.stderr)
            return 1
        cmd = [qpdf, "--empty", "--pages"] + [str(p) for p, _, _ in entries] + ["--", str(output_pdf)]
        result = subprocess.run(cmd)
        if result.returncode:
            return result.returncode
        print(f"PDF unido: {output_pdf}")
    else:
        print("Para unirlos después: bash unir_pdf.sh")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
