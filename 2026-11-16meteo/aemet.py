#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
aemet.py  -  Prevision, avisos y observaciones de AEMET OpenData para el sur de Avila, con contraste frente a ECMWF (meteo.py).

Uso (Termux):
  python aemet.py                  -> consulta AEMET, analiza y muestra el informe
  python aemet.py informe [--localidad "La Adrada"] [--sin-red]
  python aemet.py estado           -> comprueba la clave y la ultima elaboracion de AEMET frente a la guardada
  python aemet.py servidor [--puerto 8780]   -> servidor local para la extension de Firefox
  python aemet.py abrir            -> abre el panel en el navegador
  python aemet.py diag             -> diagnostico + muestras crudas de cada servicio de AEMET
  python aemet.py limpiar

La clave (api_key) va en ~/aemet_datos/config.json ("api_key") o en la variable de entorno AEMET_API_KEY.
Fuente: AEMET. Informacion elaborada por la Agencia Estatal de Meteorologia (AEMET), usada segun sus condiciones de reutilizacion.
"""
import os
import sys

# Evita que la carpeta del script tape paquetes con el mismo nombre.
_AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path = [p for p in sys.path if os.path.abspath(p or ".") != _AQUI]

import io
import re
import json
import glob
import time
import shutil
import tarfile
import argparse
import threading
import traceback
import subprocess
import unicodedata
import datetime as dt
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

VERSION = "1.0.0"
PUERTO_DEFECTO = 8780
ANCHO = 121
API = "https://opendata.aemet.es/opendata/api"

BASE = os.environ.get("AEMET_DIR", os.path.join(os.path.expanduser("~"), "aemet_datos"))
DIR_DATOS = os.path.join(BASE, "datos")
DIR_INFORMES = os.path.join(BASE, "informes")
DIR_LOGS = os.path.join(BASE, "logs")
FICH_CFG = os.path.join(BASE, "config.json")
DIR_METEO = os.environ.get("METEO_DIR", os.path.join(os.path.expanduser("~"), "meteo_datos"))

CFG_DEF = {
    "api_key": "",
    "puerto": PUERTO_DEFECTO,
    "provincia_ine": "05",              # Avila
    "pausa_s": 1.2,                     # pausa entre llamadas a la API (AEMET limita las peticiones por minuto)
    "auto_minutos": 120,
    "notificar_termux": True,
    "localidad_detalle": "La Adrada",
    "area_avisos": "esp",               # codigo de area de avisos CAP ("esp" = toda Espana; luego se filtra por texto)
    "filtro_avisos": ["avila", "gredos", "tietar", "alberche"],   # textos que debe contener la zona del aviso (sin acentos)
    "observacion": {"lat_min": 39.90, "lat_max": 40.70, "lon_min": -5.80, "lon_max": -4.20},
    "umbrales": {"lluvia_h_mm": 5.0, "lluvia_3h_mm": 10.0, "racha_kmh": 70.0, "racha_fuerte_kmh": 90.0, "helada_c": 0.0},
    "localidades": [
        {"nombre": "La Adrada"}, {"nombre": "Sotillo de la Adrada"}, {"nombre": "Burgohondo"},
        {"nombre": "Navaluenga"}, {"nombre": "Cebreros"}, {"nombre": "Pedro Bernardo"},
        {"nombre": "Mombeltran"}, {"nombre": "Arenas de San Pedro"}, {"nombre": "Candeleda"},
    ],
}

DIAS = ["lun", "mar", "mie", "jue", "vie", "sab", "dom"]
NIVEL = {"verde": 0, "amarillo": 1, "naranja": 2, "rojo": 3}

_estado = {"trabajando": False, "fase": "", "inicio": None, "fin": None, "error": None, "ultimo_informe": None,
           "ultima_elaboracion": None, "alertas": [], "informe_id": None, "mensaje": "", "clave": None}
_cerrojo = threading.Lock()


# --------------------------------------------------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------------------------------------------------
def crear_dirs():
    for d in (BASE, DIR_DATOS, DIR_INFORMES, DIR_LOGS):
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
    cfg["api_key"] = (os.environ.get("AEMET_API_KEY") or cfg.get("api_key") or "").strip()
    return cfg


def marca(t=None):
    return time.strftime("%Y-%m-%d-%H-%M-%S", time.localtime(t if t is not None else time.time()))


class Log:
    def __init__(self):
        crear_dirs()
        self.ruta = os.path.join(DIR_LOGS, "aemet_en_curso.log")
        self.clave = ""

    def __call__(self, *a):
        txt = " ".join(str(x) for x in a)
        if self.clave:
            txt = txt.replace(self.clave, "<CLAVE>")
        linea = "%s  %s" % (time.strftime("%H:%M:%S"), txt)
        try:
            with open(self.ruta, "a", encoding="utf-8") as f:
                f.write(linea + "\n")
        except Exception:
            pass
        print(linea, flush=True)

    def cerrar(self):
        try:
            if os.path.exists(self.ruta) and os.path.getsize(self.ruta) > 0:
                m = marca(os.path.getmtime(self.ruta))
                destino = os.path.join(DIR_LOGS, "%s_aemet.log" % m)
                n = 1
                while os.path.exists(destino):
                    n += 1
                    destino = os.path.join(DIR_LOGS, "%s_aemet_%d.log" % (m, n))
                os.rename(self.ruta, destino)
            for viejo in sorted(glob.glob(os.path.join(DIR_LOGS, "*_aemet*.log")))[:-30]:
                os.remove(viejo)
        except Exception:
            pass


LOG = Log()


def _ultimo_domingo(anio, mes):
    d = dt.datetime(anio, mes, 31)
    while d.weekday() != 6:
        d -= dt.timedelta(days=1)
    return d


def desfase_madrid(utc):
    ini = _ultimo_domingo(utc.year, 3).replace(hour=1)
    fin = _ultimo_domingo(utc.year, 10).replace(hour=1)
    return 2 if ini <= utc < fin else 1


def a_madrid(utc):
    return utc + dt.timedelta(hours=desfase_madrid(utc))


def de_madrid_a_utc(local):
    """Hora local de Madrid (ingenua) -> UTC ingenua."""
    u = local - dt.timedelta(hours=2)
    return u if desfase_madrid(u) == 2 else local - dt.timedelta(hours=1)


def fmt_local(utc):
    m = a_madrid(utc)
    return "%s %02d/%02d %02d:%02d" % (DIAS[m.weekday()], m.day, m.month, m.hour, m.minute)


def ahora_utc():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def f0(x):
    return "%.0f" % x


def f1(x):
    return "%.1f" % x


def sin_acentos(s):
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower().strip()


def ajusta(texto, sangria="", sub=None):
    import textwrap
    return textwrap.fill(texto, width=ANCHO, initial_indent=sangria, subsequent_indent=sangria if sub is None else sub,
                         break_long_words=False)


def tabla(cabeceras, filas, alin=None):
    n = len(cabeceras)
    alin = alin or ("i" + "d" * (n - 1))
    anchos = [len(str(c)) for c in cabeceras]
    for fila in filas:
        for k, c in enumerate(fila):
            anchos[k] = max(anchos[k], len(str(c)))

    def celda(k, c):
        return str(c).ljust(anchos[k]) if alin[k] == "i" else str(c).rjust(anchos[k])
    sep = "  "
    out = [sep.join(celda(k, c) for k, c in enumerate(cabeceras)).rstrip(), sep.join("-" * a for a in anchos)]
    for fila in filas:
        out.append(sep.join(celda(k, c) for k, c in enumerate(fila)).rstrip())
    return out


def num(x, defecto=None):
    """Convierte valores de AEMET ('12', '0,5', 'Ip', '', None) a float."""
    if x is None:
        return defecto
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip().replace(",", ".")
    if s.lower() in ("ip", "inap", "inapreciable"):
        return 0.0
    if s == "" or s.lower() in ("varias", "nan", "none"):
        return defecto
    try:
        return float(s)
    except ValueError:
        return defecto


# --------------------------------------------------------------------------------------------------------------------
# Cliente de la API (dos pasos)
# --------------------------------------------------------------------------------------------------------------------
class ErrorAemet(Exception):
    pass


def _leer(url, cabeceras=None, timeout=60):
    req = urllib.request.Request(url, headers=cabeceras or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), r.headers.get("Content-Type", "")


def decodificar(b):
    for enc in ("utf-8", "latin-1"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", "replace")


def api_datos(cfg, ruta, binario=False, reintentos=3):
    """Paso 1: pide ruta con la clave; paso 2: descarga la URL 'datos'. Devuelve JSON (o bytes si binario)."""
    cab = {"api_key": cfg["api_key"], "Accept": "application/json", "Cache-Control": "no-cache",
           "User-Agent": "aemet-termux/%s" % VERSION}
    ultimo = None
    for intento in range(1, reintentos + 1):
        try:
            time.sleep(float(cfg.get("pausa_s", 1.2)))
            try:
                b, _ = _leer(API + ruta, cab)
            except urllib.error.HTTPError as e:
                b = e.read()
                if e.code == 429:
                    raise ErrorAemet("429 demasiadas peticiones")
                if e.code in (401, 403):
                    raise ErrorAemet("clave rechazada (%d)" % e.code)
            meta = json.loads(decodificar(b))
            estado = int(meta.get("estado", 0))
            if estado == 429:
                raise ErrorAemet("429 demasiadas peticiones")
            if estado in (401, 403):
                raise ErrorAemet("clave rechazada: %s" % meta.get("descripcion"))
            if estado != 200 or not meta.get("datos"):
                raise ErrorAemet("respuesta %s: %s" % (estado, meta.get("descripcion")))
            time.sleep(float(cfg.get("pausa_s", 1.2)) / 2)
            datos, _ = _leer(meta["datos"], {"User-Agent": "aemet-termux/%s" % VERSION}, timeout=120)
            if binario:
                return datos
            return json.loads(decodificar(datos))
        except ErrorAemet as e:
            ultimo = e
            if "clave" in str(e):
                raise
            espera = 25 * intento if "429" in str(e) else 4 * intento
            LOG("AEMET %s (%s); reintento %d en %d s" % (ruta, e, intento, espera))
            time.sleep(espera)
        except Exception as e:
            ultimo = e
            LOG("fallo en %s: %s; reintento %d" % (ruta, e, intento))
            time.sleep(4 * intento)
    raise ErrorAemet("%s: %s" % (ruta, ultimo))


# --------------------------------------------------------------------------------------------------------------------
# Municipios
# --------------------------------------------------------------------------------------------------------------------
def clave_nombre(n):
    """'Adrada, La' -> 'la adrada'; quita acentos."""
    n = sin_acentos(n)
    m = re.match(r"^(.*),\s*(el|la|los|las|l'|els|les)$", n)
    if m:
        n = "%s %s" % (m.group(2), m.group(1))
    return re.sub(r"\s+", " ", n).strip()


def resolver_municipios(cfg):
    """Devuelve lista de {'nombre','codigo','lat','lon'} usando maestro/municipios (en cache)."""
    ruta = os.path.join(DIR_DATOS, "maestro_municipios.json")
    maestro = None
    if os.path.exists(ruta) and time.time() - os.path.getmtime(ruta) < 30 * 86400:
        try:
            maestro = json.load(open(ruta, encoding="utf-8"))
        except Exception:
            maestro = None
    if maestro is None:
        LOG("descargando maestro de municipios")
        maestro = api_datos(cfg, "/maestro/municipios")
        with open(ruta, "w", encoding="utf-8") as f:
            json.dump(maestro, f, ensure_ascii=False)
    prov = cfg["provincia_ine"]
    indice = {}
    for m in maestro:
        cod = str(m.get("id", "")).replace("id", "")
        if cod.startswith(prov) and len(cod) == 5:
            indice[clave_nombre(m.get("nombre", ""))] = m
    res = []
    for loc in cfg["localidades"]:
        if loc.get("codigo"):
            res.append({"nombre": loc["nombre"], "codigo": str(loc["codigo"]), "lat": loc.get("lat"), "lon": loc.get("lon")})
            continue
        m = indice.get(clave_nombre(loc["nombre"]))
        if m is None:
            cand = [k for k in indice if clave_nombre(loc["nombre"]) in k or k in clave_nombre(loc["nombre"])]
            m = indice[cand[0]] if cand else None
        if m is None:
            LOG("AVISO: no encuentro el municipio '%s' en el maestro de AEMET (provincia %s)" % (loc["nombre"], prov))
            continue
        res.append({"nombre": loc["nombre"], "codigo": str(m["id"]).replace("id", ""),
                    "lat": num(m.get("latitud_dec")), "lon": num(m.get("longitud_dec"))})
    return res


# --------------------------------------------------------------------------------------------------------------------
# Lectura de predicciones
# --------------------------------------------------------------------------------------------------------------------
def horas_municipio(datos):
    """Devuelve (elaborado, [ {utc, local, lluvia, nieve, prob, temp, viento, dir, racha, cielo} ])."""
    if isinstance(datos, list):
        datos = datos[0] if datos else {}
    elab = datos.get("elaborado")
    filas = {}
    for dia in (datos.get("prediccion") or {}).get("dia", []):
        fecha = str(dia.get("fecha", ""))[:10]
        try:
            f = dt.datetime.strptime(fecha, "%Y-%m-%d")
        except ValueError:
            continue

        def poner(lista, clave, fn=None):
            for it in lista or []:
                per = str(it.get("periodo", ""))
                if not re.fullmatch(r"\d{1,2}", per):
                    continue
                h = int(per)
                loc = f + dt.timedelta(hours=h)
                fila = filas.setdefault(loc, {"local": loc})
                if fn:
                    fn(fila, it)
                else:
                    fila[clave] = num(it.get("value"))
        poner(dia.get("precipitacion"), "lluvia")
        poner(dia.get("nieve"), "nieve")
        poner(dia.get("temperatura"), "temp")
        poner(dia.get("sensTermica"), "sens")
        poner(dia.get("humedadRelativa"), "hr")
        poner(dia.get("estadoCielo"), None, lambda fila, it: fila.__setitem__("cielo", str(it.get("descripcion", ""))))

        def viento(fila, it):
            if "value" in it:
                fila["racha"] = num(it.get("value"))
            if "velocidad" in it:
                v = it.get("velocidad")
                d = it.get("direccion")
                fila["viento"] = num(v[0] if isinstance(v, list) and v else v)
                fila["dir"] = (d[0] if isinstance(d, list) and d else d) or ""
        poner(dia.get("vientoAndRachaMax"), None, viento)
        # probabilidad de precipitacion: periodos tipo "0107" (horas 01 a 07)
        for it in dia.get("probPrecipitacion", []) or []:
            per = str(it.get("periodo", ""))
            if re.fullmatch(r"\d{4}", per):
                h0, h1 = int(per[:2]), int(per[2:])
                p = num(it.get("value"))
                for h in range(h0, (h1 if h1 > h0 else 24) + 1):
                    loc = f + dt.timedelta(hours=h % 24 if h < 24 else 23)
                    if loc in filas and p is not None:
                        filas[loc]["prob"] = p
    res = []
    for loc in sorted(filas):
        fila = filas[loc]
        fila["utc"] = de_madrid_a_utc(loc)
        res.append(fila)
    return elab, res


def dias_municipio(datos):
    """Prediccion diaria 7 dias: lista de {fecha, tmax, tmin, prob, racha, viento, dir, cielo}."""
    if isinstance(datos, list):
        datos = datos[0] if datos else {}
    elab = datos.get("elaborado")
    res = []
    for dia in (datos.get("prediccion") or {}).get("dia", []):
        try:
            f = dt.datetime.strptime(str(dia.get("fecha", ""))[:10], "%Y-%m-%d")
        except ValueError:
            continue

        def total(lista):
            if not lista:
                return None
            for it in lista:
                if str(it.get("periodo", "")) in ("00-24", ""):
                    return it
            return lista[0]
        pp = [num(it.get("value")) for it in (dia.get("probPrecipitacion") or []) if num(it.get("value")) is not None]
        rm = total(dia.get("rachaMax"))
        vv = total(dia.get("viento"))
        cs = total(dia.get("estadoCielo"))
        t = dia.get("temperatura") or {}
        res.append({"fecha": f, "tmax": num(t.get("maxima")), "tmin": num(t.get("minima")),
                    "prob": max(pp) if pp else None, "racha": num(rm.get("value")) if rm else None,
                    "viento": num(vv.get("velocidad")) if vv else None, "dir": (vv.get("direccion") if vv else "") or "",
                    "cielo": (cs.get("descripcion") if cs else "") or ""})
    return elab, res


# --------------------------------------------------------------------------------------------------------------------
# Avisos CAP y observaciones
# --------------------------------------------------------------------------------------------------------------------
def leer_avisos(cfg):
    """Devuelve (lista_avisos_zona, n_ficheros) o lanza ErrorAemet."""
    raw = api_datos(cfg, "/avisos_cap/ultimoelaborado/area/%s" % cfg["area_avisos"], binario=True)
    avisos = []
    n = 0
    filtro = [sin_acentos(x) for x in cfg["filtro_avisos"]]
    try:
        tf = tarfile.open(fileobj=io.BytesIO(raw), mode="r:*")
        miembros = [(m.name, tf.extractfile(m).read()) for m in tf.getmembers() if m.isfile()]
    except tarfile.TarError:
        miembros = [("aviso.xml", raw)]
    for nombre, cont in miembros:
        n += 1
        try:
            raiz = ET.fromstring(cont)
        except ET.ParseError:
            continue
        ns = {"c": raiz.tag.split("}")[0].strip("{")} if "}" in raiz.tag else {}

        def hallar(el, ruta):
            return el.findall(ruta, ns) if ns else el.findall(ruta.replace("c:", ""))
        infos = hallar(raiz, "c:info")
        for info in infos:
            leng = (info.findtext("c:language", default="", namespaces=ns) if ns else info.findtext("language", default="")) or ""
            if leng and not leng.lower().startswith("es"):
                continue

            def t(tag):
                return (info.findtext("c:" + tag, default="", namespaces=ns) if ns else info.findtext(tag, default="")) or ""
            params = {}
            for p in hallar(info, "c:parameter"):
                k = (p.findtext("c:valueName", default="", namespaces=ns) if ns else p.findtext("valueName", default="")) or ""
                v = (p.findtext("c:value", default="", namespaces=ns) if ns else p.findtext("value", default="")) or ""
                params[k] = v
            for ar in hallar(info, "c:area"):
                desc = (ar.findtext("c:areaDesc", default="", namespaces=ns) if ns else ar.findtext("areaDesc", default="")) or ""
                if not any(f in sin_acentos(desc) for f in filtro):
                    continue
                nivel = ""
                for k, v in params.items():
                    if "nivel" in k.lower():
                        nivel = v
                sev = t("severity")
                niv = sin_acentos(nivel) if nivel else {"minor": "verde", "moderate": "amarillo", "severe": "naranja",
                                                        "extreme": "rojo"}.get(sev.lower(), sev.lower())
                avisos.append({
                    "zona": desc, "evento": t("event"), "nivel": niv, "gravedad": sev, "inicio": t("onset") or t("effective"),
                    "fin": t("expires"), "titulo": t("headline"), "descripcion": t("description"),
                    "fenomeno": params.get("AEMET-Meteoalerta fenomeno", ""), "probabilidad": params.get("AEMET-Meteoalerta probabilidad", ""),
                })
    return avisos, n


def parsea_iso(s):
    if not s:
        return None
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo:
            d = d.astimezone(dt.timezone.utc).replace(tzinfo=None)
        return d
    except Exception:
        return None


def leer_observacion(cfg):
    """Observacion convencional de las ultimas horas, filtrada al recuadro de la zona. Devuelve lista de estaciones."""
    datos = api_datos(cfg, "/observacion/convencional/todas")
    o = cfg["observacion"]
    est = {}
    for r in datos:
        lat, lon = num(r.get("lat")), num(r.get("lon"))
        if lat is None or lon is None:
            continue
        if not (o["lat_min"] <= lat <= o["lat_max"] and o["lon_min"] <= lon <= o["lon_max"]):
            continue
        e = est.setdefault(r.get("idema") or r.get("ubi"), {"idema": r.get("idema"), "nombre": r.get("ubi", ""), "lat": lat, "lon": lon,
                                                            "alt": num(r.get("alt")), "obs": []})
        e["obs"].append(r)
    res = []
    for e in est.values():
        obs = sorted(e["obs"], key=lambda r: str(r.get("fint")))
        if not obs:
            continue
        prec = [num(r.get("prec")) for r in obs if num(r.get("prec")) is not None]
        tmax = [num(r.get("tamax"), num(r.get("ta"))) for r in obs]
        tmin = [num(r.get("tamin"), num(r.get("ta"))) for r in obs]
        vmax = [num(r.get("vmax")) for r in obs if num(r.get("vmax")) is not None]
        vv = [num(r.get("vv")) for r in obs if num(r.get("vv")) is not None]
        ult = obs[-1]
        res.append({
            "idema": e["idema"], "nombre": e["nombre"], "alt": e["alt"], "n": len(obs),
            "ultima": parsea_iso(str(ult.get("fint"))), "ta": num(ult.get("ta")), "hr": num(ult.get("hr")),
            "vv": (num(ult.get("vv")) * 3.6) if num(ult.get("vv")) is not None else None,
            "prec24": sum(prec) if prec else None,
            "tmax": max([x for x in tmax if x is not None], default=None), "tmin": min([x for x in tmin if x is not None], default=None),
            "racha": max(vmax) * 3.6 if vmax else None, "vmed": max(vv) * 3.6 if vv else None,
        })
    return sorted(res, key=lambda x: x["nombre"])


# --------------------------------------------------------------------------------------------------------------------
# Actualizacion
# --------------------------------------------------------------------------------------------------------------------
def fecha_elab(s):
    d = parsea_iso(str(s)) if s else None
    return d


def actualizar(cfg, solo_local=False):
    """Descarga todo y guarda aemet_AAAAMMDDHHMMSS.json. Devuelve dict de datos."""
    ruta_ult = os.path.join(DIR_DATOS, "ultimo.json")
    if solo_local:
        if not os.path.exists(ruta_ult):
            raise RuntimeError("no hay datos guardados")
        return json.load(open(ruta_ult, encoding="utf-8")), False
    if not cfg["api_key"]:
        raise RuntimeError("falta la api_key: ponla en %s (campo api_key) o en AEMET_API_KEY" % FICH_CFG)
    LOG.clave = cfg["api_key"]
    previo = None
    if os.path.exists(ruta_ult):
        try:
            previo = json.load(open(ruta_ult, encoding="utf-8"))
        except Exception:
            previo = None
    _estado["fase"] = "municipios"
    muni = resolver_municipios(cfg)
    if not muni:
        raise RuntimeError("no se resolvio ningun municipio; revisa 'localidades' y 'provincia_ine' en config.json")
    out = {"descargado": marca(), "descargado_utc": ahora_utc().strftime("%Y-%m-%dT%H:%M:%S"), "municipios": {}, "avisos": None,
           "avisos_error": None, "obs": None, "obs_error": None}
    for m in muni:
        _estado["fase"] = "prediccion de %s" % m["nombre"]
        reg = {"codigo": m["codigo"], "lat": m["lat"], "lon": m["lon"], "horaria": None, "diaria": None, "errores": []}
        try:
            datos = api_datos(cfg, "/prediccion/especifica/municipio/horaria/%s" % m["codigo"])
            elab, horas = horas_municipio(datos)
            reg["horaria"] = {"elaborado": elab, "horas": [dict(h, local=h["local"].strftime("%Y-%m-%dT%H:%M"),
                                                                utc=h["utc"].strftime("%Y-%m-%dT%H:%M")) for h in horas]}
            LOG("%s horaria: %d horas, elaborada %s" % (m["nombre"], len(horas), elab))
        except Exception as e:
            reg["errores"].append("horaria: %s" % e)
            LOG("ERROR horaria %s: %s" % (m["nombre"], e))
            if "clave" in str(e):
                raise
        try:
            datos = api_datos(cfg, "/prediccion/especifica/municipio/diaria/%s" % m["codigo"])
            elab, dias = dias_municipio(datos)
            reg["diaria"] = {"elaborado": elab, "dias": [dict(d, fecha=d["fecha"].strftime("%Y-%m-%d")) for d in dias]}
        except Exception as e:
            reg["errores"].append("diaria: %s" % e)
            LOG("ERROR diaria %s: %s" % (m["nombre"], e))
        out["municipios"][m["nombre"]] = reg
    _estado["fase"] = "avisos"
    try:
        av, n = leer_avisos(cfg)
        out["avisos"] = {"n_ficheros": n, "lista": av}
        LOG("avisos: %d ficheros CAP, %d avisos en la zona" % (n, len(av)))
    except Exception as e:
        out["avisos_error"] = str(e)
        LOG("ERROR avisos: %s" % e)
    _estado["fase"] = "observaciones"
    try:
        obs = leer_observacion(cfg)
        for o in obs:
            o["ultima"] = o["ultima"].strftime("%Y-%m-%dT%H:%M") if o["ultima"] else None
        out["obs"] = obs
        LOG("observaciones: %d estaciones en la zona" % len(obs))
    except Exception as e:
        out["obs_error"] = str(e)
        LOG("ERROR observaciones: %s" % e)
    nuevo = True
    if previo:
        a = {k: (v.get("horaria") or {}).get("elaborado") for k, v in previo.get("municipios", {}).items()}
        b = {k: (v.get("horaria") or {}).get("elaborado") for k, v in out["municipios"].items()}
        nuevo = a != b
    out["cambio_elaboracion"] = nuevo
    with open(os.path.join(DIR_DATOS, "aemet_%s.json" % marca()), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    with open(ruta_ult, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    for viejo in sorted(glob.glob(os.path.join(DIR_DATOS, "aemet_20*.json")))[:-6]:
        try:
            os.remove(viejo)
        except OSError:
            pass
    return out, nuevo


# --------------------------------------------------------------------------------------------------------------------
# Contraste con ECMWF (lee la cache de meteo.py, si existe)
# --------------------------------------------------------------------------------------------------------------------
def cargar_ecmwf():
    fs = sorted(glob.glob(os.path.join(DIR_METEO, "datos", "ciclo_*.json")))
    if not fs:
        return None
    try:
        d = json.load(open(fs[-1], encoding="utf-8"))
        d["_base"] = dt.datetime.strptime(d["ciclo"], "%Y-%m-%dT%H:00:00Z")
        return d
    except Exception:
        return None


def _bilin(malla_lats, malla_lons, valores, lat, lon):
    import bisect
    la, lo = malla_lats, malla_lons
    lat = min(max(lat, la[0]), la[-1])
    lon = min(max(lon, lo[0]), lo[-1])
    i1 = min(max(bisect.bisect_left(la, lat), 1), len(la) - 1)
    j1 = min(max(bisect.bisect_left(lo, lon), 1), len(lo) - 1)
    i0, j0 = i1 - 1, j1 - 1
    ty = (lat - la[i0]) / (la[i1] - la[i0]) if la[i1] != la[i0] else 0.0
    tx = (lon - lo[j0]) / (lo[j1] - lo[j0]) if lo[j1] != lo[j0] else 0.0
    return (valores[i0][j0] * (1 - tx) * (1 - ty) + valores[i0][j1] * tx * (1 - ty) +
            valores[i1][j0] * (1 - tx) * ty + valores[i1][j1] * tx * ty)


def ecmwf_ventana(ec, lat, lon, t0, t1):
    """Lluvia (mm), racha maxima (km/h), Tmax y Tmin (C) de ECMWF en la ventana (t0,t1] UTC. None si no hay solape."""
    base = ec["_base"]
    c = ec["campos"]
    la, lo = ec["lats"], ec["lons"]
    tp = {int(k): _bilin(la, lo, v, lat, lon) for k, v in c.get("tp", {}).items()}
    if not tp:
        return None
    pasos = sorted(tp)
    fin = base + dt.timedelta(hours=pasos[-1])
    a, b = max(t0, base), min(t1, fin)
    if (b - a).total_seconds() < 6 * 3600:
        return None

    def acum(t):
        h = (t - base).total_seconds() / 3600.0
        xs = [0] + pasos
        ys = [0.0] + [max(tp[p], 0.0) for p in pasos]
        for k in range(1, len(xs)):
            if h <= xs[k]:
                f = (h - xs[k - 1]) / (xs[k] - xs[k - 1])
                return ys[k - 1] + f * (ys[k] - ys[k - 1])
        return ys[-1]
    lluvia = acum(b) - acum(a)
    racha = None
    if "10fg" in c:
        rs = []
        for k, v in c["10fg"].items():
            s = int(k)
            ini, finv = base + dt.timedelta(hours=s - 3), base + dt.timedelta(hours=s)
            if finv > a and ini < b:
                rs.append(_bilin(la, lo, v, lat, lon) * 3.6)
        racha = max(rs) if rs else None
    ts = []
    if "2t" in c:
        for k, v in c["2t"].items():
            t = base + dt.timedelta(hours=int(k))
            if a <= t <= b:
                ts.append(_bilin(la, lo, v, lat, lon) - 273.15)
    return {"lluvia": lluvia, "racha": racha, "tmax": max(ts) if ts else None, "tmin": min(ts) if ts else None, "desde": a, "hasta": b}


def aemet_ventana(horas, t0, t1):
    """Misma ventana (t0,t1] sobre la prediccion horaria de AEMET (el valor de la hora h es la lluvia de la hora anterior)."""
    ll, rs, ts = 0.0, [], []
    n = 0
    for h in horas:
        u = h["utc"]
        if t0 < u <= t1:
            n += 1
            ll += h.get("lluvia") or 0.0
            if h.get("racha") is not None:
                rs.append(h["racha"])
            if h.get("temp") is not None:
                ts.append(h["temp"])
    if n < 6:
        return None
    return {"lluvia": ll, "racha": max(rs) if rs else None, "tmax": max(ts) if ts else None, "tmin": min(ts) if ts else None}


# --------------------------------------------------------------------------------------------------------------------
# Informe
# --------------------------------------------------------------------------------------------------------------------
def hs(reg):
    """Horas de un registro (con fechas como texto) -> lista con datetimes."""
    out = []
    for h in (reg.get("horaria") or {}).get("horas", []):
        h = dict(h)
        h["utc"] = dt.datetime.strptime(h["utc"], "%Y-%m-%dT%H:%M")
        h["local"] = dt.datetime.strptime(h["local"], "%Y-%m-%dT%H:%M")
        out.append(h)
    return out


def construir_informe(cfg, datos, nuevo, localidad=None):
    um = cfg["umbrales"]
    L = []
    alertas = []

    def tit(t):
        L.append("")
        L.append("=" * ANCHO)
        L.append(t)
        L.append("=" * ANCHO)

    ahora = ahora_utc()
    L.append("=" * ANCHO)
    L.append("INFORME AEMET OPENDATA - PREVISION, AVISOS Y OBSERVACION - SUR DE AVILA")
    L.append("=" * ANCHO)
    L.append("Generado:           %s (hora de Madrid)" % fmt_local(ahora))
    L.append("Descargado de AEMET: %s (hora del movil)" % datos.get("descargado"))
    elabs = [(v.get("horaria") or {}).get("elaborado") for v in datos["municipios"].values() if (v.get("horaria") or {}).get("elaborado")]
    if elabs:
        e0 = parsea_iso(max(elabs))
        L.append("Prevision elaborada por AEMET: %s" % (fmt_local(de_madrid_a_utc(e0)) + " (hora de Madrid)" if e0 else max(elabs)))
    L.append("Respecto a la descarga anterior: %s" % ("prevision nueva de AEMET" if nuevo else "AEMET no ha publicado una prevision nueva"))
    L.append("Municipios consultados: %s" % ", ".join(datos["municipios"].keys()))

    # ---- avisos
    tit("1. AVISOS OFICIALES VIGENTES (Meteoalerta) EN LA ZONA")
    if datos.get("avisos_error"):
        L.append(ajusta("No se pudieron leer los avisos: %s. Consulta los avisos en aemet.es." % datos["avisos_error"]))
    else:
        lista = datos["avisos"]["lista"]
        activos = [a for a in lista if a["nivel"] in NIVEL and NIVEL[a["nivel"]] >= 1]
        if not lista:
            L.append(ajusta("No hay avisos vigentes que mencionen Avila, Gredos o el Tietar (se revisaron %d ficheros CAP de AEMET)."
                            % datos["avisos"]["n_ficheros"]))
        for a in sorted(lista, key=lambda x: -NIVEL.get(x["nivel"], 0)):
            ini, fin = parsea_iso(a["inicio"]), parsea_iso(a["fin"])
            cuando = "%s -> %s" % (fmt_local(ini) if ini else "?", fmt_local(fin) if fin else "?")
            L.append(ajusta("[%s] %s - %s. Zona: %s. Periodo (hora de Madrid): %s." % (
                (a["nivel"] or "?").upper(), a["evento"] or a["fenomeno"], a["probabilidad"] and ("probabilidad " + a["probabilidad"]) or "",
                a["zona"], cuando), "  ", "      "))
            if a["descripcion"]:
                L.append(ajusta(a["descripcion"], "      "))
            if NIVEL.get(a["nivel"], 0) >= 1:
                alertas.append("AVISO %s: %s (%s)" % (a["nivel"].upper(), a["evento"] or a["fenomeno"], a["zona"]))
        if lista and not activos:
            L.append("  (todos en nivel verde)")

    # ---- resumen por localidades (48 h horarias)
    tit("2. PREVISION HORARIA PROXIMAS HORAS POR LOCALIDAD (hasta donde llegue la prevision horaria de AEMET)")
    filas = []
    base_det = None
    nombre_det = localidad or cfg.get("localidad_detalle")
    for nombre, reg in datos["municipios"].items():
        horas = [h for h in hs(reg) if h["utc"] > ahora]
        if not horas:
            filas.append([nombre, "-", "-", "-", "-", "-", "-"] if reg.get("errores") else [nombre, "sin datos", "", "", "", "", ""])
            continue
        ll = sum(h.get("lluvia") or 0.0 for h in horas)
        ll24 = sum(h.get("lluvia") or 0.0 for h in horas if h["utc"] <= ahora + dt.timedelta(hours=24))
        rachas = [(h.get("racha"), h) for h in horas if h.get("racha") is not None]
        rmax, hr = max(rachas, key=lambda x: x[0]) if rachas else (None, None)
        vmax = max([h.get("viento") or 0.0 for h in horas])
        temps = [h["temp"] for h in horas if h.get("temp") is not None]
        pmax = max([h.get("prob") for h in horas if h.get("prob") is not None], default=None)
        filas.append([nombre, f1(ll24), f1(ll), "-" if pmax is None else f0(pmax) + "%", f0(vmax),
                      ("%s (%s)" % (f0(rmax), fmt_local(hr["utc"]))) if rmax is not None else "-",
                      "%s / %s" % (f0(min(temps)), f0(max(temps))) if temps else "-"])
        if rmax is not None and rmax >= um["racha_kmh"]:
            alertas.append("%s: racha prevista de %s km/h (%s)" % (nombre, f0(rmax), fmt_local(hr["utc"])))
        tres = {}
        for h in horas:
            k = (h["utc"] - ahora).total_seconds() // (3 * 3600)
            tres[k] = tres.get(k, 0.0) + (h.get("lluvia") or 0.0)
        if tres and max(tres.values()) >= um["lluvia_3h_mm"]:
            alertas.append("%s: %s mm en 3 h previstos" % (nombre, f0(max(tres.values()))))
        if temps and min(temps) <= um["helada_c"]:
            alertas.append("%s: posible helada (%s C)" % (nombre, f0(min(temps))))
    L += tabla(["Localidad", "Lluvia 24h mm", "Lluvia total mm", "Prob. max", "Viento max km/h", "Racha max km/h (cuando)", "T min / max C"],
               filas, alin="idddddd")
    L.append("Lluvia = suma de la precipitacion horaria prevista desde ahora. Viento y rachas en km/h a 10 m.")

    # ---- 7 dias
    tit("3. PREVISION A 7 DIAS (AEMET, por dia; hora local)")
    ref = None
    for nombre in datos["municipios"]:
        if nombre_det and sin_acentos(nombre_det) == sin_acentos(nombre):
            ref = nombre
    ref = ref or next(iter(datos["municipios"]))
    reg = datos["municipios"][ref]
    L.append("Municipio de referencia: %s" % ref)
    d = (reg.get("diaria") or {}).get("dias", [])
    if d:
        filas = [["%s %s/%s" % (DIAS[dt.datetime.strptime(x["fecha"], "%Y-%m-%d").weekday()], x["fecha"][8:10], x["fecha"][5:7]),
                  "-" if x["tmin"] is None else f0(x["tmin"]), "-" if x["tmax"] is None else f0(x["tmax"]),
                  "-" if x["prob"] is None else f0(x["prob"]) + "%", "-" if x["viento"] is None else f0(x["viento"]),
                  x["dir"] or "-", "-" if x["racha"] is None else f0(x["racha"]), x["cielo"][:34]] for x in d]
        L += tabla(["Dia", "Tmin", "Tmax", "Prob. lluvia", "Viento km/h", "Dir.", "Racha km/h", "Cielo"], filas, alin="idddddd" + "i")
        for x in d:
            if x["racha"] is not None and x["racha"] >= um["racha_fuerte_kmh"]:
                alertas.append("%s: racha de %s km/h prevista el %s/%s" % (ref, f0(x["racha"]), x["fecha"][8:10], x["fecha"][5:7]))
    else:
        L.append("  Sin prevision diaria (%s)." % "; ".join(reg.get("errores") or ["no disponible"]))

    # ---- detalle horario
    tit("4. DETALLE HORARIO CADA 3 HORAS - %s (hora de Madrid; lluvia = suma de las 3 horas que terminan a esa hora)" % ref.upper())
    horas = hs(reg)
    filas = []
    for k in range(2, len(horas), 3):
        h = horas[k]
        if h["utc"] <= ahora:
            continue
        grupo = horas[k - 2:k + 1]
        filas.append([fmt_local(h["utc"]), f1(sum(g.get("lluvia") or 0.0 for g in grupo)),
                      "-" if h.get("prob") is None else f0(h["prob"]) + "%", "-" if h.get("viento") is None else f0(h["viento"]),
                      h.get("dir") or "-", "-" if h.get("racha") is None else f0(max([g.get("racha") or 0 for g in grupo])),
                      "-" if h.get("temp") is None else f0(h["temp"]), (h.get("cielo") or "")[:28]])
    if filas:
        L += tabla(["Hora", "Lluvia 3h mm", "Prob.", "Viento km/h", "Dir.", "Racha km/h", "T C", "Cielo"], filas, alin="iddddd" + "di")
    else:
        L.append("  Sin prevision horaria futura.")

    # ---- observaciones
    tit("5. OBSERVACION REAL EN LA ZONA (estaciones de AEMET dentro del recuadro; ultimas horas disponibles)")
    if datos.get("obs_error"):
        L.append(ajusta("No se pudieron leer las observaciones: %s." % datos["obs_error"]))
    elif not datos.get("obs"):
        L.append("  Ninguna estacion convencional de AEMET dentro del recuadro de la zona.")
    else:
        filas = []
        for o in datos["obs"]:
            u = parsea_iso(o["ultima"]) if o.get("ultima") else None
            filas.append([o["nombre"][:28], "-" if o["alt"] is None else f0(o["alt"]), "%d" % o["n"],
                          "-" if o["prec24"] is None else f1(o["prec24"]), "-" if o["tmax"] is None else f1(o["tmax"]),
                          "-" if o["tmin"] is None else f1(o["tmin"]), "-" if o["racha"] is None else f0(o["racha"]),
                          fmt_local(u) if u else "-", "-" if o["ta"] is None else f1(o["ta"])])
        L += tabla(["Estacion", "Alt. m", "Horas", "Lluvia mm", "Tmax C", "Tmin C", "Racha max km/h", "Ultima obs", "T ult. C"],
                   filas, alin="idddddddd")
        L.append("Lluvia, Tmax, Tmin y racha son sobre las horas devueltas por AEMET (columna Horas).")

    # ---- contraste con ECMWF
    tit("6. CONTRASTE CON ECMWF (ultimo ciclo de meteo.py, mismo periodo)")
    ec = cargar_ecmwf()
    if ec is None:
        L.append(ajusta("No hay datos de ECMWF en %s. Ejecuta antes python meteo.py para poder comparar." % DIR_METEO))
    else:
        L.append("Ciclo ECMWF: %s UTC.  Ventana comparada por localidad: desde ahora hasta donde coincidan ambas previsiones." %
                 ec["_base"].strftime("%Y-%m-%d %H:%M"))
        filas = []
        veredictos = []
        for nombre, reg in datos["municipios"].items():
            horas = hs(reg)
            if reg.get("lat") is None or not horas:
                continue
            t0 = max(ahora, ec["_base"])
            t1 = max(h["utc"] for h in horas)
            e = ecmwf_ventana(ec, reg["lat"], reg["lon"], t0, t1)
            if not e:
                filas.append([nombre, "sin solape suficiente", "", "", "", "", "", ""])
                continue
            a = aemet_ventana(horas, e["desde"], e["hasta"])
            if not a:
                continue
            mx = max(a["lluvia"], e["lluvia"])
            if mx < 1.0:
                vl = "ambos secos"
            elif abs(a["lluvia"] - e["lluvia"]) <= max(2.0, 0.3 * mx):
                vl = "coincide"
            else:
                vl = "DISCREPA"
            if a["racha"] is not None and e["racha"] is not None:
                vr = "coincide" if abs(a["racha"] - e["racha"]) <= 15 else "DISCREPA"
            else:
                vr = "-"
            veredictos.append((vl, vr))
            filas.append([nombre, f1(a["lluvia"]), f1(e["lluvia"]),
                          "-" if a["racha"] is None else f0(a["racha"]), "-" if e["racha"] is None else f0(e["racha"]),
                          ("%s / %s" % (f0(a["tmax"]), f0(e["tmax"]))) if a["tmax"] is not None and e["tmax"] is not None else "-",
                          vl, vr])
        if filas:
            L += tabla(["Localidad", "Lluvia AEMET mm", "Lluvia ECMWF mm", "Racha AEMET", "Racha ECMWF", "Tmax AEMET/ECMWF", "Lluvia", "Racha"],
                       filas, alin="idddddii")
            disc = sum(1 for v in veredictos if "DISCREPA" in v)
            if disc:
                L.append(ajusta("En %d de %d localidades hay discrepancia relevante entre los dos modelos: es una senal de incertidumbre; "
                                "prima la prevision y los avisos de AEMET, y vuelve a consultar en las proximas horas." % (disc, len(veredictos))))
            else:
                L.append("Los dos sistemas coinciden en lo esencial en las localidades comparadas.")
            L.append(ajusta("ECMWF es una malla de 28 km sin ajuste local y sus rachas/temperaturas son por intervalos de 3 h; AEMET es "
                            "prevision por municipio revisada por predictores. Diferencias pequenas son normales."))
        else:
            L.append("  No se pudo calcular el contraste (sin solape o sin datos).")

    tit("NOTAS Y LIMITES")
    for n in [
        "Los avisos de Meteoalerta de AEMET son la referencia oficial. Este informe los resume, pero consulta siempre aemet.es o la "
        "app oficial; si el apartado de avisos falla, el informe lo dice explicitamente (no equivale a 'sin avisos').",
        "La prevision horaria de AEMET cubre unas 48 horas y la diaria 7 dias. La probabilidad de lluvia es la de AEMET, no un calculo propio.",
        "Las observaciones son de estaciones concretas (pocas en la sierra) y pueden tener huecos; no representan todo el municipio.",
        "Las alertas del apartado de resumen usan umbrales propios editables en config.json; no son umbrales de Meteoalerta.",
        "Fuente: AEMET (Agencia Estatal de Meteorologia), AEMET OpenData. Informacion elaborada por AEMET. ECMWF: CC BY 4.0.",
    ]:
        L.append(ajusta(n))
        L.append("")
    L.pop()
    plano = "\n".join(L).split("\n")
    texto = "\n".join(l if len(l) <= ANCHO else l[:ANCHO] for l in plano) + "\n"
    uniq = []
    for a in alertas:
        if a not in uniq:
            uniq.append(a)
    resumen = {"generado": marca(), "nuevo": nuevo, "alertas": uniq[:30], "avisos_error": datos.get("avisos_error"),
               "obs_error": datos.get("obs_error")}
    return texto, resumen


def generar(cfg, solo_local=False, localidad=None):
    datos, nuevo = actualizar(cfg, solo_local=solo_local)
    _estado["fase"] = "analizando"
    texto, resumen = construir_informe(cfg, datos, nuevo, localidad)
    ruta = os.path.join(DIR_INFORMES, "%s_aemet_informe.txt" % marca())
    with open(ruta, "w", encoding="utf-8") as f:
        f.write(texto)
    LOG("informe guardado en %s" % ruta)
    for viejo in sorted(glob.glob(os.path.join(DIR_INFORMES, "*_aemet_informe.txt")))[:-20]:
        try:
            os.remove(viejo)
        except OSError:
            pass
    with open(os.path.join(DIR_INFORMES, "ultimo_resumen.json"), "w", encoding="utf-8") as f:
        json.dump(resumen, f, indent=1, ensure_ascii=False)
    _estado.update({"ultimo_informe": os.path.basename(ruta), "informe_id": os.path.basename(ruta), "alertas": resumen["alertas"],
                    "ultima_elaboracion": datos.get("descargado")})
    return texto, resumen, nuevo


def notificar(cfg, resumen, nuevo):
    if not (cfg.get("notificar_termux") and nuevo and resumen["alertas"] and shutil.which("termux-notification")):
        return
    try:
        subprocess.run(["termux-notification", "--title", "AEMET sur de Avila", "--content", "; ".join(resumen["alertas"][:3]),
                        "--id", "aemet"], timeout=15)
    except Exception:
        pass


def trabajo(cfg, localidad=None):
    if not _cerrojo.acquire(blocking=False):
        return None
    try:
        _estado.update({"trabajando": True, "error": None, "inicio": marca(), "fase": "iniciando"})
        try:
            texto, resumen, nuevo = generar(cfg, localidad=localidad)
            notificar(cfg, resumen, nuevo)
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
    fs = sorted(glob.glob(os.path.join(DIR_INFORMES, "*_aemet_informe.txt")))
    return open(fs[-1], encoding="utf-8").read() if fs else None


# --------------------------------------------------------------------------------------------------------------------
# Servidor
# --------------------------------------------------------------------------------------------------------------------
PANEL = """<!DOCTYPE html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AEMET Avila</title><style>body{font-family:sans-serif;margin:8px}pre{font-size:9px;line-height:1.15;overflow-x:auto}
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
    e["clave"] = "configurada" if cfg.get("api_key") else "FALTA"
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
                self._enviar(200, json.dumps({"ok": True, "programa": "aemet", "version": VERSION}))
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
                fs = [os.path.join(DIR_LOGS, "aemet_en_curso.log")] + sorted(glob.glob(os.path.join(DIR_LOGS, "*_aemet*.log")))[::-1]
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
                threading.Thread(target=trabajo, args=(cfg,), daemon=True).start()
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
        r = json.load(open(os.path.join(DIR_INFORMES, "ultimo_resumen.json"), encoding="utf-8"))
        _estado["alertas"] = r.get("alertas", [])
        fs = sorted(glob.glob(os.path.join(DIR_INFORMES, "*_aemet_informe.txt")))
        _estado["ultimo_informe"] = _estado["informe_id"] = os.path.basename(fs[-1]) if fs else None
    except Exception:
        pass
    srv = ThreadingHTTPServer(("127.0.0.1", puerto), crear_manejador(cfg))
    LOG("servidor AEMET en http://127.0.0.1:%d  (panel: /panel)  auto cada %s min" % (puerto, cfg.get("auto_minutos")))
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
    ruta = os.path.join(BASE, "%s_aemet_diagnostico.txt" % marca())
    L = ["DIAGNOSTICO aemet.py %s  -  %s" % (VERSION, marca()), "", "Python: %s" % sys.version.split()[0], "Base: %s" % BASE,
         "api_key: %s" % ("configurada (%d caracteres)" % len(cfg["api_key"]) if cfg["api_key"] else "FALTA"),
         "Datos de ECMWF (meteo.py) encontrados: %s" % bool(cargar_ecmwf()), ""]
    if cfg["api_key"]:
        LOG.clave = cfg["api_key"]

        def muestra(titulo, ruta_api, binario=False):
            L.append("--- %s  (%s)" % (titulo, ruta_api))
            try:
                d = api_datos(cfg, ruta_api, binario=binario)
                if binario:
                    L.append("bytes recibidos: %d; empieza por %r" % (len(d), d[:20]))
                    try:
                        tf = tarfile.open(fileobj=io.BytesIO(d), mode="r:*")
                        nombres = tf.getnames()
                        L.append("tar con %d ficheros; primeros: %s" % (len(nombres), nombres[:5]))
                        L.append(tf.extractfile(tf.getmembers()[0]).read(1500).decode("utf-8", "replace"))
                    except Exception as e:
                        L.append("no es tar: %s" % e)
                else:
                    L.append(json.dumps(d, ensure_ascii=False)[:1800])
            except Exception as e:
                L.append("FALLO: %s" % e)
            L.append("")
        muestra("maestro municipios", "/maestro/municipios")
        try:
            muni = resolver_municipios(cfg)
            L.append("Municipios resueltos: %s" % ", ".join("%s=%s" % (m["nombre"], m["codigo"]) for m in muni))
            if muni:
                muestra("prediccion horaria", "/prediccion/especifica/municipio/horaria/%s" % muni[0]["codigo"])
                muestra("prediccion diaria", "/prediccion/especifica/municipio/diaria/%s" % muni[0]["codigo"])
        except Exception as e:
            L.append("resolver municipios FALLO: %s" % e)
        muestra("avisos", "/avisos_cap/ultimoelaborado/area/%s" % cfg["area_avisos"], binario=True)
        muestra("observacion convencional (todas)", "/observacion/convencional/todas")
    L.append("")
    L.append("Configuracion (sin la clave):")
    c2 = dict(cfg)
    c2["api_key"] = "<oculta>"
    L.append(json.dumps(c2, indent=1, ensure_ascii=False))
    texto = "\n".join(L) + "\n"
    if cfg["api_key"]:
        texto = texto.replace(cfg["api_key"], "<CLAVE>")
    with open(ruta, "w", encoding="utf-8") as f:
        f.write(texto)
    print(texto[:3000])
    print("\nDiagnostico guardado en: %s" % ruta)


def main():
    ap = argparse.ArgumentParser(description="Informe AEMET OpenData del sur de Avila")
    ap.add_argument("orden", nargs="?", default="informe", choices=["informe", "estado", "servidor", "abrir", "diag", "limpiar"])
    ap.add_argument("--sin-red", action="store_true", help="usar solo los ultimos datos guardados")
    ap.add_argument("--localidad")
    ap.add_argument("--puerto", type=int)
    ap.add_argument("--version", action="version", version=VERSION)
    a = ap.parse_args()
    cfg = cargar_cfg()
    puerto = a.puerto or int(cfg["puerto"])
    if cfg["api_key"]:
        LOG.clave = cfg["api_key"]
    if a.orden == "diag":
        diagnostico(cfg)
    elif a.orden == "limpiar":
        n = 0
        for f in sorted(glob.glob(os.path.join(DIR_DATOS, "aemet_20*.json")))[:-1]:
            os.remove(f)
            n += 1
        print("Borrados %d ficheros antiguos" % n)
    elif a.orden == "estado":
        if not cfg["api_key"]:
            print("Falta la api_key en %s" % FICH_CFG)
            sys.exit(1)
        try:
            muni = resolver_municipios(cfg)
            m = muni[0]
            datos = api_datos(cfg, "/prediccion/especifica/municipio/horaria/%s" % m["codigo"])
            elab, _ = horas_municipio(datos)
            print("Clave valida. Prevision horaria de %s elaborada por AEMET: %s" % (m["nombre"], elab))
            ult = os.path.join(DIR_DATOS, "ultimo.json")
            if os.path.exists(ult):
                d = json.load(open(ult, encoding="utf-8"))
                guard = (d["municipios"].get(m["nombre"], {}).get("horaria") or {}).get("elaborado")
                print("Guardada en el movil: %s -> %s" % (guard, "al dia" if guard == elab else "HAY PREVISION NUEVA"))
        except Exception as e:
            print("Error: %s" % e)
            sys.exit(1)
    elif a.orden == "servidor":
        servidor(cfg, puerto)
    elif a.orden == "abrir":
        url = "http://127.0.0.1:%d/panel" % puerto
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/ping" % puerto, timeout=3)
        except Exception:
            print("El servidor no esta en marcha. Arranca en otra sesion: python aemet.py servidor")
        if shutil.which("termux-open-url"):
            subprocess.run(["termux-open-url", url])
        print("Panel: %s" % url)
    else:
        if a.sin_red:
            texto, resumen, _ = generar(cfg, solo_local=True, localidad=a.localidad)
            LOG.cerrar()
        else:
            texto = trabajo(cfg, localidad=a.localidad)
        if texto:
            print(texto)
        else:
            print("No se pudo generar el informe: %s" % _estado.get("error"))
            sys.exit(1)


if __name__ == "__main__":
    main()
