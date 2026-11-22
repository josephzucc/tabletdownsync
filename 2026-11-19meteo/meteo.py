#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
meteo.py  -  Informe de lluvia, viento y temperatura del sur de Avila con datos abiertos de ECMWF (IFS 0,25 grados).

Uso (Termux):
  python meteo.py                  -> comprueba el ultimo ciclo, descarga si hay uno nuevo, analiza y muestra el informe
  python meteo.py informe [--forzar] [--localidad "La Adrada"] [--horas 96]
  python meteo.py estado           -> ultimo ciclo publicado por ECMWF frente al ultimo ciclo local
  python meteo.py servidor [--puerto 8789]   -> servidor local para la extension de Firefox
  python meteo.py abrir            -> abre el panel en el navegador (despierta la interfaz)
  python meteo.py diag             -> escribe un fichero de diagnostico
  python meteo.py limpiar          -> borra ciclos antiguos y GRIB temporales

Datos: ECMWF Open Data (CC BY 4.0). Contiene datos de ECMWF.
"""
import os
import sys

# El nombre de este fichero no debe chocar con el paquete "ecmwf" de ecmwf-opendata: se quita la carpeta del script del path.
_AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path = [p for p in sys.path if os.path.abspath(p or ".") != _AQUI]
import json
import math
import time
import glob
import shutil
import argparse
import threading
import traceback
import subprocess
import datetime as dt
import urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

VERSION = "1.0.0"
PUERTO_DEFECTO = 8789
ANCHO = 121
URL_DATOS = "https://data.ecmwf.int/forecasts/"

PREFIJO = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
BASE = os.environ.get("METEO_DIR", os.path.join(os.path.expanduser("~"), "meteo_datos"))
DIR_DATOS = os.path.join(BASE, "datos")
DIR_INFORMES = os.path.join(BASE, "informes")
DIR_LOGS = os.path.join(BASE, "logs")
DIR_TMP = os.path.join(BASE, "tmp")
FICH_CFG = os.path.join(BASE, "config.json")

CFG_DEF = {
    "puerto": PUERTO_DEFECTO,
    "fuente": "ecmwf",          # ecmwf | aws | azure | google
    "modelo": "ifs",
    "resolucion": "0p25",
    "horas": 96,                # horizonte del informe (maximo 144 para que valga cualquier ciclo)
    "paso_h": 3,
    "auto_minutos": 60,         # en modo servidor: cada cuantos minutos mira si hay ciclo nuevo (0 = no)
    "conservar_ciclos": 4,
    "conservar_grib": False,
    "notificar_termux": True,
    "localidad_detalle": "La Adrada",
    # malla que se guarda (se incluye un margen para interpolar las localidades)
    "extraccion": {"lat_min": 39.75, "lat_max": 40.75, "lon_min": -5.75, "lon_max": -4.25},
    # malla que cuenta como "sur de Avila" para las estadisticas de area
    "area_informe": {"lat_min": 40.0, "lat_max": 40.5, "lon_min": -5.5, "lon_max": -4.5},
    "umbrales": {
        "lluvia_3h_mm": 10.0,
        "lluvia_24h_mm": 40.0,
        "racha_kmh": 70.0,
        "racha_fuerte_kmh": 90.0,
        "helada_c": 0.0,
    },
    "localidades": [
        {"nombre": "La Adrada", "lat": 40.2997, "lon": -4.6475},
        {"nombre": "Sotillo de la Adrada", "lat": 40.2917, "lon": -4.5767},
        {"nombre": "Burgohondo", "lat": 40.4000, "lon": -4.7800},
        {"nombre": "Navaluenga", "lat": 40.3689, "lon": -4.7011},
        {"nombre": "Cebreros", "lat": 40.4578, "lon": -4.4691},
        {"nombre": "Pedro Bernardo", "lat": 40.2417, "lon": -4.9103},
        {"nombre": "Mombeltran", "lat": 40.2567, "lon": -5.0011},
        {"nombre": "Arenas de San Pedro", "lat": 40.2036, "lon": -5.0925},
        {"nombre": "Candeleda", "lat": 40.1558, "lon": -5.2403},
        {"nombre": "Gredos (Almanzor)", "lat": 40.2467, "lon": -5.2976},
    ],
}

PARAMS_INST = ["10u", "10v", "2t"]
PARAMS_ACUM = ["tp", "10fg"]
DIAS = ["lun", "mar", "mie", "jue", "vie", "sab", "dom"]
RUMBOS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSO", "SO", "OSO", "O", "ONO", "NO", "NNO"]

_estado = {
    "trabajando": False, "fase": "", "inicio": None, "fin": None, "error": None,
    "ultimo_informe": None, "ultimo_ciclo": None, "ciclo_remoto": None, "mensaje": "",
    "alertas": [], "informe_id": None,
}
_cerrojo = threading.Lock()


# --------------------------------------------------------------------------------------------------------------------
# Utilidades: rutas, configuracion, logs con nombre de fecha/hora
# --------------------------------------------------------------------------------------------------------------------
def crear_dirs():
    for d in (BASE, DIR_DATOS, DIR_INFORMES, DIR_LOGS, DIR_TMP):
        os.makedirs(d, exist_ok=True)


def cargar_cfg():
    crear_dirs()
    cfg = json.loads(json.dumps(CFG_DEF))
    if os.path.exists(FICH_CFG):
        try:
            with open(FICH_CFG, "r", encoding="utf-8") as f:
                usr = json.load(f)
            for k, v in usr.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except Exception as e:
            print("AVISO: config.json no se pudo leer (%s); uso valores por defecto" % e)
    else:
        with open(FICH_CFG, "w", encoding="utf-8") as f:
            json.dump(CFG_DEF, f, indent=2, ensure_ascii=False)
    cfg["horas"] = max(6, min(int(cfg["horas"]), 144))
    return cfg


def marca(t=None):
    """Formato YYYY-MM-DD-HH-MM-SS (hora local del movil)."""
    return time.strftime("%Y-%m-%d-%H-%M-%S", time.localtime(t if t is not None else time.time()))


class Log:
    """Escribe en logs/ecmwf_en_curso.log; al cerrar se renombra a la fecha/hora de su ultima escritura."""

    def __init__(self):
        crear_dirs()
        self.ruta = os.path.join(DIR_LOGS, "ecmwf_en_curso.log")

    def __call__(self, *a):
        linea = "%s  %s" % (time.strftime("%H:%M:%S"), " ".join(str(x) for x in a))
        try:
            with open(self.ruta, "a", encoding="utf-8") as f:
                f.write(linea + "\n")
        except Exception:
            pass
        print(linea, flush=True)

    def cerrar(self):
        try:
            if os.path.exists(self.ruta) and os.path.getsize(self.ruta) > 0:
                destino = os.path.join(DIR_LOGS, "%s_ecmwf.log" % marca(os.path.getmtime(self.ruta)))
                n = 1
                while os.path.exists(destino):
                    n += 1
                    destino = os.path.join(DIR_LOGS, "%s_ecmwf_%d.log" % (marca(os.path.getmtime(self.ruta)), n))
                os.rename(self.ruta, destino)
            logs = sorted(glob.glob(os.path.join(DIR_LOGS, "*_ecmwf*.log")))
            for viejo in logs[:-30]:
                os.remove(viejo)
        except Exception:
            pass


LOG = Log()


# --------------------------------------------------------------------------------------------------------------------
# Tiempo: hora de Madrid sin depender de tzdata
# --------------------------------------------------------------------------------------------------------------------
def _ultimo_domingo(anio, mes):
    d = dt.datetime(anio, mes, 31 if mes in (3, 10) else 30)
    while d.weekday() != 6:
        d -= dt.timedelta(days=1)
    return d


def desfase_madrid(utc):
    """Horas de desfase de Madrid respecto a UTC (1 invierno, 2 verano). utc: datetime ingenuo en UTC."""
    ini = _ultimo_domingo(utc.year, 3).replace(hour=1)
    fin = _ultimo_domingo(utc.year, 10).replace(hour=1)
    return 2 if ini <= utc < fin else 1


def a_madrid(utc):
    return utc + dt.timedelta(hours=desfase_madrid(utc))


def fmt_local(utc):
    m = a_madrid(utc)
    return "%s %02d/%02d %02d:%02d" % (DIAS[m.weekday()], m.day, m.month, m.hour, m.minute)


def fmt_utc(utc):
    return utc.strftime("%Y-%m-%d %H:%M UTC")


# --------------------------------------------------------------------------------------------------------------------
# Entorno eccodes
# --------------------------------------------------------------------------------------------------------------------
def preparar_eccodes():
    lib = os.path.join(PREFIJO, "lib", "libeccodes.so")
    if os.path.exists(lib):
        os.environ.setdefault("ECCODES_DIR", PREFIJO)
        defs = os.path.join(PREFIJO, "share", "eccodes", "definitions")
        if os.path.isdir(defs):
            os.environ.setdefault("ECCODES_DEFINITION_PATH", defs)
        smp = os.path.join(PREFIJO, "share", "eccodes", "samples")
        if os.path.isdir(smp):
            os.environ.setdefault("ECCODES_SAMPLES_PATH", smp)


def importar_eccodes():
    preparar_eccodes()
    import eccodes  # noqa
    return eccodes


# --------------------------------------------------------------------------------------------------------------------
# Remoto: ultimo ciclo y descarga
# --------------------------------------------------------------------------------------------------------------------
def cliente(cfg):
    from ecmwf.opendata import Client
    try:
        return Client(source=cfg["fuente"], model=cfg["modelo"], resol=cfg["resolucion"])
    except TypeError:
        return Client(source=cfg["fuente"])


def pasos_de(cfg, desde=0):
    return list(range(desde, int(cfg["horas"]) + 1, int(cfg["paso_h"])))


def ultimo_ciclo_remoto(cfg):
    """Devuelve datetime (UTC ingenuo) del ultimo ciclo con los campos que necesitamos."""
    c = cliente(cfg)
    r = c.latest(type="fc", step=pasos_de(cfg, 3), param=PARAMS_ACUM)
    return r.replace(tzinfo=None) if getattr(r, "tzinfo", None) else r


def web_datos_modificada():
    """Cabecera Last-Modified de data.ecmwf.int/forecasts/ (texto), o None."""
    try:
        req = urllib.request.Request(URL_DATOS, method="HEAD", headers={"User-Agent": "ecmwf-termux/%s" % VERSION})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.headers.get("Last-Modified")
    except Exception:
        return None


def ruta_cache(ciclo):
    return os.path.join(DIR_DATOS, "ciclo_%s.json" % ciclo.strftime("%Y%m%d%H"))


def ciclos_locales():
    res = []
    for f in sorted(glob.glob(os.path.join(DIR_DATOS, "ciclo_*.json"))):
        try:
            res.append(dt.datetime.strptime(os.path.basename(f)[6:16], "%Y%m%d%H"))
        except Exception:
            pass
    return res


def descargar_con_reintentos(cfg, ciclo, params, pasos, destino):
    c = cliente(cfg)
    ultimo_error = None
    for intento in range(1, 4):
        try:
            if os.path.exists(destino):
                os.remove(destino)
            LOG("descarga %s pasos %d..%d (%d) intento %d" % (",".join(params), pasos[0], pasos[-1], len(pasos), intento))
            c.retrieve(date=ciclo.strftime("%Y%m%d"), time=ciclo.hour, type="fc", step=pasos, param=params,
                       target=destino)
            if os.path.exists(destino) and os.path.getsize(destino) > 1000:
                LOG("descargado %s: %.1f MB" % (os.path.basename(destino), os.path.getsize(destino) / 1e6))
                return
            raise RuntimeError("fichero vacio")
        except Exception as e:
            ultimo_error = e
            LOG("fallo de descarga: %s" % e)
            time.sleep(5 * intento)
    raise RuntimeError("no se pudo descargar %s: %s" % (",".join(params), ultimo_error))


def extraer(cfg, ciclo, ficheros):
    """Lee los GRIB con eccodes y guarda solo la malla de la zona en JSON."""
    import numpy as np
    ec = importar_eccodes()
    ex = cfg["extraccion"]
    out = {"ciclo": ciclo.strftime("%Y-%m-%dT%H:00:00Z"), "extraido": marca(), "resolucion": cfg["resolucion"],
           "modelo": cfg["modelo"], "lats": None, "lons": None, "campos": {}}
    sel = None
    n_msg = 0
    for fich in ficheros:
        with open(fich, "rb") as f:
            while True:
                h = ec.codes_grib_new_from_file(f)
                if h is None:
                    break
                try:
                    sn = ec.codes_get(h, "shortName")
                    paso = int(ec.codes_get(h, "endStep"))
                    if sel is None:
                        ni = int(ec.codes_get(h, "Ni"))
                        nj = int(ec.codes_get(h, "Nj"))
                        lat0 = float(ec.codes_get(h, "latitudeOfFirstGridPointInDegrees"))
                        lon0 = float(ec.codes_get(h, "longitudeOfFirstGridPointInDegrees"))
                        dlat = float(ec.codes_get(h, "jDirectionIncrementInDegrees"))
                        dlon = float(ec.codes_get(h, "iDirectionIncrementInDegrees"))
                        sube = int(ec.codes_get(h, "jScansPositively")) == 1
                        lats = np.array([lat0 + (j * dlat if sube else -j * dlat) for j in range(nj)])
                        lons = np.array([((lon0 + i * dlon + 180.0) % 360.0) - 180.0 for i in range(ni)])
                        ij = np.where((lats >= ex["lat_min"] - 1e-6) & (lats <= ex["lat_max"] + 1e-6))[0]
                        ii = np.where((lons >= ex["lon_min"] - 1e-6) & (lons <= ex["lon_max"] + 1e-6))[0]
                        if len(ij) == 0 or len(ii) == 0:
                            raise RuntimeError("la zona no cae dentro de la malla del GRIB")
                        sel = (ni, nj, ij, ii)
                        sub_lat = lats[ij]
                        sub_lon = lons[ii]
                        oj = np.argsort(sub_lat)
                        oi = np.argsort(sub_lon)
                        out["lats"] = [round(float(x), 4) for x in sub_lat[oj]]
                        out["lons"] = [round(float(x), 4) for x in sub_lon[oi]]
                        sel = (ni, nj, ij, ii, oj, oi)
                    ni, nj, ij, ii, oj, oi = sel
                    vals = ec.codes_get_values(h).reshape(nj, ni)
                    sub = vals[np.ix_(ij, ii)][np.ix_(oj, oi)]
                    out["campos"].setdefault(sn, {})[str(paso)] = [[round(float(v), 4) for v in fila] for fila in sub]
                    n_msg += 1
                finally:
                    ec.codes_release(h)
    if not out["campos"]:
        raise RuntimeError("no se decodifico ningun campo")
    LOG("decodificados %d campos: %s" % (n_msg, ", ".join("%s(%d)" % (k, len(v)) for k, v in out["campos"].items())))
    return out


def actualizar(cfg, forzar=False, solo_local=False):
    """Comprueba el ultimo ciclo, descarga si hace falta. Devuelve (datos_dict, descargado_bool, ciclo_remoto)."""
    locales = ciclos_locales()
    remoto = None
    if not solo_local:
        _estado["fase"] = "consultando ultimo ciclo"
        try:
            remoto = ultimo_ciclo_remoto(cfg)
            LOG("ultimo ciclo publicado por ECMWF: %s" % fmt_utc(remoto))
        except Exception as e:
            LOG("no se pudo consultar ECMWF: %s" % e)
    _estado["ciclo_remoto"] = remoto.strftime("%Y-%m-%dT%H:00:00Z") if remoto else None

    candidatos = []
    if remoto:
        candidatos = [remoto - dt.timedelta(hours=6 * k) for k in range(3)]
    descargado = False
    objetivo = None
    if remoto and not forzar and remoto in locales:
        objetivo = remoto
        LOG("el ciclo %s ya esta descargado: no hace falta bajar nada" % fmt_utc(remoto))
    elif remoto:
        error = None
        for ciclo in candidatos:
            if ciclo in locales and not forzar:
                objetivo = ciclo
                LOG("se usa el ciclo local %s" % fmt_utc(ciclo))
                break
            try:
                _estado["fase"] = "descargando ciclo %s" % ciclo.strftime("%d/%m %H UTC")
                g1 = os.path.join(DIR_TMP, "inst_%s.grib2" % ciclo.strftime("%Y%m%d%H"))
                g2 = os.path.join(DIR_TMP, "acum_%s.grib2" % ciclo.strftime("%Y%m%d%H"))
                descargar_con_reintentos(cfg, ciclo, PARAMS_INST, pasos_de(cfg, 0), g1)
                descargar_con_reintentos(cfg, ciclo, PARAMS_ACUM, pasos_de(cfg, 3), g2)
                _estado["fase"] = "decodificando"
                datos = extraer(cfg, ciclo, [g1, g2])
                with open(ruta_cache(ciclo), "w", encoding="utf-8") as f:
                    json.dump(datos, f, separators=(",", ":"))
                if not cfg.get("conservar_grib"):
                    for g in (g1, g2):
                        try:
                            os.remove(g)
                        except OSError:
                            pass
                objetivo = ciclo
                descargado = True
                break
            except Exception as e:
                error = e
                LOG("ciclo %s fallo: %s" % (fmt_utc(ciclo), e))
                LOG(traceback.format_exc().strip().splitlines()[-1])
        if objetivo is None and not locales:
            raise RuntimeError("no se pudo obtener ningun ciclo: %s" % error)
    if objetivo is None:
        if not locales:
            raise RuntimeError("sin conexion y sin datos locales: ejecuta primero con internet")
        objetivo = locales[-1]
        LOG("se usa el ultimo ciclo local %s" % fmt_utc(objetivo))
    with open(ruta_cache(objetivo), "r", encoding="utf-8") as f:
        datos = json.load(f)
    podar(cfg)
    return datos, descargado, remoto


def podar(cfg):
    locales = ciclos_locales()
    for c in locales[:-int(cfg["conservar_ciclos"])]:
        try:
            os.remove(ruta_cache(c))
        except OSError:
            pass


# --------------------------------------------------------------------------------------------------------------------
# Analisis
# --------------------------------------------------------------------------------------------------------------------
class Ciclo:
    """Datos de un ciclo ya convertidos a unidades utiles (mm, km/h, grados C)."""

    def __init__(self, datos):
        import numpy as np
        self.np = np
        self.base = dt.datetime.strptime(datos["ciclo"], "%Y-%m-%dT%H:00:00Z")
        self.lats = np.array(datos["lats"])
        self.lons = np.array(datos["lons"])
        campos = datos["campos"]
        self.pasos_inst = sorted(int(k) for k in campos.get("10u", {}))
        self.pasos_acum = sorted(int(k) for k in campos.get("tp", {}))

        def pila(nombre, pasos):
            return np.array([campos[nombre][str(p)] for p in pasos], dtype=float)

        u = pila("10u", self.pasos_inst)
        v = pila("10v", self.pasos_inst)
        self.viento = np.hypot(u, v) * 3.6
        self.dir = (270.0 - np.degrees(np.arctan2(v, u))) % 360.0
        self.t2 = pila("2t", self.pasos_inst) - 273.15 if "2t" in campos else None
        tp = pila("tp", self.pasos_acum)
        tp = np.maximum(tp, 0.0)
        self.tp_acum = tp
        inter = np.diff(np.concatenate([np.zeros((1,) + tp.shape[1:]), tp], axis=0), axis=0)
        self.tp_int = np.maximum(inter, 0.0)
        # Las rachas (10fg) pueden faltar en los ultimos pasos: se rellenan con 0 y se anota hasta donde hay dato.
        self.racha = None
        self.pasos_racha = set()
        if "10fg" in campos and campos["10fg"]:
            self.pasos_racha = set(int(k) for k in campos["10fg"])
            forma = tp.shape[1:]
            self.racha = np.array([np.array(campos["10fg"][str(p)], dtype=float) * 3.6 if str(p) in campos["10fg"]
                                   else np.zeros(forma) for p in self.pasos_acum])
        self.horas_fin = self.pasos_acum

    def validez(self, paso):
        return self.base + dt.timedelta(hours=paso)

    def _bilineal(self, campo, lat, lon):
        np = self.np
        la, lo = self.lats, self.lons
        lat = min(max(lat, la[0]), la[-1])
        lon = min(max(lon, lo[0]), lo[-1])
        i1 = int(np.searchsorted(la, lat))
        i1 = min(max(i1, 1), len(la) - 1)
        j1 = int(np.searchsorted(lo, lon))
        j1 = min(max(j1, 1), len(lo) - 1)
        i0, j0 = i1 - 1, j1 - 1
        ty = (lat - la[i0]) / (la[i1] - la[i0]) if la[i1] != la[i0] else 0.0
        tx = (lon - lo[j0]) / (lo[j1] - lo[j0]) if lo[j1] != lo[j0] else 0.0
        a = campo[..., i0, j0]
        b = campo[..., i0, j1]
        c = campo[..., i1, j0]
        d = campo[..., i1, j1]
        return a * (1 - tx) * (1 - ty) + b * tx * (1 - ty) + c * (1 - tx) * ty + d * tx * ty

    def punto(self, lat, lon):
        """Series (por paso) interpoladas en un punto."""
        r = {
            "tp_int": self._bilineal(self.tp_int, lat, lon),
            "tp_acum": self._bilineal(self.tp_acum, lat, lon),
            "viento": self._bilineal(self.viento, lat, lon),
            "racha": self._bilineal(self.racha, lat, lon) if self.racha is not None else None,
            "t2": self._bilineal(self.t2, lat, lon) if self.t2 is not None else None,
        }
        # direccion: se interpola por componentes para no mezclar 359 con 1
        np = self.np
        rad = np.radians(self.dir)
        sx = self._bilineal(np.sin(rad), lat, lon)
        sy = self._bilineal(np.cos(rad), lat, lon)
        r["dir"] = np.degrees(np.arctan2(sx, sy)) % 360.0
        return r

    def mascara_area(self, a):
        np = self.np
        mj = (self.lats >= a["lat_min"] - 1e-6) & (self.lats <= a["lat_max"] + 1e-6)
        mi = (self.lons >= a["lon_min"] - 1e-6) & (self.lons <= a["lon_max"] + 1e-6)
        return np.outer(mj, mi)


def rumbo(grados):
    return RUMBOS[int((grados + 11.25) // 22.5) % 16]


def f1(x):
    return "%.1f" % x


def f0(x):
    return "%.0f" % x


def ajusta(texto, sangria=""):
    import textwrap
    return textwrap.fill(texto, width=ANCHO, initial_indent=sangria, subsequent_indent=sangria)


def tabla(cabeceras, filas, alin=None):
    """Tabla en texto plano con columnas alineadas. alin: cadena con 'i' (izquierda) o 'd' (derecha) por columna."""
    n = len(cabeceras)
    alin = alin or ("i" + "d" * (n - 1))
    anchos = [len(str(c)) for c in cabeceras]
    for fila in filas:
        for k, c in enumerate(fila):
            anchos[k] = max(anchos[k], len(str(c)))

    def celda(k, c):
        return str(c).ljust(anchos[k]) if alin[k] == "i" else str(c).rjust(anchos[k])

    sep = "  "
    lineas = [sep.join(celda(k, c) for k, c in enumerate(cabeceras)).rstrip(),
              sep.join("-" * a for a in anchos)]
    for fila in filas:
        lineas.append(sep.join(celda(k, c) for k, c in enumerate(fila)).rstrip())
    return lineas


def construir_informe(cfg, datos, descargado, remoto, previo=None, localidad=None):
    """Devuelve (texto, resumen_json)."""
    np = __import__("numpy")
    cy = Ciclo(datos)
    um = cfg["umbrales"]
    ahora_utc = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    area = cy.mascara_area(cfg["area_informe"])
    L = []
    alertas = []

    def tit(t):
        L.append("")
        L.append("=" * ANCHO)
        L.append(t)
        L.append("=" * ANCHO)

    # ---- cabecera
    L.append("=" * ANCHO)
    L.append("INFORME ECMWF - LLUVIA, VIENTO Y TEMPERATURA - SUR DE AVILA (Tietar y vertiente sur de Gredos)")
    L.append("=" * ANCHO)
    edad_h = (ahora_utc - cy.base).total_seconds() / 3600.0
    L.append("Generado:           %s (hora de Madrid)" % fmt_local(ahora_utc))
    L.append("Ciclo del modelo:   %s  =  %s hora de Madrid   (%s, malla %s grados)" % (
        fmt_utc(cy.base), fmt_local(cy.base), datos.get("modelo", "ifs").upper(),
        datos.get("resolucion", "0p25").replace("p", ",")))
    L.append("Antiguedad:         %.1f horas desde la hora base del ciclo" % edad_h)
    if remoto:
        if remoto == cy.base:
            L.append("Comprobacion web:   este es el ultimo ciclo publicado por ECMWF (%s)" % fmt_utc(remoto))
        else:
            L.append("Comprobacion web:   ECMWF ya publico un ciclo mas nuevo (%s) pero no se pudo usar" % fmt_utc(remoto))
    else:
        L.append("Comprobacion web:   no se pudo comprobar en ECMWF (sin conexion o modo --sin-descarga); datos guardados")
    L.append("Datos de este run:  %s" % ("descargados ahora (ciclo nuevo)" if descargado else "ya estaban en el movil"))
    mod = web_datos_modificada()
    if mod:
        L.append("data.ecmwf.int:     listado modificado por ultima vez: %s" % mod)
    L.append("Horizonte:          hasta +%d h (hasta %s hora de Madrid)" % (
        cy.pasos_acum[-1], fmt_local(cy.validez(cy.pasos_acum[-1]))))

    # ---- area
    tp_area_total = cy.tp_acum[-1][area]
    pico_int = cy.tp_int[:, area]
    rach_area = cy.racha[:, area] if cy.racha is not None else None
    media_ar = cy.tp_int[:, area].mean(axis=1)
    max_ar = cy.tp_int[:, area].max(axis=1)

    tit("1. RESUMEN DEL AREA (media y maximo de los %d puntos de malla entre %.2f y %.2f N, %.2f y %.2f E)" % (
        int(area.sum()), cfg["area_informe"]["lat_min"], cfg["area_informe"]["lat_max"],
        cfg["area_informe"]["lon_min"], cfg["area_informe"]["lon_max"]))
    k_max = int(np.argmax(max_ar))
    L.append(ajusta("Lluvia total prevista en el periodo: media del area %s mm, maximo en un punto %s mm." % (
        f1(float(tp_area_total.mean())), f1(float(tp_area_total.max())))))
    if max_ar[k_max] >= 0.1:
        L.append(ajusta("Intervalo de 3 h mas lluvioso: termina el %s (hora de Madrid), con hasta %s mm en 3 h en algun punto "
                        "(media del area %s mm)." % (fmt_local(cy.validez(cy.horas_fin[k_max])), f1(float(max_ar[k_max])),
                                                      f1(float(media_ar[k_max])))))
    else:
        L.append("No se preve lluvia apreciable en el periodo.")
    if rach_area is not None:
        k_r = int(np.argmax(rach_area.max(axis=1)))
        L.append(ajusta("Racha maxima prevista: %s km/h, intervalo que termina el %s (hora de Madrid)." % (
            f0(float(rach_area[k_r].max())), fmt_local(cy.validez(cy.horas_fin[k_r])))))
    vmax = cy.viento[:, area]
    k_v = int(np.argmax(vmax.max(axis=1)))
    L.append(ajusta("Viento medio maximo (10 m): %s km/h el %s (hora de Madrid), de componente %s." % (
        f0(float(vmax[k_v].max())), fmt_local(cy.validez(cy.pasos_inst[k_v])),
        rumbo(float(np.mean(cy.dir[k_v][area]))))))
    if cy.t2 is not None:
        L.append("Temperatura a 2 m en el area: minima %s C, maxima %s C." % (
            f1(float(cy.t2[:, area].min())), f1(float(cy.t2[:, area].max()))))

    if cy.racha is not None and cy.pasos_racha and max(cy.pasos_racha) < cy.pasos_acum[-1]:
        L.append(ajusta("Aviso: ECMWF solo publica rachas (10fg) hasta +%d h en este ciclo; despues de esa hora no hay dato de "
                        "racha (se muestra '-')." % max(cy.pasos_racha)))

    # ---- por dias
    tit("2. POR DIAS (dia local de Madrid; el primero y el ultimo pueden estar incompletos)")
    dias = {}
    for k, p in enumerate(cy.horas_fin):
        m = a_madrid(cy.validez(p) - dt.timedelta(minutes=1))   # el intervalo pertenece al dia donde empieza
        d = m.date()
        e = dias.setdefault(d, {"med": 0.0, "max": 0.0, "n": 0, "rach": 0.0, "pmax": 0.0, "mm_pto": np.zeros(int(area.sum()))})
        e["mm_pto"] = e["mm_pto"] + cy.tp_int[k][area]
        e["n"] += 1
        if cy.racha is not None:
            e["rach"] = max(e["rach"], float(cy.racha[k][area].max()))
        e["pmax"] = max(e["pmax"], float(cy.tp_int[k][area].max()))
    vientos_dia, temps_dia = {}, {}
    for k, p in enumerate(cy.pasos_inst):
        d = a_madrid(cy.validez(p)).date()
        vientos_dia[d] = max(vientos_dia.get(d, 0.0), float(cy.viento[k][area].max()))
        if cy.t2 is not None:
            lo, hi = temps_dia.get(d, (99.0, -99.0))
            temps_dia[d] = (min(lo, float(cy.t2[k][area].min())), max(hi, float(cy.t2[k][area].max())))
    filas = []
    for d in sorted(dias):
        e = dias[d]
        filas.append([
            "%s %02d/%02d" % (DIAS[d.weekday()], d.day, d.month),
            f1(float(e["mm_pto"].mean())), f1(float(e["mm_pto"].max())), f1(e["pmax"]),
            f0(e["rach"]) if cy.racha is not None else "-", f0(vientos_dia.get(d, 0.0)),
            f1(temps_dia[d][0]) if d in temps_dia else "-", f1(temps_dia[d][1]) if d in temps_dia else "-",
            "%d" % (e["n"] * 3),
        ])
        if float(e["mm_pto"].max()) >= um["lluvia_24h_mm"]:
            alertas.append("Lluvia >= %.0f mm en el dia %02d/%02d (maximo %.0f mm)" % (
                um["lluvia_24h_mm"], d.day, d.month, float(e["mm_pto"].max())))
    L += tabla(["Dia", "Lluvia media mm", "Lluvia max mm", "Max 3h mm", "Racha max km/h", "Viento medio max", "Tmin C", "Tmax C",
                "Horas cubiertas"], filas)

    # ---- por localidades
    tit("3. POR LOCALIDADES (interpolado desde la malla de 0,25 grados; coordenadas aproximadas)")
    filas = []
    puntos = {}
    for loc in cfg["localidades"]:
        pt = cy.punto(loc["lat"], loc["lon"])
        puntos[loc["nombre"]] = pt
        acum = pt["tp_acum"]

        def acum_h(h):
            idx = [i for i, p in enumerate(cy.horas_fin) if p <= h]
            return float(acum[idx[-1]]) if idx else 0.0
        if pt["racha"] is not None:
            kr = int(np.argmax(pt["racha"]))
            rr = "%s (%s)" % (f0(float(pt["racha"][kr])), fmt_local(cy.validez(cy.horas_fin[kr])))
            rmax = float(pt["racha"][kr])
        else:
            rr, rmax = "-", 0.0
        tmin = f1(float(pt["t2"].min())) if pt["t2"] is not None else "-"
        tmax = f1(float(pt["t2"].max())) if pt["t2"] is not None else "-"
        filas.append([loc["nombre"], f1(acum_h(24)), f1(acum_h(48)), f1(acum_h(72)), f1(float(acum[-1])),
                      f0(float(pt["viento"].max())), rr, tmin, tmax])
        if acum_h(24) >= um["lluvia_24h_mm"]:
            alertas.append("%s: %s mm en las primeras 24 h" % (loc["nombre"], f0(acum_h(24))))
        if rmax >= um["racha_kmh"]:
            alertas.append("%s: racha de %s km/h prevista (%s)" % (
                loc["nombre"], f0(rmax), fmt_local(cy.validez(cy.horas_fin[int(np.argmax(pt['racha']))]))))
    L += tabla(["Localidad", "mm 24h", "mm 48h", "mm 72h", "mm total", "Viento max", "Racha max km/h (cuando)", "Tmin", "Tmax"],
               filas, alin="idddddidd")
    L.append("Las ventanas de 24/48/72 h se cuentan desde la hora base del ciclo (%s UTC)." % cy.base.strftime("%d/%m %H:%M"))

    # ---- detalle horario
    nombre_det = localidad or cfg.get("localidad_detalle") or cfg["localidades"][0]["nombre"]
    clave = None
    for n in puntos:
        if n.lower() == nombre_det.lower():
            clave = n
    if clave is None:
        for n in puntos:
            if nombre_det.lower() in n.lower():
                clave = n
                break
    if clave is None:
        clave = cfg["localidades"][0]["nombre"]
    pt = puntos[clave]
    tit("4. DETALLE CADA 3 HORAS - %s (hora de Madrid; cada fila es el intervalo que TERMINA a esa hora)" % clave.upper())
    filas = []
    inst_idx = {p: i for i, p in enumerate(cy.pasos_inst)}
    for k, p in enumerate(cy.horas_fin):
        i = inst_idx.get(p)
        filas.append([
            fmt_local(cy.validez(p)), f1(float(pt["tp_int"][k])), f1(float(pt["tp_acum"][k])),
            f0(float(pt["viento"][i])) if i is not None else "-",
            rumbo(float(pt["dir"][i])) if i is not None else "-",
            f0(float(pt["racha"][k])) if (pt["racha"] is not None and p in cy.pasos_racha) else "-",
            f1(float(pt["t2"][i])) if (pt["t2"] is not None and i is not None) else "-",
        ])
    L += tabla(["Hora", "Lluvia 3h mm", "Acum mm", "Viento km/h", "Rumbo", "Racha km/h", "T C"], filas, alin="idddidd")

    # ---- horas criticas
    tit("5. EPISODIOS A VIGILAR EN EL AREA")
    hechos = 0
    for k, p in enumerate(cy.horas_fin):
        mmax = float(cy.tp_int[k][area].max())
        if mmax >= um["lluvia_3h_mm"]:
            L.append("  Lluvia intensa: hasta %s mm en 3 h, intervalo que termina el %s." % (f1(mmax), fmt_local(cy.validez(p))))
            alertas.append("Lluvia intensa (%s mm/3h) hasta %s" % (f0(mmax), fmt_local(cy.validez(p))))
            hechos += 1
        if cy.racha is not None:
            r = float(cy.racha[k][area].max())
            if r >= um["racha_kmh"]:
                nivel = "FUERTE" if r >= um["racha_fuerte_kmh"] else "moderada"
                L.append("  Racha %s: %s km/h, intervalo que termina el %s." % (nivel, f0(r), fmt_local(cy.validez(p))))
                hechos += 1
    if cy.t2 is not None:
        for k, p in enumerate(cy.pasos_inst):
            t = float(cy.t2[k][area].min())
            if t <= um["helada_c"]:
                L.append("  Posible helada: %s C en algun punto el %s." % (f1(t), fmt_local(cy.validez(p))))
                hechos += 1
                break
    if hechos == 0:
        L.append("  Nada que supere los umbrales (lluvia %.0f mm/3 h, racha %.0f km/h, helada %.0f C)." % (
            um["lluvia_3h_mm"], um["racha_kmh"], um["helada_c"]))
    # recorta lineas repetidas muy largas
    # ---- comparacion con el ciclo anterior
    if previo is not None:
        try:
            cp = Ciclo(previo)
            tit("6. CAMBIOS RESPECTO AL CICLO ANTERIOR (%s)" % fmt_utc(cp.base))
            desfase = int((cy.base - cp.base).total_seconds() // 3600)
            comunes = [p for p in cy.horas_fin if (p + desfase) in cp.horas_fin]
            if comunes and desfase in cp.horas_fin:
                ult = comunes[-1]
                ia = cy.horas_fin.index(ult)
                ib = cp.horas_fin.index(ult + desfase)
                tp_n = float(cy.tp_acum[ia][area].mean())
                tp_b = float(cp.tp_acum[ib][area].mean()) - float(cp.tp_acum[cp.horas_fin.index(desfase)][area].mean())
                L.append(ajusta("Para el mismo tramo de tiempo (hasta %s), la lluvia media del area pasa de %s mm (ciclo anterior) a %s mm "
                                "(este ciclo)." % (fmt_local(cy.validez(ult)), f1(tp_b), f1(tp_n))))
                if cy.racha is not None and cp.racha is not None:
                    rn = max(float(cy.racha[cy.horas_fin.index(p)][area].max()) for p in comunes)
                    rb = max(float(cp.racha[cp.horas_fin.index(p + desfase)][area].max()) for p in comunes)
                    L.append(ajusta("Racha maxima del area en ese tramo: %s km/h antes, %s km/h ahora." % (f0(rb), f0(rn))))
            else:
                L.append("Los ciclos no comparten tramo de tiempo suficiente para comparar.")
        except Exception as e:
            L.append("(no se pudo comparar con el ciclo anterior: %s)" % e)

    # ---- notas
    tit("NOTAS Y LIMITES")
    notas = [
        "Esto es un pronostico de modelo global (ECMWF IFS, malla de unos 28 km), no un aviso oficial. Para avisos vigentes consulta "
        "siempre a AEMET (avisos de la zona Gredos y Valle del Tietar) y a Proteccion Civil.",
        "A 28 km el modelo no distingue bien valles de sierra: la lluvia y el viento en las cumbres de Gredos y en los valles "
        "encajonados pueden diferir bastante. Los puntos de Gredos (Almanzor) son solo orientativos.",
        "Lluvia = precipitacion total (mm = litros por m2) acumulada y desglosada en intervalos de 3 h. Racha = maximo de viento "
        "a 10 m en cada intervalo. Viento medio = instantaneo a 10 m a la hora indicada.",
        "Las alertas de este informe usan umbrales propios editables en config.json; no equivalen a los umbrales de AEMET.",
        "Fuente: ECMWF Open Data, licencia CC BY 4.0. Contiene datos de ECMWF (https://data.ecmwf.int/forecasts/).",
    ]
    for n in notas:
        L.append(ajusta(n))
        L.append("")
    L.pop()
    plano = "\n".join(L).split("\n")
    texto = "\n".join(l if len(l) <= ANCHO else l[:ANCHO] for l in plano) + "\n"
    resumen = {
        "ciclo": datos["ciclo"], "generado": marca(), "descargado": descargado,
        "lluvia_total_media_mm": round(float(tp_area_total.mean()), 1),
        "lluvia_total_max_mm": round(float(tp_area_total.max()), 1),
        "racha_max_kmh": round(float(rach_area.max()), 0) if rach_area is not None else None,
        "alertas": alertas[:30],
    }
    return texto, resumen


# --------------------------------------------------------------------------------------------------------------------
# Orquestacion
# --------------------------------------------------------------------------------------------------------------------
def generar(cfg, forzar=False, solo_local=False, localidad=None):
    datos, descargado, remoto = actualizar(cfg, forzar=forzar, solo_local=solo_local)
    _estado["fase"] = "analizando"
    previo = None
    locales = ciclos_locales()
    base = dt.datetime.strptime(datos["ciclo"], "%Y-%m-%dT%H:00:00Z")
    anteriores = [c for c in locales if c < base]
    if anteriores:
        try:
            with open(ruta_cache(anteriores[-1]), "r", encoding="utf-8") as f:
                previo = json.load(f)
        except Exception:
            previo = None
    texto, resumen = construir_informe(cfg, datos, descargado, remoto, previo, localidad)
    ruta = os.path.join(DIR_INFORMES, "%s_ecmwf_informe.txt" % marca())
    with open(ruta, "w", encoding="utf-8") as f:
        f.write(texto)
    LOG("informe guardado en %s" % ruta)
    for viejo in sorted(glob.glob(os.path.join(DIR_INFORMES, "*_ecmwf_informe.txt")))[:-20]:
        try:
            os.remove(viejo)
        except OSError:
            pass
    with open(os.path.join(DIR_INFORMES, "ultimo_resumen.json"), "w", encoding="utf-8") as f:
        json.dump(resumen, f, indent=1, ensure_ascii=False)
    _estado["ultimo_informe"] = os.path.basename(ruta)
    _estado["ultimo_ciclo"] = datos["ciclo"]
    _estado["alertas"] = resumen["alertas"]
    _estado["informe_id"] = os.path.basename(ruta)
    return texto, resumen, ruta, descargado


def notificar(cfg, resumen, descargado):
    if not (cfg.get("notificar_termux") and descargado and resumen["alertas"] and shutil.which("termux-notification")):
        return
    try:
        subprocess.run(["termux-notification", "--title", "ECMWF sur de Avila",
                        "--content", "; ".join(resumen["alertas"][:3]), "--id", "ecmwf"], timeout=15)
    except Exception:
        pass


def trabajo(cfg, forzar=False, localidad=None):
    """Un trabajo completo protegido por cerrojo (para servidor y linea de comandos)."""
    if not _cerrojo.acquire(blocking=False):
        return None
    try:
        _estado.update({"trabajando": True, "error": None, "inicio": marca(), "fase": "iniciando"})
        try:
            texto, resumen, ruta, descargado = generar(cfg, forzar=forzar, localidad=localidad)
            notificar(cfg, resumen, descargado)
            _estado["mensaje"] = "informe listo"
            return texto
        except Exception as e:
            _estado["error"] = str(e)
            LOG("ERROR: %s" % e)
            LOG(traceback.format_exc())
            return None
        finally:
            _estado.update({"trabajando": False, "fin": marca(), "fase": ""})
            LOG.cerrar()
    finally:
        _cerrojo.release()


def ultimo_informe_texto():
    fs = sorted(glob.glob(os.path.join(DIR_INFORMES, "*_ecmwf_informe.txt")))
    if not fs:
        return None
    with open(fs[-1], "r", encoding="utf-8") as f:
        return f.read()


# --------------------------------------------------------------------------------------------------------------------
# Servidor para la extension
# --------------------------------------------------------------------------------------------------------------------
PANEL = """<!DOCTYPE html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ECMWF Avila</title><style>body{font-family:sans-serif;margin:8px}pre{font-size:9px;line-height:1.15;overflow-x:auto}
button{padding:8px 12px;margin:2px}</style></head><body>
<button onclick="act()">Actualizar ahora</button><button onclick="cargar()">Recargar informe</button><span id="e"></span>
<pre id="p">Cargando...</pre>
<script>
async function cargar(){try{const r=await fetch('/informe');document.getElementById('p').textContent=await r.text();
const s=await (await fetch('/estado')).json();document.getElementById('e').textContent=' '+(s.trabajando?('trabajando: '+s.fase):'listo')}catch(e){document.getElementById('p').textContent='Sin conexion con el servidor'}}
async function act(){await fetch('/actualizar',{method:'POST'});document.getElementById('e').textContent=' iniciado';setTimeout(cargar,4000)}
cargar();setInterval(cargar,15000);
</script></body></html>"""


def estado_completo(cfg):
    e = dict(_estado)
    e["version"] = VERSION
    e["ciclos_locales"] = [c.strftime("%Y-%m-%dT%H:00:00Z") for c in ciclos_locales()]
    return e


def crear_manejador(cfg):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _enviar(self, codigo, cuerpo, tipo="application/json; charset=utf-8"):
            b = cuerpo if isinstance(cuerpo, bytes) else cuerpo.encode("utf-8")
            self.send_response(codigo)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.end_headers()

        def do_GET(self):
            ruta = self.path.split("?")[0]
            if ruta == "/ping":
                self._enviar(200, json.dumps({"ok": True, "programa": "meteo", "version": VERSION}))
            elif ruta == "/estado":
                self._enviar(200, json.dumps(estado_completo(cfg)))
            elif ruta == "/informe":
                t = ultimo_informe_texto()
                self._enviar(200, t if t else "Aun no hay informe. Pulsa Actualizar.", "text/plain; charset=utf-8")
            elif ruta == "/resumen":
                f = os.path.join(DIR_INFORMES, "ultimo_resumen.json")
                self._enviar(200, open(f, encoding="utf-8").read() if os.path.exists(f) else "{}")
            elif ruta == "/panel":
                self._enviar(200, PANEL, "text/html; charset=utf-8")
            elif ruta == "/log":
                fs = [os.path.join(DIR_LOGS, "ecmwf_en_curso.log")] + sorted(glob.glob(os.path.join(DIR_LOGS, "*_ecmwf*.log")))[::-1]
                for f in fs:
                    if os.path.exists(f):
                        self._enviar(200, "".join(open(f, encoding="utf-8").readlines()[-80:]), "text/plain; charset=utf-8")
                        return
                self._enviar(200, "", "text/plain; charset=utf-8")
            else:
                self._enviar(404, json.dumps({"ok": False, "error": "ruta desconocida"}))

        def do_POST(self):
            ruta = self.path.split("?")[0]
            if ruta == "/actualizar":
                if _estado["trabajando"]:
                    self._enviar(200, json.dumps({"ok": True, "mensaje": "ya hay un trabajo en marcha"}))
                    return
                forzar = "forzar=1" in self.path
                threading.Thread(target=trabajo, args=(cfg, forzar), daemon=True).start()
                self._enviar(200, json.dumps({"ok": True, "mensaje": "iniciado"}))
            else:
                self._enviar(404, json.dumps({"ok": False, "error": "ruta desconocida"}))
    return H


def bucle_auto(cfg):
    minutos = int(cfg.get("auto_minutos", 0))
    if minutos <= 0:
        return
    time.sleep(10)
    while True:
        if not _estado["trabajando"]:
            trabajo(cfg)
        time.sleep(minutos * 60)


def servidor(cfg, puerto):
    try:
        with open(os.path.join(DIR_INFORMES, "ultimo_resumen.json"), "r", encoding="utf-8") as f:
            r = json.load(f)
        _estado["ultimo_ciclo"] = r.get("ciclo")
        _estado["alertas"] = r.get("alertas", [])
        fs = sorted(glob.glob(os.path.join(DIR_INFORMES, "*_ecmwf_informe.txt")))
        _estado["ultimo_informe"] = _estado["informe_id"] = os.path.basename(fs[-1]) if fs else None
    except Exception:
        pass
    srv = ThreadingHTTPServer(("127.0.0.1", puerto), crear_manejador(cfg))
    LOG("servidor ECMWF en http://127.0.0.1:%d  (panel: /panel)  auto cada %s min" % (puerto, cfg.get("auto_minutos")))
    threading.Thread(target=bucle_auto, args=(cfg,), daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        LOG("servidor detenido")
        LOG.cerrar()


# --------------------------------------------------------------------------------------------------------------------
# Diagnostico y comandos
# --------------------------------------------------------------------------------------------------------------------
def diagnostico(cfg):
    ruta = os.path.join(BASE, "%s_ecmwf_diagnostico.txt" % marca())
    L = ["DIAGNOSTICO meteo.py %s  -  %s" % (VERSION, marca()), "",
         "Python: %s" % sys.version.replace("\n", " "), "Base: %s" % BASE, "Prefijo: %s" % PREFIJO]
    try:
        usado = shutil.disk_usage(BASE)
        L.append("Disco libre: %.2f GB" % (usado.free / 1e9))
    except Exception:
        pass
    L.append("libeccodes.so: %s" % os.path.exists(os.path.join(PREFIJO, "lib", "libeccodes.so")))
    L.append("libaec.so: %s" % os.path.exists(os.path.join(PREFIJO, "lib", "libaec.so")))
    try:
        ec = importar_eccodes()
        L.append("eccodes: OK, API %s" % ec.codes_get_api_version())
    except Exception as e:
        L.append("eccodes: FALLO: %s" % e)
    try:
        import ecmwf.opendata  # noqa
        L.append("ecmwf-opendata: OK")
    except Exception as e:
        L.append("ecmwf-opendata: FALLO: %s" % e)
    try:
        import numpy
        L.append("numpy: %s" % numpy.__version__)
    except Exception as e:
        L.append("numpy: FALLO: %s" % e)
    L.append("data.ecmwf.int Last-Modified: %s" % web_datos_modificada())
    try:
        L.append("Ultimo ciclo remoto: %s" % fmt_utc(ultimo_ciclo_remoto(cfg)))
    except Exception as e:
        L.append("Ultimo ciclo remoto: FALLO: %s" % e)
    L.append("Ciclos locales: %s" % ", ".join(c.strftime("%Y%m%d%H") for c in ciclos_locales()))
    L.append("")
    L.append("Configuracion:")
    L.append(json.dumps(cfg, indent=1, ensure_ascii=False))
    with open(ruta, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print("\n".join(L))
    print("\nDiagnostico guardado en: %s" % ruta)


def limpiar(cfg):
    n = 0
    for f in glob.glob(os.path.join(DIR_TMP, "*")):
        try:
            os.remove(f)
            n += 1
        except OSError:
            pass
    podar(cfg)
    print("Borrados %d ficheros temporales; ciclos conservados: %s" % (n, ", ".join(c.strftime("%Y%m%d%H") for c in ciclos_locales())))


def main():
    ap = argparse.ArgumentParser(description="Informe ECMWF del sur de Avila")
    ap.add_argument("orden", nargs="?", default="informe",
                    choices=["informe", "estado", "servidor", "abrir", "diag", "limpiar"])
    ap.add_argument("--forzar", action="store_true", help="volver a descargar aunque el ciclo ya exista")
    ap.add_argument("--sin-descarga", action="store_true", help="usar solo datos locales")
    ap.add_argument("--localidad", help="localidad para el detalle horario")
    ap.add_argument("--horas", type=int, help="horizonte en horas (max 144)")
    ap.add_argument("--puerto", type=int)
    ap.add_argument("--version", action="version", version=VERSION)
    a = ap.parse_args()
    cfg = cargar_cfg()
    if a.horas:
        cfg["horas"] = max(6, min(a.horas, 144))
    puerto = a.puerto or int(cfg["puerto"])

    if a.orden == "diag":
        diagnostico(cfg)
    elif a.orden == "limpiar":
        limpiar(cfg)
    elif a.orden == "estado":
        try:
            r = ultimo_ciclo_remoto(cfg)
            print("Ultimo ciclo publicado por ECMWF: %s (%s hora de Madrid)" % (fmt_utc(r), fmt_local(r)))
        except Exception as e:
            r = None
            print("No se pudo consultar ECMWF: %s" % e)
        loc = ciclos_locales()
        print("Ciclos guardados: %s" % (", ".join(c.strftime("%Y%m%d%H") for c in loc) or "ninguno"))
        if r and loc:
            print("Estado: %s" % ("al dia" if loc[-1] >= r else "HAY UN CICLO NUEVO por descargar"))
    elif a.orden == "servidor":
        servidor(cfg, puerto)
    elif a.orden == "abrir":
        url = "http://127.0.0.1:%d/panel" % puerto
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/ping" % puerto, timeout=3)
        except Exception:
            print("El servidor no esta en marcha. Arranca en otra sesion: python meteo.py servidor")
        if shutil.which("termux-open-url"):
            subprocess.run(["termux-open-url", url])
        print("Panel: %s" % url)
    else:
        texto = trabajo(cfg, forzar=a.forzar, localidad=a.localidad) if not a.sin_descarga else None
        if a.sin_descarga:
            texto, resumen, ruta, _ = generar(cfg, solo_local=True, localidad=a.localidad)
            LOG.cerrar()
        if texto:
            print(texto)
        else:
            print("No se pudo generar el informe: %s" % _estado.get("error"))
            sys.exit(1)


if __name__ == "__main__":
    main()
