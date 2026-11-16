#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fuentes_ia.py  v1.0  -  Fuentes publicas mas alla de la CNMV (Termux / Linux).

Subcomandos:
  borme     Actos del BORME (Registro Mercantil) filtrados por palabras o nombres.
  concursal Atajo de borme con palabras concursales (el Registro Publico
            Concursal NO se consulta por robots/aviso legal; ver manual).
  sec       Documentos recientes (8-K, 10-Q, 10-K, S-1, 424B...) y cifras de deuda
            en SEC EDGAR por ticker o CIK.
  red       Mapa/listado de acceso de demanda de Red Electrica (REE): descarga o
            lee un fichero xlsx/csv bajado a mano y filtra por CCAA y MW.
  boa       Boletin Oficial de Aragon por su API de datos abiertos (BOA y BOP).
  estado    Resumen de la base local y de los ficheros de diagnostico.

Reglas: solo fuentes oficiales/publicas, User-Agent honesto, pausas entre
peticiones, sin saltarse bloqueos (si una web bloquea, se anota el motivo y se
indica la via manual).  Todo se guarda en ~/fuentes_ia/ (SQLite + diagnosticos).
"""
import argparse
import csv
import datetime as dt
import html
import io
import json
import logging
import os
import re
import sqlite3
import sys
import textwrap
import time
import unicodedata
import xml.etree.ElementTree as ET

import requests

VERSION = "1.3"
HOME_DIR = os.environ.get("FUENTES_IA_DIR") or os.path.join(os.path.expanduser("~"), "fuentes_ia")
DB_PATH = os.path.join(HOME_DIR, "fuentes.db")
DIAG_DIR = os.path.join(HOME_DIR, "diagnostico")
DESC_DIR = os.path.join(HOME_DIR, "descargas")
CONF_PATH = os.path.join(HOME_DIR, "config.json")
ANCHO = 100
PAUSA_S = 0.4
TIMEOUT = 30
MAX_DUMPS = 40

BORME_SUMARIO = "https://www.boe.es/datosabiertos/api/borme/sumario/{f}"
BORME_XML = "https://www.boe.es/diario_borme/xml.php?id={id}"
SEC_SUBM = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_FACTS = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
SEC_DOC = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/{doc}"
REE_PAGINAS = [
    "https://www.ree.es/es/clientes/consumidor/acceso-conexion/conoce-el-estado-de-las-solicitudes",
    "https://www.ree.es/es/clientes/generador/acceso-conexion/conoce-la-capacidad-de-acceso",
]
BOA_BASE = "https://www.boa.aragon.es/cgi-bin/EBOA/BRSCGI"

# Palabras por defecto (se comparan sin tildes y en minusculas)
PALABRAS_CONCURSAL = ["concurso", "insolvencia", "administrador concursal",
                      "liquidacion", "disolucion", "extincion", "preconcurso",
                      "plan de reestructuracion", "reestructuracion"]
PALABRAS_INFRA = ["centro de datos", "data center", "datacenter", "cpd",
                  "computacion", "nube", "cloud", "hosting", "servidores",
                  "inteligencia artificial", "fotovoltaic", "bateria", "almacenamiento"]

# CIK conocidos (10 digitos); cualquier otro: --cik
CIK_CONOCIDOS = {
    "CRWV": "0001769628",   # CoreWeave
}

log = logging.getLogger("fuentes_ia")
_dumps = 0


# ------------------------------------------------------------------ utilidades
def norm(s):
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s).lower().strip()


def fallar(msg, codigo=2):
    """Los errores van a la salida normal (para que queden en tus capturas) y al log."""
    print("ERROR: " + msg)
    log.error(msg)
    raise SystemExit(codigo)


def asegurar_dirs():
    for d in (HOME_DIR, DIAG_DIR, DESC_DIR):
        os.makedirs(d, exist_ok=True)


def cargar_conf():
    try:
        with open(CONF_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def user_agent():
    """UA honesto. El contacto (SEC lo pide) es opcional: config.json
    {"contacto": "..."} o variable FUENTES_CONTACTO. Nunca se inventa."""
    contacto = os.environ.get("FUENTES_CONTACTO") or cargar_conf().get("contacto")
    base = "fuentes_ia/%s (herramienta personal de lectura de fuentes publicas)" % VERSION
    return base + (" contacto: %s" % contacto if contacto else "")


def nueva_sesion():
    s = requests.Session()
    s.headers.update({"User-Agent": user_agent(),
                      "Accept-Language": "es-ES,es;q=0.9,en;q=0.5"})
    return s


def dump(nombre, contenido, binario=False):
    """Guarda diagnostico (maximo MAX_DUMPS por ejecucion)."""
    global _dumps
    if _dumps >= MAX_DUMPS:
        return None
    _dumps += 1
    asegurar_dirs()
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    ruta = os.path.join(DIAG_DIR, "%s_%s" % (ts, nombre))
    with open(ruta, "wb" if binario else "w", **({} if binario else {"encoding": "utf-8"})) as f:
        f.write(contenido)
    log.info("Diagnostico: %s", ruta)
    return ruta


def get(sesion, url, accept=None, reintentos=2, **kw):
    """GET con reintentos en timeout/5xx.  Devuelve (resp|None, motivo)."""
    hdr = {"Accept": accept} if accept else {}
    ultimo = ""
    for i in range(reintentos + 1):
        try:
            r = sesion.get(url, headers=hdr, timeout=TIMEOUT, **kw)
            if r.status_code in (500, 502, 503, 504) and i < reintentos:
                ultimo = "HTTP %d" % r.status_code
                time.sleep(2 * (i + 1))
                continue
            return r, ""
        except requests.exceptions.RequestException as e:
            ultimo = "%s: %s" % (type(e).__name__, str(e)[:120])
            time.sleep(1.5 * (i + 1))
    return None, ultimo


def bloqueo(r):
    """Detecta pagina de proteccion antibot (no se intenta saltar)."""
    if r is None:
        return True
    if r.status_code in (401, 403, 429):
        return True
    t = (r.text[:3000] if "text" in r.headers.get("Content-Type", "") else "").lower()
    return "incapsula" in t or "_incapsula_resource" in t or "access denied" in t


def db_conectar():
    asegurar_dirs()
    c = sqlite3.connect(DB_PATH)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS hallazgos(
      id INTEGER PRIMARY KEY, fuente TEXT, clave TEXT UNIQUE, fecha TEXT,
      entidad TEXT, resumen TEXT, url TEXT, etiquetas TEXT, visto TEXT);
    CREATE INDEX IF NOT EXISTS ix_fuente ON hallazgos(fuente, fecha);
    """)
    return c


def guardar(c, fuente, clave, fecha, entidad, resumen, url="", etiquetas=""):
    """True si es nuevo."""
    try:
        c.execute("INSERT INTO hallazgos(fuente,clave,fecha,entidad,resumen,url,etiquetas,visto)"
                  " VALUES(?,?,?,?,?,?,?,?)",
                  (fuente, clave, fecha, entidad, resumen, url, etiquetas,
                   dt.datetime.now().isoformat(timespec="seconds")))
        c.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def imprimir_bloque(titulo, lineas, nuevo=True):
    marca = "NUEVO " if nuevo else "visto "
    print("%s%s" % (marca, titulo))
    for ln in lineas:
        for w in textwrap.wrap(ln, ANCHO - 6) or [""]:
            print("      " + w)


def parse_fecha(s, defecto=None):
    if not s:
        return defecto
    for f in ("%Y-%m-%d", "%d/%m/%Y", "%Y%m%d", "%d-%m-%Y"):
        try:
            return dt.datetime.strptime(s, f).date()
        except ValueError:
            pass
    fallar("Fecha no valida: %r (usa AAAA-MM-DD o DD/MM/AAAA)" % s)


def lista_csv(s):
    return [x.strip() for x in (s or "").split(",") if x.strip()]


# ------------------------------------------------------------------ BORME
def borme_sumario(ses, dia):
    r, motivo = get(ses, BORME_SUMARIO.format(f=dia.strftime("%Y%m%d")),
                    accept="application/json")
    if r is None:
        return None, motivo
    if r.status_code == 404:
        return [], "sin BORME ese dia"
    if r.status_code != 200:
        return None, "HTTP %d" % r.status_code
    try:
        d = r.json()
    except ValueError:
        dump("borme_sumario_%s_noJSON.txt" % dia.strftime("%Y%m%d"), r.text)
        return None, "respuesta no JSON"
    items = []
    diarios = d.get("data", {}).get("sumario", {}).get("diario", [])
    if isinstance(diarios, dict):
        diarios = [diarios]
    for dia_ in diarios:
        secs = dia_.get("seccion", [])
        if isinstance(secs, dict):
            secs = [secs]
        for sec in secs:
            its = sec.get("item", [])
            if isinstance(its, dict):
                its = [its]
            for it in its:
                items.append({"seccion": sec.get("codigo", ""),
                              "id": it.get("identificador", ""),
                              "titulo": it.get("titulo", ""),
                              "pdf": (it.get("url_pdf") or {}).get("texto", "")})
    return items, ""


def borme_articulos(xml_texto):
    """Devuelve [(num, empresa, texto)] de un item XML del BORME-A."""
    try:
        raiz = ET.fromstring(xml_texto.encode("utf-8") if isinstance(xml_texto, str) else xml_texto)
    except ET.ParseError:
        return []
    out, actual = [], None
    for p in raiz.iter("p"):
        cl = p.get("class", "")
        txt = "".join(p.itertext()).strip()
        if cl == "articulo":
            m = re.match(r"(\d+)\s*-\s*(.+)", txt)
            actual = [m.group(1), m.group(2).strip(), []] if m else ["", txt, []]
            out.append(actual)
        elif cl.startswith("parrafo") and actual is not None:
            actual[2].append(txt)
    return [(n, e, " ".join(t)) for n, e, t in out]


def _palabra(n, t):
    """Coincidencia como palabra completa (evita ACS dentro de ISAACSON)."""
    return re.search(r"(?<![a-z0-9])" + re.escape(norm(n)) + r"(?![a-z0-9])", t) is not None


def coincide(texto, palabras, nombres, empresa=""):
    t = norm(texto)
    e = norm(empresa)
    hit_p = [p for p in palabras if norm(p) in t]
    hit_n = []
    for n in nombres:
        if _palabra(n, e):
            hit_n.append(n + "(empresa)")
        elif _palabra(n, t):
            hit_n.append(n + "(texto)")
    return hit_p, hit_n


def cmd_borme(args, palabras_def=None):
    ses, c = nueva_sesion(), db_conectar()
    palabras = lista_csv(args.palabras) or (palabras_def or PALABRAS_CONCURSAL)
    nombres = lista_csv(args.nombre)
    hoy = dt.date.today()
    desde = parse_fecha(args.desde, hoy - dt.timedelta(days=args.dias - 1))
    hasta = parse_fecha(args.hasta, hoy)
    seccion = args.seccion.upper()
    provs = [norm(p) for p in lista_csv(args.provincia)]
    print("BORME %s a %s | seccion %s | palabras: %s%s" % (
        desde, hasta, seccion, ", ".join(palabras),
        (" | nombres: " + ", ".join(nombres)) if nombres else ""))
    if args.solo_nombres and not nombres:
        fallar("--solo-nombres requiere --nombre")
    tot_items = tot_art = tot_hit = nuevos = 0
    d = desde
    while d <= hasta:
        if d.weekday() == 6:      # domingo: no hay BORME
            d += dt.timedelta(days=1)
            continue
        items, motivo = borme_sumario(ses, d)
        time.sleep(PAUSA_S)
        if items is None:
            print("  %s: no se pudo leer el sumario (%s)" % (d, motivo))
        elif not items:
            print("  %s: %s" % (d, motivo or "sin items"))
        else:
            sel = [i for i in items if i["seccion"] == seccion]
            if provs:
                sel = [i for i in sel if any(p in norm(i["titulo"]) for p in provs)]
            print("  %s: %d items en seccion %s" % (d, len(sel), seccion))
            for it in sel:
                r, motivo = get(ses, BORME_XML.format(id=it["id"]))
                time.sleep(PAUSA_S)
                tot_items += 1
                if r is None or r.status_code != 200:
                    print("    %s: fallo (%s)" % (it["id"], motivo or r.status_code))
                    continue
                for num, emp, txt in borme_articulos(r.content.decode("utf-8", "replace")):
                    tot_art += 1
                    hp, hn = coincide(emp + " " + txt, palabras, nombres, emp)
                    if args.nombre_en_empresa:
                        hn = [x for x in hn if x.endswith("(empresa)")]
                    ok = bool(hn) if args.solo_nombres else bool(hp or hn)
                    if not ok:
                        continue
                    tot_hit += 1
                    clave = "borme|%s|%s" % (it["id"], num)
                    etiquetas = ",".join(hp + hn)
                    nuevo = guardar(c, "borme", clave, d.isoformat(), emp, txt,
                                    it["pdf"], etiquetas)
                    nuevos += nuevo
                    imprimir_bloque("%s  %s  [%s]" % (d, emp, it["titulo"]),
                                    ["Acto %s | coincide: %s" % (num, etiquetas), txt[:args.largo]],
                                    nuevo)
        d += dt.timedelta(days=1)
    print("Resumen BORME: %d items leidos, %d actos revisados, %d coincidencias (%d nuevas)."
          % (tot_items, tot_art, tot_hit, nuevos))
    if tot_items and not tot_art:
        print("AVISO: se leyeron items pero 0 actos: el formato XML puede haber cambiado.")
    return 0


def cmd_concursal(args):
    print("Registro Publico Concursal: su web (publicoconcursal.es) prohibe el acceso automatizado\n"
          "en su robots.txt, asi que esta herramienta NO la consulta.  Se usa el BORME (actos\n"
          "mercantiles: concurso, disolucion, extincion...) y la consulta manual, ver manual.\n")
    return cmd_borme(args)


# ------------------------------------------------------------------ SEC
def sec_cik(args):
    if args.cik:
        return args.cik.zfill(10)
    t = (args.ticker or "").upper()
    if t in CIK_CONOCIDOS:
        return CIK_CONOCIDOS[t]
    fallar("Ticker %r no esta en la lista local %s. Usa --cik NUMERO "
           "(se ve en la URL de EDGAR)." % (t, ", ".join(CIK_CONOCIDOS)))


def hechos_deuda(facts):
    """Ultimos valores de conceptos de deuda/caja (us-gaap)."""
    conceptos = ["LongTermDebt", "LongTermDebtNoncurrent", "DebtInstrumentCarryingAmount",
                 "DebtCurrent", "Liabilities", "CashAndCashEquivalentsAtCarryingValue",
                 "Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                 "InterestExpense", "NetCashProvidedByUsedInOperatingActivities",
                 "PaymentsToAcquirePropertyPlantAndEquipment"]
    out = []
    ug = facts.get("facts", {}).get("us-gaap", {})
    for k in conceptos:
        u = ug.get(k, {}).get("units", {}).get("USD")
        if not u:
            continue
        u = sorted(u, key=lambda x: (x.get("end", ""), x.get("filed", "")))
        v = u[-1]
        out.append((k, v.get("end", ""), v.get("form", ""), v.get("val", 0), v.get("fp", "")))
    return out


def cmd_sec(args):
    cik = sec_cik(args)
    ses, c = nueva_sesion(), db_conectar()
    if not (os.environ.get("FUENTES_CONTACTO") or cargar_conf().get("contacto")):
        print("Nota: la SEC pide un contacto en el User-Agent.  No se usa ningun dato personal.\n"
              "      Si quieres, define FUENTES_CONTACTO (ver manual).  Continuo con UA generico.")
    r, motivo = get(ses, SEC_SUBM.format(cik=cik), accept="application/json")
    if r is None or r.status_code != 200:
        if r is not None:
            dump("sec_subm_%s_http%d.txt" % (cik, r.status_code), r.text[:20000])
        fallar("SEC no responde bien: %s %s" % (motivo, r.status_code if r else ""))
    d = r.json()
    nombre = d.get("name", cik)
    print("SEC | %s | CIK %s | tickers %s | ejercicio fiscal acaba %s" % (
        nombre, cik, ",".join(d.get("tickers", [])), d.get("fiscalYearEnd", "?")))
    rec = d.get("filings", {}).get("recent", {})
    forms = lista_csv(args.formularios.upper())
    desde = dt.date.today() - dt.timedelta(days=args.dias)
    n = nuevos = 0
    print("Formularios %s, ultimos %d dias:" % (",".join(forms), args.dias))
    for i, form in enumerate(rec.get("form", [])):
        fecha = rec["filingDate"][i]
        if dt.date.fromisoformat(fecha) < desde:
            break
        if not any(form == f or form.startswith(f) for f in forms):
            continue
        acc = rec["accessionNumber"][i]
        doc = rec["primaryDocument"][i]
        url = SEC_DOC.format(cik_int=int(cik), acc_nodash=acc.replace("-", ""), doc=doc)
        desc = rec.get("primaryDocDescription", [""] * (i + 1))[i]
        items8 = rec.get("items", [""] * (i + 1))[i]
        nuevo = guardar(c, "sec", "sec|%s|%s" % (cik, acc), fecha, nombre,
                        "%s %s %s" % (form, desc, items8), url, form)
        nuevos += nuevo
        n += 1
        imprimir_bloque("%s  %s  %s" % (fecha, form, desc),
                        [("Items 8-K: " + items8) if items8 else "", url], nuevo)
    print("Resumen SEC: %d documentos (%d nuevos).  Los 8-K con items 1.01/1.02/2.03/2.04/"
          "3.03/8.01 son los relevantes para deuda." % (n, nuevos))
    if args.deuda:
        time.sleep(PAUSA_S)
        r, motivo = get(ses, SEC_FACTS.format(cik=cik), accept="application/json")
        if r is None or r.status_code != 200:
            print("Cifras: no disponibles (%s)" % (motivo or r.status_code))
        else:
            print("Cifras XBRL (ultimo valor declarado, USD):")
            for k, end, form, val, fp in hechos_deuda(r.json()):
                print("  %-52s %-10s %-5s %-4s %18s" % (k, end, form, fp, "{:,.0f}".format(val)))
    return 0


# ------------------------------------------------------------------ REE
def enlaces_ree(html_texto, base):
    # REE escribe href sin comillas (href=https://...), por eso se aceptan las dos formas
    hrefs = [a or b for a, b in re.findall(
        r'href=(?:"([^"]+\.(?:xlsx|xls|csv|pdf)[^"]*)"|([^\s">]+\.(?:xlsx|xls|csv|pdf)[^\s">]*))',
        html_texto, re.I)]
    out = []
    for h in hrefs:
        h = html.unescape(h)
        if h.startswith("/"):
            h = "https://www.ree.es" + h
        out.append(h)
    return sorted(set(out))


def cmd_red_descargar(args):
    ses = nueva_sesion()
    asegurar_dirs()
    todos = []
    for pag in REE_PAGINAS:
        r, motivo = get(ses, pag)
        if r is None or bloqueo(r):
            print("REE bloquea la descarga automatica en %s (%s)." % (pag, motivo or r.status_code))
            if r is not None:
                dump("ree_bloqueo.html", r.text[:30000])
            print("No intento saltarlo.  Via manual: abre la pagina en Firefox, descarga el xlsx/csv\n"
                  "'Listado de solicitudes de acceso de demanda' y ejecuta:\n"
                  "  python3 fuentes_ia.py red --archivo RUTA_DEL_FICHERO --ccaa Aragon")
            return 2
        dump("ree_pagina_%d.html" % (REE_PAGINAS.index(pag) + 1), r.text)
        todos += enlaces_ree(r.text, pag)
    todos = sorted(set(todos))
    print("Enlaces a ficheros encontrados: %d" % len(todos))
    for u in todos:
        print("  " + u)
    if not todos:
        print("No hay enlaces: revisa el HTML guardado en diagnostico/.")
        return 2
    elegidos = [u for u in todos if re.search(r"(demanda|solicitud)", u, re.I)
                and re.search(r"\.(xlsx|xls|csv)", u, re.I)]
    if not elegidos:
        elegidos = [u for u in todos if re.search(r"\.(xlsx|xls|csv)", u, re.I)][:2]
    for u in elegidos:
        r, motivo = get(ses, u, stream=False)
        time.sleep(PAUSA_S)
        if r is None or r.status_code != 200 or bloqueo(r):
            print("  No se pudo bajar %s (%s)" % (u, motivo or r.status_code))
            continue
        nombre = os.path.basename(u.split("?")[0])
        ruta = os.path.join(DESC_DIR, nombre)
        with open(ruta, "wb") as f:
            f.write(r.content)
        print("  Descargado: %s (%d bytes)" % (ruta, len(r.content)))
        args.archivo = ruta
        cmd_red_leer(args)
    return 0


def leer_filas(ruta):
    ext = os.path.splitext(ruta)[1].lower()
    if ext == ".csv":
        with open(ruta, "rb") as f:
            raw = f.read()
        txt = None
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                txt = raw.decode(enc)
                break
            except UnicodeDecodeError:
                pass
        dial = csv.Sniffer().sniff(txt[:4000], delimiters=";,\t") if txt else csv.excel
        return [r for r in csv.reader(io.StringIO(txt), dial)]
    if ext in (".xlsx", ".xlsm"):
        try:
            import openpyxl
        except ImportError:
            fallar("Falta openpyxl: pip install openpyxl   (o guarda la hoja como CSV)")
        wb = openpyxl.load_workbook(ruta, read_only=True, data_only=True)
        filas = []
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                filas.append(["" if v is None else str(v) for v in row])
        return filas
    fallar("Formato %s no soportado: usa .xlsx o .csv (el PDF no se interpreta)." % ext)


COLS_REE = {  # clave interna -> fragmentos de cabecera (sin tildes)
    "id": ["id solicitud", "identificador"],
    "tipo": ["tipo de solicitud", "tipo"],
    "nudo": ["nudo"],
    "ccaa": ["comunidad"],
    "mw": ["capacidad solicitada", "mw"],
    "comparten": ["comparten"],
    "fecha": ["fecha"],
    "concurso": ["concurso"],
}


def localizar_cabecera(filas):
    for i, f in enumerate(filas[:40]):
        celdas = [norm(x) for x in f]
        if any("comunidad" in x for x in celdas) and any("nudo" in x for x in celdas):
            return i, celdas
    return None, None


def a_numero(s):
    s = (s or "").strip().replace(" ", "")
    if not s:
        return 0.0
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def cmd_red_leer(args):
    ruta = args.archivo
    if not ruta or not os.path.exists(ruta):
        fallar("No existe el archivo %r.  Comprueba la ruta (en Termux: ls ~/storage/downloads)." % ruta)
    filas = leer_filas(ruta)
    idx, cab = localizar_cabecera(filas)
    if idx is None:
        dump("ree_sin_cabecera.txt", "\n".join(";".join(f) for f in filas[:60]))
        fallar("No encuentro la fila de cabecera (columnas 'Comunidad' y 'Nudo').  "
               "Se guardaron las primeras filas en diagnostico/.")
    pos = {}
    for k, frags in COLS_REE.items():
        for j, h in enumerate(cab):
            if any(fr in h for fr in frags):
                pos.setdefault(k, j)
                break
    print("REE | %s | cabecera en fila %d | columnas: %s" % (
        os.path.basename(ruta), idx + 1, ", ".join("%s=%d" % kv for kv in pos.items())))
    for k in ("nudo", "ccaa", "mw"):
        if k not in pos:
            dump("ree_cabecera.txt", "\n".join(cab))
            fallar("Falta la columna %r; cabecera guardada en diagnostico/." % k)
    ccaa_f = [norm(x) for x in lista_csv(args.ccaa)]
    nudo_f = [norm(x) for x in lista_csv(args.nudo)]
    datos = []
    for f in filas[idx + 1:]:
        if len(f) <= max(pos.values()):
            continue
        ccaa = f[pos["ccaa"]].strip()
        nudo = f[pos["nudo"]].strip()
        if not nudo and not ccaa:
            continue
        if ccaa_f and not any(x in norm(ccaa) for x in ccaa_f):
            continue
        if nudo_f and not any(x in norm(nudo) for x in nudo_f):
            continue
        mw = a_numero(f[pos["mw"]])
        if mw < args.min_mw:
            continue
        conc = f[pos["concurso"]].strip() if "concurso" in pos else ""
        datos.append({"id": f[pos["id"]] if "id" in pos else "", "tipo": f[pos["tipo"]] if "tipo" in pos else "",
                      "nudo": nudo, "ccaa": ccaa, "mw": mw, "conc": conc,
                      "fecha": f[pos["fecha"]] if "fecha" in pos else ""})
    print("Solicitudes que cumplen filtro: %d | MW totales: %s" % (
        len(datos), "{:,.0f}".format(sum(d["mw"] for d in datos))))
    por_nudo = {}
    for d in datos:
        k = (d["nudo"], d["ccaa"])
        por_nudo.setdefault(k, [0.0, 0, 0])
        por_nudo[k][0] += d["mw"]
        por_nudo[k][1] += 1
        if norm(d["conc"]) in ("si", "s", "x", "1", "true"):
            por_nudo[k][2] += 1
    print("\n  %-46s %-14s %10s %6s %8s" % ("NUDO", "CCAA", "MW", "SOLIC", "CONCURSO"))
    top = sorted(por_nudo.items(), key=lambda kv: -kv[1][0])[:args.top]
    for (nudo, ccaa), (mw, n, nc) in top:
        print("  %-46s %-14s %10s %6d %8d" % (nudo[:46], ccaa[:14], "{:,.0f}".format(mw), n, nc))
    c = db_conectar()
    nuevas = 0
    mes = dt.date.today().strftime("%Y-%m")
    for (nudo, ccaa), (mw, n, nc) in por_nudo.items():
        nuevas += guardar(c, "ree", "ree|%s|%s|%s|%s" % (os.path.basename(ruta), nudo, ccaa, mes),
                          dt.date.today().isoformat(), nudo,
                          "%s: %.0f MW en %d solicitudes (%d en concurso)" % (ccaa, mw, n, nc),
                          ruta, "red")
    print("\nGuardado en la base: %d nudos nuevos (compara con el mes siguiente: 'estado')." % nuevas)
    return 0


def buscar_ficheros_red():
    """Busca xlsx/csv recientes en las carpetas habituales de descargas."""
    base = [os.path.expanduser("~/storage/downloads"), os.path.expanduser("~/storage/shared/Download"),
            "/storage/emulated/0/Download", os.path.expanduser("~/Downloads"), os.getcwd(), DESC_DIR]
    vistos, out = set(), []
    for b in base:
        if not os.path.isdir(b):
            continue
        for raiz, dirs, files in os.walk(b):
            if raiz[len(b):].count(os.sep) >= 3:
                dirs[:] = []
            for f in files:
                if re.search(r"\.(xlsx|xlsm|csv)$", f, re.I):
                    ruta = os.path.join(raiz, f)
                    if ruta not in vistos:
                        vistos.add(ruta)
                        out.append((os.path.getmtime(ruta), ruta, os.path.getsize(ruta)))
    return sorted(out, reverse=True)


def cmd_red_listar(args):
    fs = buscar_ficheros_red()
    if not fs:
        print("No encuentro ningun .xlsx/.csv en las carpetas de descargas.")
        print("Si usas Termux ejecuta una vez:  termux-setup-storage   (y acepta el permiso).")
        print("Despues descarga el Excel de REE con Firefox (ver manual 3.4) y repite:  red --listar")
        return 1
    print("Ficheros .xlsx/.csv encontrados (mas recientes primero).  Copia la ruta completa en --archivo:")
    for t, ruta, n in fs[:15]:
        print("  %s  %9d bytes  %s" % (dt.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M"), n, ruta))
    return 0


def cmd_red(args):
    if args.listar:
        return cmd_red_listar(args)
    if args.archivo:
        return cmd_red_leer(args)
    return cmd_red_descargar(args)


# ------------------------------------------------------------------ BOA
def boa_url(base, texto, desde, hasta, formato="JSON", docs="1-200", materia=""):
    sec = "OPENDATABOAJSON" if formato == "JSON" else "OPENDATABOAXML"
    q = [("CMD", "VERLST"), ("OUTPUTMODE", formato), ("BASE", base), ("DOCS", docs),
         ("SEC", sec), ("SORT", "-PUBL"), ("SEPARADOR", "")]
    if desde:
        q.append(("@PUBL-GE", desde.strftime("%Y%m%d")))
    if hasta:
        q.append(("@PUBL-LE", hasta.strftime("%Y%m%d")))
    if texto:
        q.append(("TITU", texto))
    if materia:
        q.append(("MATE-C", materia))
    return BOA_BASE + "?" + "&".join("%s=%s" % (k, requests.utils.quote(v, safe="@")) for k, v in q)


def buscar_lista(obj):
    """Primera lista de dicts relevante dentro del JSON (esquema no documentado)."""
    if isinstance(obj, list):
        if obj and isinstance(obj[0], dict):
            return obj
        for x in obj:
            r = buscar_lista(x)
            if r:
                return r
    elif isinstance(obj, dict):
        for v in obj.values():
            r = buscar_lista(v)
            if r:
                return r
    return []


def campo(reg, *claves):
    for k in reg:
        nk = norm(k)
        if any(c in nk for c in claves) and reg[k]:
            return str(reg[k])
    return ""


def cmd_boa(args):
    ses, c = nueva_sesion(), db_conectar()
    hoy = dt.date.today()
    desde = parse_fecha(args.desde, hoy - dt.timedelta(days=args.dias))
    hasta = parse_fecha(args.hasta, hoy)
    base = {"boa": "BOLE", "bop-zaragoza": "BZHT", "bop-huesca": "BZHT", "bop-teruel": "BZHT"}[args.boletin]
    textos = lista_csv(args.texto) or ["centro de datos"]
    nuevos = total = 0
    for texto in textos:
        url = boa_url(base, texto, desde, hasta, "JSON")
        print("BOA | %s | texto en titulo: %r | %s a %s" % (args.boletin, texto, desde, hasta))
        r, motivo = get(ses, url, accept="application/json")
        time.sleep(PAUSA_S)
        if r is None or bloqueo(r) or r.status_code != 200:
            code = r.status_code if r is not None else motivo
            print("  El BOA no entrego datos (%s).  Se guarda lo recibido en diagnostico/ y no se intenta\n"
                  "  saltar la proteccion.  Via manual: abre en Firefox esta URL y guarda el resultado:\n  %s"
                  % (code, url))
            if r is not None:
                dump("boa_error_%s.txt" % code, r.text[:30000])
            continue
        dump("boa_respuesta_%s.json" % re.sub(r"\W+", "_", texto), r.text[:2_000_000])
        if "No se han recuperado documentos" in r.text:
            print("  Registros: 0 (el BOA no encontro documentos con ese texto en ese periodo).")
            continue
        try:
            lista = buscar_lista(r.json())
        except ValueError:
            inicio = re.sub(r"\s+", " ", r.text[:150]).strip()
            print("  Sin registros (el BOA no devolvio JSON; inicio de la respuesta: %r).  Volcado en diagnostico/."
                  % inicio)
            continue
        print("  Registros: %d" % len(lista))
        if lista:
            print("  Campos detectados: %s" % ", ".join(list(lista[0].keys())[:14]))
        for reg in lista:
            titulo = html.unescape(str(reg.get("Titulo") or campo(reg, "titulo", "title")))
            titulo = re.sub(r"\s+", " ", titulo).strip()
            fecha = str(reg.get("FechaPublicacion") or campo(reg, "publ", "fecha"))
            if re.fullmatch(r"\d{8}", fecha):
                fecha = "%s-%s-%s" % (fecha[:4], fecha[4:6], fecha[6:])
            enlace = str(reg.get("UrlPdf") or campo(reg, "url", "enlace"))
            enlace = " ".join(re.findall(r"https?://[^\s`\u00b4]+", enlace.replace("\n", "")))
            ident = str(reg.get("DOCN") or titulo[:40])
            if args.excluir and any(norm(x) in norm(titulo) for x in lista_csv(args.excluir)):
                continue
            total += 1
            nuevo = guardar(c, "boa", "boa|%s|%s" % (args.boletin, ident), fecha, args.boletin,
                            titulo, enlace, texto)
            nuevos += nuevo
            imprimir_bloque("%s  %s  %s" % (fecha, ident, str(reg.get("Emisor") or "")[:40]),
                            [titulo, enlace], nuevo)
    print("Resumen BOA: %d registros (%d nuevos)." % (total, nuevos))
    return 0


# ------------------------------------------------------------------ estado
def cmd_estado(args):
    c = db_conectar()
    print("Base local: %s" % DB_PATH)
    for fuente, n, ult in c.execute("SELECT fuente,COUNT(*),MAX(visto) FROM hallazgos GROUP BY fuente"):
        print("  %-8s %6d hallazgos | ultimo %s" % (fuente, n, ult))
    if os.path.isdir(DIAG_DIR):
        fs = sorted(os.listdir(DIAG_DIR))[-8:]
        print("Ultimos diagnosticos (%s):" % DIAG_DIR)
        for f in fs:
            print("  " + f)
    print("UA en uso: %s" % user_agent())
    return 0


# ------------------------------------------------------------------ CLI
def construir_parser():
    p = argparse.ArgumentParser(prog="fuentes_ia.py", description=__doc__.split("\n\n")[0])
    p.add_argument("--version", action="version", version="fuentes_ia " + VERSION)
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--dir", help="carpeta de trabajo (base, diagnosticos y descargas); defecto ~/fuentes_ia")
    sp = p.add_subparsers(dest="cmd", required=True)

    def comunes_borme(q):
        q.add_argument("--dias", type=int, default=5, help="ultimos N dias (defecto 5)")
        q.add_argument("--desde"); q.add_argument("--hasta")
        q.add_argument("--palabras", default="", help="lista separada por comas")
        q.add_argument("--nombre", default="", help="empresas a vigilar, separadas por comas")
        q.add_argument("--solo-nombres", action="store_true",
                       help="mostrar solo actos de las empresas de --nombre")
        q.add_argument("--nombre-en-empresa", action="store_true",
                       help="el nombre debe estar en la razon social, no en el texto del acto")
        q.add_argument("--seccion", default="A", help="A (actos inscritos) o C (anuncios)")
        q.add_argument("--provincia", default="", help="p.ej. zaragoza,huesca,teruel,madrid")
        q.add_argument("--largo", type=int, default=500, help="caracteres de texto por acto")

    b = sp.add_parser("borme", help="actos del BORME por palabras/nombres"); comunes_borme(b)
    k = sp.add_parser("concursal", help="atajo concursal sobre BORME"); comunes_borme(k)
    s = sp.add_parser("sec", help="SEC EDGAR por ticker o CIK")
    s.add_argument("--ticker"); s.add_argument("--cik")
    s.add_argument("--formularios", default="8-K,10-Q,10-K,S-1,424B,SC 13")
    s.add_argument("--dias", type=int, default=60)
    s.add_argument("--deuda", action="store_true", help="cifras XBRL de deuda/caja/ingresos")
    r = sp.add_parser("red", help="mapa/listado de acceso de demanda de REE")
    r.add_argument("--archivo", help="xlsx/csv ya descargado (si no, intenta descargar)")
    r.add_argument("--listar", action="store_true", help="lista los .xlsx/.csv de tus descargas")
    r.add_argument("--ccaa", default="Aragon"); r.add_argument("--nudo", default="")
    r.add_argument("--min-mw", type=float, default=0.0); r.add_argument("--top", type=int, default=25)
    o = sp.add_parser("boa", help="Boletin Oficial de Aragon (datos abiertos)")
    o.add_argument("--boletin", choices=["boa", "bop-zaragoza", "bop-huesca", "bop-teruel"], default="boa")
    o.add_argument("--texto", default="centro de datos", help="palabras en el titulo, separadas por comas")
    o.add_argument("--excluir", default="")
    o.add_argument("--dias", type=int, default=30); o.add_argument("--desde"); o.add_argument("--hasta")
    sp.add_parser("estado", help="resumen de la base y diagnosticos")
    return p


def main(argv=None):
    global HOME_DIR, DB_PATH, DIAG_DIR, DESC_DIR, CONF_PATH
    args = construir_parser().parse_args(argv)
    if args.dir:
        HOME_DIR = os.path.abspath(os.path.expanduser(args.dir))
    DB_PATH = os.path.join(HOME_DIR, "fuentes.db")
    DIAG_DIR = os.path.join(HOME_DIR, "diagnostico")
    DESC_DIR = os.path.join(HOME_DIR, "descargas")
    CONF_PATH = os.path.join(HOME_DIR, "config.json")
    asegurar_dirs()
    print("[fuentes_ia %s | carpeta de trabajo: %s]" % (VERSION, HOME_DIR))
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(os.path.join(HOME_DIR, "fuentes_ia.log"), encoding="utf-8")])
    try:
        return {"borme": cmd_borme, "concursal": cmd_concursal, "sec": cmd_sec,
                "red": cmd_red, "boa": cmd_boa, "estado": cmd_estado}[args.cmd](args)
    except KeyboardInterrupt:
        print("\nInterrumpido.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
