#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cta.py - Paradas, lineas y horarios del Consorcio de Transportes de Asturias (CTA)

Fuente de datos: feed GTFS oficial del CTA (Punto de Acceso Nacional de Transporte).
Solo usa la libreria estandar de Python. Pensado para Termux.

Todo el texto normal sale por stdout; solo los errores salen por stderr.
Ver ctaManualBono.txt para el manual completo.
"""

VERSION = "1.0"

import sys
import os
import re
import csv
import io
import json
import zipfile
import sqlite3
import unicodedata
import difflib
import datetime
import math
import argparse
import threading
import urllib.request
import urllib.error
import urllib.parse
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME = os.path.expanduser("~")
DIR = os.environ.get("CTA_DIR") or os.path.join(HOME, ".cta")
DB = os.path.join(DIR, "cta.db")
ZIPF = os.path.join(DIR, "gtfs.zip")
CONF = os.path.join(DIR, "config.json")
F_LOCALIDADES = os.path.join(DIR, "localidades.txt")
F_OVERRIDES = os.path.join(DIR, "paradas_concello.txt")

URL_NAP = "https://nap.transportes.gob.es/api/Fichero/download/1178"
URL_MDB_LATEST = "https://files.mobilitydatabase.org/mdb-2827/latest.zip"
URL_MDB_FICHA = "https://mobilitydatabase.org/feeds/gtfs/mdb-2827"

DIAS_ES = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]
DIAS_ES_ACENTO = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]

# ----------------------------------------------------------------------------
# Salida
# ----------------------------------------------------------------------------

class Tee:
    """Duplica stdout en un fichero (opcion --txt)."""
    def __init__(self, stream, fichero):
        self.stream = stream
        self.fichero = fichero

    def write(self, s):
        self.stream.write(s)
        self.fichero.write(s)

    def flush(self):
        self.stream.flush()
        self.fichero.flush()


def out(s=""):
    print(s)


def err(s):
    print(s, file=sys.stderr)


def norm(s):
    s = unicodedata.normalize("NFD", str(s or "").lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def clave_natural(s):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", str(s).lower())]


# ----------------------------------------------------------------------------
# Catalogo de concellos (nombre | alias separados por ; | lat | lon de la capital)
# Las coordenadas son aproximadas y solo se usan como ultimo recurso.
# ----------------------------------------------------------------------------

CONCEJOS_TXT = """
Allande|Pola de Allande|43.2667|-6.6000
Aller|Ayer;Cabañaquinta|43.1667|-5.6000
Amieva|Sames|43.2167|-5.0833
Avilés|Aviles|43.5547|-5.9248
Belmonte de Miranda|Belmonte|43.2667|-6.3167
Bimenes|Martimporra|43.3000|-5.5333
Boal|Boal|43.4167|-6.8167
Cabrales|Arenas de Cabrales;Carreña|43.3000|-4.8400
Cabranes|Santa Eulalia de Cabranes|43.4167|-5.4333
Candamo|Grullos|43.4333|-6.1000
Cangas de Onís|Cangues d'Onís;Cangas de Onis|43.3500|-5.1333
Cangas del Narcea|Cangas del Narcea|43.1833|-6.5500
Caravia|Caravia|43.4667|-5.2000
Carreño|Carreno;Candás;Candas|43.5875|-5.7625
Caso|Casu;Campo de Caso|43.1900|-5.3800
Castrillón|Castrillon;Piedras Blancas;Salinas;Arnao|43.5528|-5.9672
Castropol|Castropol|43.5167|-6.9833
Coaña|Coana|43.5500|-6.8000
Colunga|Colunga|43.4833|-5.2667
Corvera de Asturias|Corvera;Nubledo|43.5333|-5.8167
Cudillero|Cuideiru|43.5619|-6.1472
Degaña|Degana|42.9500|-6.5833
El Franco|Franco El;La Caridad|43.5333|-6.8500
Gijón|Gijon;Xixón;Xixon|43.5322|-5.6611
Gozón|Gozon;Luanco|43.6139|-5.7917
Grado|Grau|43.3833|-6.0667
Grandas de Salime|Grandas|43.2167|-6.8833
Ibias|San Antolín de Ibias;San Antolin de Ibias|43.0167|-6.7500
Illano|Illano|43.3000|-6.8000
Illas|Illas|43.5000|-5.9333
Langreo|Llangréu;Llangreu;Sama de Langreo;La Felguera;Ciaño;Ciano|43.3000|-5.6833
Laviana|Llaviana;Pola de Laviana|43.2500|-5.5667
Lena|Llena;Pola de Lena|43.1583|-5.8333
Llanera|Posada de Llanera|43.4167|-5.8333
Llanes|Llanes|43.4200|-4.7500
Mieres|Mieres del Camín;Mieres del Camin|43.2503|-5.7706
Morcín|Morcin;Santa Eulalia de Morcín;Santa Eulalia de Morcin|43.2667|-5.8500
Muros de Nalón|Muros de Nalon;Muros|43.5500|-6.1000
Nava|Nava|43.3500|-5.5167
Navia|Navia|43.5400|-6.7200
Noreña|Norena|43.3833|-5.7000
Onís|Onis;Benia de Onís;Benia de Onis|43.3667|-5.0500
Oviedo|Uviéu;Uvieu|43.3614|-5.8494
Parres|Arriondas|43.3833|-5.1833
Peñamellera Alta|Penamellera Alta|43.3000|-4.7333
Peñamellera Baja|Penamellera Baja;Panes|43.3000|-4.5833
Pesoz|Pesoz|43.3333|-6.8333
Piloña|Pilona;Infiesto|43.3500|-5.3667
Ponga|San Juan de Beleño;Beleno|43.2167|-5.2333
Pravia|Pravia|43.4833|-6.1167
Proaza|Proaza|43.2667|-6.0667
Quirós|Quiros;Bárzana;Barzana|43.1667|-6.0667
Las Regueras|Regueras Las;Regueras;Les Regueres|43.4000|-6.0000
Ribadedeva|Colombres|43.3667|-4.5667
Ribadesella|Ribeseya|43.4667|-5.0667
Ribera de Arriba|Soto de Ribera|43.3333|-5.9833
Riosa|Riosa|43.2833|-5.9000
Salas|Salas|43.4167|-6.2500
San Martín de Oscos|San Martin de Oscos|43.2833|-6.9833
San Martín del Rey Aurelio|San Martin del Rey Aurelio;SMRA;Samartín del Rei Aurelio;Sotrondio|43.2833|-5.6167
San Tirso de Abres|San Tirso de Abres|43.4000|-7.0000
Santa Eulalia de Oscos|Santa Eulalia de Oscos|43.3000|-7.0000
Santo Adriano|Santu Adrianu|43.3000|-6.0667
Sariego|Sariego|43.4167|-5.5667
Siero|Sieru;Pola de Siero|43.3911|-5.6639
Sobrescobio|Sobrescobiu;Rioseco|43.2167|-5.4833
Somiedo|Somiedu;Pola de Somiedo|43.0833|-6.2500
Soto del Barco|Soto del Barco|43.5500|-6.0667
Tapia de Casariego|Tapia|43.5667|-6.9433
Taramundi|Taramundi|43.3500|-7.1000
Teverga|Teberga;San Martín de Teverga;San Martin de Teverga|43.1500|-6.0833
Tineo|Tinéu;Tineu|43.3333|-6.4167
Valdés|Valdes;Luarca|43.5400|-6.5333
Vegadeo|Vegadeo|43.4667|-7.0500
Villanueva de Oscos|Villanueva de Oscos|43.3000|-6.9500
Villaviciosa|Villaviciosa|43.4833|-5.4333
Villayón|Villayon|43.4500|-6.7167
Yernes y Tameza|Yernes;Tameza|43.3167|-6.1167
"""

# Localidades conocidas (no son concello) -> concello. Se pueden ampliar en localidades.txt
LOCALIDADES_TXT = """
Lugones=Siero
La Fresneda=Siero
Colloto=Siero
El Berrón=Siero
Pola de Siero=Siero
Sama=Langreo
Riaño=Langreo
Lada=Langreo
Tuilla=Langreo
El Entrego=San Martín del Rey Aurelio
Blimea=San Martín del Rey Aurelio
Moreda=Aller
Felechosa=Aller
Turón=Mieres
Ujo=Mieres
Figaredo=Mieres
Ablaña=Mieres
Baíña=Mieres
Trubia=Oviedo
Tudela Veguín=Oviedo
La Corredoria=Oviedo
Olloniego=Oviedo
Perlora=Carreño
San Martín de Teverga=Teverga
Pola de Somiedo=Somiedo
Pola de Allande=Allande
"""

CONC = {}      # nombre canonico -> {"lat":..,"lon":..}
ALIAS = {}     # alias normalizado -> nombre canonico


def cargar_catalogo():
    CONC.clear()
    ALIAS.clear()
    for linea in CONCEJOS_TXT.strip().splitlines():
        partes = linea.split("|")
        nombre = partes[0].strip()
        alias = [a for a in partes[1].split(";") if a.strip()]
        CONC[nombre] = {"lat": float(partes[2]), "lon": float(partes[3])}
        for a in [nombre] + alias:
            ALIAS[norm(a)] = nombre
    # variantes de articulo ("Franco, El")
    ALIAS[norm("Franco, El")] = "El Franco"
    ALIAS[norm("Regueras, Las")] = "Las Regueras"
    texto = LOCALIDADES_TXT
    if os.path.exists(F_LOCALIDADES):
        try:
            with open(F_LOCALIDADES, encoding="utf-8") as f:
                texto += "\n" + f.read()
        except OSError:
            pass
    for linea in texto.strip().splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        loc, conc = linea.split("=", 1)
        conc = resolver_exacto(conc)
        if conc:
            ALIAS[norm(loc)] = conc


def resolver_exacto(texto):
    n = norm(texto)
    return ALIAS.get(n)


def resolver_concello(texto):
    """Devuelve lista de nombres canonicos que encajan con lo escrito."""
    n = norm(texto)
    if not n:
        return []
    if n in ALIAS:
        return [ALIAS[n]]
    cand = []
    for a, c in ALIAS.items():
        if a.startswith(n) or (len(n) >= 3 and (" " + n + " ") in (" " + a + " ")):
            if c not in cand:
                cand.append(c)
    if not cand and len(n) >= 3:
        for a, c in ALIAS.items():
            if n in a and c not in cand:
                cand.append(c)
    if not cand:
        for m in difflib.get_close_matches(n, list(ALIAS.keys()), n=3, cutoff=0.72):
            if ALIAS[m] not in cand:
                cand.append(ALIAS[m])
    return sorted(cand, key=norm)


def regex_alias():
    claves = sorted(ALIAS.keys(), key=len, reverse=True)
    return re.compile(r"(?<![a-z0-9])(" + "|".join(re.escape(k) for k in claves) + r")(?![a-z0-9])")


# ----------------------------------------------------------------------------
# Configuracion
# ----------------------------------------------------------------------------

def cargar_config():
    conf = {"url": "", "nap_apikey": "", "geo": True, "geo_max_km": 30}
    try:
        with open(CONF, encoding="utf-8") as f:
            conf.update(json.load(f))
    except (OSError, ValueError):
        pass
    return conf


# ----------------------------------------------------------------------------
# Descarga del feed GTFS
# ----------------------------------------------------------------------------

def descargar(url, destino, headers=None, log=out):
    h = {"User-Agent": "cta.py/%s (Termux; uso personal)" % VERSION}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    parte = destino + ".part"
    with urllib.request.urlopen(req, timeout=90) as r, open(parte, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        hecho = 0
        marca = 0
        while True:
            trozo = r.read(1 << 16)
            if not trozo:
                break
            f.write(trozo)
            hecho += len(trozo)
            if hecho - marca >= (1 << 20):
                marca = hecho
                if total:
                    log("  descargados %.1f de %.1f MB" % (hecho / 1048576.0, total / 1048576.0))
                else:
                    log("  descargados %.1f MB" % (hecho / 1048576.0))
    os.replace(parte, destino)
    return zipfile.is_zipfile(destino)


def url_desde_ficha(log=out):
    req = urllib.request.Request(URL_MDB_FICHA, headers={"User-Agent": "cta.py/%s (Termux; uso personal)" % VERSION})
    with urllib.request.urlopen(req, timeout=60) as r:
        html = r.read().decode("utf-8", "replace")
    m = re.findall(r"https://files\.mobilitydatabase\.org/mdb-2827/[^\"'\s<>)\\]+?\.zip", html)
    return m[0] if m else None


def obtener_zip(args_zip=None, args_url=None, log=out):
    """Devuelve la ruta de un zip GTFS valido, descargandolo si hace falta."""
    os.makedirs(DIR, exist_ok=True)
    if args_zip:
        if not os.path.isfile(args_zip):
            err("ERROR: no existe el fichero %s" % args_zip)
            return None
        if not zipfile.is_zipfile(args_zip):
            err("ERROR: %s no es un zip valido" % args_zip)
            return None
        return args_zip
    conf = cargar_config()
    intentos = []
    if args_url:
        intentos.append((args_url, {}))
    elif conf.get("url"):
        intentos.append((conf["url"], {}))
    if conf.get("nap_apikey"):
        intentos.append((URL_NAP, {"ApiKey": conf["nap_apikey"]}))
    intentos.append((URL_MDB_LATEST, {}))
    intentos.append(("FICHA", {}))
    for url, hdr in intentos:
        try:
            if url == "FICHA":
                log("Buscando enlace de descarga en la ficha de Mobility Database...")
                url = url_desde_ficha(log)
                if not url:
                    log("  no se ha encontrado enlace en la ficha")
                    continue
            log("Descargando %s" % url)
            if descargar(url, ZIPF, hdr, log):
                log("Descarga completada.")
                return ZIPF
            log("  el fichero descargado no es un zip valido")
        except urllib.error.HTTPError as e:
            log("  falló (HTTP %s)" % e.code)
        except (urllib.error.URLError, OSError, ValueError) as e:
            log("  falló (%s)" % getattr(e, "reason", e))
    err("ERROR: no se ha podido descargar el feed GTFS. Descargalo a mano y usa: python3 cta.py actualizar --zip RUTA")
    return None


# ----------------------------------------------------------------------------
# Construccion de la base de datos
# ----------------------------------------------------------------------------

SCHEMA = """
PRAGMA journal_mode=OFF;
PRAGMA synchronous=OFF;
CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE stops(stop_id TEXT PRIMARY KEY, code TEXT, name TEXT, descr TEXT, lat REAL, lon REAL, zone TEXT, extra TEXT, concejo TEXT, metodo TEXT);
CREATE TABLE routes(route_id TEXT PRIMARY KEY, corto TEXT, largo TEXT, tipo TEXT);
CREATE TABLE trips(trip_id TEXT PRIMARY KEY, route_id TEXT, service_id TEXT, headsign TEXT, dir TEXT, first_seq INTEGER, last_seq INTEGER);
CREATE TABLE stop_times(trip_id TEXT, stop_id TEXT, seq INTEGER, arr INTEGER, dep INTEGER);
CREATE TABLE cal(service_id TEXT PRIMARY KEY, flags TEXT, ini TEXT, fin TEXT);
CREATE TABLE caldates(service_id TEXT, fecha TEXT, tipo INTEGER);
CREATE TABLE stop_lines(stop_id TEXT, route_id TEXT);
"""


def leer_csv(zf, nombre):
    real = None
    for n in zf.namelist():
        if os.path.basename(n).lower() == nombre:
            real = n
            break
    if real is None:
        return None

    def gen():
        with zf.open(real) as f:
            rd = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig", newline=""))
            for row in rd:
                yield {(k or "").strip(): (v or "").strip() for k, v in row.items() if k is not None}
    return gen()


def hm(s):
    """'HH:MM:SS' (puede pasar de 24h) -> minutos enteros, o None."""
    s = (s or "").strip()
    if not s:
        return None
    p = s.split(":")
    try:
        return int(p[0]) * 60 + int(p[1])
    except (ValueError, IndexError):
        return None


def iso_fecha(s):
    s = (s or "").strip()
    if re.fullmatch(r"\d{8}", s):
        return "%s-%s-%s" % (s[:4], s[4:6], s[6:])
    return s


def haversine(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def lotes(it, n=50000):
    buf = []
    for x in it:
        buf.append(x)
        if len(buf) >= n:
            yield buf
            buf = []
    if buf:
        yield buf


def asignar_concellos(con, log=out):
    cargar_catalogo()
    rx = regex_alias()
    conf = cargar_config()
    overrides = {}
    if os.path.exists(F_OVERRIDES):
        try:
            with open(F_OVERRIDES, encoding="utf-8") as f:
                for linea in f:
                    linea = linea.strip()
                    if not linea or linea.startswith("#") or "=" not in linea:
                        continue
                    sid, conc = linea.split("=", 1)
                    c = resolver_exacto(conc)
                    if c:
                        overrides[sid.strip()] = c
                    else:
                        err("AVISO: concello no reconocido en paradas_concello.txt: %s" % conc.strip())
        except OSError:
            pass
    patron_col = re.compile(r"conc|munic|town|local|ciud|pobl|city|zona")
    con.execute("UPDATE stops SET concejo=NULL, metodo=NULL")
    cuenta = Counter()
    actualizaciones = []
    filas = con.execute("SELECT stop_id, name, descr, lat, lon, zone, extra FROM stops").fetchall()
    for sid, name, descr, lat, lon, zone, extra in filas:
        concejo, metodo = None, None
        if sid in overrides:
            concejo, metodo = overrides[sid], "manual"
        else:
            campos = []
            try:
                ex = json.loads(extra or "{}")
            except ValueError:
                ex = {}
            for k, v in ex.items():
                if k in ("stop_name", "stop_desc", "stop_id", "stop_code", "stop_lat", "stop_lon", "zone_id"):
                    continue
                if patron_col.search(k.lower()) and v:
                    campos.append(v)
            if descr:
                campos.append(descr)
            if zone and re.search(r"[A-Za-zÁÉÍÓÚáéíóú]", zone):
                campos.append(zone)
            if name:
                campos.append(name)
            for txt in campos:
                ms = [(m.start(), ALIAS[m.group(1)]) for m in rx.finditer(norm(txt))]
                if ms:
                    ms.sort()
                    concejo = ms[0][1]
                    distintos = set(c for _, c in ms)
                    metodo = "texto" if len(distintos) == 1 else "texto*"
                    break
            if concejo is None and conf.get("geo", True) and lat is not None and lon is not None:
                mejor, dmin = None, 1e9
                for c, d in CONC.items():
                    dist = haversine(lat, lon, d["lat"], d["lon"])
                    if dist < dmin:
                        mejor, dmin = c, dist
                if mejor and dmin <= float(conf.get("geo_max_km", 30)):
                    concejo, metodo = mejor, "geo"
        if concejo:
            cuenta[metodo] += 1
            actualizaciones.append((concejo, metodo, sid))
        else:
            cuenta["sin"] += 1
    con.executemany("UPDATE stops SET concejo=?, metodo=? WHERE stop_id=?", actualizaciones)
    return cuenta


def construir(zip_path, log=out):
    os.makedirs(DIR, exist_ok=True)
    tmp = DB + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    con = sqlite3.connect(tmp)
    con.executescript(SCHEMA)
    zf = zipfile.ZipFile(zip_path)

    log("Leyendo paradas...")
    gen = leer_csv(zf, "stops.txt")
    if gen is None:
        err("ERROR: el zip no contiene stops.txt (no parece un GTFS)")
        con.close()
        os.remove(tmp)
        return False
    filas = []
    for r in gen:
        sid = r.get("stop_id")
        if not sid:
            continue
        try:
            lat = float(r.get("stop_lat") or "")
            lon = float(r.get("stop_lon") or "")
        except ValueError:
            lat = lon = None
        filas.append((sid, r.get("stop_code", ""), r.get("stop_name", ""), r.get("stop_desc", ""), lat, lon,
                      r.get("zone_id", ""), json.dumps(r, ensure_ascii=False), None, None))
    con.executemany("INSERT OR REPLACE INTO stops VALUES (?,?,?,?,?,?,?,?,?,?)", filas)
    log("  %d paradas" % len(filas))

    log("Leyendo lineas...")
    gen = leer_csv(zf, "routes.txt")
    if gen is not None:
        con.executemany("INSERT OR REPLACE INTO routes VALUES (?,?,?,?)",
                        [(r.get("route_id"), r.get("route_short_name", ""), r.get("route_long_name", ""),
                          r.get("route_type", "")) for r in gen if r.get("route_id")])

    log("Leyendo viajes...")
    gen = leer_csv(zf, "trips.txt")
    if gen is not None:
        for lote in lotes(gen):
            con.executemany("INSERT OR REPLACE INTO trips VALUES (?,?,?,?,?,NULL,NULL)",
                            [(r.get("trip_id"), r.get("route_id"), r.get("service_id"), r.get("trip_headsign", ""),
                              r.get("direction_id", "")) for r in lote if r.get("trip_id")])

    log("Leyendo calendario...")
    gen = leer_csv(zf, "calendar.txt")
    if gen is not None:
        reg = []
        for r in gen:
            flags = "".join("1" if r.get(d, "0") == "1" else "0" for d in
                            ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"))
            reg.append((r.get("service_id"), flags, iso_fecha(r.get("start_date")), iso_fecha(r.get("end_date"))))
        con.executemany("INSERT OR REPLACE INTO cal VALUES (?,?,?,?)", reg)
    gen = leer_csv(zf, "calendar_dates.txt")
    if gen is not None:
        con.executemany("INSERT INTO caldates VALUES (?,?,?)",
                        [(r.get("service_id"), iso_fecha(r.get("date")), int(r.get("exception_type") or 0))
                         for r in gen])

    log("Leyendo horarios (puede tardar un par de minutos)...")
    gen = leer_csv(zf, "stop_times.txt")
    if gen is None:
        err("ERROR: el zip no contiene stop_times.txt")
        con.close()
        os.remove(tmp)
        return False
    n = 0
    for lote in lotes(gen, 100000):
        reg = []
        for r in lote:
            try:
                seq = int(r.get("stop_sequence") or 0)
            except ValueError:
                seq = 0
            reg.append((r.get("trip_id"), r.get("stop_id"), seq, hm(r.get("arrival_time")), hm(r.get("departure_time"))))
        con.executemany("INSERT INTO stop_times VALUES (?,?,?,?,?)", reg)
        n += len(reg)
        log("  %d filas" % n)

    log("Creando indices...")
    con.execute("CREATE INDEX ix_st_trip ON stop_times(trip_id, seq)")
    con.execute("CREATE INDEX ix_st_stop ON stop_times(stop_id)")
    con.execute("CREATE INDEX ix_trips_route ON trips(route_id)")
    con.execute("UPDATE trips SET first_seq=(SELECT MIN(seq) FROM stop_times WHERE stop_times.trip_id=trips.trip_id),"
                " last_seq=(SELECT MAX(seq) FROM stop_times WHERE stop_times.trip_id=trips.trip_id)")

    # Horas vacias: completar entre llegada/salida y, si faltan, interpolar
    con.execute("UPDATE stop_times SET dep=arr WHERE dep IS NULL AND arr IS NOT NULL")
    con.execute("UPDATE stop_times SET arr=dep WHERE arr IS NULL AND dep IS NOT NULL")
    pend = [r[0] for r in con.execute("SELECT DISTINCT trip_id FROM stop_times WHERE dep IS NULL").fetchall()]
    if pend:
        log("Interpolando horas vacias en %d viajes..." % len(pend))
    for tid in pend:
        filas = con.execute("SELECT rowid, dep FROM stop_times WHERE trip_id=? ORDER BY seq", (tid,)).fetchall()
        conocidos = [i for i, f in enumerate(filas) if f[1] is not None]
        if len(conocidos) < 2:
            continue
        upd = []
        for a, b in zip(conocidos, conocidos[1:]):
            ta, tb = filas[a][1], filas[b][1]
            for i in range(a + 1, b):
                t = int(round(ta + (tb - ta) * (i - a) / float(b - a)))
                upd.append((t, t, filas[i][0]))
        con.executemany("UPDATE stop_times SET dep=?, arr=? WHERE rowid=?", upd)

    con.execute("INSERT INTO stop_lines SELECT DISTINCT st.stop_id, t.route_id FROM stop_times st "
                "JOIN trips t ON t.trip_id=st.trip_id")
    con.execute("CREATE INDEX ix_sl_stop ON stop_lines(stop_id)")

    log("Asignando paradas a concellos...")
    cuenta = asignar_concellos(con, log)

    meta = {
        "construido": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "origen": os.path.basename(zip_path),
        "paradas": str(con.execute("SELECT COUNT(*) FROM stops").fetchone()[0]),
        "paradas_servidas": str(con.execute("SELECT COUNT(DISTINCT stop_id) FROM stop_lines").fetchone()[0]),
        "lineas": str(con.execute("SELECT COUNT(*) FROM routes").fetchone()[0]),
        "viajes": str(con.execute("SELECT COUNT(*) FROM trips").fetchone()[0]),
        "asig_texto": str(cuenta["texto"] + cuenta["texto*"] + cuenta["manual"]),
        "asig_geo": str(cuenta["geo"]),
        "asig_sin": str(cuenta["sin"]),
    }
    r = con.execute("SELECT MIN(ini), MAX(fin) FROM cal").fetchone()
    mn, mx = r[0], r[1]
    r2 = con.execute("SELECT MIN(fecha), MAX(fecha) FROM caldates").fetchone()
    if r2[0]:
        mn = min(x for x in (mn, r2[0]) if x)
        mx = max(x for x in (mx, r2[1]) if x)
    meta["servicio_ini"] = mn or ""
    meta["servicio_fin"] = mx or ""
    gen = leer_csv(zf, "feed_info.txt")
    if gen is not None:
        for r in gen:
            meta["feed_inicio"] = iso_fecha(r.get("feed_start_date", ""))
            meta["feed_fin"] = iso_fecha(r.get("feed_end_date", ""))
            meta["feed_version"] = r.get("feed_version", "")
            break
    con.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)", list(meta.items()))
    con.commit()
    con.close()
    os.replace(tmp, DB)
    global _CAL
    _CAL = None
    _PLAN.clear()
    log("Base de datos lista: %s paradas, %s lineas, %s viajes." % (meta["paradas"], meta["lineas"], meta["viajes"]))
    log("Paradas con concello: %s por texto/manual, %s aproximadas por posicion, %s sin concello." %
        (meta["asig_texto"], meta["asig_geo"], meta["asig_sin"]))
    return True


def abrir_db():
    if not os.path.exists(DB):
        return None
    con = sqlite3.connect(DB)
    return con


def meta(con):
    return dict(con.execute("SELECT k, v FROM meta").fetchall())


# ----------------------------------------------------------------------------
# Calendario: que dias circula cada servicio
# ----------------------------------------------------------------------------

_CAL = None


def cargar_cal(con):
    global _CAL
    if _CAL is not None:
        return _CAL
    cal = {}
    for sid, flags, ini, fin in con.execute("SELECT service_id, flags, ini, fin FROM cal"):
        cal[sid] = (ini, fin, flags)
    add, rem = {}, {}
    for sid, fecha, tipo in con.execute("SELECT service_id, fecha, tipo FROM caldates"):
        (add if tipo == 1 else rem).setdefault(fecha, set()).add(sid)
    _CAL = (cal, add, rem)
    return _CAL


def servicios_activos(con, d):
    cal, add, rem = cargar_cal(con)
    iso = d.isoformat()
    wd = d.weekday()
    s = set(sid for sid, (a, b, fl) in cal.items() if a <= iso <= b and fl[wd] == "1")
    s -= rem.get(iso, set())
    s |= add.get(iso, set())
    return frozenset(s)


def hoy_real():
    v = os.environ.get("CTA_HOY")
    if v:
        try:
            return datetime.date.fromisoformat(v)
        except ValueError:
            pass
    return datetime.date.today()


def fmt_fecha(d):
    return d.strftime("%d/%m/%Y")


def parse_fecha(txt, hoy):
    t = norm(txt)
    if t == "hoy":
        return hoy
    if t == "manana":
        return hoy + datetime.timedelta(days=1)
    if t == "pasado manana":
        return hoy + datetime.timedelta(days=2)
    txt = (txt or "").strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", txt)
    try:
        if m:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", txt)
        if m:
            return datetime.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})", txt)
        if m:
            d = datetime.date(hoy.year, int(m.group(2)), int(m.group(1)))
            if d < hoy:
                d = datetime.date(hoy.year + 1, int(m.group(2)), int(m.group(1)))
            return d
    except ValueError:
        return None
    return None


def nombres_dias(wds):
    if len(wds) == 1:
        return DIAS_ES_ACENTO[wds[0]]
    return "%s a %s" % (DIAS_ES_ACENTO[wds[0]], DIAS_ES_ACENTO[wds[-1]])


def jaccard(a, b):
    if not a and not b:
        return 1.0
    return len(a & b) / float(len(a | b))


_PLAN = {}


def plan_dias(con, dia="todos", hoy=None):
    """Con cache por (dia, hoy). Ver _plan_dias."""
    hoy = hoy or hoy_real()
    clave = (str(dia), hoy.isoformat())
    if clave not in _PLAN:
        _PLAN[clave] = _plan_dias(con, dia, hoy)
    return _PLAN[clave]


def _plan_dias(con, dia, hoy):
    """
    Devuelve (lista de entradas, mensaje_error).
    Cada entrada: clave, etiqueta, fecha, servicios, nota, igual_a
    """
    m = meta(con)
    try:
        ini = datetime.date.fromisoformat(m.get("servicio_ini", ""))
        fin = datetime.date.fromisoformat(m.get("servicio_fin", ""))
    except ValueError:
        return [], "El feed no trae calendario de servicios (calendar.txt / calendar_dates.txt)."
    dia_n = norm(dia)
    if dia_n not in ("todos", "lv", "sab", "dom", "fest"):
        d = parse_fecha(dia, hoy)
        if d is None:
            return [], "Dia no reconocido: %s (usa lv, sab, dom, fest, hoy, manana o una fecha como 26/10/2026)" % dia
        s = servicios_activos(con, d)
        nota = ""
        if d < ini or d > fin:
            nota = "La fecha esta fuera del calendario del feed (%s a %s)." % (fmt_fecha(ini), fmt_fecha(fin))
        elif not s:
            nota = "No hay ningun servicio programado ese dia."
        return [{"clave": "fecha", "etiqueta": "%s %s" % (DIAS_ES_ACENTO[d.weekday()].upper(), fmt_fecha(d)),
                 "fecha": d, "servicios": s, "nota": nota, "igual_a": None}], None

    inicio = max(hoy, ini)
    if inicio > fin:
        inicio = ini
    final = min(fin, inicio + datetime.timedelta(days=400))
    fechas = [inicio + datetime.timedelta(days=i) for i in range((final - inicio).days + 1)]
    S = {d: servicios_activos(con, d) for d in fechas}
    modal = {}
    for wd in range(7):
        cnt = Counter(S[d] for d in fechas if d.weekday() == wd and S[d])
        if cnt:
            modal[wd] = cnt.most_common(1)[0][0]

    def primera(wds, s):
        for d in fechas:
            if d.weekday() in wds and S[d] == s:
                return d
        return fechas[0]

    entradas = []
    grupos = []
    for wd in range(5):
        if wd not in modal:
            continue
        if grupos and grupos[-1][1] == modal[wd]:
            grupos[-1][0].append(wd)
        else:
            grupos.append(([wd], modal[wd]))
    for wds, s in grupos:
        d = primera(wds, s)
        entradas.append({"clave": "lv", "etiqueta": nombres_dias(wds).upper(), "fecha": d, "servicios": s,
                         "nota": "", "igual_a": None})
    if 5 in modal:
        entradas.append({"clave": "sab", "etiqueta": "SÁBADOS", "fecha": primera([5], modal[5]),
                         "servicios": modal[5], "nota": "", "igual_a": None})
    if 6 in modal:
        entradas.append({"clave": "dom", "etiqueta": "DOMINGOS", "fecha": primera([6], modal[6]),
                         "servicios": modal[6], "nota": "", "igual_a": None})
        fest = [d for d in fechas if d.weekday() <= 5 and S[d] and d.weekday() in modal
                and S[d] != modal[d.weekday()] and jaccard(S[d], modal[6]) >= 0.9]
        if fest:
            ej = ", ".join(fmt_fecha(d) for d in fest[:8])
            nota = "Dias festivos detectados en el calendario: %s%s." % (ej, " ..." if len(fest) > 8 else "")
        else:
            nota = ("No se detectan festivos entre semana en el calendario del feed; "
                    "se aplica el horario de domingo.")
        entradas.append({"clave": "fest", "etiqueta": "FESTIVOS", "fecha": fest[0] if fest else primera([6], modal[6]),
                         "servicios": modal[6], "nota": nota, "igual_a": "dom"})
    if dia_n != "todos":
        sel = [e for e in entradas if e["clave"] == dia_n]
        if dia_n == "fest" and sel:
            sel[0]["igual_a"] = None
        entradas = sel
    if not entradas:
        return [], "No hay servicios en el calendario para el periodo consultado."
    return entradas, None


# ----------------------------------------------------------------------------
# Consultas
# ----------------------------------------------------------------------------

def fmt_t(m):
    if m is None:
        return "--:--"
    h, mi = divmod(int(m), 60)
    if h >= 24:
        return "%02d:%02d+" % (h - 24, mi)
    return "%02d:%02d" % (h, mi)


def nombre_linea(corto, largo):
    if corto:
        return corto
    return (largo or "?")[:18]


def lineas_de_parada(con, stop_id):
    filas = con.execute("SELECT DISTINCT r.corto, r.largo FROM stop_lines sl JOIN routes r ON r.route_id=sl.route_id "
                        "WHERE sl.stop_id=?", (stop_id,)).fetchall()
    return sorted(set(nombre_linea(c, l) for c, l in filas), key=clave_natural)


def filtro_linea(corto, largo, filtro):
    if not filtro:
        return True
    f = norm(filtro)
    c, l = norm(corto), norm(largo)
    return f == c or (f and f in c.split()) or f == c.replace(" ", "")


def datos_concello(con, nombre, estricto=False, linea=None):
    cond = "concejo=? AND EXISTS (SELECT 1 FROM stop_lines sl WHERE sl.stop_id=stops.stop_id)"
    if estricto:
        cond += " AND metodo != 'geo'"
    filas = con.execute("SELECT stop_id, name, lat, lon, metodo FROM stops WHERE " + cond + " ORDER BY name",
                        (nombre,)).fetchall()
    paradas = []
    todas = set()
    for sid, name, lat, lon, metodo in filas:
        ls = lineas_de_parada(con, sid)
        if linea:
            ls_f = [l for l in ls if norm(l) == norm(linea) or norm(linea) in norm(l).split()]
            if not ls_f:
                continue
        todas.update(ls)
        paradas.append({"id": sid, "nombre": name, "lat": lat, "lon": lon, "metodo": metodo, "lineas": ls})
    return {"concello": nombre, "paradas": paradas, "lineas": sorted(todas, key=clave_natural),
            "aprox": sum(1 for p in paradas if p["metodo"] == "geo"),
            "dudosas": sum(1 for p in paradas if p["metodo"] == "texto*")}


def datos_parada(con, stop_id, dia="todos", linea=None, llegadas=False, hoy=None):
    fila = con.execute("SELECT stop_id, name, descr, lat, lon, concejo, metodo, code FROM stops WHERE stop_id=?",
                       (stop_id,)).fetchone()
    if not fila:
        return None, "No existe la parada %s" % stop_id
    parada = {"id": fila[0], "nombre": fila[1], "descripcion": fila[2], "lat": fila[3], "lon": fila[4],
              "concello": fila[5], "metodo": fila[6], "codigo": fila[7], "lineas": lineas_de_parada(con, fila[0])}
    entradas, e = plan_dias(con, dia, hoy)
    if e:
        return None, e
    filas = con.execute(
        "SELECT r.corto, r.largo, t.headsign, st.dep, st.arr, st.seq, t.last_seq, t.service_id "
        "FROM stop_times st JOIN trips t ON t.trip_id=st.trip_id JOIN routes r ON r.route_id=t.route_id "
        "WHERE st.stop_id=?", (stop_id,)).fetchall()
    dias = []
    for en in entradas:
        grupos = {}
        for corto, largo, hs, dep, arr, seq, last, svc in filas:
            if svc not in en["servicios"]:
                continue
            if not llegadas and last is not None and seq == last:
                continue
            if not filtro_linea(corto, largo, linea):
                continue
            t = dep if dep is not None else arr
            if t is None:
                continue
            clave = (nombre_linea(corto, largo), hs or largo or "")
            grupos.setdefault(clave, set()).add(t)
        lineas = []
        for (ln, dest), horas in sorted(grupos.items(), key=lambda kv: (clave_natural(kv[0][0]), kv[0][1])):
            hs = sorted(horas)
            lineas.append({"linea": ln, "destino": dest, "horas": [fmt_t(h) for h in hs]})
        dias.append({"clave": en["clave"], "etiqueta": en["etiqueta"], "fecha": fmt_fecha(en["fecha"]),
                     "nota": en["nota"], "igual_a": en["igual_a"], "lineas": lineas})
    return {"parada": parada, "dias": dias}, None


def buscar_paradas(con, texto, limite=60):
    t = (texto or "").strip()
    if not t:
        return []
    filas = con.execute("SELECT stop_id, name, concejo, metodo, code FROM stops WHERE stop_id=? OR code=?",
                        (t, t)).fetchall()
    if filas:
        return filas
    toks = norm(t).split()
    res = []
    for sid, name, descr, conc, metodo, code in con.execute(
            "SELECT stop_id, name, descr, concejo, metodo, code FROM stops "
            "WHERE EXISTS (SELECT 1 FROM stop_lines sl WHERE sl.stop_id=stops.stop_id)"):
        h = norm("%s %s %s" % (name, descr, conc))
        if all(k in h for k in toks):
            res.append((sid, name, conc, metodo, code))
            if len(res) >= limite:
                break
    return res


def datos_linea(con, texto, dia="todos", hoy=None):
    rutas = [r for r in con.execute("SELECT route_id, corto, largo FROM routes").fetchall()
             if filtro_linea(r[1], r[2], texto) or norm(texto) in norm(r[2])]
    if not rutas:
        return None, "No se encuentra ninguna linea que encaje con '%s'" % texto
    entradas, e = plan_dias(con, dia, hoy)
    if e:
        return None, e
    resultado = []
    for rid, corto, largo in rutas[:12]:
        viajes = con.execute("SELECT trip_id, service_id, headsign, dir, first_seq FROM trips WHERE route_id=?",
                             (rid,)).fetchall()
        por_dir = {}
        for tid, svc, hs, d, fs in viajes:
            por_dir.setdefault((d, hs), []).append((tid, svc, fs))
        sentidos = []
        for (d, hs), vs in sorted(por_dir.items(), key=lambda kv: str(kv[0])):
            mejor, nmax = None, -1
            for tid, svc, fs in vs[:60]:
                n = con.execute("SELECT COUNT(*) FROM stop_times WHERE trip_id=?", (tid,)).fetchone()[0]
                if n > nmax:
                    mejor, nmax = tid, n
            paradas = con.execute(
                "SELECT s.name, s.concejo FROM stop_times st JOIN stops s ON s.stop_id=st.stop_id "
                "WHERE st.trip_id=? ORDER BY st.seq", (mejor,)).fetchall()
            dias = []
            for en in entradas:
                horas = set()
                for tid, svc, fs in vs:
                    if svc in en["servicios"]:
                        r = con.execute("SELECT dep FROM stop_times WHERE trip_id=? AND seq=?", (tid, fs)).fetchone()
                        if r and r[0] is not None:
                            horas.add(r[0])
                dias.append({"clave": en["clave"], "etiqueta": en["etiqueta"], "fecha": fmt_fecha(en["fecha"]),
                             "igual_a": en["igual_a"], "salidas": [fmt_t(h) for h in sorted(horas)]})
            sentidos.append({"destino": hs or "", "paradas": [{"nombre": a, "concello": b} for a, b in paradas],
                             "dias": dias})
        resultado.append({"linea": nombre_linea(corto, largo), "nombre": largo, "sentidos": sentidos})
    return {"lineas": resultado}, None


def lista_concellos(con):
    filas = con.execute(
        "SELECT concejo, COUNT(*), SUM(CASE WHEN metodo='geo' THEN 0 ELSE 1 END) FROM stops "
        "WHERE concejo IS NOT NULL AND EXISTS (SELECT 1 FROM stop_lines sl WHERE sl.stop_id=stops.stop_id) "
        "GROUP BY concejo").fetchall()
    d = {c: (n, e or 0) for c, n, e in filas}
    res = []
    for c in sorted(CONC.keys(), key=norm):
        n, e = d.get(c, (0, 0))
        res.append({"nombre": c, "paradas": n, "exactas": e})
    return res


# ----------------------------------------------------------------------------
# Texto para pantalla / fichero
# ----------------------------------------------------------------------------

def marca(metodo):
    return {"geo": " ~", "texto*": " ?"}.get(metodo, "")


def texto_horas(horas, cols=6, sangria="    "):
    if not horas:
        return [sangria + "(sin salidas)"]
    filas = []
    for i in range(0, len(horas), cols):
        filas.append(sangria + "  ".join("%-6s" % h for h in horas[i:i + cols]).rstrip())
    return filas


def texto_parada(d, cols=6):
    p = d["parada"]
    L = []
    L.append("PARADA: %s  (id %s)" % (p["nombre"], p["id"]))
    if p.get("concello"):
        L.append("Concello: %s%s" % (p["concello"], {"geo": "  (asignado por posicion, aproximado)",
                                                     "texto*": "  (asignacion dudosa)"}.get(p["metodo"], "")))
    if p.get("lat") is not None:
        L.append("Posicion: %.5f, %.5f" % (p["lat"], p["lon"]))
    L.append("Lineas: %s" % (", ".join(p["lineas"]) or "-"))
    for dia in d["dias"]:
        L.append("")
        L.append("== %s (referencia: %s) ==" % (dia["etiqueta"], dia["fecha"]))
        if dia["nota"]:
            L.append(dia["nota"])
        if dia["igual_a"] == "dom":
            L.append("Igual que los domingos.")
            continue
        if not dia["lineas"]:
            L.append("  (sin servicio en esta parada)")
            continue
        for ln in dia["lineas"]:
            cab = ("  Linea %s" if len(ln["linea"]) <= 10 else "  %s") % ln["linea"]
            if ln["destino"] and norm(ln["destino"]) != norm(ln["linea"]):
                cab += "  ->  %s" % ln["destino"]
            L.append(cab)
            L.extend(texto_horas(ln["horas"], cols))
    return "\n".join(L)


def texto_concello(dc, cols=6):
    L = []
    n = len(dc["paradas"])
    L.append("CONCELLO: %s - %d parada(s) con servicio" % (dc["concello"], n))
    if dc["aprox"] or dc["dudosas"]:
        L.append("(~ = asignada por posicion, aproximada; ? = asignacion dudosa por texto)")
    L.append("Lineas con parada en el concello: %s" % (", ".join(dc["lineas"]) or "-"))
    L.append("")
    for i, p in enumerate(dc["paradas"], 1):
        L.append("%3d. %s%s  (id %s)" % (i, p["nombre"], marca(p["metodo"]), p["id"]))
        L.append("       lineas: %s" % (", ".join(p["lineas"]) or "-"))
    return "\n".join(L)


def texto_linea(dl, cols=6):
    L = []
    for ln in dl["lineas"]:
        L.append("LINEA %s  %s" % (ln["linea"], ln["nombre"] or ""))
        for s in ln["sentidos"]:
            L.append("")
            L.append("  Sentido: %s" % (s["destino"] or "(sin destino)"))
            L.append("  Paradas:")
            for i, p in enumerate(s["paradas"], 1):
                L.append("    %2d. %s%s" % (i, p["nombre"], " (%s)" % p["concello"] if p["concello"] else ""))
            for dia in s["dias"]:
                L.append("  == Salidas de cabecera - %s (referencia: %s) ==" % (dia["etiqueta"], dia["fecha"]))
                if dia["igual_a"] == "dom":
                    L.append("    Igual que los domingos.")
                else:
                    L.extend(texto_horas(dia["salidas"], cols, "    "))
        L.append("")
    return "\n".join(L).rstrip()


# ----------------------------------------------------------------------------
# Comandos
# ----------------------------------------------------------------------------

def necesita_db():
    con = abrir_db()
    if con is None:
        err("ERROR: no hay base de datos. Ejecuta primero: python3 cta.py actualizar")
        return None
    return con


def elegir_concello(texto, interactivo=False):
    cargar_catalogo()
    c = resolver_concello(texto)
    if not c:
        err("ERROR: no reconozco ningun concello que encaje con '%s'. Prueba: python3 cta.py concellos" % texto)
        return None
    if len(c) == 1:
        return c[0]
    if interactivo:
        out("Hay varios que encajan:")
        for i, n in enumerate(c, 1):
            out("  %d. %s" % (i, n))
        try:
            r = input("Numero (vacio = cancelar): ").strip()
        except EOFError:
            return None
        if r.isdigit() and 1 <= int(r) <= len(c):
            return c[int(r) - 1]
        return None
    out("Hay varios concellos que encajan con '%s':" % texto)
    for n in c:
        out("  %s" % n)
    out("Escribe el nombre completo de uno de ellos.")
    return None


def cmd_actualizar(a):
    z = obtener_zip(a.zip, a.url)
    if not z:
        return 1
    return 0 if construir(z) else 1


def cmd_reasignar(a):
    con = necesita_db()
    if not con:
        return 1
    out("Reasignando paradas a concellos (usa localidades.txt y paradas_concello.txt)...")
    cuenta = asignar_concellos(con)
    con.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)", [
        ("asig_texto", str(cuenta["texto"] + cuenta["texto*"] + cuenta["manual"])),
        ("asig_geo", str(cuenta["geo"])), ("asig_sin", str(cuenta["sin"]))])
    con.commit()
    out("Hecho: %d por texto/manual, %d aproximadas por posicion, %d sin concello." %
        (cuenta["texto"] + cuenta["texto*"] + cuenta["manual"], cuenta["geo"], cuenta["sin"]))
    return 0


def cmd_estado(a):
    con = necesita_db()
    if not con:
        return 1
    m = meta(con)
    out("Base de datos: %s" % DB)
    out("Construida el: %s (origen: %s)" % (m.get("construido"), m.get("origen")))
    out("Calendario de servicios: %s a %s" % (m.get("servicio_ini"), m.get("servicio_fin")))
    out("Paradas: %s (%s con servicio)   Lineas: %s   Viajes: %s" % (m.get("paradas"), m.get("paradas_servidas"),
                                                                      m.get("lineas"), m.get("viajes")))
    out("Concello asignado por texto o manual: %s   por posicion (aprox.): %s   sin concello: %s" %
        (m.get("asig_texto"), m.get("asig_geo"), m.get("asig_sin")))
    hoy = hoy_real()
    try:
        fin = datetime.date.fromisoformat(m.get("servicio_fin", ""))
        if hoy > fin:
            out("AVISO: el calendario del feed ya ha terminado. Ejecuta: python3 cta.py actualizar")
        elif (fin - hoy).days < 7:
            out("AVISO: al feed le quedan menos de 7 dias de calendario. Conviene actualizar.")
    except ValueError:
        pass
    return 0


def cmd_concellos(a):
    con = necesita_db()
    if not con:
        return 1
    cargar_catalogo()
    out("%-30s %8s %8s" % ("Concello", "Paradas", "Exactas"))
    for r in lista_concellos(con):
        out("%-30s %8d %8d" % (r["nombre"], r["paradas"], r["exactas"]))
    return 0


def cmd_concello(a):
    con = necesita_db()
    if not con:
        return 1
    nombre = elegir_concello(a.nombre)
    if not nombre:
        return 2
    dc = datos_concello(con, nombre, a.estricto, a.linea)
    if not dc["paradas"]:
        err("ERROR: no hay paradas con servicio asignadas a %s. Prueba 'python3 cta.py diagnostico' o 'buscar'." % nombre)
        return 1
    out(texto_concello(dc, a.cols))
    if a.horarios:
        for p in dc["paradas"][:a.max]:
            d, e = datos_parada(con, p["id"], a.dia, a.linea, a.llegadas)
            if e:
                err("ERROR: " + e)
                return 1
            out("")
            out("-" * 60)
            out(texto_parada(d, a.cols))
        if len(dc["paradas"]) > a.max:
            out("")
            out("(mostradas %d de %d paradas; usa --max para ver mas)" % (a.max, len(dc["paradas"])))
    return 0


def cmd_parada(a):
    con = necesita_db()
    if not con:
        return 1
    res = buscar_paradas(con, " ".join(a.texto))
    if not res:
        err("ERROR: no se encuentra ninguna parada con '%s'" % " ".join(a.texto))
        return 1
    if len(res) > 1:
        out("Hay %d paradas que encajan; usa el id exacto:" % len(res))
        for sid, name, conc, metodo, code in res[:60]:
            out("  %-12s %s%s%s" % (sid, name, " (%s)" % conc if conc else "", marca(metodo)))
        return 2
    d, e = datos_parada(con, res[0][0], a.dia, a.linea, a.llegadas)
    if e:
        err("ERROR: " + e)
        return 1
    out(texto_parada(d, a.cols))
    return 0


def cmd_buscar(a):
    con = necesita_db()
    if not con:
        return 1
    res = buscar_paradas(con, " ".join(a.texto))
    if not res:
        err("ERROR: no se encuentra ninguna parada con '%s'" % " ".join(a.texto))
        return 1
    for sid, name, conc, metodo, code in res:
        out("%-12s %s%s%s" % (sid, name, " (%s)" % conc if conc else "", marca(metodo)))
    return 0


def cmd_linea(a):
    con = necesita_db()
    if not con:
        return 1
    d, e = datos_linea(con, " ".join(a.texto), a.dia)
    if e:
        err("ERROR: " + e)
        return 1
    out(texto_linea(d, a.cols))
    return 0


def cmd_diagnostico(a):
    con = necesita_db()
    if not con:
        return 1
    cargar_catalogo()
    m = meta(con)
    out("DIAGNOSTICO DE ASIGNACION DE PARADAS A CONCELLOS")
    out("Paradas con servicio: %s" % m.get("paradas_servidas"))
    for metodo, etq in (("texto", "por texto (seguro)"), ("texto*", "por texto, dudosas"), ("manual", "manuales"),
                        ("geo", "por posicion (aproximado)")):
        n = con.execute("SELECT COUNT(*) FROM stops WHERE metodo=? AND EXISTS (SELECT 1 FROM stop_lines sl WHERE "
                        "sl.stop_id=stops.stop_id)", (metodo,)).fetchone()[0]
        out("  %-28s %6d" % (etq, n))
    n = con.execute("SELECT COUNT(*) FROM stops WHERE concejo IS NULL AND EXISTS (SELECT 1 FROM stop_lines sl WHERE "
                    "sl.stop_id=stops.stop_id)").fetchone()[0]
    out("  %-28s %6d" % ("sin concello", n))
    out("")
    out("Columnas de stops.txt en este feed: %s" % ", ".join(
        sorted(json.loads(con.execute("SELECT extra FROM stops LIMIT 1").fetchone()[0]).keys())))
    out("")
    out("Ejemplos de paradas SIN concello (nombre | descripcion | zona):")
    for sid, name, descr, zone in con.execute(
            "SELECT stop_id, name, descr, zone FROM stops WHERE concejo IS NULL AND EXISTS (SELECT 1 FROM stop_lines "
            "sl WHERE sl.stop_id=stops.stop_id) LIMIT 15"):
        out("  %s | %s | %s | %s" % (sid, name, descr, zone))
    out("")
    out("Ejemplos de paradas asignadas por posicion (~):")
    for sid, name, conc in con.execute(
            "SELECT stop_id, name, concejo FROM stops WHERE metodo='geo' LIMIT 15"):
        out("  %s | %s -> %s" % (sid, name, conc))
    out("")
    out("Para corregir: anade lineas 'texto=Concello' en %s (localidades) o 'id_parada=Concello' en %s," %
        (F_LOCALIDADES, F_OVERRIDES))
    out("y ejecuta: python3 cta.py reasignar")
    return 0


# ----------------------------------------------------------------------------
# Menu interactivo
# ----------------------------------------------------------------------------

def pedir(msg):
    try:
        return input(msg).strip()
    except EOFError:
        return None


def cmd_menu(a):
    con = necesita_db()
    if not con:
        out("Todavia no hay datos. Quieres descargarlos ahora? (s/n)")
        r = pedir("> ")
        if r and r.lower().startswith("s"):
            if cmd_actualizar(argparse.Namespace(zip=None, url=None)) != 0:
                return 1
            con = necesita_db()
        else:
            return 1
    cargar_catalogo()
    out("CTA - horarios de autobus de Asturias (version %s). Linea vacia para salir." % VERSION)
    while True:
        t = pedir("\nConcello (o 'p' para buscar parada por nombre): ")
        if not t:
            return 0
        if t.lower() == "p":
            q = pedir("Texto de la parada: ")
            if not q:
                continue
            res = buscar_paradas(con, q)
            if not res:
                err("ERROR: sin resultados")
                continue
            for i, (sid, name, conc, metodo, code) in enumerate(res[:40], 1):
                out("%3d. %s%s%s" % (i, name, " (%s)" % conc if conc else "", marca(metodo)))
            n = pedir("Numero de parada: ")
            if n and n.isdigit() and 1 <= int(n) <= min(40, len(res)):
                dia = pedir("Dia (todos/lv/sab/dom/fest/hoy/fecha) [todos]: ") or "todos"
                d, e = datos_parada(con, res[int(n) - 1][0], dia)
                out(texto_parada(d, a.cols) if d else "ERROR: " + str(e))
            continue
        nombre = elegir_concello(t, interactivo=True)
        if not nombre:
            continue
        linea = None
        while True:
            dc = datos_concello(con, nombre, False, linea)
            if not dc["paradas"]:
                err("ERROR: sin paradas con servicio en %s" % nombre)
                break
            out(texto_concello(dc, a.cols))
            r = pedir("\nNumero de parada | 'l X' filtrar linea X | 'l' quitar filtro | vacio = otro concello: ")
            if not r:
                break
            if r.lower() == "l":
                linea = None
                continue
            if r.lower().startswith("l "):
                linea = r[2:].strip()
                continue
            if r.isdigit() and 1 <= int(r) <= len(dc["paradas"]):
                dia = pedir("Dia (todos/lv/sab/dom/fest/hoy/fecha) [todos]: ") or "todos"
                d, e = datos_parada(con, dc["paradas"][int(r) - 1]["id"], dia, linea)
                out(texto_parada(d, a.cols) if d else "ERROR: " + str(e))
                pedir("\n(Enter para volver a la lista)")
            else:
                err("ERROR: opcion no valida")
    return 0


# ----------------------------------------------------------------------------
# Servidor local para la extension de Firefox
# ----------------------------------------------------------------------------

ESTADO_ACT = {"activo": False, "log": []}


def actualizar_en_hilo(zip_path=None):
    if ESTADO_ACT["activo"]:
        return False

    def trabajo():
        ESTADO_ACT["activo"] = True
        ESTADO_ACT["log"] = []

        def log(s):
            ESTADO_ACT["log"].append(s)
            ESTADO_ACT["log"] = ESTADO_ACT["log"][-40:]
        try:
            z = obtener_zip(zip_path, None, log)
            if z:
                construir(z, log)
            else:
                log("ERROR: no se pudo obtener el feed")
        except Exception as e:  # noqa
            log("ERROR: %s" % e)
        ESTADO_ACT["activo"] = False

    threading.Thread(target=trabajo, daemon=True).start()
    return True


class Manejador(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _enviar(self, codigo, cuerpo, tipo="application/json; charset=utf-8"):
        if tipo.startswith("application/json"):
            cuerpo = json.dumps(cuerpo, ensure_ascii=False)
        datos = cuerpo.encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(datos)))
        self._cors()
        self.end_headers()
        self.wfile.write(datos)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_POST(self):
        origen = self.headers.get("Origin", "")
        if origen and not origen.startswith("moz-extension://"):
            self._enviar(403, {"error": "origen no permitido"})
            return
        ruta = urllib.parse.urlparse(self.path).path
        if ruta == "/api/actualizar":
            ok = actualizar_en_hilo()
            self._enviar(200, {"iniciado": ok, "mensaje": "actualizando" if ok else "ya hay una actualizacion en curso"})
        else:
            self._enviar(404, {"error": "no existe"})

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            self._get(u.path, q)
        except Exception as e:  # noqa
            self._enviar(500, {"error": str(e)})

    def _get(self, ruta, q):
        cargar_catalogo()
        if ruta == "/api/estado":
            con = abrir_db()
            self._enviar(200, {"version": VERSION, "db": con is not None, "meta": meta(con) if con else {},
                               "actualizando": ESTADO_ACT["activo"], "progreso": ESTADO_ACT["log"][-12:]})
            return
        con = abrir_db()
        if con is None:
            self._enviar(200, {"error": "No hay datos. Pulsa Actualizar datos."})
            return
        cols = 6
        if ruta == "/api/concellos":
            self._enviar(200, lista_concellos(con))
        elif ruta == "/api/resolver":
            self._enviar(200, {"coincidencias": resolver_concello(q.get("q", ""))})
        elif ruta in ("/api/concello", "/api/txt/concello"):
            c = resolver_concello(q.get("q", ""))
            if len(c) != 1:
                self._enviar(200, {"error": "Concello no reconocido o ambiguo", "coincidencias": c})
                return
            dc = datos_concello(con, c[0], q.get("estricto") == "1", q.get("linea") or None)
            if ruta == "/api/concello":
                self._enviar(200, dc)
            else:
                partes = [texto_concello(dc, cols)]
                if q.get("horarios") == "1":
                    for p in dc["paradas"][:int(q.get("max", "40"))]:
                        d, e = datos_parada(con, p["id"], q.get("dia", "todos"), q.get("linea") or None)
                        partes.append("-" * 60)
                        partes.append(texto_parada(d, cols) if d else "ERROR: " + str(e))
                self._enviar(200, "\n".join(partes) + "\n", "text/plain; charset=utf-8")
        elif ruta in ("/api/parada", "/api/txt/parada"):
            d, e = datos_parada(con, q.get("id", ""), q.get("dia", "todos"), q.get("linea") or None,
                                q.get("llegadas") == "1")
            if e:
                self._enviar(200, {"error": e})
            elif ruta == "/api/parada":
                self._enviar(200, d)
            else:
                self._enviar(200, texto_parada(d, cols) + "\n", "text/plain; charset=utf-8")
        elif ruta == "/api/buscar":
            res = buscar_paradas(con, q.get("q", ""))
            self._enviar(200, [{"id": a, "nombre": b, "concello": c, "metodo": d_} for a, b, c, d_, _ in res])
        elif ruta in ("/api/linea", "/api/txt/linea"):
            d, e = datos_linea(con, q.get("q", ""), q.get("dia", "todos"))
            if e:
                self._enviar(200, {"error": e})
            elif ruta == "/api/linea":
                self._enviar(200, d)
            else:
                self._enviar(200, texto_linea(d, cols) + "\n", "text/plain; charset=utf-8")
        else:
            self._enviar(404, {"error": "no existe"})


def cmd_servir(a):
    cargar_catalogo()
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", a.puerto), Manejador)
    except OSError as e:
        err("ERROR: no se puede abrir el puerto %d (%s). Prueba con --puerto OTRO" % (a.puerto, e))
        return 1
    out("Servidor CTA escuchando en http://127.0.0.1:%d (solo este dispositivo). Ctrl+C para parar." % a.puerto)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        out("Servidor detenido.")
    return 0


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------

def construir_parser():
    p = argparse.ArgumentParser(prog="cta.py", description="Paradas, lineas y horarios del Consorcio de Transportes de Asturias")
    p.add_argument("--txt", nargs="?", const="auto", default=None, metavar="FICHERO",
                   help="guarda tambien la salida en un .txt")
    p.add_argument("--cols", type=int, default=6, help="horas por fila (6)")
    comun = argparse.ArgumentParser(add_help=False)
    comun.add_argument("--txt", nargs="?", const="auto", default=argparse.SUPPRESS, metavar="FICHERO")
    comun.add_argument("--cols", type=int, default=argparse.SUPPRESS)
    sp = p.add_subparsers(dest="cmd")

    s = sp.add_parser("actualizar", parents=[comun], help="descarga el feed GTFS y construye la base de datos")
    s.add_argument("--zip", help="usar un GTFS ya descargado")
    s.add_argument("--url", help="descargar desde esta URL")
    s.set_defaults(f=cmd_actualizar)

    s = sp.add_parser("reasignar", parents=[comun], help="recalcula el concello de cada parada (tras editar los ficheros de correccion)")
    s.set_defaults(f=cmd_reasignar)

    s = sp.add_parser("estado", parents=[comun], help="informacion de los datos cargados")
    s.set_defaults(f=cmd_estado)

    s = sp.add_parser("concellos", parents=[comun], help="lista de concellos y numero de paradas")
    s.set_defaults(f=cmd_concellos)

    s = sp.add_parser("concello", parents=[comun], help="paradas de un concello")
    s.add_argument("nombre")
    s.add_argument("--dia", default="todos", help="todos, lv, sab, dom, fest, hoy, manana o una fecha")
    s.add_argument("--horarios", action="store_true", help="incluye los horarios de cada parada")
    s.add_argument("--linea", help="solo esa linea")
    s.add_argument("--max", type=int, default=15, help="maximo de paradas con --horarios (15)")
    s.add_argument("--estricto", action="store_true", help="oculta paradas asignadas solo por posicion")
    s.add_argument("--llegadas", action="store_true", help="incluye viajes que terminan en la parada")
    s.set_defaults(f=cmd_concello)

    s = sp.add_parser("parada", parents=[comun], help="horarios de una parada (id o texto)")
    s.add_argument("texto", nargs="+")
    s.add_argument("--dia", default="todos")
    s.add_argument("--linea")
    s.add_argument("--llegadas", action="store_true")
    s.set_defaults(f=cmd_parada)

    s = sp.add_parser("buscar", parents=[comun], help="buscar paradas por nombre")
    s.add_argument("texto", nargs="+")
    s.set_defaults(f=cmd_buscar)

    s = sp.add_parser("linea", parents=[comun], help="recorrido y salidas de una linea")
    s.add_argument("texto", nargs="+")
    s.add_argument("--dia", default="todos")
    s.set_defaults(f=cmd_linea)

    s = sp.add_parser("diagnostico", parents=[comun], help="revisa la asignacion de paradas a concellos")
    s.set_defaults(f=cmd_diagnostico)

    s = sp.add_parser("servir", parents=[comun], help="servidor local para la extension de Firefox")
    s.add_argument("--puerto", type=int, default=8765)
    s.set_defaults(f=cmd_servir)

    s = sp.add_parser("menu", parents=[comun], help="menu interactivo")
    s.set_defaults(f=cmd_menu)
    return p


def ruta_txt(valor):
    if valor and valor != "auto":
        return os.path.expanduser(valor)
    descargas = os.path.join(HOME, "storage", "downloads")
    base = descargas if os.path.isdir(descargas) else HOME
    return os.path.join(base, "cta_salida.txt")


def main(argv=None):
    p = construir_parser()
    # permite poner --txt y --cols tambien despues del subcomando
    argv = list(sys.argv[1:] if argv is None else argv)
    a = p.parse_args(argv)
    if not a.cmd:
        a.cmd = "menu"
        a.f = cmd_menu
    fich = None
    if a.txt is not None and a.cmd not in ("servir", "menu"):
        destino = ruta_txt(a.txt)
        try:
            fich = open(destino, "w", encoding="utf-8")
        except OSError as e:
            err("ERROR: no se puede escribir %s (%s)" % (destino, e))
            return 1
        sys.stdout = Tee(sys.__stdout__, fich)
    try:
        rc = a.f(a)
    finally:
        if fich:
            sys.stdout = sys.__stdout__
            fich.close()
            out("Guardado tambien en: %s" % destino)
    return rc


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except BrokenPipeError:
        sys.exit(0)
