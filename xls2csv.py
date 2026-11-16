#!/usr/bin/env python3
"""xls2csv: conversor de libros Excel .xls/.xlsx a CSV para Termux."""

import argparse
import sys
import traceback
import zipfile
from pathlib import Path

try:
    import pandas as pd
except ImportError:
    print("ERROR: falta pandas. Instale con: pip install pandas openpyxl xlrd", flush=True)
    sys.exit(2)

EXTENSIONS = {".xls", ".xlsx"}


def log(message=""):
    print(message, flush=True)


def detect_engine(path):
    """Detecta el formato por el contenido, no solo por la extension."""
    with path.open("rb") as stream:
        signature = stream.read(8)
    log(f"  Firma inicial (hex): {signature.hex(' ')}")

    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
        if "[Content_Types].xml" in names and "xl/workbook.xml" in names:
            return "openpyxl"
        raise ValueError(
            "Es un ZIP, pero no parece un libro XLSX valido "
            "(faltan [Content_Types].xml o xl/workbook.xml)."
        )

    if signature == bytes.fromhex("D0CF11E0A1B11AE1"):
        return "xlrd"

    raise ValueError(
        "Formato no reconocido. El archivo puede estar danado, incompleto, "
        "tener una extension incorrecta o no ser un libro Excel."
    )


def output_paths(path, sheet_names):
    """Una hoja: mismo nombre base.csv. Varias: un CSV por hoja."""
    if len(sheet_names) == 1:
        return {sheet_names[0]: path.with_suffix(".csv")}

    outputs = {}
    for sheet_name in sheet_names:
        safe_name = "".join(
            "_" if char in '<>:"/\\|?*' else char
            for char in str(sheet_name)
        ).strip() or "hoja"
        outputs[sheet_name] = path.with_name(f"{path.stem}_{safe_name}.csv")
    return outputs


def convert_file(path):
    """Convierte un archivo; devuelve True solo si se escribieron sus CSV."""
    log()
    log(f"Archivo: {path}")

    if not path.exists():
        log("  ERROR: el archivo no existe.")
        return False
    if not path.is_file():
        log("  ERROR: la ruta no es un archivo.")
        return False
    if path.suffix.lower() not in EXTENSIONS:
        log(f"  ERROR: extension no admitida: {path.suffix!r}. Use .xls o .xlsx.")
        return False

    try:
        size = path.stat().st_size
        log(f"  Tamano: {size} bytes")
        if size == 0:
            raise ValueError("El archivo esta vacio (0 bytes).")

        engine = detect_engine(path)
        log(f"  Formato detectado; motor elegido: {engine}")

        sheets = pd.read_excel(path, sheet_name=None, engine=engine)
        if not sheets:
            raise ValueError("No se encontraron hojas legibles.")

        log(f"  Hojas encontradas: {len(sheets)}")
        outputs = output_paths(path, list(sheets.keys()))

        for sheet_name, dataframe in sheets.items():
            destination = outputs[sheet_name]
            log(f"  Hoja: {sheet_name!r}")
            log(f"    Filas de datos: {len(dataframe)}")
            log(f"    Columnas: {len(dataframe.columns)}")
            log(f"    CSV de salida: {destination}")

            dataframe.to_csv(destination, index=False, encoding="utf-8-sig")

            if not destination.is_file():
                raise OSError(f"No se creo el archivo esperado: {destination}")
            log(f"    OK: creado ({destination.stat().st_size} bytes)")

        log("  RESULTADO: conversion terminada correctamente.")
        return True

    except Exception as exc:
        log(f"  ERROR: {type(exc).__name__}: {exc}")
        log("  Detalle tecnico:")
        traceback.print_exc(file=sys.stdout)
        return False


def main():
    parser = argparse.ArgumentParser(
        prog="xls2csv",
        description="Convierte un archivo Excel o los Excel de una carpeta a CSV.",
        epilog=(
            "Ejemplos: xls2csv archivo.xls | "
            "xls2csv '/ruta/archivo con espacios.xlsx' | "
            "xls2csv -f '/ruta/DownloadSync'"
        ),
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("archivo", nargs="?", help="archivo Excel .xls o .xlsx")
    group.add_argument(
        "-f", "--folder", metavar="CARPETA",
        help="convertir los .xls y .xlsx directamente dentro de esta carpeta",
    )
    args = parser.parse_args()

    log("=== xls2csv: conversor Excel a CSV ===")
    log(f"Python: {sys.version.split()[0]}")
    log(f"Pandas: {pd.__version__}")

    if args.archivo:
        ok = convert_file(Path(args.archivo).expanduser())
        sys.exit(0 if ok else 1)

    folder = Path(args.folder).expanduser()
    log(f"Carpeta indicada: {folder}")
    log(f"Carpeta existe: {folder.is_dir()}")

    if not folder.is_dir():
        log("ERROR: la carpeta no existe o no es accesible.")
        log("Compruebe la ruta y los permisos de almacenamiento de Android.")
        sys.exit(2)

    try:
        files = sorted(
            (
                item for item in folder.iterdir()
                if item.is_file()
                and item.suffix.lower() in EXTENSIONS
                and not item.name.startswith("~$")
            ),
            key=lambda item: item.name.lower(),
        )
    except OSError as exc:
        log(f"ERROR al listar la carpeta: {exc}")
        sys.exit(2)

    log(f"Archivos Excel encontrados (.xls/.xlsx): {len(files)}")
    if not files:
        log("No se encontraron archivos Excel compatibles en esa carpeta.")
        log("Contenido de la carpeta (maximo 30 entradas):")
        try:
            for item in list(folder.iterdir())[:30]:
                log(f"  {item.name}")
        except OSError as exc:
            log(f"ERROR al listar contenido: {exc}")
        sys.exit(1)

    succeeded = 0
    failed = 0
    for path in files:
        if convert_file(path):
            succeeded += 1
        else:
            failed += 1

    log()
    log("=== RESUMEN ===")
    log(f"Archivos encontrados: {len(files)}")
    log(f"Convertidos correctamente: {succeeded}")
    log(f"Fallidos: {failed}")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
