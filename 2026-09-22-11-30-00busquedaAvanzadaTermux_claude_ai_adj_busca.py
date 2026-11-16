#!/usr/bin/env python3
"""
busca_es.py

Busca un termino (palabra completa) en archivos de texto de una o varias
carpetas, y descarta las coincidencias cuyo contexto no este en el idioma
indicado (por defecto, espanol).

Instalacion:
    pip install langdetect
    (si Termux da error de "externally managed environment":
     pip install langdetect --break-system-packages)
    (si en vez de eso falla al compilar/instalar con un error tipo
     "install_layout" o "Failed building wheel", crea un entorno virtual
     y prueba ahi dentro:
       python -m venv venv && venv/bin/pip install langdetect
     y luego ejecuta el script con venv/bin/python busca_es.py ...)

Uso:
    python busca_es.py "palabra" /ruta1 /ruta2
    python busca_es.py "palabra" /ruta -e txt,md -i -l es
"""
import argparse
import os
import re
import sys

try:
    from langdetect import detect, DetectorFactory
    from langdetect.lang_detect_exception import LangDetectException
    DetectorFactory.seed = 0  # para que detect() sea reproducible
except ImportError:
    sys.exit("Falta langdetect. Instala con: pip install langdetect")


def leer_lineas(ruta):
    """Lee un archivo probando varias codificaciones habituales en texto
    en espanol, en orden de fiabilidad: utf-8 (la mas comun hoy en dia),
    cp1252 (tipica de archivos viejos de Windows, incluye comillas
    tipograficas) y por ultimo latin-1, que nunca falla pero puede
    interpretar mal algun caracter suelto si ninguna de las anteriores
    encaja. Sin esto, un archivo .txt guardado en Latin-1 (muy comun en
    exports antiguos) pierde tildes y enes al forzar utf-8, y terminos
    como 'año' dejan de encontrarse aunque esten ahi."""
    for codificacion in ('utf-8', 'cp1252', 'latin-1'):
        try:
            with open(ruta, encoding=codificacion) as f:
                return f.readlines()
        except UnicodeDecodeError:
            continue
        except OSError:
            return None
    return None


def extraer_contexto(lineas, idx, minimo):
    """Devuelve la linea del match, ampliada con lineas vecinas si es
    demasiado corta para que la deteccion de idioma sea fiable."""
    contexto = lineas[idx].strip()
    salto = 1
    while len(contexto) < minimo:
        crecio = False
        if idx - salto >= 0:
            contexto = lineas[idx - salto].strip() + " " + contexto
            crecio = True
        if idx + salto < len(lineas):
            contexto = contexto + " " + lineas[idx + salto].strip()
            crecio = True
        if not crecio:
            break
        salto += 1
    return contexto


def detectar(texto):
    try:
        return detect(texto)
    except LangDetectException:
        return None


def iter_archivos(rutas, extensiones):
    """Recorre las rutas dadas y devuelve los archivos a revisar. Admite
    tanto carpetas (recorridas recursivamente y filtradas por extension)
    como un archivo suelto (se revisa igual, sin filtrar por extension,
    porque el usuario ya lo señalo explicitamente). Avisa por stderr si
    una ruta no existe, en vez de devolver 0 resultados en silencio."""
    for raiz in rutas:
        if not os.path.exists(raiz):
            print(f"Aviso: no existe la ruta '{raiz}', se omite.", file=sys.stderr)
            continue
        if os.path.isfile(raiz):
            yield raiz
            continue
        for carpeta, _, ficheros in os.walk(raiz):
            for nombre in ficheros:
                if extensiones and not nombre.lower().endswith(extensiones):
                    continue
                yield os.path.join(carpeta, nombre)


def buscar(termino, rutas, extensiones, minimo_contexto, sin_mayus, idioma):
    flags = re.IGNORECASE if sin_mayus else 0
    # \b en Python es Unicode-aware: reconoce tildes y ñ como parte de la
    # palabra sin depender del locale (a diferencia de grep en algunos casos)
    patron = re.compile(r'\b' + re.escape(termino) + r'\b', flags)

    total, validas = 0, 0
    for ruta in iter_archivos(rutas, extensiones):
        lineas = leer_lineas(ruta)
        if lineas is None:
            continue

        for i, linea in enumerate(lineas):
            if not patron.search(linea):
                continue
            total += 1
            contexto = extraer_contexto(lineas, i, minimo_contexto)
            detectado = detectar(contexto)
            if detectado == idioma:
                validas += 1
                print(f"{ruta}:{i + 1}: {linea.strip()}")

    print(f"\n{validas}/{total} coincidencias en '{idioma}'", file=sys.stderr)


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description="Busca un termino y filtra por idioma del contexto")
    ap.add_argument('termino', help="palabra o frase a buscar")
    ap.add_argument('rutas', nargs='+', help="una o varias carpetas donde buscar")
    ap.add_argument('-e', '--extensiones', default='txt',
                     help="extensiones separadas por coma (default: txt)")
    ap.add_argument('-c', '--contexto', type=int, default=60,
                     help="minimo de caracteres de contexto para detectar idioma (default: 60)")
    ap.add_argument('-i', '--sin-mayus', action='store_true',
                     help="ignora mayusculas/minusculas en la busqueda")
    ap.add_argument('-l', '--idioma', default='es',
                     help="idioma a conservar: es, en, fr... (default: es)")
    args = ap.parse_args()

    exts = tuple('.' + e.strip().lstrip('.') for e in args.extensiones.split(','))
    buscar(args.termino, args.rutas, exts, args.contexto, args.sin_mayus, args.idioma)
