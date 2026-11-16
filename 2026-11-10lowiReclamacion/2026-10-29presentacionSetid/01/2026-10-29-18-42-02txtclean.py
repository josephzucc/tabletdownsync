#!/usr/bin/env python3
# txtclean.py - Limpia caracteres de control de archivos TXT.

import argparse
import re
import sys
from pathlib import Path

# Secuencias ANSI: CSI, OSC y otras secuencias ESC.
ANSI_RE = re.compile(
    rb'\x1B(?:\[[0-?]*[ -/]*[@-~]|'
    rb'\][^\x07]*(?:\x07|\x1B\\)|[@-_])'
)

# Controles C0 y DEL, excepto TAB, LF y CR.
CONTROL_RE = re.compile(rb'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]')


def clean(data):
    data, ansi_count = ANSI_RE.subn(b'', data)
    data, control_count = CONTROL_RE.subn(b'', data)
    return data, ansi_count, control_count


def main():
    parser = argparse.ArgumentParser(
        description='Limpia secuencias ANSI y caracteres de control de TXT.'
    )
    parser.add_argument('archivo', help='Archivo TXT de entrada')
    parser.add_argument(
        '-o', '--output',
        help='Archivo de salida (por defecto: nombre-clean.txt)'
    )
    parser.add_argument(
        '--in-place',
        action='store_true',
        help='Modifica el original y crea antes una copia .bak'
    )
    args = parser.parse_args()

    src = Path(args.archivo).expanduser()

    if not src.is_file():
        print(f'ERROR: no existe el archivo: {src}', file=sys.stderr)
        return 2

    if args.in_place and args.output:
        print('ERROR: --in-place y --output son incompatibles.',
              file=sys.stderr)
        return 2

    try:
        original = src.read_bytes()
    except OSError as exc:
        print(f'ERROR al leer: {exc}', file=sys.stderr)
        return 1

    resultado, n_ansi, n_control = clean(original)

    if args.in_place:
        destino = src
        backup = src.with_name(src.name + '.bak')
        try:
            if not backup.exists():
                backup.write_bytes(original)
            src.write_bytes(resultado)
        except OSError as exc:
            print(f'ERROR al guardar: {exc}', file=sys.stderr)
            return 1
        print(f'Copia de seguridad: {backup}')
    else:
        if args.output:
            destino = Path(args.output).expanduser()
        else:
            destino = src.with_name(
                f'{src.stem}-clean{src.suffix}'
            )

        if destino.resolve() == src.resolve():
            print('ERROR: la salida coincide con el original.',
                  file=sys.stderr)
            return 2

        try:
            destino.write_bytes(resultado)
        except OSError as exc:
            print(f'ERROR al guardar: {exc}', file=sys.stderr)
            return 1

    print(f'Entrada: {src}')
    print(f'Salida: {destino}')
    print(f'Tamaño: {len(original)} -> {len(resultado)} bytes')
    print(f'Secuencias ANSI eliminadas: {n_ansi}')
    print(f'Otros controles eliminados: {n_control}')
    print(f'ESC restantes: {resultado.count(bytes([27]))}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
