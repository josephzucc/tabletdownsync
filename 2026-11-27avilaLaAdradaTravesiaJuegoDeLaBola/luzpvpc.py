#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
luzpvpc.py - Precios hora a hora de la luz (PVPC 2.0TD) desde la API publica de REE
(apidatos.ree.es, sin token) y almacenamiento al final de un fichero de texto con el formato:

    dia;tipo;periodo;preciomin;preciomax;preciomedio

Cada hora se guarda como una linea: tipo = tramo de esa hora (valle/llano/punta),
periodo = "H-H+1" (p. ej. 10-11, 23-0), min = max = medio = precio en EUR/kWh.

Solo usa la libreria estandar de Python 3 (funciona en Termux sin instalar nada).

Uso:
    python luzpvpc.py                      -> precios de hoy, fichero *precioMedioLuzPVPC.txt de la carpeta actual
    python luzpvpc.py --manana             -> precios de manana (REE los publica sobre las 20:15)
    python luzpvpc.py --desde 2026-11-01 --hasta 2026-11-13
    python luzpvpc.py --fichero ruta/mi.txt
    python luzpvpc.py --sin-renombrar      -> no cambia el prefijo de fecha/hora del nombre del fichero
"""

import argparse
import glob
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

URL = "https://apidatos.ree.es/es/datos/mercados/precios-mercados-tiempo-real"
SUFIJO_DATOS = "precioMedioLuzPVPC.txt"
SUFIJO_LOG = "luzPVPCdiagnostico.txt"
CABECERA = "dia;tipo;periodo;preciomin;preciomax;preciomedio"
PREFIJO_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2})(.*)$")

_log_lineas = []


def ahora():
    return datetime.now().strftime("%Y-%m-%d-%H-%M-%S")


def log(msg):
    linea = "%s %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    _log_lineas.append(linea)
    print(msg)


# ---------------------------------------------------------------- tramos 2.0TD
def pascua(anio):
    a = anio % 19
    b, c = divmod(anio, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mes = (h + l - 7 * m + 114) // 31
    dia = (h + l - 7 * m + 114) % 31 + 1
    return date(anio, mes, dia)


def festivos_nacionales(anio):
    fijos = [(1, 1), (1, 6), (5, 1), (8, 15), (10, 12), (11, 1), (12, 6), (12, 8), (12, 25)]
    s = set(date(anio, m, d) for m, d in fijos)
    s.add(pascua(anio) - timedelta(days=2))  # Viernes Santo
    return s


def tramo(d, hora):
    """Tramo 2.0TD de la hora 'hora' (0-23) del dia d."""
    if d.weekday() >= 5 or d in festivos_nacionales(d.year):
        return "valle"
    if hora < 8:
        return "valle"
    if hora < 10 or 14 <= hora < 18 or hora >= 22:
        return "llano"
    return "punta"  # 10-14 y 18-22


# ---------------------------------------------------------------- descarga
def descargar(desde, hasta):
    """Devuelve lista de (datetime_local_naive, eur_kwh) del PVPC entre dos fechas."""
    params = urllib.parse.urlencode({
        "start_date": desde.strftime("%Y-%m-%dT00:00"),
        "end_date": hasta.strftime("%Y-%m-%dT23:59"),
        "time_trunc": "hour",
    })
    url = URL + "?" + params
    ultimo = None
    for intento in range(1, 4):
        try:
            req = urllib.request.Request(url, headers={
                "Accept": "application/json", "User-Agent": "luzpvpc/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                datos = json.loads(r.read().decode("utf-8"))
            break
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            ultimo = e
            log("Intento %d fallido (%s): %s" % (intento, url, e))
            time.sleep(2 * intento)
    else:
        raise RuntimeError("No se pudo descargar: %s" % ultimo)

    serie = None
    for it in datos.get("included", []):
        if str(it.get("type", "")).upper() == "PVPC":
            serie = it["attributes"]["values"]
            break
    if not serie:
        raise RuntimeError("La respuesta no contiene la serie PVPC (aun no publicada?)")
    out = []
    for v in serie:
        dt = datetime.strptime(v["datetime"][:19], "%Y-%m-%dT%H:%M:%S")  # hora local de Espana
        out.append((dt, round(float(v["value"]) / 1000.0, 4)))  # EUR/MWh -> EUR/kWh
    return out


# ---------------------------------------------------------------- fichero
def localizar_fichero(arg):
    if arg:
        return arg
    cand = sorted(glob.glob("*" + SUFIJO_DATOS))
    if cand:
        return cand[-1]  # el de prefijo de fecha mas reciente
    return ahora() + SUFIJO_DATOS


def leer_claves(ruta):
    claves = set()
    if not os.path.exists(ruta):
        return claves
    with open(ruta, encoding="utf-8") as f:
        for ln in f:
            p = ln.strip().split(";")
            if len(p) >= 3 and p[0] != "dia":
                claves.add((p[0], p[1], p[2]))
    return claves


def anadir(ruta, filas):
    """Anade filas al final (quita lineas en blanco finales antes)."""
    if os.path.exists(ruta):
        with open(ruta, encoding="utf-8") as f:
            texto = f.read()
    else:
        texto = CABECERA + "\n"
    texto = texto.rstrip("\n\r \t") + "\n"
    texto += "".join(l + "\n" for l in filas)
    with open(ruta, "w", encoding="utf-8", newline="\n") as f:
        f.write(texto)


def renombrar_con_fecha(ruta):
    """Pone al fichero el prefijo YYYY-MM-DD-HH-MM-SS de su ultima escritura."""
    carpeta, nombre = os.path.split(ruta)
    m = PREFIJO_RE.match(nombre)
    resto = m.group(2) if m else nombre
    ts = datetime.fromtimestamp(os.path.getmtime(ruta)).strftime("%Y-%m-%d-%H-%M-%S")
    nuevo = os.path.join(carpeta, ts + resto)
    if nuevo != ruta:
        os.replace(ruta, nuevo)
    return nuevo


def guardar_log(carpeta):
    """Diagnostico acumulado; el fichero se renombra a la fecha/hora de su ultima escritura."""
    previos = sorted(glob.glob(os.path.join(carpeta, "*" + SUFIJO_LOG)))
    ruta = previos[-1] if previos else os.path.join(carpeta, ahora() + SUFIJO_LOG)
    with open(ruta, "a", encoding="utf-8") as f:
        f.write("\n".join(_log_lineas) + "\n")
    renombrar_con_fecha(ruta)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Precios horarios PVPC -> fichero de texto")
    ap.add_argument("--fichero", help="fichero destino (por defecto, el *%s mas reciente)" % SUFIJO_DATOS)
    ap.add_argument("--desde", help="fecha inicial YYYY-MM-DD (por defecto hoy)")
    ap.add_argument("--hasta", help="fecha final YYYY-MM-DD (por defecto = desde)")
    ap.add_argument("--manana", action="store_true", help="descarga los precios de manana")
    ap.add_argument("--sin-renombrar", action="store_true", help="no cambiar el prefijo de fecha del fichero")
    a = ap.parse_args()

    hoy = date.today()
    if a.manana:
        desde = hasta = hoy + timedelta(days=1)
    else:
        desde = datetime.strptime(a.desde, "%Y-%m-%d").date() if a.desde else hoy
        hasta = datetime.strptime(a.hasta, "%Y-%m-%d").date() if a.hasta else desde
    carpeta = os.path.dirname(os.path.abspath(a.fichero)) if a.fichero else os.getcwd()
    rc = 0
    try:
        ruta = localizar_fichero(a.fichero)
        log("Fichero destino: %s | rango %s a %s" % (ruta, desde, hasta))
        claves = leer_claves(ruta)
        filas, vistos, dup = [], set(), 0
        # trozos de 30 dias para no pasarse del limite de la API
        ini = desde
        valores = []
        while ini <= hasta:
            fin = min(ini + timedelta(days=29), hasta)
            valores += descargar(ini, fin)
            ini = fin + timedelta(days=1)
        for dt, precio in valores:
            d = dt.date()
            if not (desde <= d <= hasta):
                continue
            h = dt.hour
            clave = (d.isoformat(), tramo(d, h), "%d-%d" % (h, (h + 1) % 24))
            if clave in claves or clave in vistos:
                dup += 1
                continue
            vistos.add(clave)
            p = "%.4f" % precio
            filas.append("%s;%s;%s;%s;%s;%s" % (clave + (p, p, p)))
        if filas:
            anadir(ruta, filas)
            log("Anadidas %d lineas (%d ya existian u horas repetidas por cambio horario)" % (len(filas), dup))
        else:
            log("Nada que anadir (%d ya existian). Si es 'manana', quiza REE aun no ha publicado." % dup)
        if os.path.exists(ruta) and not a.sin_renombrar:
            ruta = renombrar_con_fecha(ruta)
            log("Fichero final: %s" % ruta)
    except Exception as e:
        rc = 1
        log("ERROR: %s" % e)
    finally:
        try:
            guardar_log(carpeta)
        except OSError as e:
            print("No se pudo escribir el diagnostico: %s" % e)
    return rc


if __name__ == "__main__":
    sys.exit(main())
