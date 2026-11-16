#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
subv.py - Buscador local de la Base de Datos Nacional de Subvenciones (BDNS / infosubvenciones.es).

Usa la API abierta de la BDNS (https://www.infosubvenciones.es/bdnstrans/api, sin clave).
  - Por defecto (--via auto) prueba la conexion directa desde Termux; si la web la rechaza,
    pasa solo a la extension de Firefox "SUBV BDNS Termux" (siempre en segundo plano).
  - Todo lo que baja se guarda en una base SQLite local (~/subv/subv.db) y se muestra por pantalla.
  - Cada ejecucion deja un paquete de diagnostico en ~/subv/diag para poder enviarlo.

Ejemplos:
  python subv.py buscar -c "digitalizacion" --3meses --ambito estatal --abiertas
  python subv.py buscar --region "madrid" --finalidad "empleo" --ultimos 30 --detalle
  python subv.py ultimas --max 50
  python subv.py detalle 939605
  python subv.py concesiones --beneficiario P0500500A --anio 2025
  python subv.py consultar convocatorias -c vivienda --abiertas       (solo base local, sin red)
  python subv.py catalogo organos -b "ministerio de ciencia"
  python subv.py api                                                  (endpoints y parametros reales)

Ver subvManualBono.txt para todos los detalles.
"""

import argparse
import base64
import csv
import datetime as dt
import hashlib
import io
import json
import os
import platform
import queue
import re
import sqlite3
import sys
import textwrap
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.0.1"
BASE_API = os.environ.get("SUBV_BASE", "https://www.infosubvenciones.es/bdnstrans/api")
BASE_API_ESPEJO = "https://www.pap.hacienda.gob.es/bdnstrans/api"
FICHA = "https://www.infosubvenciones.es/bdnstrans/GE/es/convocatorias/%s"
SWAGGER_URL = os.environ.get("SUBV_SWAGGER", "https://www.infosubvenciones.es/bdnstrans/estaticos/doc/snpsap-api.json")
VPD = "GE"
PUERTO_DEF = 8769
DIR = os.environ.get("SUBV_DIR", os.path.join(os.path.expanduser("~"), "subv"))
DB_PATH = os.path.join(DIR, "subv.db")
DIAG_DIR = os.path.join(DIR, "diag")
DOCS_DIR = os.path.join(DIR, "docs")
SWAGGER_PATH = os.path.join(DIR, "swagger.json")
CONFIG = os.path.join(DIR, "config.json")
PAUSA_DIRECTA = 0.12      # el limite oficial es de 10 peticiones GET por segundo y por IP
UA = "Mozilla/5.0 (Android 14; Mobile; rv:142.0) Gecko/142.0 Firefox/142.0"

# Catalogos de la API (ruta -> nombre). Para cada uno: campo con el codigo que usan los filtros.
CATALOGOS = {
    "organos": "organos", "regiones": "regiones", "finalidades": "finalidades", "instrumentos": "instrumentos",
    "actividades": "actividades", "sectores": "sectores", "beneficiarios": "beneficiarios",
    "reglamentos": "reglamentos", "objetivos": "objetivos",
}
# Busquedas "otras" (ruta de la API)
OTROS = {
    "ayudasestado": "ayudasestado/busqueda", "minimis": "minimis/busqueda", "sanciones": "sanciones/busqueda",
    "partidos": "partidospoliticos/busqueda", "grandes": "grandesbeneficiarios/busqueda",
    "planes": "planesestrategicos/busqueda",
}
AMBITOS = {"estatal": "C", "autonomica": "A", "local": "L", "otros": "O"}   # tipoAdministracion

LOG = []            # peticiones de esta ejecucion (para el diagnostico)
MUESTRAS = {}       # primera respuesta cruda de cada ruta (para el diagnostico)
AVISOS = []         # avisos de esta ejecucion
EVENTOS = []        # eventos de texto (decisiones, errores)


def evento(txt):
    EVENTOS.append("%s %s" % (dt.datetime.now().strftime("%H:%M:%S"), txt))


def aviso(txt):
    AVISOS.append(txt)
    evento("AVISO " + txt)
    print("Aviso: " + txt, file=sys.stderr)


# ----------------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------------
def norm(s):
    s = unicodedata.normalize("NFD", str(s or "").lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def ancho_terminal():
    try:
        return max(60, min(os.get_terminal_size().columns, 160))
    except OSError:
        return 100


def parse_fecha(s):
    if s is None:
        return None
    if isinstance(s, (int, float)):
        try:
            return dt.datetime.utcfromtimestamp(s / 1000.0).date() if s > 1e11 else dt.datetime.utcfromtimestamp(s).date()
        except Exception:
            return None
    s = str(s).strip()
    if not s:
        return None
    if s.lower() in ("hoy", "today"):
        return dt.date.today()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = re.match(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", s)
    if m:
        try:
            return dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    return None


def iso(s):
    d = parse_fecha(s)
    return d.isoformat() if d else ""


def f_es(d):
    d = parse_fecha(d)
    return d.strftime("%d/%m/%Y") if d else ""


def fecha_arg(s):
    d = parse_fecha(s)
    if not d:
        raise argparse.ArgumentTypeError("fecha no valida: %r (usa dd-mm-aaaa, aaaa-mm-dd o 'hoy')" % s)
    return d


def restar_meses(f, n):
    m = f.year * 12 + (f.month - 1) - n
    y, mes = divmod(m, 12)
    mes += 1
    dia = f.day
    while True:
        try:
            return dt.date(y, mes, dia)
        except ValueError:
            dia -= 1


def rango_fechas(a):
    """desde/hasta a partir de --desde/--hasta/-f/--anio/--ultimos/--3meses/--6meses/--12meses."""
    hoy = dt.date.today()
    desde, hasta = getattr(a, "desde", None), getattr(a, "hasta", None)
    if getattr(a, "fecha", None):
        desde = hasta = a.fecha
    if getattr(a, "anio", None):
        desde = desde or dt.date(a.anio, 1, 1)
        hasta = hasta or dt.date(a.anio, 12, 31)
    if getattr(a, "ultimos", None):
        hasta = hasta or hoy
        desde = desde or (hasta - dt.timedelta(days=a.ultimos))
    for attr, n in (("tres_meses", 3), ("seis_meses", 6), ("doce_meses", 12)):
        if getattr(a, attr, False):
            hasta = hasta or hoy
            desde = desde or restar_meses(hasta, n)
    if desde and hasta and desde > hasta:
        raise ValueError("la fecha inicial es posterior a la final")
    return desde, hasta


def num(x):
    """Numero a partir de texto/numero (1.234,56 o 1234.56). None si no hay."""
    if x is None or x == "":
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip().replace("€", "").replace(" ", "")
    if re.match(r"^-?\d{1,3}(\.\d{3})+(,\d+)?$", s):
        s = s.replace(".", "").replace(",", ".")
    elif "," in s and "." not in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def eur(x):
    n = num(x)
    if n is None:
        return ""
    return ("{:,.2f}".format(n)).replace(",", "X").replace(".", ",").replace("X", ".") + " EUR"


def pick(d, *claves):
    """Primer valor no vacio de varias claves posibles (admite 'a.b' para anidados)."""
    for c in claves:
        cur = d
        ok = True
        for p in c.split("."):
            if isinstance(cur, dict) and p in cur:
                cur = cur[p]
            else:
                ok = False
                break
        if ok and cur not in (None, "", [], {}):
            return cur
    return None


def textos_lista(v, *claves):
    """Une una lista de objetos/valores en un texto 'a; b; c'."""
    if v in (None, "", [], {}):
        return ""
    if not isinstance(v, list):
        v = [v]
    out = []
    for e in v:
        if isinstance(e, dict):
            t = pick(e, *(claves or ("descripcion", "nombre", "texto", "codigo", "id")))
            out.append(str(t) if t is not None else "")
        else:
            out.append(str(e))
    return "; ".join(x for x in out if x)


def leer_config():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


VIA_DEF = os.environ.get("SUBV_VIA") or leer_config().get("via") or "auto"
if VIA_DEF not in ("auto", "directo", "extension"):
    VIA_DEF = "auto"
PUERTO_CFG = int(os.environ.get("SUBV_PUERTO") or leer_config().get("puerto") or PUERTO_DEF)


def guardar_config(c):
    os.makedirs(DIR, exist_ok=True)
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(c, f, indent=2, ensure_ascii=False)


# ----------------------------------------------------------------------------
# Base de datos local
# ----------------------------------------------------------------------------
def abrir_db():
    os.makedirs(DIR, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript("""
    CREATE TABLE IF NOT EXISTS convocatorias(
        num TEXT PRIMARY KEY, id TEXT, descripcion TEXT, desc_norm TEXT, fecha TEXT,
        organo1 TEXT, organo2 TEXT, organo3 TEXT, mrr TEXT,
        abierta INTEGER, inicio TEXT, fin TEXT, presupuesto REAL, tipo TEXT, finalidad TEXT,
        instrumentos TEXT, beneficiarios TEXT, sectores TEXT, regiones TEXT, url_bases TEXT, sede TEXT,
        raw_busqueda TEXT, raw_detalle TEXT, actualizado TEXT);
    CREATE INDEX IF NOT EXISTS ix_conv_fecha ON convocatorias(fecha);
    CREATE TABLE IF NOT EXISTS concesiones(
        cid TEXT PRIMARY KEY, num_conv TEXT, convocatoria TEXT, fecha TEXT, beneficiario TEXT, nif TEXT,
        importe REAL, instrumento TEXT, organo1 TEXT, organo2 TEXT, organo3 TEXT, benef_norm TEXT,
        raw TEXT, actualizado TEXT);
    CREATE INDEX IF NOT EXISTS ix_conc_fecha ON concesiones(fecha);
    CREATE INDEX IF NOT EXISTS ix_conc_nif ON concesiones(nif);
    CREATE TABLE IF NOT EXISTS otros(
        tipo TEXT, clave TEXT, fecha TEXT, titulo TEXT, titulo_norm TEXT, importe REAL, beneficiario TEXT,
        organo TEXT, raw TEXT, actualizado TEXT, PRIMARY KEY(tipo, clave));
    CREATE TABLE IF NOT EXISTS catalogos(
        tipo TEXT, grupo TEXT, codigo TEXT, descripcion TEXT, desc_norm TEXT, ruta TEXT, ancestros TEXT, ids TEXT, raw TEXT,
        PRIMARY KEY(tipo, grupo, codigo));
    CREATE TABLE IF NOT EXISTS busquedas(
        id INTEGER PRIMARY KEY AUTOINCREMENT, cuando TEXT, comando TEXT, ruta TEXT, params TEXT,
        n INTEGER, total INTEGER, completa INTEGER, via TEXT);
    CREATE TABLE IF NOT EXISTS textos(
        num TEXT, nombre TEXT, texto_norm TEXT, caracteres INTEGER, PRIMARY KEY(num, nombre));
    CREATE TABLE IF NOT EXISTS docs(
        num TEXT, nombre TEXT, ruta TEXT, bytes INTEGER, cuando TEXT, PRIMARY KEY(num, nombre));
    """)
    return con


def ahora_txt():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _j(o):
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"))


def org_niveles(d):
    o = pick(d, "organo") if isinstance(pick(d, "organo"), dict) else d
    return (str(pick(o, "nivel1") or ""), str(pick(o, "nivel2") or ""), str(pick(o, "nivel3") or ""))


def fecha_en_texto(t):
    """Si el texto libre del plazo ('16/11/2026', 'hasta el 15-12-2026') contiene una fecha, la devuelve en ISO."""
    m = re.search(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})\b", str(t or ""))
    return iso("%s/%s/%s" % m.groups()) if m else ""


def conv_de_busqueda(it):
    """Campos de una convocatoria a partir de un elemento de convocatorias/busqueda o /ultimas."""
    n1, n2, n3 = org_niveles(it)
    numero = pick(it, "numeroConvocatoria", "codigoBDNS", "numConv", "codigo", "id")
    return {
        "num": str(numero) if numero is not None else "",
        "id": str(pick(it, "id", "idConvocatoria") or ""),
        "descripcion": str(pick(it, "descripcion", "titulo", "descripcionLeng") or ""),
        "fecha": iso(pick(it, "fechaRecepcion", "fechaRegistro", "fecha")),
        "organo1": n1, "organo2": n2, "organo3": n3, "mrr": str(pick(it, "mrr") or ""),
    }


def conv_de_detalle(d):
    n1, n2, n3 = org_niveles(d)
    ab = pick(d, "abierto")
    return {
        "id": str(pick(d, "id", "idConvocatoria") or ""),
        "descripcion": str(pick(d, "descripcion", "titulo") or ""),
        "fecha": iso(pick(d, "fechaRecepcion", "fechaRegistro")),
        "organo1": n1, "organo2": n2, "organo3": n3,
        "abierta": (1 if ab in (True, "true", "S", 1) else 0) if ab is not None else None,
        "inicio": iso(pick(d, "fechaInicioSolicitud")) or fecha_en_texto(pick(d, "textInicio")),
        "fin": iso(pick(d, "fechaFinSolicitud")) or fecha_en_texto(pick(d, "textFin")),
        "presupuesto": num(pick(d, "presupuestoTotal", "presupuesto")),
        "tipo": str(pick(d, "tipoConvocatoria") or ""),
        "finalidad": str(pick(d, "descripcionFinalidad", "finalidad.descripcion", "finalidad") or ""),
        "instrumentos": textos_lista(pick(d, "instrumentos")),
        "beneficiarios": textos_lista(pick(d, "tiposBeneficiarios", "tipoBeneficiarios", "tiposBeneficiario")),
        "sectores": textos_lista(pick(d, "sectores")),
        "regiones": textos_lista(pick(d, "regiones")),
        "url_bases": str(pick(d, "urlBasesReguladoras") or ""),
        "sede": str(pick(d, "sedeElectronica") or ""),
        "mrr": str(pick(d, "mrr") or ""),
    }


def guardar_convocatorias(con, items, detalle=False):
    n = 0
    for it in items:
        c = conv_de_detalle(it) if detalle else conv_de_busqueda(it)
        numero = str(pick(it, "numeroConvocatoria", "codigoBDNS", "numConv", "codigo") or c.get("num") or "")
        if detalle and not numero:
            continue
        if not numero:
            continue
        c["num"] = numero
        fila = con.execute("SELECT num FROM convocatorias WHERE num=?", (numero,)).fetchone()
        if fila is None:
            con.execute("INSERT INTO convocatorias(num, actualizado) VALUES(?,?)", (numero, ahora_txt()))
        sets, vals = [], []
        for k, v in c.items():
            if k == "num" or v in (None, ""):
                continue
            sets.append("%s=?" % k)
            vals.append(v)
        if "descripcion" in c and c["descripcion"]:
            sets.append("desc_norm=?")
            vals.append(norm(c["descripcion"]))
        sets.append("raw_detalle=?" if detalle else "raw_busqueda=?")
        vals.append(_j(it))
        sets.append("actualizado=?")
        vals.append(ahora_txt())
        con.execute("UPDATE convocatorias SET %s WHERE num=?" % ", ".join(sets), vals + [numero])
        n += 1
    con.commit()
    return n


def conc_de_item(it):
    n1, n2, n3 = org_niveles(it)
    if not n1:
        n1 = str(pick(it, "convocante", "nivel1") or "")
    ben = str(pick(it, "beneficiario", "nombreBeneficiario", "razonSocial") or "")
    nif = str(pick(it, "nifCif", "numeroIdentificacion", "nif", "idPersona") or "")
    if not nif:
        m = re.match(r"^\s*([A-Z]?\d{6,8}[A-Z]?|[A-Z]\d{7}[A-Z0-9])\b", ben)
        nif = m.group(1) if m else ""
    cid = str(pick(it, "id", "codConcesion", "idConcesion", "codigoConcesion") or "")
    num_conv = str(pick(it, "numeroConvocatoria", "idConvocatoria", "codigoBDNS", "numConv") or "")
    fecha = iso(pick(it, "fechaConcesion", "fecha"))
    imp = num(pick(it, "importe", "importeConcedido", "importeNominal"))
    if not cid:
        cid = hashlib.sha1((num_conv + "|" + ben + "|" + fecha + "|" + str(imp)).encode("utf-8")).hexdigest()[:16]
    return {"cid": cid, "num_conv": num_conv, "convocatoria": str(pick(it, "convocatoria", "descripcion", "tituloConvocatoria") or ""),
            "fecha": fecha, "beneficiario": ben, "nif": nif, "importe": imp,
            "instrumento": str(pick(it, "instrumento", "descripcionInstrumento") or ""),
            "organo1": n1, "organo2": n2, "organo3": n3, "benef_norm": norm(ben)}


def guardar_concesiones(con, items):
    for it in items:
        c = conc_de_item(it)
        con.execute("""INSERT OR REPLACE INTO concesiones(cid,num_conv,convocatoria,fecha,beneficiario,nif,importe,instrumento,
            organo1,organo2,organo3,benef_norm,raw,actualizado) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (c["cid"], c["num_conv"], c["convocatoria"], c["fecha"], c["beneficiario"], c["nif"], c["importe"],
                     c["instrumento"], c["organo1"], c["organo2"], c["organo3"], c["benef_norm"], _j(it), ahora_txt()))
    con.commit()
    return len(items)


def otros_de_item(tipo, it):
    """Normaliza un registro de ayudasestado / minimis / sanciones / partidos / grandes / planes."""
    n1 = " > ".join(str(x) for x in (pick(it, "nivel1"), pick(it, "nivel2"), pick(it, "nivel3")) if x)
    org = n1 or str(pick(it, "convocante") or "")
    ben = str(pick(it, "beneficiario", "sancionado") or "")
    fecha = iso(pick(it, "fechaConcesion", "fechaSancion", "fechaAlta", "fechaRegistro", "fechaAprobacion"))
    imp = num(pick(it, "importe", "importeMulta", "ayudaETotal", "ayudaEquivalente"))
    num_c = str(pick(it, "numeroConvocatoria") or "")
    if tipo == "sanciones":
        titulo = "%s | %s" % (pick(it, "infraccion") or "", pick(it, "convocatoria") or "")
        extra = "multa %s, inhabilitacion %s a %s" % (eur(pick(it, "importeMulta")) or "?", f_es(pick(it, "inicioInhabilitacion")) or "?", f_es(pick(it, "finInhabilitacion")) or "?")
    elif tipo == "minimis":
        titulo = "%s | %s | %s" % (pick(it, "reglamento") or "", pick(it, "instrumento") or "", pick(it, "sectorActividad") or "")
        extra = ""
    elif tipo == "grandes":
        titulo = "Gran beneficiario ejercicio %s" % (pick(it, "ejercicio") or "?")
        extra = ""
    elif tipo == "planes":
        titulo = str(pick(it, "descripcion") or "")
        extra = "%s | vigencia %s-%s | %s" % (pick(it, "tipoPlan") or "", pick(it, "vigenciaDesde") or "?", pick(it, "vigenciaHasta") or "?", pick(it, "ambitos") or "")
    else:   # ayudasestado, partidos
        titulo = str(pick(it, "convocatoria", "descripcion") or "")
        extra = " | ".join(str(x) for x in (pick(it, "reglamento"), pick(it, "objetivo"), pick(it, "instrumento")) if x)
    clave = str(pick(it, "idConcesion", "codConcesion", "codigoConcesion", "id", "idPersona") or "")
    if not clave or tipo in ("sanciones", "grandes"):
        clave = hashlib.sha1(_j(it).encode("utf-8")).hexdigest()[:16]
    return {"tipo": tipo, "clave": clave, "fecha": fecha, "titulo": titulo.strip(" |"), "importe": imp, "beneficiario": ben,
            "organo": org, "num_conv": num_c, "extra": extra}


def guardar_otros(con, tipo, items):
    filas = []
    for it in items:
        o = otros_de_item(tipo, it)
        con.execute("""INSERT OR REPLACE INTO otros(tipo,clave,fecha,titulo,titulo_norm,importe,beneficiario,organo,raw,actualizado)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (tipo, o["clave"], o["fecha"], o["titulo"], norm(o["titulo"] + " " + o["beneficiario"]), o["importe"], o["beneficiario"], o["organo"], _j(it), ahora_txt()))
        filas.append(o)
    con.commit()
    return filas


def guardar_catalogo(con, tipo, items, grupo=""):
    """Guarda un catalogo de la API. Los catalogos de organos y regiones vienen en arbol (children):
    se aplanan guardando para cada nodo su ruta, sus ancestros y los ids de el y de todos sus descendientes
    (los filtros de la API piden ids concretos: el de un ministerio NO incluye a sus direcciones generales)."""
    n = [0]

    def ids_de(nodo):
        r = []
        if isinstance(nodo, dict):
            c = pick(nodo, "id", "codigo")
            if c is not None:
                r.append(str(c))
            for h in nodo.get("children") or []:
                r.extend(ids_de(h))
        return r

    def rec(nodos, ruta, ancestros):
        for it in nodos:
            if not isinstance(it, dict):
                continue
            cod = pick(it, "id", "codigo")
            desc = pick(it, "descripcion", "nombre", "texto")
            if cod is None or desc is None:
                rec(it.get("children") or [], ruta, ancestros)
                continue
            desc = str(desc).strip()
            r = (ruta + " > " if ruta else "") + desc
            anc = ancestros
            ids = ",".join(dict.fromkeys(ids_de(it)))
            hijo = {k: v for k, v in it.items() if k != "children"}
            con.execute("INSERT OR REPLACE INTO catalogos(tipo,grupo,codigo,descripcion,desc_norm,ruta,ancestros,ids,raw) VALUES(?,?,?,?,?,?,?,?,?)",
                        (tipo, grupo, str(cod), desc, norm(desc), r, anc, ids, _j(hijo)))
            n[0] += 1
            rec(it.get("children") or [], r, ancestros + grupo + ":" + str(cod) + ",")
    rec(items, "", ",")
    con.commit()
    return n[0]


def registrar_busqueda(con, comando, ruta, params, n, total, completa, via):
    con.execute("INSERT INTO busquedas(cuando,comando,ruta,params,n,total,completa,via) VALUES(?,?,?,?,?,?,?,?)",
                (ahora_txt(), comando, ruta, _j(params), n, total, 1 if completa else 0, via))
    con.commit()


# ----------------------------------------------------------------------------
# Servidor local para la extension
# ----------------------------------------------------------------------------
class Cola:
    def __init__(self):
        self.lock = threading.Lock()
        self.pend = []
        self.en_curso = {}
        self.resultados = queue.Queue()
        self.ultimo_contacto = 0.0
        self.al_progreso = None

    def progreso(self, datos):
        with self.lock:
            self.ultimo_contacto = time.time()
            ent = self.en_curso.get(datos.get("id"))
            if ent:
                self.en_curso[datos["id"]] = (ent[0], time.time())
        if self.al_progreso:
            try:
                self.al_progreso(datos)
            except Exception:
                pass

    def add(self, job):
        with self.lock:
            self.pend.append(job)

    def siguiente(self):
        with self.lock:
            ahora = time.time()
            self.ultimo_contacto = ahora
            for jid, (job, t) in list(self.en_curso.items()):
                if ahora - t > job.get("timeout", 120):
                    del self.en_curso[jid]
                    self.resultados.put({"job": job, "datos": {"id": jid, "error": "tiempo agotado en la extension"}})
            if self.pend:
                job = self.pend.pop(0)
                self.en_curso[job["id"]] = (job, ahora)
                return job
        return None

    def recibir(self, datos):
        with self.lock:
            self.ultimo_contacto = time.time()
            ent = self.en_curso.pop(datos.get("id"), None)
        if ent is None:
            return False
        self.resultados.put({"job": ent[0], "datos": datos})
        return True


class Manejador(BaseHTTPRequestHandler):
    cola = None

    def log_message(self, *a):
        pass

    def _enviar(self, codigo, obj=None):
        cuerpo = json.dumps(obj).encode("utf-8") if obj is not None else b""
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        if cuerpo:
            self.wfile.write(cuerpo)

    def do_GET(self):
        if self.headers.get("X-SUBV") != "1":
            return self._enviar(403, {"error": "falta cabecera"})
        if self.path.startswith("/tarea"):
            job = self.cola.siguiente()
            return self._enviar(200, job) if job else self._enviar(204)
        self._enviar(404, {"error": "no existe"})

    def do_POST(self):
        if self.headers.get("X-SUBV") != "1":
            return self._enviar(403, {"error": "falta cabecera"})
        if not (self.path.startswith("/resultado") or self.path.startswith("/progreso")):
            return self._enviar(404, {"error": "no existe"})
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n <= 0 or n > 120 * 1024 * 1024:
                return self._enviar(413, {"error": "tamano no valido"})
            datos = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception as e:
            return self._enviar(400, {"error": str(e)})
        if self.path.startswith("/progreso"):
            self.cola.progreso(datos)
            return self._enviar(200, {"ok": True})
        ok = self.cola.recibir(datos)
        self._enviar(200 if ok else 410, {"ok": ok})


class Extension:
    """Servidor local + envio de tareas a la extension (una a una)."""

    def __init__(self, puerto):
        self.puerto = puerto
        self.cola = Cola()
        self.srv = None
        self.avisado = False
        self.cola.al_progreso = self._progreso

    def _progreso(self, d):
        f = d.get("fase")
        if f == "pagina":
            t = d.get("total")
            print("\r  extension: pagina %s - %s resultados%s   " % (d.get("pagina"), d.get("items"), (" de " + str(t)) if t is not None else ""),
                  end="", file=sys.stderr, flush=True)
        elif f == "lote":
            print("\r  extension: %s de %s peticiones   " % (d.get("hechas"), d.get("total")), end="", file=sys.stderr, flush=True)

    def arrancar(self):
        if self.srv:
            return True
        Manejador.cola = self.cola
        try:
            self.srv = ThreadingHTTPServer(("127.0.0.1", self.puerto), Manejador)
        except OSError as e:
            print("No puedo abrir el puerto %d (%s). Quiza hay otro subv.py en marcha." % (self.puerto, e))
            return False
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return True

    def parar(self):
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()
            self.srv = None

    def ejecutar(self, job, espera_ext=25, max_espera_ext=300):
        if not self.arrancar():
            return {"error": "no se pudo abrir el servidor local"}
        job["id"] = uuid.uuid4().hex
        if os.environ.get("SUBV_BASE_EXT"):          # solo para pruebas: la extension recibe la base oficial
            job = json.loads(json.dumps(job).replace(BASE_API.replace("/", "\\/"), os.environ["SUBV_BASE_EXT"]).replace(BASE_API, os.environ["SUBV_BASE_EXT"]))
        self.cola.add(job)
        t0 = time.time()
        while True:
            try:
                r = self.cola.resultados.get(timeout=1)
            except queue.Empty:
                ahora = time.time()
                c = self.cola.ultimo_contacto
                if c == 0:
                    if ahora - t0 > espera_ext and not self.avisado:
                        print("Esperando a la extension... Abre Firefox (con 'SUBV BDNS Termux' instalada) y dejalo abierto.\n"
                              "Ctrl+C para cancelar.", file=sys.stderr)
                        self.avisado = True
                    if ahora - t0 > max_espera_ext:
                        return {"error": "la extension no ha contactado en %d s" % max_espera_ext}
                elif ahora - c > 150 and not self.cola.en_curso and not self.cola.pend:
                    pass
                elif ahora - c > 150:
                    return {"error": "la extension dejo de responder (Firefox en segundo plano?)"}
                continue
            if r["datos"].get("id") == job["id"]:
                return r["datos"]


# ----------------------------------------------------------------------------
# Cliente de la API (directo o por extension)
# ----------------------------------------------------------------------------
class Cliente:
    def __init__(self, via="auto", puerto=PUERTO_DEF, espejo=False, tiempo=40):
        self.via = via
        self.puerto = puerto
        self.base = BASE_API_ESPEJO if espejo else BASE_API
        self.tiempo = tiempo
        self.ext = None
        self.modo = None            # 'directo' o 'extension' una vez decidido
        self.ultima = 0.0

    # --- decision de via ---
    def decidir(self):
        if self.modo:
            return self.modo
        if self.via == "extension":
            self.modo = "extension"
        elif self.via == "directo":
            self.modo = "directo"
        else:
            r = self._directo(self.url("convocatorias/ultimas", {"page": 0, "pageSize": 1}), False, reintentos=1)
            ok = r.get("status") == 200 and isinstance(r.get("texto"), str) and r["texto"].lstrip()[:1] in ("{", "[")
            self.modo = "directo" if ok else "extension"
            evento("via automatica: %s (sonda status=%s %s)" % (self.modo, r.get("status"), r.get("error", "")))
            if not ok:
                print("La conexion directa no funciona (%s); uso la extension de Firefox."
                      % (r.get("error") or "HTTP %s" % r.get("status")), file=sys.stderr)
        if self.modo == "extension":
            self.ext = Extension(self.puerto)
        return self.modo

    def cerrar(self):
        if self.ext:
            self.ext.parar()

    def url(self, ruta, params=None):
        u = self.base + "/" + ruta.lstrip("/")
        p = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        if p:
            u += ("&" if "?" in u else "?") + urllib.parse.urlencode(p, doseq=True)
        return u

    # --- peticion directa ---
    def _directo(self, url, binario, accept=None, reintentos=4):
        ultimo = {"status": 0, "error": "sin intentos"}
        for i in range(reintentos):
            espera = self.ultima + PAUSA_DIRECTA - time.time()
            if espera > 0:
                time.sleep(espera)
            self.ultima = time.time()
            t0 = time.time()
            try:
                rq = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept or "application/json, text/plain, */*",
                                                          "Accept-Language": "es-ES,es;q=0.9"})
                with urllib.request.urlopen(rq, timeout=self.tiempo) as r:
                    datos = r.read()
                    res = {"status": r.status, "ctype": r.headers.get("Content-Type", ""), "url_final": r.geturl(),
                           "bytes": len(datos), "ms": int((time.time() - t0) * 1000)}
                    if binario:
                        res["b64"] = base64.b64encode(datos).decode("ascii")
                    else:
                        res["texto"] = datos.decode("utf-8", "replace")
                    return res
            except urllib.error.HTTPError as e:
                cuerpo = ""
                try:
                    cuerpo = e.read().decode("utf-8", "replace")[:2000]
                except Exception:
                    pass
                ultimo = {"status": e.code, "texto": cuerpo, "error": "HTTP %s" % e.code, "ms": int((time.time() - t0) * 1000)}
                if e.code == 429 or e.code >= 500:
                    evento("reintento %d: HTTP %s en %s" % (i + 1, e.code, url[:120]))
                    time.sleep(1.5 * (i + 1))
                    continue
                return ultimo
            except Exception as e:
                ultimo = {"status": 0, "error": "%s: %s" % (type(e).__name__, e), "ms": int((time.time() - t0) * 1000)}
                time.sleep(1.0 * (i + 1))
        return ultimo

    def _registrar(self, url, r):
        LOG.append({"url": url, "status": r.get("status"), "bytes": r.get("bytes"), "ms": r.get("ms"),
                    "error": r.get("error", ""), "via": self.modo, "ctype": r.get("ctype", "")})

    # --- API publica del cliente ---
    def get(self, ruta, params=None, binario=False, accept=None):
        """Una peticion. Devuelve dict {status, texto|b64, error...}."""
        self.decidir()
        url = ruta if ruta.startswith("http") else self.url(ruta, params)
        if self.modo == "directo":
            r = self._directo(url, binario, accept)
        else:
            r = self.ext.ejecutar({"tipo": "bin" if binario else "get", "url": url, "accept": accept, "timeout": 180})
        self._registrar(url, r)
        return r

    def get_json(self, ruta, params=None):
        r = self.get(ruta, params)
        obj = None
        if isinstance(r.get("texto"), str):
            try:
                obj = json.loads(r["texto"])
            except ValueError:
                obj = None
        if ruta not in MUESTRAS and isinstance(r.get("texto"), str):
            MUESTRAS[ruta] = r["texto"][:120000]
        return r, obj

    def lote(self, urls, al_paso=None):
        """Varias URL -> lista de respuestas (mismo orden)."""
        self.decidir()
        if not urls:
            return []
        if self.modo == "extension":
            for i in range(0, len(urls), 200):
                trozo = urls[i:i + 200]
                r = self.ext.ejecutar({"tipo": "lote", "urls": trozo, "timeout": 120 + 3 * len(trozo)})
                res = r.get("resultados")
                if not res:
                    res = [{"status": 0, "error": r.get("error", "sin resultado")} for _ in trozo]
                for u, x in zip(trozo, res):
                    self._registrar(u, x)
                if i == 0:
                    out = list(res)
                else:
                    out += list(res)
            return out
        out = []
        for i, u in enumerate(urls):
            x = self._directo(u, False)
            self._registrar(u, x)
            out.append(x)
            if al_paso:
                al_paso(i + 1, len(urls))
        return out

    def paginar(self, ruta, params, tam=200, limite=0, max_paginas=100000):
        """Recorre las paginas de una busqueda. Devuelve dict items,total,paginas,completa,errores."""
        self.decidir()
        params = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        if limite:
            tam = max(1, min(tam, limite))
        url = self.base + "/" + ruta.lstrip("/")
        if self.modo == "extension":
            r = self.ext.ejecutar({"tipo": "paginar", "url": url, "params": params, "page_size": tam, "limite": limite,
                                   "max_paginas": max_paginas, "timeout": 600})
            print("", file=sys.stderr)
            if r.get("error") and "items" not in r:
                return {"items": [], "total": None, "paginas": 0, "completa": False, "errores": [{"error": r["error"]}]}
            if r.get("muestra") and ruta not in MUESTRAS:
                MUESTRAS[ruta] = r["muestra"]
            LOG.append({"url": url + " (paginar %s paginas)" % r.get("paginas"), "status": r.get("status"), "bytes": None,
                        "ms": None, "error": "; ".join(str(e.get("error") or e.get("status")) for e in r.get("errores", [])),
                        "via": "extension", "ctype": ""})
            return r
        items, total, paginas, errores, completa, p, tp = [], None, 0, [], True, 0, None
        while True:
            q = dict(params)
            q["page"] = p
            q["pageSize"] = tam
            r, j = self.get_json(ruta, q)
            if r.get("status") != 200 or j is None:
                errores.append({"pagina": p, "status": r.get("status"), "error": r.get("error", "") or "no es JSON",
                                "texto": (r.get("texto") or "")[:500]})
                completa = False
                break
            cont = j if isinstance(j, list) else (j.get("content") or j.get("items") or j.get("data") or [])
            if isinstance(j, dict):
                if isinstance(j.get("totalElements"), int):
                    total = j["totalElements"]
                if isinstance(j.get("totalPages"), int):
                    tp = j["totalPages"]
            items.extend(cont)
            paginas += 1
            print("\r  pagina %d - %d resultados%s   " % (p, len(items), (" de %s" % total) if total is not None else ""),
                  end="", file=sys.stderr, flush=True)
            if isinstance(j, list):
                if len(cont) < tam:
                    break
            elif j.get("last") is True or not cont:
                break
            if tp is not None and p + 1 >= tp:
                break
            if limite and len(items) >= limite:
                if total is None or len(items) < total:
                    completa = False
                break
            if paginas >= max_paginas:
                completa = False
                break
            p += 1
        print("", file=sys.stderr)
        if limite and len(items) > limite:
            del items[limite:]
        return {"items": items, "total": total, "paginas": paginas, "completa": completa, "errores": errores}


# ----------------------------------------------------------------------------
# Swagger (descripcion real de la API) y validacion de parametros
# ----------------------------------------------------------------------------
def cargar_swagger():
    try:
        with open(SWAGGER_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _ruta_norm(p):
    p = p.strip("/")
    return re.sub(r"^(bdnstrans/)?api/", "", p)


def swagger_rutas(sw):
    """dict ruta -> {'metodo': {'params': [{name,in,required,type,desc,enum}], 'resumen': str}}"""
    out = {}
    if not sw:
        return out
    for ruta, ops in (sw.get("paths") or {}).items():
        for met, op in (ops or {}).items():
            if met.lower() not in ("get", "post", "put", "delete"):
                continue
            params = []
            for p in (op.get("parameters") or []) + (ops.get("parameters") or []):
                if "$ref" in p:
                    ref = p["$ref"].split("/")[-1]
                    p = ((sw.get("components") or {}).get("parameters") or {}).get(ref) or (sw.get("parameters") or {}).get(ref) or p
                sch = p.get("schema") or {}
                params.append({"name": p.get("name"), "in": p.get("in"), "required": bool(p.get("required")),
                               "type": sch.get("type") or p.get("type") or "", "desc": (p.get("description") or "").strip(),
                               "enum": sch.get("enum") or p.get("enum") or [], "formato": sch.get("format") or p.get("format") or ""})
            out.setdefault(_ruta_norm(ruta), {})[met.lower()] = {"params": params, "resumen": op.get("summary") or op.get("description") or ""}
    return out


def validar_params(ruta, params):
    sw = cargar_swagger()
    if not sw:
        return
    rutas = swagger_rutas(sw)
    r = rutas.get(_ruta_norm(ruta))
    if not r:
        aviso("la ruta %s no aparece en el swagger guardado (subv.py api --actualizar)." % ruta)
        return
    nombres = set()
    for op in r.values():
        nombres |= {p["name"] for p in op["params"]}
    for k in params:
        if k not in nombres and k not in ("page", "pageSize"):
            aviso("el parametro '%s' no existe en %s segun el swagger (valen: %s)." % (k, ruta, ", ".join(sorted(n for n in nombres if n))))


def nombre_param(ruta, candidatos, defecto):
    """Nombre real de un parametro segun el swagger (si esta guardado); si no, el habitual."""
    sw = cargar_swagger()
    if not sw:
        return defecto
    r = swagger_rutas(sw).get(_ruta_norm(ruta))
    if not r:
        return defecto
    nombres = set()
    for op in r.values():
        nombres |= {p["name"] for p in op["params"]}
    for c in candidatos:
        if c in nombres:
            return c
    return defecto


# ----------------------------------------------------------------------------
# Catalogos y resolucion de nombres a codigos
# ----------------------------------------------------------------------------
def lista_de_respuesta(j):
    if isinstance(j, list):
        return j
    if isinstance(j, dict):
        for k in ("content", "items", "data", "resultados", "terceros"):
            if isinstance(j.get(k), list):
                return j[k]
    return []


def traer_catalogo(cli, con, tipo, admon=None):
    ruta = CATALOGOS[tipo]
    total = 0
    admons = [admon] if admon else (["C", "A", "L", "O"] if tipo == "organos" else [None])
    for ad in admons:
        params = {"vpd": VPD}
        if tipo == "organos":
            params["idAdmon"] = ad
        r, j = cli.get_json(ruta, params)
        if r.get("status") != 200 or j is None:
            aviso("catalogo %s%s: HTTP %s %s" % (tipo, (" idAdmon=" + ad) if ad else "", r.get("status"), r.get("error", "")))
            continue
        total += guardar_catalogo(con, tipo, lista_de_respuesta(j), ad or "")
    return total


def resolver_catalogo(cli, con, tipo, valor, admon=None, unico=False, grupos=None):
    """'madrid, galicia' -> '26,27,3,...' (ids). Los valores que ya son numeros pasan tal cual.
    Para cada nombre se incluyen los ids de todos sus descendientes. unico=True: la API solo admite UN valor.
    Si se pasa el set `grupos`, se rellena con los ambitos (C/A/L/O) de los organos elegidos."""
    if not valor:
        return None
    salida = []
    for tok in [t.strip() for t in str(valor).split(",") if t.strip()]:
        if re.match(r"^\d+$", tok):
            salida.append(tok)
            continue
        n = con.execute("SELECT COUNT(*) FROM catalogos WHERE tipo=?", (tipo,)).fetchone()[0]
        if n == 0:
            print("Descargando el catalogo '%s' (solo la primera vez)..." % tipo, file=sys.stderr)
            traer_catalogo(cli, con, tipo, None)
        q = norm(tok)
        palabras = [w for w in re.split(r"[\s,]+", q) if w and w not in ("de", "la", "el", "los", "las", "del")] or [q]
        sql = "SELECT grupo, codigo, descripcion, desc_norm, ancestros, ids, ruta FROM catalogos WHERE tipo=?"
        args = [tipo]
        for w in palabras:
            sql += " AND (desc_norm LIKE ? OR lower(ruta) LIKE ?)"
            args += ["%" + w + "%", "%" + w + "%"]
        if tipo == "organos" and admon:
            sql += " AND grupo=?"
            args.append(admon)
        filas = con.execute(sql, args).fetchall()
        if not filas:
            raise ValueError("'%s' no coincide con ningun valor del catalogo %s. Mira:  subv.py catalogo %s -b %s" % (tok, tipo, tipo, tok))
        exactas = [f for f in filas if f["desc_norm"] == q or re.search(r"(^|- )" + re.escape(q) + r"$", f["desc_norm"])]
        # regiones: 'madrid' debe valer para la comunidad entera (ES3, ES30 y la provincia ES300), como en la web
        elegidas = filas if tipo == "regiones" else (exactas or filas)
        claves = {"%s:%s" % (f["grupo"], f["codigo"]) for f in elegidas}
        # quitar los que ya estan incluidos en un ancestro elegido (sus ids ya van en el del ancestro)
        elegidas = [f for f in elegidas if not any(("," + c + ",") in f["ancestros"] for c in claves)]
        if len(elegidas) > 25:
            raise ValueError("'%s' es ambiguo en %s (%d coincidencias). Afinalo o usa el id (subv.py catalogo %s -b ...)." % (tok, tipo, len(elegidas), tipo))
        if len(elegidas) > 1:
            aviso("'%s' coincide con %d valores de %s: %s" % (tok, len(elegidas), tipo, "; ".join("%s=%s" % (f["codigo"], f["descripcion"][:40]) for f in elegidas[:6])))
        for f in elegidas:
            salida.extend((f["ids"] or f["codigo"]).split(","))
            if grupos is not None and f["grupo"]:
                grupos.add(f["grupo"])
    res = list(dict.fromkeys(salida))
    if unico and len(res) > 1:
        raise ValueError("este filtro solo admite UN valor y '%s' corresponde a %d. Usa un id concreto (subv.py catalogo %s -b ...)." % (valor, len(res), tipo))
    return ",".join(res)


def parse_p(lista):
    out = {}
    for x in lista or []:
        if "=" not in x:
            raise ValueError("--p debe ser NOMBRE=VALOR (recibido: %r)" % x)
        k, v = x.split("=", 1)
        out[k.strip()] = v.strip()
    return out


# ----------------------------------------------------------------------------
# Salida
# ----------------------------------------------------------------------------
def estado_plazo(r, hoy=None):
    """abierto / cerrado / proximamente / abierto (indefinido) / sin_plazo / ?.
    OJO: en la API 'abierto' significa 'se puede solicitar indefinidamente', no 'plazo en curso'."""
    hoy = (hoy or dt.date.today()).isoformat()
    ini, fin, ab = r.get("inicio") or "", r.get("fin") or "", r.get("abierta")
    if ini and hoy < ini:
        return "proximamente"
    if fin and hoy > fin:
        return "cerrado"
    if ini or fin:
        return "abierto"
    if ab is None:
        return "?"
    return "abierto (indefinido)" if ab == 1 else "sin_plazo"


def organo_txt(r):
    return " > ".join(x for x in (r.get("organo1"), r.get("organo2"), r.get("organo3")) if x)


def envolver(txt, ancho, sangria="     "):
    return textwrap.fill(txt, width=ancho, initial_indent=sangria, subsequent_indent=sangria)


def filas_dict(filas):
    return [dict(f) if not isinstance(f, dict) else f for f in filas]


def salida_texto(texto, a):
    if getattr(a, "guardar", None):
        with open(a.guardar, "w", encoding="utf-8") as f:
            f.write(texto if texto.endswith("\n") else texto + "\n")
        print("Guardado en %s" % a.guardar)
    else:
        print(texto)


CAMPOS_CSV_CONV = ["num", "fecha", "organo1", "organo2", "organo3", "descripcion", "inicio", "fin", "presupuesto", "finalidad",
                   "instrumentos", "beneficiarios", "regiones", "tipo", "url_bases", "sede", "ficha"]
CAMPOS_CSV_CONC = ["cid", "num_conv", "fecha", "beneficiario", "nif", "importe", "instrumento", "organo1", "organo2", "organo3", "convocatoria"]
CAMPOS_CSV_OTROS = ["tipo", "clave", "fecha", "titulo", "importe", "beneficiario", "organo", "num_conv", "extra"]


def _csv(filas, campos):
    b = io.StringIO()
    w = csv.writer(b, delimiter=";", lineterminator="\n")
    w.writerow(campos)
    for f in filas:
        w.writerow([f.get(c, "") if f.get(c) is not None else "" for c in campos])
    return b.getvalue()


def mostrar_convocatorias(filas, a, titulo=None):
    filas = filas_dict(filas)
    for f in filas:
        f["ficha"] = FICHA % f["num"]
        f["estado_plazo"] = estado_plazo(f)
    fmt = getattr(a, "formato", "lista")
    if fmt == "json":
        salida_texto(json.dumps(filas, ensure_ascii=False, indent=1, default=str), a)
        return
    if fmt == "jsonl":
        salida_texto("\n".join(json.dumps(f, ensure_ascii=False, default=str) for f in filas), a)
        return
    if fmt == "csv":
        salida_texto(_csv(filas, CAMPOS_CSV_CONV).rstrip("\n"), a)
        return
    ancho = ancho_terminal()
    out = []
    if titulo:
        out.append(titulo)
    for i, f in enumerate(filas, 1):
        if fmt == "tabla":
            out.append("%-8s %s %-12s %s" % (f["num"], f_es(f.get("fecha")), (f.get("estado_plazo") or "")[:12], (f.get("descripcion") or "")[:ancho - 36]))
            continue
        out.append("%d) BDNS %s | registrada %s | %s" % (i, f["num"], f_es(f.get("fecha")) or "?", organo_txt(f) or "(organo no indicado)"))
        out.append(envolver(f.get("descripcion") or "(sin descripcion)", ancho))
        if f.get("inicio") or f.get("fin") or f.get("presupuesto") or f.get("abierta") is not None:
            partes = ["plazo: %s" % f["estado_plazo"]]
            if f.get("inicio") or f.get("fin"):
                partes.append("del %s al %s" % (f_es(f.get("inicio")) or "?", f_es(f.get("fin")) or "?"))
            if f.get("presupuesto"):
                partes.append("presupuesto " + eur(f["presupuesto"]))
            out.append(envolver(" | ".join(partes), ancho))
        for k, et in (("finalidad", "Finalidad"), ("beneficiarios", "Beneficiarios"), ("regiones", "Regiones"), ("instrumentos", "Instrumentos")):
            if f.get(k):
                out.append(envolver("%s: %s" % (et, f[k]), ancho))
        out.append("     " + f["ficha"])
    salida_texto("\n".join(out), a)


def mostrar_concesiones(filas, a, titulo=None):
    filas = filas_dict(filas)
    fmt = getattr(a, "formato", "lista")
    if fmt == "json":
        return salida_texto(json.dumps(filas, ensure_ascii=False, indent=1, default=str), a)
    if fmt == "jsonl":
        return salida_texto("\n".join(json.dumps(f, ensure_ascii=False, default=str) for f in filas), a)
    if fmt == "csv":
        return salida_texto(_csv(filas, CAMPOS_CSV_CONC).rstrip("\n"), a)
    ancho = ancho_terminal()
    out = [titulo] if titulo else []
    total = 0.0
    for i, f in enumerate(filas, 1):
        total += f.get("importe") or 0
        if fmt == "tabla":
            out.append("%s %-14s %s %s" % (f_es(f.get("fecha")), eur(f.get("importe")), (f.get("beneficiario") or "")[:34].ljust(34), (f.get("convocatoria") or "")[:max(10, ancho - 66)]))
            continue
        out.append("%d) %s | %s | %s" % (i, f_es(f.get("fecha")) or "?", eur(f.get("importe")) or "importe ?", f.get("beneficiario") or "(sin beneficiario)"))
        if f.get("convocatoria"):
            out.append(envolver(f["convocatoria"], ancho))
        o = organo_txt(f)
        if o or f.get("instrumento"):
            out.append(envolver(" | ".join(x for x in (o, f.get("instrumento")) if x), ancho))
        if f.get("num_conv"):
            out.append("     convocatoria BDNS %s  %s" % (f["num_conv"], FICHA % f["num_conv"]))
    if filas:
        out.append("")
        out.append("Total mostrado: %d concesiones, %s" % (len(filas), eur(total)))
    salida_texto("\n".join(out), a)


def mostrar_otros(filas, a, titulo=None):
    filas = filas_dict(filas)
    fmt = getattr(a, "formato", "lista")
    if fmt == "json":
        return salida_texto(json.dumps(filas, ensure_ascii=False, indent=1, default=str), a)
    if fmt == "jsonl":
        return salida_texto("\n".join(json.dumps(f, ensure_ascii=False, default=str) for f in filas), a)
    if fmt == "csv":
        return salida_texto(_csv(filas, CAMPOS_CSV_OTROS).rstrip("\n"), a)
    ancho = ancho_terminal()
    out = [titulo] if titulo else []
    for i, f in enumerate(filas, 1):
        out.append("%d) [%s] %s | %s | %s" % (i, f.get("tipo"), f_es(f.get("fecha")) or "?", eur(f.get("importe")) or "-", f.get("beneficiario") or f.get("organo") or ""))
        if f.get("titulo"):
            out.append(envolver(f["titulo"], ancho))
        if f.get("extra"):
            out.append(envolver(f["extra"], ancho))
        if f.get("organo") and f.get("beneficiario"):
            out.append(envolver(f["organo"], ancho))
        if f.get("num_conv"):
            out.append("     convocatoria BDNS %s  %s" % (f["num_conv"], FICHA % f["num_conv"]))
    salida_texto("\n".join(out), a)


# ----------------------------------------------------------------------------
# Diagnostico
# ----------------------------------------------------------------------------
def informe_txt(comando, argv, cli, resumen, error_tb=None):
    L = ["INFORME DE DIAGNOSTICO subv.py %s" % VERSION, "=" * 60,
         "Fecha: %s" % ahora_txt(), "Comando: %s" % " ".join(argv),
         "Python %s en %s" % (sys.version.split()[0], platform.platform()),
         "Via solicitada: %s | via usada: %s | base: %s" % (getattr(cli, "via", "-"), getattr(cli, "modo", "-"), getattr(cli, "base", "-")),
         "Swagger guardado: %s" % ("si" if os.path.exists(SWAGGER_PATH) else "NO (ejecuta: subv.py api --actualizar)"), ""]
    L.append("RESUMEN")
    L.append("-" * 60)
    for k, v in (resumen or {}).items():
        L.append("%s: %s" % (k, v if not isinstance(v, (dict, list)) else _j(v)))
    L += ["", "AVISOS (%d)" % len(AVISOS), "-" * 60] + AVISOS
    L += ["", "EVENTOS", "-" * 60] + EVENTOS
    L += ["", "PETICIONES (%d)" % len(LOG), "-" * 60]
    for p in LOG[:300]:
        L.append("%s | status=%s | %s bytes | %s ms | %s %s | %s" % (p.get("via"), p.get("status"), p.get("bytes"), p.get("ms"), p.get("ctype", ""), p.get("error", ""), p.get("url")))
    if len(LOG) > 300:
        L.append("... (%d mas, ver peticiones.json)" % (len(LOG) - 300))
    if error_tb:
        L += ["", "ERROR DEL PROGRAMA", "-" * 60, error_tb]
    return "\n".join(L) + "\n"


def guardar_bundle(comando, argv, cli, resumen, error_tb=None):
    try:
        os.makedirs(DIAG_DIR, exist_ok=True)
        ruta = os.path.join(DIAG_DIR, "diag_%s_%s.zip" % (re.sub(r"\W+", "", comando) or "x", dt.datetime.now().strftime("%Y%m%d_%H%M%S")))
        with zipfile.ZipFile(ruta, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("informe.txt", informe_txt(comando, argv, cli, resumen, error_tb))
            z.writestr("peticiones.json", json.dumps(LOG, ensure_ascii=False, indent=1))
            for r, t in MUESTRAS.items():
                z.writestr("muestra_%s.json" % re.sub(r"\W+", "_", r), t)
            sw = cargar_swagger()
            if sw:
                resu = {r: {m: [p["name"] for p in op["params"]] for m, op in ops.items()} for r, ops in swagger_rutas(sw).items()}
                z.writestr("swagger_resumen.json", json.dumps(resu, ensure_ascii=False, indent=1))
            z.writestr("subv.py.version", VERSION + "\n")
        zs = sorted(f for f in os.listdir(DIAG_DIR) if f.startswith("diag_") and f.endswith(".zip"))
        for viejo in zs[:-30]:
            try:
                os.remove(os.path.join(DIAG_DIR, viejo))
            except OSError:
                pass
        return ruta
    except Exception as e:
        print("No pude guardar el diagnostico: %s" % e, file=sys.stderr)
        return None


# ----------------------------------------------------------------------------
# Comandos de busqueda en la API
# ----------------------------------------------------------------------------
def nuevo_cliente(a):
    return Cliente(via=a.via, puerto=a.puerto, espejo=a.espejo)


# (atributo de la linea de comandos, parametro de la API, catalogo, la API solo admite un valor)
OPCIONES = [
    ("texto", "descripcion", None, False), ("numero", "numeroConvocatoria", None, False),
    ("cod_concesion", "codConcesion", None, False), ("ambito", "tipoAdministracion", "ambito", False),
    ("organo", "organos", "organos", False), ("region", "regiones", "regiones", False),
    ("beneficiario_tipo", "tiposBeneficiario", "beneficiarios", False), ("instrumento", "instrumentos", "instrumentos", False),
    ("finalidad", "finalidad", "finalidades", True), ("actividad", "actividad", "actividades", False),
    ("objetivo", "objetivos", "objetivos", False), ("reglamento", "reglamento", "reglamentos", True),
    ("producto", "producto", "sectores", True), ("ayuda_estado", "ayudaEstado", None, False),
    ("anios", "anios", None, False), ("vigencia_desde", "vigenciaDesde", None, False), ("vigencia_hasta", "vigenciaHasta", None, False),
]
MODOS = {"frase": 0, "todas": 1, "alguna": 2}
RUTAS_SIN_VPD = ("grandesbeneficiarios/", "planesestrategicos", "convocatorias/documentos", "convocatorias/pdf", "convocatorias/zip", "organos/codigo")
AMBITO_TERCEROS = {"concesiones/busqueda": "C", "ayudasestado/busqueda": "A", "minimis/busqueda": "M", "sanciones/busqueda": "S",
                   "partidospoliticos/busqueda": "P", "grandesbeneficiarios/busqueda": "G"}
NIF_RE = re.compile(r"^[A-Za-z]?\d{6,8}[A-Za-z]?$|^[A-Za-z]\d{7}[A-Za-z0-9]$")


def parametros_de_ruta(ruta):
    """Nombres de parametros de una ruta segun el swagger guardado (o None si no se sabe)."""
    sw = cargar_swagger()
    if not sw:
        return None
    r = swagger_rutas(sw).get(_ruta_norm(ruta))
    if not r:
        return None
    nombres = set()
    for op in r.values():
        nombres |= {p["name"] for p in op["params"]}
    return nombres


def resolver_beneficiario(cli, con, ruta, valor):
    """NIF -> {'nifCif': ..}; nombre -> {'beneficiario': idPersona} buscando en /terceros."""
    v = valor.strip()
    if NIF_RE.match(v):
        return {"nifCif": v.upper()}
    if re.match(r"^\d{1,9}$", v):
        return {"beneficiario": v}
    amb = AMBITO_TERCEROS.get(_ruta_norm(ruta), "C")
    if len(v) < 3:
        raise ValueError("el nombre del beneficiario debe tener al menos 3 letras")
    r, j = cli.get_json("terceros", {"vpd": VPD, "ambito": amb, "busqueda": v})
    cand = lista_de_respuesta(j) if j is not None else []
    if not cand:
        raise ValueError("no hay beneficiarios que coincidan con '%s' (ambito %s)." % (v, amb))
    exactos = [c for c in cand if norm(v) in norm(c.get("descripcion"))]
    cand = exactos or cand
    if len(cand) > 1:
        lista = "; ".join("%s=%s" % (c.get("id"), (c.get("descripcion") or "")[:50]) for c in cand[:8])
        raise ValueError("'%s' coincide con %d beneficiarios: %s. Usa el NIF o el id: -b <id>" % (v, len(cand), lista))
    print("Beneficiario: %s (id %s)" % (cand[0].get("descripcion"), cand[0].get("id")), file=sys.stderr)
    return {"beneficiario": cand[0].get("id")}


def construir_params(ruta, a, ctx, semantica_fechas=True):
    """Convierte las opciones de la linea de comandos en parametros de la API, comprobando contra el swagger."""
    con, cli = ctx["con"], ctx["cli"]
    validos = parametros_de_ruta(ruta)
    p = {}

    def poner(clave, valor, origen):
        if valor in (None, ""):
            return
        if validos is not None and clave not in validos:
            aviso("%s no admite el filtro '%s' (opcion %s); lo ignoro. Admitidos: %s" % (ruta, clave, origen, ", ".join(sorted(x for x in validos if x))))
            return
        p[clave] = valor

    if validos is None:
        if not any(ruta.startswith(x) or ruta == x for x in RUTAS_SIN_VPD):
            p["vpd"] = VPD
    elif "vpd" in validos:
        p["vpd"] = VPD
    admon = AMBITOS.get(getattr(a, "ambito", None) or "", None)
    grupos = set()
    for attr, clave, cat, unico in OPCIONES:
        v = getattr(a, attr, None)
        if v in (None, "", False):
            continue
        if cat == "ambito":
            v = AMBITOS[v]
        elif cat:
            v = resolver_catalogo(cli, con, cat, v, admon if cat == "organos" else None, unico, grupos if cat == "organos" else None)
        poner(clave, v, "--" + attr.replace("_", "-"))
    if grupos and "tipoAdministracion" not in p:
        if len(grupos) == 1:
            poner("tipoAdministracion", next(iter(grupos)), "(deducido del organo)")
            evento("tipoAdministracion=%s deducido del organo elegido" % next(iter(grupos)))
        else:
            raise ValueError("el organo coincide en varios ambitos (%s); indica --ambito estatal|autonomica|local|otros." % ",".join(sorted(grupos)))
    if getattr(a, "texto", None):
        modo = MODOS[getattr(a, "modo", None) or "todas"]
        poner("descripcionTipoBusqueda", modo, "--modo")
    for attr, clave in (("mrr", "mrr"), ("contribucion", "contribucion")):
        if getattr(a, attr, False):
            poner(clave, "true", "--" + attr)
    if getattr(a, "beneficiario", None):
        for k, v in resolver_beneficiario(cli, con, ruta, a.beneficiario).items():
            poner(k, v, "-b")
    desde, hasta = rango_fechas(a)
    poner("fechaDesde", f_es(desde) if desde else None, "fechas")
    poner("fechaHasta", f_es(hasta) if hasta else None, "fechas")
    poner("fechaRegInicio", f_es(a.reg_desde) if getattr(a, "reg_desde", None) else None, "--reg-desde")
    poner("fechaRegFin", f_es(a.reg_hasta) if getattr(a, "reg_hasta", None) else None, "--reg-hasta")
    if getattr(a, "orden", None):
        o, _, d = a.orden.partition(":")
        poner("order", o, "--orden")
        poner("direccion", d or "desc", "--orden")
    extra = parse_p(getattr(a, "p", None))
    p.update(extra)
    validar_params(ruta, p)
    return p


def exportar_api(a, ctx, ruta, p):
    """--exportar csv|xlsx|pdf: la propia web genera el archivo con los resultados de la busqueda."""
    cli = ctx["cli"]
    rexp = ruta.replace("/busqueda", "/exportar").replace("/ultimas", "/ultimas/exportar")
    q = {k: v for k, v in p.items() if k not in ("page", "pageSize")}
    q["tipoDoc"] = a.exportar
    q["page"] = 0
    q["pageSize"] = a.max or 3000
    os.makedirs(os.path.join(DIR, "export"), exist_ok=True)
    destino = a.guardar or os.path.join(DIR, "export", "%s_%s.%s" % (re.sub(r"\W+", "_", ruta), dt.datetime.now().strftime("%Y%m%d_%H%M%S"), a.exportar))
    r = cli.get(rexp, q, binario=True, accept="*/*")
    if r.get("status") == 200 and r.get("b64"):
        with open(destino, "wb") as f:
            f.write(base64.b64decode(r["b64"]))
        print("Exportado por la web a %s (%d bytes)" % (destino, r.get("bytes", 0)))
        return 0
    aviso("exportar: HTTP %s %s" % (r.get("status"), r.get("error", "")))
    return 1


def detalles_de(cli, con, nums, al_paso=None):
    """Baja y guarda el detalle de varias convocatorias. Devuelve cuantas se guardaron."""
    urls = [cli.url("convocatorias", {"vpd": VPD, "numConv": n}) for n in nums]
    res = cli.lote(urls, al_paso)
    ok = 0
    for n, r in zip(nums, res):
        try:
            d = json.loads(r.get("texto") or "")
        except ValueError:
            d = None
        if r.get("status") == 200 and isinstance(d, dict):
            d.setdefault("numeroConvocatoria", n)
            d.pop("advertencia", None)
            if "convocatorias" not in MUESTRAS and r.get("texto"):
                MUESTRAS["convocatorias"] = r["texto"][:120000]
            guardar_convocatorias(con, [d], detalle=True)
            ok += 1
        else:
            evento("detalle %s: status=%s %s" % (n, r.get("status"), r.get("error", "")))
    return ok


def aplicar_filtros_locales(filas, a):
    """Filtros que la API no ofrece, sobre filas de convocatorias (dict)."""
    out = []
    hoy = dt.date.today()
    sin = [norm(x) for x in (getattr(a, "sin", None) or [])]
    for f in filas:
        if getattr(a, "abiertas", False):
            e = estado_plazo(f, hoy)
            if e not in ("abierto", "abierto (indefinido)") and not (e == "sin_plazo" and getattr(a, "incluir_sin_plazo", False)):
                continue
        if getattr(a, "importe_min", None) is not None and (f.get("presupuesto") or 0) < a.importe_min:
            continue
        if getattr(a, "importe_max", None) is not None and (f.get("presupuesto") or 0) > a.importe_max:
            continue
        if sin and any(w in norm(f.get("descripcion")) for w in sin):
            continue
        out.append(f)
    return out


def necesita_detalle(a):
    return bool(getattr(a, "detalle", False) or getattr(a, "abiertas", False) or getattr(a, "importe_min", None) is not None
                or getattr(a, "importe_max", None) is not None)


def leer_paginas(a, ctx, ruta, p):
    cli = ctx["cli"]
    limite = 0 if a.max == 0 else a.max
    if limite == 0:
        r0, j0 = cli.get_json(ruta, dict(p, page=0, pageSize=1))
        tot = j0.get("totalElements") if isinstance(j0, dict) else None
        if isinstance(tot, int) and tot > 100000:
            raise ValueError("la web tiene %d resultados con ese filtro: demasiados para --max 0 (limite 100.000). "
                             "Acota con fechas, organo, region... o usa --max N." % tot)
    print("Consultando %s (via %s)..." % (ruta, cli.decidir()), file=sys.stderr)
    r = cli.paginar(ruta, p, tam=a.tam, limite=limite)
    for e in r.get("errores", []):
        aviso("pagina %s: %s %s %s" % (e.get("pagina"), e.get("status"), e.get("error"), (e.get("texto") or "")[:200]))
    return r


def cmd_buscar(a, ctx):
    con, cli = ctx["con"], ctx["cli"]
    ruta = "convocatorias/busqueda"
    p = construir_params(ruta, a, ctx)
    evento("buscar params=%s" % _j(p))
    if a.exportar:
        return exportar_api(a, ctx, ruta, p)
    if necesita_detalle(a) and a.max == 0:
        aviso("se piden todos los resultados y ademas el detalle: puede tardar mucho (10 peticiones por segundo como maximo).")
    r = leer_paginas(a, ctx, ruta, p)
    items = r["items"]
    if not items and r.get("errores"):
        ctx["resumen"].update({"resultado": "error", "errores": r["errores"]})
        print("La API no devolvio resultados (mira los avisos). Se guardo el diagnostico para enviarlo.")
        return 1
    guardar_convocatorias(con, items)
    registrar_busqueda(con, "buscar", ruta, p, len(items), r.get("total"), r.get("completa"), cli.modo)
    nums = [c["num"] for c in (conv_de_busqueda(i) for i in items) if c["num"]]
    if necesita_detalle(a) and nums:
        tope = a.max_detalle or len(nums)
        if len(nums) > tope:
            aviso("detalle solo de las %d primeras de %d (--max-detalle)." % (tope, len(nums)))
        nums = nums[:tope]
        print("Bajando el detalle de %d convocatorias..." % len(nums), file=sys.stderr)
        detalles_de(cli, con, nums, lambda i, n: print("\r  detalle %d de %d   " % (i, n), end="", file=sys.stderr, flush=True))
        print("", file=sys.stderr)
    filas = []
    for n in nums:
        row = con.execute("SELECT * FROM convocatorias WHERE num=?", (n,)).fetchone()
        if row:
            filas.append(dict(row))
    filas = aplicar_filtros_locales(filas, a)
    ctx["resumen"].update({"resultados_api": len(items), "total_web": r.get("total"), "mostrados": len(filas), "completa": r.get("completa"), "params": p})
    cab = "%d convocatorias mostradas (la web tiene %s con ese filtro%s). Guardadas en %s" % (
        len(filas), r.get("total") if r.get("total") is not None else "?", "" if r.get("completa") else "; lectura incompleta: sube --max", DB_PATH)
    if not items:
        cab += ("\nSin resultados. Pistas: escribe sin acentos ('formacion'), prueba --modo alguna (por defecto se piden TODAS las palabras), "
                "ensancha las fechas o quita filtros.")
    mostrar_convocatorias(filas, a, cab)
    return 0


def cmd_ultimas(a, ctx):
    con, cli = ctx["con"], ctx["cli"]
    ruta = "convocatorias/ultimas"
    p = construir_params(ruta, a, ctx)
    if a.exportar:
        return exportar_api(a, ctx, ruta, p)
    r = leer_paginas(a, ctx, ruta, p)
    items = r["items"]
    guardar_convocatorias(con, items)
    registrar_busqueda(con, "ultimas", ruta, p, len(items), r.get("total"), r.get("completa"), cli.modo)
    nums = [c["num"] for c in (conv_de_busqueda(i) for i in items) if c["num"]]
    if necesita_detalle(a) and nums:
        nums = nums[:a.max_detalle or len(nums)]
        detalles_de(cli, con, nums)
    filas = [dict(con.execute("SELECT * FROM convocatorias WHERE num=?", (n,)).fetchone()) for n in nums]
    filas = aplicar_filtros_locales(filas, a)
    ctx["resumen"].update({"resultados_api": len(items), "mostrados": len(filas)})
    mostrar_convocatorias(filas, a, "%d ultimas convocatorias publicadas" % len(filas))
    return 0 if items else 1


def detalle_texto(row, raw, ancho):
    L = ["BDNS %s - %s" % (row["num"], FICHA % row["num"]), "=" * min(ancho, 78)]
    L.append(envolver(row.get("descripcion") or "", ancho, ""))
    L.append("")
    plazo = estado_plazo(row)
    if row.get("inicio") or row.get("fin"):
        plazo += " (%s a %s)" % (f_es(row.get("inicio")) or "?", f_es(row.get("fin")) or "?")
    campos = [("Organo", organo_txt(row)), ("Registrada", f_es(row.get("fecha"))), ("Plazo", plazo),
              ("Presupuesto", eur(row.get("presupuesto"))), ("Tipo", row.get("tipo")), ("Finalidad", row.get("finalidad")),
              ("Instrumentos", row.get("instrumentos")), ("Beneficiarios", row.get("beneficiarios")), ("Sectores", row.get("sectores")),
              ("Regiones", row.get("regiones")), ("Bases reguladoras", row.get("url_bases")), ("Sede electronica", row.get("sede")),
              ("MRR", "si" if row.get("mrr") in ("True", "true", "1") else "")]
    if raw:
        campos += [("Texto inicio plazo", raw.get("textInicio")), ("Texto fin plazo", raw.get("textFin")),
                   ("Titulo de las bases", raw.get("descripcionBasesReguladoras")),
                   ("Ayuda de Estado (SA)", raw.get("ayudaEstado")), ("Fondos UE", textos_lista(raw.get("fondos"))),
                   ("Objetivos", textos_lista(raw.get("objetivos"))), ("Reglamento", textos_lista(raw.get("reglamento")))]
    for k, v in campos:
        if v:
            L.append(textwrap.fill("%s: %s" % (k, v), width=ancho, subsequent_indent="    "))
    if raw:
        docs = pick(raw, "documentos") or []
        if docs:
            L.append("")
            L.append("Documentos (%d):" % len(docs))
            for d in docs:
                L.append(textwrap.fill("  - [%s] %s - %s %s" % (pick(d, "id", "idDocumento"), pick(d, "nombreFic", "nombre") or "", pick(d, "descripcion") or "",
                                                               ("(%d KB)" % (d["long"] // 1024)) if isinstance(d.get("long"), int) else ""), width=ancho, subsequent_indent="      "))
        anun = pick(raw, "anuncios") or []
        if anun:
            L.append("")
            L.append("Extractos / anuncios (%d):" % len(anun))
            for d in anun:
                L.append(textwrap.fill("  - %s %s %s" % (iso(pick(d, "datPublicacion", "fechaPublicacion")), pick(d, "descripcionDiarioOficial", "diarioOficial") or "", pick(d, "titulo", "url") or ""), width=ancho, subsequent_indent="      "))
    return "\n".join(L)


def cmd_detalle(a, ctx):
    con, cli = ctx["con"], ctx["cli"]
    nums = [n.strip() for x in a.numeros for n in x.split(",") if n.strip()]
    ok = detalles_de(cli, con, nums)
    ctx["resumen"].update({"pedidos": len(nums), "guardados": ok})
    sal = []
    for n in nums:
        row = con.execute("SELECT * FROM convocatorias WHERE num=?", (n,)).fetchone()
        if not row or not row["raw_detalle"]:
            sal.append("BDNS %s: no se pudo obtener el detalle (mira los avisos / el diagnostico)." % n)
            continue
        raw = json.loads(row["raw_detalle"])
        if a.json:
            sal.append(json.dumps(raw, ensure_ascii=False, indent=1))
        else:
            sal.append(detalle_texto(dict(row), raw, ancho_terminal()))
    salida_texto("\n\n".join(sal), a)
    return 0 if ok else 1


def cmd_documentos(a, ctx):
    con, cli = ctx["con"], ctx["cli"]
    num_ = a.numero
    row = con.execute("SELECT * FROM convocatorias WHERE num=?", (num_,)).fetchone()
    if not row or not row["raw_detalle"]:
        detalles_de(cli, con, [num_])
        row = con.execute("SELECT * FROM convocatorias WHERE num=?", (num_,)).fetchone()
    if not row or not row["raw_detalle"]:
        print("No pude obtener la convocatoria %s." % num_)
        return 1
    raw = json.loads(row["raw_detalle"])
    docs = pick(raw, "documentos") or []
    print("Convocatoria %s: %d documentos." % (num_, len(docs)))
    for d in docs:
        print("  [%s] %s - %s" % (pick(d, "id", "idDocumento"), pick(d, "nombreFic", "nombre") or "", pick(d, "descripcion") or ""))
    if not (a.descargar or a.pdf or a.zip):
        return 0
    carpeta = os.path.join(DOCS_DIR, num_)
    os.makedirs(carpeta, exist_ok=True)
    pedidos = []
    if a.pdf:
        pedidos.append(("convocatoria_%s.pdf" % num_, "convocatorias/pdf", {"id": raw.get("id") or row["id"] or num_, "vpd": VPD}))
    if a.zip:
        pedidos.append(("convocatoria_%s_documentos.zip" % num_, "convocatorias/zip", {"id": num_, "vpd": VPD}))
    if a.descargar:
        for d in docs:
            did = pick(d, "id", "idDocumento")
            if did is None:
                continue
            nombre = re.sub(r"[^\w.\-]+", "_", "%s_%s" % (did, pick(d, "nombreFic", "nombre") or "doc"))
            pedidos.append((nombre, "convocatorias/documentos", {"idDocumento": did}))
    nok = 0
    for nombre, ruta, params in pedidos:
        r = cli.get(ruta, params, binario=True, accept="*/*")
        if r.get("status") == 200 and r.get("b64"):
            datos = base64.b64decode(r["b64"])
            if datos[:1] in (b"{", b"[") and b"error" in datos[:300].lower():
                aviso("%s: la API devolvio un error (%s)" % (nombre, datos[:150].decode("utf-8", "replace")))
                continue
            destino = os.path.join(carpeta, nombre)
            with open(destino, "wb") as f:
                f.write(datos)
            con.execute("INSERT OR REPLACE INTO docs(num,nombre,ruta,bytes,cuando) VALUES(?,?,?,?,?)", (num_, nombre, destino, len(datos), ahora_txt()))
            nok += 1
            print("  guardado %s (%d bytes)" % (destino, len(datos)))
        else:
            aviso("%s: HTTP %s %s" % (nombre, r.get("status"), r.get("error", "")))
    con.commit()
    if a.txt and nok:
        convertir_textos(con, num_, a)
    ctx["resumen"].update({"documentos_pedidos": len(pedidos), "documentos_guardados": nok})
    return 0 if nok else 1


def convertir_textos(con, num_, a):
    """Pasa a texto los PDF bajados (pdf2txt.py del usuario si se indica/encuentra; si no, pdftotext)."""
    import shutil
    import subprocess
    carpeta = os.path.join(DOCS_DIR, num_)
    tdir = os.path.join(DIR, "txt", num_)
    os.makedirs(tdir, exist_ok=True)
    cfg = leer_config()
    if getattr(a, "pdf2txt", None):
        cfg["pdf2txt"] = os.path.abspath(os.path.expanduser(a.pdf2txt))
        guardar_config(cfg)
    p2t = cfg.get("pdf2txt")
    if p2t and not os.path.isfile(p2t):
        p2t = None
    n = 0
    for fn in sorted(os.listdir(carpeta)):
        if not fn.lower().endswith(".pdf"):
            continue
        origen = os.path.join(carpeta, fn)
        destino = os.path.join(tdir, os.path.splitext(fn)[0] + ".txt")
        ok = False
        try:
            if p2t:
                antes = set(os.listdir(tdir))
                subprocess.run([sys.executable, p2t, origen], cwd=tdir, stdin=subprocess.DEVNULL, capture_output=True, timeout=240)
                nuevos = [x for x in os.listdir(tdir) if x.endswith(".txt") and x not in antes]
                if nuevos and os.path.join(tdir, nuevos[0]) != destino:
                    shutil.move(os.path.join(tdir, nuevos[0]), destino)
                ok = os.path.exists(destino)
            if not ok and shutil.which("pdftotext"):
                subprocess.run(["pdftotext", "-layout", origen, destino], timeout=240, capture_output=True)
                ok = os.path.exists(destino)
            if not ok:
                aviso("no pude convertir %s: instala poppler (pkg install poppler) o indica --pdf2txt /ruta/pdf2txt.py" % fn)
                continue
            texto = open(destino, encoding="utf-8", errors="replace").read()
            con.execute("INSERT OR REPLACE INTO textos(num,nombre,texto_norm,caracteres) VALUES(?,?,?,?)", (num_, fn, norm(re.sub(r"\s+", " ", texto))[:600000], len(texto)))
            print("  texto %s (%d caracteres)" % (destino, len(texto)))
            n += 1
        except Exception as e:
            aviso("error convirtiendo %s: %s" % (fn, e))
    con.commit()
    return n


def filtrar_concesiones(filas, a):
    out = []
    for f in filas:
        if getattr(a, "importe_min", None) is not None and (f.get("importe") or 0) < a.importe_min:
            continue
        if getattr(a, "importe_max", None) is not None and (f.get("importe") or 0) > a.importe_max:
            continue
        out.append(f)
    return out


def cmd_concesiones(a, ctx):
    con, cli = ctx["con"], ctx["cli"]
    ruta = "concesiones/busqueda"
    p = construir_params(ruta, a, ctx)
    evento("concesiones params=%s" % _j(p))
    if a.exportar:
        return exportar_api(a, ctx, ruta, p)
    r = leer_paginas(a, ctx, ruta, p)
    items = r["items"]
    if not items and r.get("errores"):
        ctx["resumen"].update({"resultado": "error", "errores": r["errores"]})
        return 1
    guardar_concesiones(con, items)
    registrar_busqueda(con, "concesiones", ruta, p, len(items), r.get("total"), r.get("completa"), cli.modo)
    filas = filtrar_concesiones([conc_de_item(i) for i in items], a)
    ctx["resumen"].update({"resultados_api": len(items), "total_web": r.get("total"), "mostrados": len(filas), "completa": r.get("completa"), "params": p})
    mostrar_concesiones(filas, a, "%d concesiones mostradas (la web tiene %s con ese filtro%s)" % (
        len(filas), r.get("total") if r.get("total") is not None else "?", "" if r.get("completa") else "; incompleta: sube --max"))
    return 0


def cmd_otros(a, ctx):
    con, cli = ctx["con"], ctx["cli"]
    ruta = OTROS[a.tipo]
    p = construir_params(ruta, a, ctx)
    evento("otros %s params=%s" % (a.tipo, _j(p)))
    if a.exportar:
        return exportar_api(a, ctx, ruta, p)
    r = leer_paginas(a, ctx, ruta, p)
    items = r["items"]
    if not items and r.get("errores"):
        ctx["resumen"].update({"resultado": "error", "errores": r["errores"]})
        return 1
    filas = guardar_otros(con, a.tipo, items)
    registrar_busqueda(con, "otros:" + a.tipo, ruta, p, len(items), r.get("total"), r.get("completa"), cli.modo)
    ctx["resumen"].update({"resultados_api": len(items), "total_web": r.get("total"), "params": p})
    mostrar_otros(filas, a, "%d registros de %s (la web tiene %s)" % (len(items), a.tipo, r.get("total") if r.get("total") is not None else "?"))
    return 0


def cmd_catalogo(a, ctx):
    con, cli = ctx["con"], ctx["cli"] if ctx.get("cli") else None
    tipos = list(CATALOGOS) if a.tipo == "todos" else [a.tipo]
    if a.actualizar or any(con.execute("SELECT COUNT(*) FROM catalogos WHERE tipo=?", (t,)).fetchone()[0] == 0 for t in tipos):
        for t in tipos:
            print("Descargando catalogo %s..." % t, file=sys.stderr)
            n = traer_catalogo(cli, con, t, AMBITOS.get(a.ambito) if a.ambito else None)
            print("  %d valores" % n, file=sys.stderr)
    for t in tipos:
        sql, args = "SELECT grupo, codigo, descripcion, ruta, ancestros FROM catalogos WHERE tipo=?", [t]
        if t == "organos" and a.ambito:
            sql += " AND grupo=?"
            args.append(AMBITOS[a.ambito])
        if a.buscar:
            for w in norm(a.buscar).split():
                sql += " AND (desc_norm LIKE ? OR lower(ruta) LIKE ?)"
                args += ["%" + w + "%", "%" + w + "%"]
        sql += " ORDER BY ruta"
        filas = con.execute(sql, args).fetchall()
        print("\n%s (%d):" % (t.upper(), len(filas)))
        for f in filas[:a.max or None]:
            print("  %-8s %s%s%s" % (f["codigo"], ("[%s] " % f["grupo"]) if f["grupo"] else "", "  " * len([x for x in f["ancestros"].split(",") if x]), f["descripcion"]))
    return 0


def cmd_api(a, ctx):
    cli = ctx["cli"]
    if a.actualizar or not os.path.exists(SWAGGER_PATH):
        print("Descargando la descripcion de la API (swagger)...", file=sys.stderr)
        r = cli.get(SWAGGER_URL)
        ok = False
        if r.get("status") == 200 and r.get("texto"):
            try:
                json.loads(r["texto"])
                os.makedirs(DIR, exist_ok=True)
                with open(SWAGGER_PATH, "w", encoding="utf-8") as f:
                    f.write(r["texto"])
                ok = True
                print("Guardado en %s" % SWAGGER_PATH)
            except ValueError:
                pass
        if not ok:
            aviso("no pude descargar el swagger: HTTP %s %s" % (r.get("status"), r.get("error", "")))
            print("No pude descargar el swagger. Se guardo el diagnostico.")
            return 1
    rutas = swagger_rutas(cargar_swagger())
    if not rutas:
        print("El swagger guardado no tiene rutas legibles.")
        return 1
    if a.ruta:
        q = _ruta_norm(a.ruta)
        sel = {k: v for k, v in rutas.items() if k == q or q in k}
    else:
        sel = rutas
    for ruta in sorted(sel):
        for met, op in sel[ruta].items():
            print("\n%s /%s" % (met.upper(), ruta))
            if op["resumen"]:
                print(envolver(op["resumen"], ancho_terminal(), "  "))
            for p in op["params"]:
                extra = []
                if p["type"]:
                    extra.append(p["type"] + (("/" + p["formato"]) if p["formato"] else ""))
                if p["required"]:
                    extra.append("obligatorio")
                if p["enum"]:
                    extra.append("valores: " + ",".join(map(str, p["enum"])))
                print("    %-26s %s %s" % (p["name"], "(%s)" % ", ".join(extra) if extra else "", ("- " + p["desc"][:90]) if p["desc"] else ""))
    ctx["resumen"].update({"rutas": len(rutas)})
    return 0


def cmd_llamar(a, ctx):
    cli = ctx["cli"]
    p = parse_p(a.p)
    if a.vpd:
        p.setdefault("vpd", VPD)
    ruta = a.ruta
    validar_params(ruta, p)
    if a.todo:
        r = cli.paginar(ruta, p, tam=a.tam, limite=a.max)
        salida = json.dumps(r["items"], ensure_ascii=False, indent=1)
        ctx["resumen"].update({"items": len(r["items"]), "total": r.get("total")})
        if r["items"] and ruta.split("/")[0] in ("convocatorias", "concesiones"):
            pass
    else:
        r = cli.get(ruta, p, binario=a.binario)
        ctx["resumen"].update({"status": r.get("status")})
        if a.binario:
            if r.get("b64"):
                destino = a.salida or "salida.bin"
                with open(destino, "wb") as f:
                    f.write(base64.b64decode(r["b64"]))
                print("Guardado en %s (%d bytes)" % (destino, r.get("bytes", 0)))
                return 0
            print("Error: HTTP %s %s" % (r.get("status"), r.get("error", "")))
            return 1
        if r.get("status") != 200:
            print("Error: HTTP %s %s\n%s" % (r.get("status"), r.get("error", ""), (r.get("texto") or "")[:800]))
            return 1
        try:
            salida = json.dumps(json.loads(r["texto"]), ensure_ascii=False, indent=1)
            MUESTRAS.setdefault(ruta, r["texto"][:120000])
        except ValueError:
            salida = r.get("texto") or ""
    if a.salida:
        with open(a.salida, "w", encoding="utf-8") as f:
            f.write(salida)
        print("Guardado en %s" % a.salida)
    else:
        print(salida)
    return 0


# ----------------------------------------------------------------------------
# Consulta local (sin red)
# ----------------------------------------------------------------------------
def cmd_consultar(a, ctx):
    con = ctx["con"]
    desde, hasta = rango_fechas(a)
    palabras = norm(a.texto).split() if a.texto else []
    if a.que == "convocatorias":
        sql, args = "SELECT * FROM convocatorias WHERE 1=1", []
        for w in palabras:
            sql += " AND desc_norm LIKE ?"
            args.append("%" + w + "%")
        if a.organo:
            for w in norm(a.organo).split():
                sql += " AND (lower(organo1||' '||organo2||' '||organo3) LIKE ?)"
                args.append("%" + w + "%")
        for campo, val in (("regiones", a.region), ("finalidad", a.finalidad), ("beneficiarios", a.beneficiario_tipo), ("instrumentos", a.instrumento)):
            if val:
                sql += " AND lower(%s) LIKE ?" % campo
                args.append("%" + val.lower() + "%")
        if a.numero:
            sql += " AND num=?"
            args.append(a.numero)
        if desde:
            sql += " AND fecha>=?"
            args.append(desde.isoformat())
        if hasta:
            sql += " AND fecha<=?"
            args.append(hasta.isoformat())
        sql += " ORDER BY fecha DESC, CAST(num AS INTEGER) DESC"
        filas = [dict(f) for f in con.execute(sql, args).fetchall()]
        if a.organo:
            pass
        filas = aplicar_filtros_locales(filas, a)
        if a.max:
            filas = filas[:a.max]
        mostrar_convocatorias(filas, a, "%d convocatorias en la base local" % len(filas))
    elif a.que == "concesiones":
        sql, args = "SELECT * FROM concesiones WHERE 1=1", []
        for w in palabras:
            sql += " AND (benef_norm LIKE ? OR lower(convocatoria) LIKE ?)"
            args += ["%" + w + "%", "%" + w + "%"]
        if a.beneficiario:
            sql += " AND (nif=? OR benef_norm LIKE ?)"
            args += [a.beneficiario.upper(), "%" + norm(a.beneficiario) + "%"]
        if a.organo:
            sql += " AND lower(organo1||' '||organo2||' '||organo3) LIKE ?"
            args.append("%" + a.organo.lower() + "%")
        if a.numero:
            sql += " AND num_conv=?"
            args.append(a.numero)
        if desde:
            sql += " AND fecha>=?"
            args.append(desde.isoformat())
        if hasta:
            sql += " AND fecha<=?"
            args.append(hasta.isoformat())
        sql += " ORDER BY fecha DESC"
        filas = filtrar_concesiones([dict(f) for f in con.execute(sql, args).fetchall()], a)
        if a.max:
            filas = filas[:a.max]
        mostrar_concesiones(filas, a, "%d concesiones en la base local" % len(filas))
    elif a.que == "textos":
        sql, args = "SELECT num, nombre, caracteres, texto_norm FROM textos WHERE 1=1", []
        for w in palabras:
            sql += " AND texto_norm LIKE ?"
            args.append("%" + w + "%")
        filas = con.execute(sql + " ORDER BY num DESC", args).fetchall()
        print("%d documentos con ese texto en la base local" % len(filas))
        for f in filas[:a.max or None]:
            tn = f["texto_norm"]
            i = tn.find(palabras[0]) if palabras else 0
            print("  BDNS %s | %s | ...%s..." % (f["num"], f["nombre"], tn[max(0, i - 60):i + 100]))
    else:
        sql, args = "SELECT * FROM otros WHERE 1=1", []
        if a.que != "otros":
            sql += " AND tipo=?"
            args.append(a.que)
        for w in palabras:
            sql += " AND titulo_norm LIKE ?"
            args.append("%" + w + "%")
        if desde:
            sql += " AND fecha>=?"
            args.append(desde.isoformat())
        if hasta:
            sql += " AND fecha<=?"
            args.append(hasta.isoformat())
        sql += " ORDER BY fecha DESC"
        filas = [dict(f) for f in con.execute(sql, args).fetchall()]
        if a.max:
            filas = filas[:a.max]
        mostrar_otros(filas, a, "%d registros en la base local" % len(filas))
    return 0


def cmd_estado(a, ctx):
    con = ctx["con"]
    print("subv.py %s - datos en %s" % (VERSION, DIR))
    for t, et in (("convocatorias", "convocatorias"), ("concesiones", "concesiones"), ("otros", "otros registros"), ("catalogos", "valores de catalogo"), ("docs", "documentos")):
        print("  %-18s %d" % (et, con.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]))
    r = con.execute("SELECT COUNT(*), SUM(raw_detalle IS NOT NULL) FROM convocatorias").fetchone()
    print("  convocatorias con detalle: %s" % (r[1] or 0))
    r = con.execute("SELECT MIN(fecha), MAX(fecha) FROM convocatorias WHERE fecha<>''").fetchone()
    if r[0]:
        print("  fechas de registro guardadas: %s a %s" % (f_es(r[0]), f_es(r[1])))
    print("  swagger guardado: %s" % ("si" if os.path.exists(SWAGGER_PATH) else "no (subv.py api --actualizar)"))
    print("  extension: puerto %d (solo si la conexion directa no funciona)" % a.puerto)
    return 0


def cmd_historial(a, ctx):
    con = ctx["con"]
    filas = con.execute("SELECT * FROM busquedas ORDER BY id DESC LIMIT ?", (a.max or 20,)).fetchall()
    for f in filas:
        print("%s | %-14s | %s resultados de %s | %s | %s" % (f["cuando"], f["comando"], f["n"], f["total"] if f["total"] is not None else "?",
                                                              "completa" if f["completa"] else "incompleta", f["params"]))
    return 0


def cmd_reprocesar(a, ctx):
    """Reconstruye las columnas a partir del JSON guardado (util si se corrigen nombres de campos)."""
    con = ctx["con"]
    n = 0
    for f in con.execute("SELECT num, raw_busqueda, raw_detalle FROM convocatorias").fetchall():
        for col, det in (("raw_busqueda", False), ("raw_detalle", True)):
            if f[col]:
                it = json.loads(f[col])
                it.setdefault("numeroConvocatoria", f["num"])
                guardar_convocatorias(con, [it], detalle=det)
        n += 1
    filas = con.execute("SELECT raw FROM concesiones").fetchall()
    guardar_concesiones(con, [json.loads(f["raw"]) for f in filas])
    print("Reprocesadas %d convocatorias y %d concesiones." % (n, len(filas)))
    return 0


def cmd_config(a, ctx):
    cfg = leer_config()
    if a.via:
        cfg["via"] = a.via
    if a.puerto_nuevo:
        cfg["puerto"] = a.puerto_nuevo
    if a.pdf2txt:
        cfg["pdf2txt"] = os.path.abspath(os.path.expanduser(a.pdf2txt))
    if a.via or a.puerto_nuevo or a.pdf2txt:
        guardar_config(cfg)
    print("Configuracion (%s):" % CONFIG)
    print("  via: %s   (auto = directo si funciona, si no la extension; directo; extension)" % (cfg.get("via") or "auto"))
    print("  puerto de la extension: %s" % (cfg.get("puerto") or PUERTO_DEF))
    print("  pdf2txt.py: %s" % (cfg.get("pdf2txt") or "(no indicado: se usa pdftotext)"))
    return 0


def cmd_informe(a, ctx):
    os.makedirs(DIAG_DIR, exist_ok=True)
    zs = sorted(f for f in os.listdir(DIAG_DIR) if f.startswith("diag_") and f.endswith(".zip"))
    if not zs:
        print("Todavia no hay informes en %s." % DIAG_DIR)
        return 1
    destino = os.path.join(DIR, "subv_informe_%s.zip" % dt.datetime.now().strftime("%Y%m%d_%H%M%S"))
    con = ctx["con"]
    with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED) as z:
        for f in zs[-a.ultimos:]:
            z.write(os.path.join(DIAG_DIR, f), f)
        h = con.execute("SELECT * FROM busquedas ORDER BY id DESC LIMIT 100").fetchall()
        z.writestr("historial.json", json.dumps([dict(x) for x in h], ensure_ascii=False, indent=1))
        z.writestr("entorno.txt", "subv.py %s\nPython %s\n%s\nSwagger: %s\n" % (VERSION, sys.version, platform.platform(), os.path.exists(SWAGGER_PATH)))
        if os.path.exists(SWAGGER_PATH):
            z.write(SWAGGER_PATH, "swagger.json")
    print("Informe listo: %s (incluye los ultimos %d diagnosticos). Enviamelo." % (destino, min(a.ultimos, len(zs))))
    return 0


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
def comunes(p, red=True):
    g = p.add_argument_group("opciones comunes")
    if red:
        g.add_argument("--via", choices=["auto", "directo", "extension"], default=VIA_DEF,
                       help="auto: directo si funciona, si no la extension de Firefox (por defecto: %s; cambiar con 'subv.py config --via ...')" % VIA_DEF)
        g.add_argument("--puerto", type=int, default=PUERTO_CFG, help="puerto local para la extension (%d)" % PUERTO_CFG)
        g.add_argument("--espejo", action="store_true", help="usar el espejo pap.hacienda.gob.es en vez de infosubvenciones.es")
        g.add_argument("--tam", type=int, default=200, help="resultados por pagina pedidos a la API (200)")
    g.add_argument("--formato", choices=["lista", "tabla", "json", "jsonl", "csv"], default="lista", help="formato de salida (lista)")
    g.add_argument("--guardar", metavar="ARCHIVO", help="escribir la salida en un archivo en vez de la pantalla")


def fechas_args(p):
    g = p.add_argument_group("fechas")
    g.add_argument("-f", "--fecha", type=fecha_arg, help="un solo dia")
    g.add_argument("--desde", type=fecha_arg, help="fecha inicial (dd-mm-aaaa)")
    g.add_argument("--hasta", type=fecha_arg, help="fecha final")
    g.add_argument("--anio", type=int, help="un año completo")
    g.add_argument("--ultimos", type=int, help="los ultimos N dias")
    g.add_argument("--3meses", dest="tres_meses", action="store_true", help="desde hace 3 meses hasta hoy")
    g.add_argument("--6meses", dest="seis_meses", action="store_true", help="desde hace 6 meses hasta hoy")
    g.add_argument("--12meses", dest="doce_meses", action="store_true", help="desde hace 12 meses hasta hoy")


def filtros_api(p, convocatorias=False):
    """Filtros comunes a buscar / concesiones / otros. Se traducen a los parametros reales de la API
    y se comprueban contra el swagger (los que la ruta no admite se avisan y se ignoran)."""
    g = p.add_argument_group("filtros de la API")
    g.add_argument("-c", "--texto", help="palabras del titulo (sin acentos: 'formacion', no 'formación')")
    g.add_argument("--modo", choices=list(MODOS), help="como combinar las palabras de -c: todas (por defecto), alguna o frase exacta")
    g.add_argument("-n", "--numero", help="numero de convocatoria BDNS")
    g.add_argument("--cod-concesion", help="codigo de concesion (SB...)")
    g.add_argument("--ambito", choices=list(AMBITOS), help="tipo de administracion: estatal, autonomica, local u otros")
    g.add_argument("-e", "--organo", help="organo (nombre o id; varios con comas). Un ministerio incluye sus direcciones generales")
    g.add_argument("--region", help="region de impacto (nombre o id): 'madrid', 'castilla y leon'...")
    g.add_argument("--beneficiario-tipo", help="tipo de beneficiario (nombre o id): pyme, gran empresa, personas fisicas...")
    g.add_argument("--instrumento", help="instrumento de ayuda: subvencion, prestamo, garantia...")
    g.add_argument("--finalidad", help="finalidad / politica de gasto: empleo, vivienda... (un solo valor)")
    g.add_argument("--actividad", help="actividad (CNAE) del beneficiario (concesiones, ayudas de Estado, minimis, sanciones)")
    g.add_argument("--objetivo", help="objetivo de la ayuda (ayudas de Estado)")
    g.add_argument("--reglamento", help="reglamento UE (ayudas de Estado, minimis)")
    g.add_argument("--producto", help="sector de productos (minimis)")
    g.add_argument("--ayuda-estado", help="numero SA de ayuda de Estado (solo cifras)")
    g.add_argument("-b", "--beneficiario", help="NIF/CIF del beneficiario, o su nombre (se busca en la BDNS)")
    g.add_argument("--anios", help="grandes beneficiarios: años separados por comas")
    g.add_argument("--vigencia-desde", type=int, help="planes estrategicos: año inicial de vigencia")
    g.add_argument("--vigencia-hasta", type=int, help="planes estrategicos: año final de vigencia")
    g.add_argument("--mrr", action="store_true", help="solo convocatorias del Plan de Recuperacion (MRR)")
    g.add_argument("--contribucion", action="store_true", help="solo convocatorias de contribucion internacional")
    g.add_argument("--reg-desde", type=fecha_arg, help="fecha de REGISTRO en la BDNS desde (concesiones y similares)")
    g.add_argument("--reg-hasta", type=fecha_arg, help="fecha de REGISTRO en la BDNS hasta")
    g.add_argument("--orden", metavar="CAMPO[:asc|desc]", help="orden de la API (campos segun la ruta: ver subv.py api)")
    g.add_argument("--p", action="append", metavar="NOMBRE=VALOR", help="cualquier parametro de la API con su nombre real (repetible)")
    g.add_argument("--exportar", choices=["csv", "xlsx", "pdf"], help="que la propia web genere el archivo con los resultados (se guarda en ~/subv/export)")
    h = p.add_argument_group("filtros locales (piden el detalle de cada convocatoria)")
    h.add_argument("--abiertas", action="store_true", help="solo con plazo de solicitud abierto hoy (o indefinido)")
    h.add_argument("--incluir-sin-plazo", action="store_true", help="con --abiertas: incluir las que no indican fechas")
    h.add_argument("--importe-min", type=float, help="importe minimo (presupuesto de la convocatoria, o importe de la concesion)")
    h.add_argument("--importe-max", type=float, help="importe maximo")
    h.add_argument("--sin", action="append", metavar="PALABRA", help="excluir si la descripcion contiene la palabra (repetible)")
    h.add_argument("--detalle", action="store_true", help="bajar y mostrar el detalle (plazo, presupuesto, beneficiarios...)")
    h.add_argument("--max-detalle", type=int, default=300, help="maximo de detalles a bajar (300; 0 = todos)")
    p.add_argument("--max", type=int, default=200, help="maximo de resultados a leer (200; 0 = todos)")


def main():
    ap = argparse.ArgumentParser(prog="subv.py", description="Buscador de la Base de Datos Nacional de Subvenciones (API abierta). Ver subvManualBono.txt.",
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version="subv.py " + VERSION)
    sp = ap.add_subparsers(dest="cmd", required=True)

    b = sp.add_parser("buscar", help="buscar convocatorias de subvenciones")
    filtros_api(b, True)
    fechas_args(b)
    comunes(b)
    b.set_defaults(fn=cmd_buscar, red=True)

    u = sp.add_parser("ultimas", help="ultimas convocatorias publicadas")
    filtros_api(u, True)
    u.set_defaults(max=50)
    comunes(u)
    u.set_defaults(fn=cmd_ultimas, red=True)

    d = sp.add_parser("detalle", help="detalle completo de una o varias convocatorias (por numero BDNS)")
    d.add_argument("numeros", nargs="+", help="numeros BDNS (varios, o separados por comas)")
    d.add_argument("--json", action="store_true", help="mostrar el JSON completo de la API")
    comunes(d)
    d.set_defaults(fn=cmd_detalle, red=True)

    dc = sp.add_parser("documentos", help="lista y descarga los documentos de una convocatoria")
    dc.add_argument("numero")
    dc.add_argument("--descargar", action="store_true", help="descarga los documentos adjuntos a ~/subv/docs/<numero>/")
    dc.add_argument("--pdf", action="store_true", help="descarga el PDF de la convocatoria")
    dc.add_argument("--zip", action="store_true", help="descarga todos los documentos de la convocatoria en un .zip")
    dc.add_argument("--txt", action="store_true", help="pasa a texto los PDF bajados (tu pdf2txt.py o pdftotext) y los guarda para buscar")
    dc.add_argument("--pdf2txt", metavar="RUTA", help="ruta de tu pdf2txt.py (se recuerda)")
    comunes(dc)
    dc.set_defaults(fn=cmd_documentos, red=True)

    c = sp.add_parser("concesiones", help="concesiones de subvenciones (quien las recibio)")
    filtros_api(c)
    fechas_args(c)
    comunes(c)
    c.set_defaults(fn=cmd_concesiones, red=True)

    o = sp.add_parser("otros", help="ayudas de Estado, minimis, sanciones, partidos, grandes beneficiarios, planes estrategicos")
    o.add_argument("tipo", choices=list(OTROS))
    filtros_api(o)
    fechas_args(o)
    comunes(o)
    o.set_defaults(fn=cmd_otros, red=True)

    k = sp.add_parser("catalogo", help="ver/buscar catalogos (organos, regiones, finalidades, instrumentos...)")
    k.add_argument("tipo", choices=list(CATALOGOS) + ["todos"])
    k.add_argument("-b", "--buscar", help="palabras a buscar")
    k.add_argument("--ambito", choices=list(AMBITOS), help="organos: solo de este ambito")
    k.add_argument("--actualizar", action="store_true", help="volver a descargarlo")
    k.add_argument("--max", type=int, default=0)
    comunes(k)
    k.set_defaults(fn=cmd_catalogo, red=True)

    ai = sp.add_parser("api", help="endpoints y parametros reales de la API (swagger)")
    ai.add_argument("ruta", nargs="?", help="filtrar por ruta (p. ej. convocatorias/busqueda)")
    ai.add_argument("--actualizar", action="store_true", help="descargar de nuevo el swagger")
    comunes(ai)
    ai.set_defaults(fn=cmd_api, red=True)

    ll = sp.add_parser("llamar", help="llamada libre a cualquier ruta de la API")
    ll.add_argument("ruta", help="p. ej. convocatorias/busqueda")
    ll.add_argument("--p", action="append", metavar="NOMBRE=VALOR")
    ll.add_argument("--vpd", action="store_true", help="añadir vpd=GE")
    ll.add_argument("--todo", action="store_true", help="recorrer todas las paginas")
    ll.add_argument("--max", type=int, default=0)
    ll.add_argument("--binario", action="store_true")
    ll.add_argument("--salida", metavar="ARCHIVO")
    comunes(ll)
    ll.set_defaults(fn=cmd_llamar, red=True)

    q = sp.add_parser("consultar", help="buscar en la base LOCAL (sin conexion)")
    q.add_argument("que", choices=["convocatorias", "concesiones", "otros", "textos"] + list(OTROS))
    q.add_argument("-c", "--texto")
    q.add_argument("-e", "--organo")
    q.add_argument("-b", "--beneficiario", help="concesiones: NIF o nombre")
    q.add_argument("-n", "--numero")
    q.add_argument("--region")
    q.add_argument("--finalidad")
    q.add_argument("--beneficiario-tipo")
    q.add_argument("--instrumento")
    q.add_argument("--abiertas", action="store_true")
    q.add_argument("--incluir-sin-plazo", action="store_true")
    q.add_argument("--importe-min", type=float)
    q.add_argument("--importe-max", type=float)
    q.add_argument("--sin", action="append")
    q.add_argument("--max", type=int, default=0)
    fechas_args(q)
    comunes(q, red=False)
    q.set_defaults(fn=cmd_consultar, red=False)

    for nombre, fn, ayuda in (("estado", cmd_estado, "resumen de la base local"), ("historial", cmd_historial, "busquedas hechas"),
                              ("reprocesar", cmd_reprocesar, "reconstruir columnas desde el JSON guardado")):
        s = sp.add_parser(nombre, help=ayuda)
        s.add_argument("--max", type=int, default=20)
        s.add_argument("--puerto", type=int, default=PUERTO_DEF)
        s.set_defaults(fn=fn, red=False)

    cf = sp.add_parser("config", help="ver o cambiar la configuracion guardada (via, puerto, pdf2txt)")
    cf.add_argument("--via", choices=["auto", "directo", "extension"])
    cf.add_argument("--puerto-nuevo", type=int, metavar="PUERTO", help="puerto local de la extension")
    cf.add_argument("--pdf2txt", metavar="RUTA")
    cf.set_defaults(fn=cmd_config, red=False)

    inf = sp.add_parser("informe", help="juntar los ultimos diagnosticos en un .zip para enviarlos")
    inf.add_argument("--ultimos", type=int, default=5)
    inf.set_defaults(fn=cmd_informe, red=False)

    a = ap.parse_args()
    os.makedirs(DIR, exist_ok=True)
    con = abrir_db()
    cli = nuevo_cliente(a) if a.red else None
    ctx = {"con": con, "cli": cli, "resumen": {}}
    comando = a.cmd
    codigo = 1
    tb = None
    try:
        codigo = a.fn(a, ctx)
    except ValueError as e:
        print("Error: %s" % e)
        evento("ValueError %s" % e)
        ctx["resumen"]["error"] = str(e)
        codigo = 2
    except KeyboardInterrupt:
        print("\nInterrumpido.")
        codigo = 130
    except BrokenPipeError:
        codigo = 0
    except Exception:
        import traceback
        tb = traceback.format_exc()
        print(tb)
        codigo = 1
    finally:
        if cli:
            cli.cerrar()
    if a.red:
        ruta = guardar_bundle(comando, sys.argv, cli, ctx["resumen"], tb)
        if ruta and (codigo != 0 or AVISOS):
            print("Diagnostico guardado: %s  (envialo con: subv.py informe)" % ruta, file=sys.stderr)
    return codigo


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
