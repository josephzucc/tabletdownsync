#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bop.py - Buscador local del Boletin Oficial de la Provincia de Avila (BOP).

Funciona en Termux junto con la extension de Firefox "BOP Avila Termux":
  - bop.py abre un servidor local (127.0.0.1:8768) y le pone tareas a la extension.
  - la extension descarga las paginas diarias del boletin (y, si se pide, los PDF)
    y devuelve los datos a bop.py.
  - bop.py guarda todo en una base de datos SQLite local y muestra los resultados.

Ejemplos:
  python bop.py buscar -e "la adrada" --exacto --desde 01-09-2026 --hasta 16-10-2026
  python bop.py buscar -c ordenanza --fecha 16-10-2026
  python bop.py buscar -e "la adrada" -c "cuenta general" --ultimos 60
  python bop.py sincronizar --ultimos 30
  python bop.py emisores -b adrada
  python bop.py estado

Ver bopManual.txt para todos los detalles.
"""

import argparse
import base64
import datetime as dt
import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unicodedata
import uuid
import zipfile
import platform
from urllib.parse import urlparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "2.3.0"
BASE = "https://www.diputacionavila.es/"
PUERTO_DEF = 8768
DIR = os.environ.get("BOP_DIR", os.path.join(os.path.expanduser("~"), "bop"))
DB_PATH = os.path.join(DIR, "bop.db")
TXT_DIR = os.path.join(DIR, "txt")   # .txt generados con pdf2txt.py
PDF_DIR = os.path.join(DIR, "pdf")   # PDF descargados (opcion --descargar)
CONFIG = os.path.join(DIR, "config.json")
DIAG_DIR = os.path.join(DIR, "diag")   # HTML de diagnostico de las busquedas web


# ----------------------------------------------------------------------------
# Utilidades de fecha y texto
# ----------------------------------------------------------------------------

def parse_fecha(s):
    s = s.strip().lower()
    hoy = dt.date.today()
    if s == "hoy":
        return hoy
    if s == "ayer":
        return hoy - dt.timedelta(days=1)
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d.%m.%Y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise ValueError("fecha no valida: %r (usa DD-MM-AAAA, DD/MM/AAAA, AAAA-MM-DD, hoy o ayer)" % s)


def f_url(d):
    return d.strftime("%d-%m-%Y")


def f_es(d):
    if isinstance(d, str):
        if not d:
            return "--/--/----"
        try:
            d = dt.date.fromisoformat(d)
        except ValueError:
            return d
    return d.strftime("%d/%m/%Y")


def url_dia(d):
    return "%sboletin-oficial/%d/%s.html" % (BASE, d.year, f_url(d))


def norm(s):
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s.lower()).strip()


def ancho_terminal():
    try:
        return max(40, shutil.get_terminal_size((80, 24)).columns - 1)
    except Exception:
        return 79


def rango_fechas(desde, hasta, fines_semana):
    d = desde
    while d <= hasta:
        if fines_semana or d.weekday() < 5:
            yield d
        d += dt.timedelta(days=1)


# ----------------------------------------------------------------------------
# Base de datos
# ----------------------------------------------------------------------------

def abrir_db():
    os.makedirs(DIR, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript("""
    CREATE TABLE IF NOT EXISTS dias (
        fecha TEXT PRIMARY KEY,          -- AAAA-MM-DD
        estado TEXT NOT NULL,            -- ok | sin_boletin
        n_anuncios INTEGER NOT NULL DEFAULT 0,
        pdf_boletin TEXT,
        consultado TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS anuncios (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fecha TEXT NOT NULL,
        numero TEXT,
        emisor TEXT NOT NULL,
        emisor_norm TEXT NOT NULL,
        titulo TEXT NOT NULL,
        titulo_norm TEXT NOT NULL,
        url TEXT NOT NULL UNIQUE,
        texto_pdf TEXT,
        texto_norm TEXT,
        pdf_estado INTEGER NOT NULL DEFAULT 0   -- 0 sin bajar, 1 con texto, 2 PDF sin texto
    );
    CREATE INDEX IF NOT EXISTS ix_anuncios_fecha ON anuncios(fecha);
    CREATE INDEX IF NOT EXISTS ix_anuncios_emisor ON anuncios(emisor_norm);
    CREATE TABLE IF NOT EXISTS busquedas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fuente TEXT NOT NULL,
        realizada TEXT NOT NULL,
        parametros TEXT NOT NULL,        -- JSON con los campos enviados al buscador
        n_resultados INTEGER NOT NULL DEFAULT 0,
        total_servidor INTEGER,
        completa INTEGER NOT NULL DEFAULT 1
    );
    """)
    # Migracion: columnas nuevas (varias fuentes). Las bases antiguas se actualizan solas.
    existentes = {r["name"] for r in con.execute("PRAGMA table_info(anuncios)")}
    nuevas = [("fuente", "TEXT NOT NULL DEFAULT 'avila'"), ("url_pdf", "TEXT"), ("seccion", "TEXT"),
              ("ref", "TEXT"), ("num_boletin", "TEXT"), ("resumen", "TEXT"), ("resumen_norm", "TEXT"),
              ("guardado", "TEXT")]
    for nombre, defin in nuevas:
        if nombre not in existentes:
            con.execute("ALTER TABLE anuncios ADD COLUMN %s %s" % (nombre, defin))
    con.execute("UPDATE anuncios SET url_pdf = url WHERE url_pdf IS NULL AND fuente = 'avila'")
    con.execute("CREATE INDEX IF NOT EXISTS ix_anuncios_fuente ON anuncios(fuente)")
    con.commit()
    return con


def guardar_dia(con, fecha, estado, anuncios, pdf_boletin=""):
    ahora = dt.datetime.now().isoformat(timespec="seconds")
    iso = fecha.isoformat()
    for a in anuncios:
        con.execute(
            """INSERT INTO anuncios (fecha, numero, emisor, emisor_norm, titulo, titulo_norm, url, url_pdf, fuente)
               VALUES (?,?,?,?,?,?,?,?, 'avila')
               ON CONFLICT(url) DO UPDATE SET
                 fecha=excluded.fecha, numero=excluded.numero, emisor=excluded.emisor,
                 emisor_norm=excluded.emisor_norm, titulo=excluded.titulo,
                 titulo_norm=excluded.titulo_norm, url_pdf=excluded.url_pdf""",
            (iso, a.get("numero", ""), a["emisor"], norm(a["emisor"]), a["titulo"],
             norm(a["titulo"]), a["url"], a["url"]))
    con.execute(
        """INSERT INTO dias (fecha, estado, n_anuncios, pdf_boletin, consultado)
           VALUES (?,?,?,?,?)
           ON CONFLICT(fecha) DO UPDATE SET estado=excluded.estado,
             n_anuncios=excluded.n_anuncios, pdf_boletin=excluded.pdf_boletin,
             consultado=excluded.consultado""",
        (iso, estado, len(anuncios), pdf_boletin, ahora))
    con.commit()


def guardar_registros(con, fuente, regs):
    """Guarda en la base los registros devueltos por un buscador web.
    Devuelve (nuevos, actualizados)."""
    ahora = dt.datetime.now().isoformat(timespec="seconds")
    nuevos = actualizados = 0
    for r in regs:
        url = (r.get("url") or r.get("url_pdf") or "").strip()
        if not url:
            continue
        existe = con.execute("SELECT 1 FROM anuncios WHERE url=?", (url,)).fetchone()
        emisor = (r.get("emisor") or "").strip()
        titulo = (r.get("titulo") or "").strip() or "(sin titulo)"
        resumen = (r.get("resumen") or "").strip()
        con.execute(
            """INSERT INTO anuncios (fecha, numero, emisor, emisor_norm, titulo, titulo_norm, url, url_pdf,
                                     fuente, seccion, ref, num_boletin, resumen, resumen_norm, guardado)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(url) DO UPDATE SET
                 fecha=excluded.fecha, numero=excluded.numero, emisor=excluded.emisor,
                 emisor_norm=excluded.emisor_norm, titulo=excluded.titulo, titulo_norm=excluded.titulo_norm,
                 url_pdf=COALESCE(NULLIF(excluded.url_pdf, ''), url_pdf), fuente=excluded.fuente,
                 seccion=excluded.seccion, ref=excluded.ref, num_boletin=excluded.num_boletin,
                 resumen=excluded.resumen, resumen_norm=excluded.resumen_norm""",
            (r.get("fecha") or "", r.get("ref") or r.get("num_boletin") or "", emisor, norm(emisor),
             titulo, norm(titulo), url, r.get("url_pdf") or "", fuente, r.get("seccion") or "",
             r.get("ref") or "", r.get("num_boletin") or "", resumen, norm(resumen), ahora))
        if existe:
            actualizados += 1
        else:
            nuevos += 1
    con.commit()
    return nuevos, actualizados


def registrar_busqueda(con, fuente, campos, n, total, completa):
    con.execute("INSERT INTO busquedas (fuente, realizada, parametros, n_resultados, total_servidor, completa) "
                "VALUES (?,?,?,?,?,?)",
                (fuente, dt.datetime.now().isoformat(timespec="seconds"),
                 json.dumps(campos, ensure_ascii=False), n, total, 1 if completa else 0))
    con.commit()


def necesita_descarga(con, fecha, actualizar):
    """True si hay que pedir ese dia a la extension."""
    if actualizar:
        return True
    row = con.execute("SELECT estado, consultado FROM dias WHERE fecha=?",
                      (fecha.isoformat(),)).fetchone()
    if row is None:
        return True
    # El boletin de hoy puede publicarse o completarse a lo largo del dia.
    if fecha >= dt.date.today():
        consultado = dt.datetime.fromisoformat(row["consultado"])
        return (dt.datetime.now() - consultado) > dt.timedelta(hours=3)
    return False


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

    def limite_silencio(self):
        """Segundos sin noticias de la extension antes de darla por perdida."""
        with self.lock:
            extra = max([j.get("timeout", 0) for j, _ in self.en_curso.values()] or [0])
        return 120 + extra

    def progreso(self, datos):
        with self.lock:
            self.ultimo_contacto = time.time()
            ent = self.en_curso.get(datos.get("id"))
            if ent:
                self.en_curso[datos["id"]] = (ent[0], time.time())   # tarea larga: reinicia el reloj
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
                if ahora - t > job.get("timeout", 90):
                    del self.en_curso[jid]
                    job["intentos"] = job.get("intentos", 0) + 1
                    if job["intentos"] < 3:
                        self.pend.insert(0, job)
                    else:
                        self.resultados.put({"job": job, "datos": {"error": "tiempo agotado"}})
            if self.pend:
                job = self.pend.pop(0)
                self.en_curso[job["id"]] = (job, ahora)
                return {k: v for k, v in job.items() if k not in ("intentos",)}
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
        if self.headers.get("X-BOP") != "1":
            return self._enviar(403, {"error": "falta cabecera"})
        if self.path.startswith("/tarea"):
            job = self.cola.siguiente()
            if job:
                return self._enviar(200, job)
            return self._enviar(204)
        self._enviar(404, {"error": "no existe"})

    def do_POST(self):
        if self.headers.get("X-BOP") != "1":
            return self._enviar(403, {"error": "falta cabecera"})
        if not (self.path.startswith("/resultado") or self.path.startswith("/progreso")):
            return self._enviar(404, {"error": "no existe"})
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n <= 0 or n > 60 * 1024 * 1024:
                return self._enviar(413, {"error": "tamano no valido"})
            datos = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception as e:
            return self._enviar(400, {"error": str(e)})
        if self.path.startswith("/progreso"):
            self.cola.progreso(datos)
            return self._enviar(200, {"ok": True})
        ok = self.cola.recibir(datos)
        self._enviar(200 if ok else 410, {"ok": ok})


def ejecutar_jobs(jobs, puerto, procesar, espera_ext=20, max_espera_ext=300, al_progreso=None):
    """Reparte `jobs` a la extension y llama a procesar(resultado, hechos, total)
    en este hilo por cada resultado. Devuelve True si se terminaron todos."""
    if not jobs:
        return True
    cola = Cola()
    cola.al_progreso = al_progreso
    Manejador.cola = cola
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", puerto), Manejador)
    except OSError as e:
        print("No puedo abrir el puerto %d (%s). Quiza hay otro bop.py en marcha." % (puerto, e))
        return False
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    for j in jobs:
        cola.add(j)
    total = len(jobs)
    hechos = 0
    t0 = time.time()
    avisado = False
    completo = True
    try:
        while hechos < total:
            try:
                r = cola.resultados.get(timeout=1)
            except queue.Empty:
                ahora = time.time()
                if cola.ultimo_contacto == 0:
                    if ahora - t0 > espera_ext and not avisado:
                        print("Esperando a la extension... Abre Firefox (con la extension 'BOP Avila Termux'\n"
                              "instalada) y dejalo abierto. Pulsa Ctrl+C para cancelar.")
                        avisado = True
                    if ahora - t0 > max_espera_ext:
                        print("La extension no ha contactado en %d s. Cancelado." % max_espera_ext)
                        completo = False
                        break
                elif ahora - cola.ultimo_contacto > cola.limite_silencio():
                    print("\nLa extension dejo de responder (Firefox en segundo plano?). Cancelado.")
                    print("Lo ya descargado esta guardado; repite el comando para continuar.")
                    completo = False
                    break
                continue
            hechos += 1
            procesar(r, hechos, total)
    except KeyboardInterrupt:
        print("\nInterrumpido. Lo ya descargado esta guardado.")
        completo = False
    finally:
        srv.shutdown()
        srv.server_close()
    return completo


# ----------------------------------------------------------------------------
# Descarga de dias
# ----------------------------------------------------------------------------

def descargar_dias(con, fechas, puerto, actualizar=False):
    pendientes = [d for d in fechas if necesita_descarga(con, d, actualizar)]
    if not pendientes:
        return True
    print("Dias por consultar en la web: %d (de %d en el rango)" % (len(pendientes), len(fechas)))
    jobs = []
    for d in pendientes:
        jobs.append({"id": uuid.uuid4().hex, "tipo": "dia", "fecha": f_url(d),
                     "fecha_iso": d.isoformat(), "url": url_dia(d)})
    resumen = {"ok": 0, "vacio": 0, "error": 0, "parseo": 0, "anuncios": 0}

    def procesar(r, hechos, total):
        job, d = r["job"], r.get("datos") or {}
        fecha = dt.date.fromisoformat(job["fecha_iso"])
        etiqueta = f_es(fecha)
        if d.get("error"):
            resumen["error"] += 1
            linea = "error: %s" % d["error"]
        elif d.get("status") == 404:
            guardar_dia(con, fecha, "sin_boletin", []) if fecha < dt.date.today() else None
            resumen["vacio"] += 1
            linea = "sin boletin"
        elif d.get("status") != 200:
            resumen["error"] += 1
            linea = "error HTTP %s" % d.get("status")
        else:
            an = d.get("anuncios") or []
            if an:
                guardar_dia(con, fecha, "ok", an, d.get("pdf_boletin", ""))
                resumen["ok"] += 1
                resumen["anuncios"] += len(an)
                linea = "%d anuncios" % len(an)
            elif d.get("pdf_boletin") or d.get("n_enlaces"):
                resumen["parseo"] += 1
                linea = "AVISO: hay boletin pero no pude leer los anuncios (usa: bop.py diagnostico %s)" % f_url(fecha)
            else:
                if fecha < dt.date.today():
                    guardar_dia(con, fecha, "sin_boletin", [])
                resumen["vacio"] += 1
                linea = "sin boletin"
        print("[%d/%d] %s  %s" % (hechos, total, etiqueta, linea))
        sys.stdout.flush()

    completo = ejecutar_jobs(jobs, puerto, procesar)
    print("Resumen: %d dias con boletin (%d anuncios), %d sin boletin, %d errores, %d sin poder leer."
          % (resumen["ok"], resumen["anuncios"], resumen["vacio"], resumen["error"], resumen["parseo"]))
    return completo


# ----------------------------------------------------------------------------
# PDF (texto completo de los anuncios)
# ----------------------------------------------------------------------------

def nombre_base(url):
    return os.path.splitext(os.path.basename(urlparse(url).path))[0]


def pdf_de(f):
    """URL del PDF de un anuncio ('' si solo tiene pagina HTML)."""
    u = f["url_pdf"] if "url_pdf" in f.keys() else ""
    if u:
        return u
    u = f["url"] or ""
    return u if re.search(r"\.pdf($|[?#])", u, re.I) else ""


def descargar_pdfs(con, filas, puerto, convertir=True, carpeta=None):
    """Baja los PDF con la extension. convertir=True: extrae el texto con pdftotext
    y lo guarda en la base. carpeta: si se indica, guarda tambien el .pdf alli."""
    if convertir and not shutil.which("pdftotext"):
        print("Falta pdftotext. Instalalo en Termux con:  pkg install poppler")
        return False
    jobs = [{"id": uuid.uuid4().hex, "tipo": "pdf", "url": pdf_de(f), "anuncio_id": f["id"],
             "cookies": (f["fuente"] or "avila") != "avila"}
            for f in filas if pdf_de(f)]
    print("PDF por descargar: %d" % len(jobs))
    t_ini = time.time()

    def procesar(r, hechos, total):
        job, d = r["job"], r.get("datos") or {}
        msg = ""
        if d.get("error") or d.get("status") != 200 or not d.get("pdf_b64"):
            msg = "error: %s" % (d.get("error") or "HTTP %s" % d.get("status"))
        elif not convertir:
            try:
                os.makedirs(carpeta, exist_ok=True)
                with open(os.path.join(carpeta, nombre_base(job["url"]) + ".pdf"), "wb") as fh:
                    fh.write(base64.b64decode(d["pdf_b64"]))
                msg = "guardado (%d KB)" % (d.get("bytes", 0) // 1024)
            except Exception as e:
                msg = "error: %s" % e
        else:
            tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
            try:
                raw = base64.b64decode(d["pdf_b64"])
                if carpeta:
                    os.makedirs(carpeta, exist_ok=True)
                    with open(os.path.join(carpeta, nombre_base(job["url"]) + ".pdf"), "wb") as fh:
                        fh.write(raw)
                tmp.write(raw)
                tmp.close()
                p = subprocess.run(["pdftotext", "-layout", tmp.name, "-"],
                                   capture_output=True, timeout=120)
                texto = p.stdout.decode("utf-8", "replace").strip()
                texto = re.sub(r"[ \t]+", " ", texto)[:400000]
                if len(texto) > 40:
                    con.execute("UPDATE anuncios SET texto_pdf=NULL, texto_norm=?, pdf_estado=1 WHERE id=?",
                                (norm(texto), job["anuncio_id"]))
                    msg = "%d caracteres" % len(texto)
                else:
                    con.execute("UPDATE anuncios SET pdf_estado=2 WHERE id=?", (job["anuncio_id"],))
                    msg = "PDF sin texto (escaneado?)"
                con.commit()
            except Exception as e:
                msg = "error: %s" % e
            finally:
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass
        resto = (time.time() - t_ini) / hechos * (total - hechos)
        print("[%d/%d] PDF %s  (faltan ~%d min)" % (hechos, total, msg, int(resto / 60) + (1 if resto % 60 else 0)))
        sys.stdout.flush()

    return ejecutar_jobs(jobs, puerto, procesar)


def descargar_html_textos(con, filas, puerto):
    """Para anuncios que no tienen PDF: baja la pagina HTML y guarda su texto."""
    jobs = [{"id": uuid.uuid4().hex, "tipo": "html_texto", "url": f["url"], "anuncio_id": f["id"],
             "cookies": True} for f in filas if f["url"]]
    if not jobs:
        return True
    print("Paginas HTML sin PDF por leer: %d" % len(jobs))

    def procesar(r, hechos, total):
        job, d = r["job"], r.get("datos") or {}
        if d.get("error") or d.get("status") != 200:
            msg = "error: %s" % (d.get("error") or "HTTP %s" % d.get("status"))
        else:
            texto = re.sub(r"[ \t]+", " ", (d.get("texto") or "").strip())
            if len(texto) > 40:
                con.execute("UPDATE anuncios SET texto_pdf=NULL, texto_norm=?, pdf_estado=1 WHERE id=?",
                            (norm(texto), job["anuncio_id"]))
                msg = "%d caracteres" % len(texto)
            else:
                con.execute("UPDATE anuncios SET pdf_estado=2 WHERE id=?", (job["anuncio_id"],))
                msg = "pagina sin texto util"
            con.commit()
        print("[%d/%d] HTML %s" % (hechos, total, msg))
        sys.stdout.flush()

    return ejecutar_jobs(jobs, puerto, procesar)


# ----------------------------------------------------------------------------
# Conversion con el programa propio pdf2txt.py
# ----------------------------------------------------------------------------

def leer_config():
    try:
        with open(CONFIG, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def localizar_pdf2txt(a):
    """Ruta de pdf2txt.py: opcion --pdf2txt, variable BOP_PDF2TXT, config guardada,
    o buscandolo junto a bop.py, en la carpeta actual y en el HOME."""
    explicita = getattr(a, "pdf2txt", None)
    cands = [explicita, os.environ.get("BOP_PDF2TXT"), leer_config().get("pdf2txt"),
             os.path.join(os.path.dirname(os.path.abspath(__file__)), "pdf2txt.py"),
             os.path.join(os.getcwd(), "pdf2txt.py"),
             os.path.join(os.path.expanduser("~"), "pdf2txt.py"),
             os.path.join(DIR, "pdf2txt.py")]
    for c in cands:
        if c and os.path.isfile(os.path.expanduser(c)):
            ruta = os.path.abspath(os.path.expanduser(c))
            if explicita:
                os.makedirs(DIR, exist_ok=True)
                cfg = leer_config()
                cfg["pdf2txt"] = ruta
                with open(CONFIG, "w", encoding="utf-8") as fh:
                    json.dump(cfg, fh)
            return ruta
    return None


def listar_txt(carpetas):
    res = set()
    for c in carpetas:
        try:
            for fn in os.listdir(c):
                if fn.lower().endswith(".txt"):
                    res.add(os.path.abspath(os.path.join(c, fn)))
        except OSError:
            pass
    return res


def localizar_txt(nombre, carpetas, antes, t0):
    """Busca el .txt que acaba de crear pdf2txt.py. Solo acepta archivos nuevos
    (que no estaban antes de la llamada) o con el mismo nombre y recien escritos."""
    ahora = listar_txt(carpetas)
    nuevos = [r for r in ahora if r not in antes]
    reescritos = [r for r in ahora & antes
                  if os.path.splitext(os.path.basename(r))[0] == nombre and os.path.getmtime(r) >= t0 - 1]
    for r in nuevos + reescritos:
        if os.path.splitext(os.path.basename(r))[0] == nombre:
            return r
    return max(nuevos, key=os.path.getmtime) if nuevos else None


def convertir_txt(con, filas, a):
    ruta = localizar_pdf2txt(a)
    if not ruta:
        print("No encuentro pdf2txt.py. Indica su ruta una vez con:  --pdf2txt /ruta/pdf2txt.py")
        print("(o ponlo junto a bop.py, o en tu HOME).")
        return False
    os.makedirs(TXT_DIR, exist_ok=True)
    print("Convirtiendo %d anuncio(s) con %s" % (len(filas), ruta))
    t_ini = time.time()
    total = len(filas)
    seguidos = 0
    for i, f in enumerate(filas, 1):
        url_pdf = pdf_de(f)
        nombre = nombre_base(url_pdf)
        destino = os.path.join(TXT_DIR, nombre + ".txt")
        msg, ok = "", False
        try:
            if not os.path.exists(destino):
                carpetas = [TXT_DIR, os.path.dirname(ruta), os.getcwd()]
                antes = listar_txt(carpetas)
                t0 = time.time()
                p = subprocess.run([sys.executable, ruta, url_pdf], cwd=TXT_DIR,
                                   stdin=subprocess.DEVNULL, capture_output=True, timeout=240)
                hallado = localizar_txt(nombre, carpetas, antes, t0)
                if hallado and os.path.abspath(hallado) != os.path.abspath(destino):
                    shutil.move(hallado, destino)
                if not os.path.exists(destino):
                    cola = (p.stderr or p.stdout or b"").decode("utf-8", "replace").strip()[-160:]
                    raise RuntimeError("pdf2txt no creo ningun .txt (codigo %d) %s" % (p.returncode, cola))
            with open(destino, encoding="utf-8", errors="replace") as fh:
                texto = fh.read().strip()
            texto = re.sub(r"[ \t]+", " ", texto)[:400000]
            if len(texto) > 40:
                con.execute("UPDATE anuncios SET texto_pdf=NULL, texto_norm=?, pdf_estado=1 WHERE id=?",
                            (norm(texto), f["id"]))
                msg = "%d caracteres" % len(texto)
            else:
                con.execute("UPDATE anuncios SET pdf_estado=2 WHERE id=?", (f["id"],))
                msg = "sin texto (escaneado?)"
            con.commit()
            ok = True
        except subprocess.TimeoutExpired:
            msg = "error: pdf2txt tardo mas de 4 minutos"
        except Exception as e:
            msg = "error: %s" % e
        seguidos = 0 if ok else seguidos + 1
        resto = (time.time() - t_ini) / i * (total - i)
        print("[%d/%d] %s  %s  (faltan ~%d min)" % (i, total, nombre, msg, int(resto / 60) + 1))
        sys.stdout.flush()
        if seguidos >= 4:
            print("4 fallos seguidos: paro. Revisa que pdf2txt.py funciona a mano:  python pdf2txt.py <url>")
            return False
    return True


def procesar_textos(con, base, a):
    """Aplica --txt / --pdf / --descargar a las filas candidatas `base`."""
    if not (a.txt or a.pdf or a.descargar):
        return
    if a.txt and a.pdf:
        print("Aviso: con --txt y --pdf a la vez se usa --txt (pdf2txt.py).")
    necesita_texto = (lambda f: f["pdf_estado"] == 0)
    con_pdf = (lambda f: bool(pdf_de(f)))
    sin_bajar = (lambda f: con_pdf(f) and not os.path.exists(os.path.join(PDF_DIR, nombre_base(pdf_de(f)) + ".pdf")))

    def limitar(c):
        if a.max_pdf and a.max_pdf > 0 and len(c) > a.max_pdf:
            print("Hay %d pendientes; hago solo los %d primeros (cambia con --max-pdf)." % (len(c), a.max_pdf))
            return c[:a.max_pdf]
        return c

    if a.txt:
        pend = [f for f in base if necesita_texto(f)]
        sin = [f for f in pend if not con_pdf(f)]
        if sin:
            print("%d anuncio(s) no tienen PDF y no se pueden convertir con pdf2txt.py (usa --pdf para leer su pagina HTML)." % len(sin))
        c = limitar([f for f in pend if con_pdf(f)])
        if c:
            convertir_txt(con, c, a)
        if a.descargar:
            c = limitar([f for f in base if sin_bajar(f)])
            if c:
                descargar_pdfs(con, c, a.puerto, convertir=False, carpeta=PDF_DIR)
    elif a.pdf:
        pend = [f for f in base if necesita_texto(f) or (a.descargar and sin_bajar(f))]
        c = limitar([f for f in pend if con_pdf(f)])
        if c:
            descargar_pdfs(con, c, a.puerto, convertir=True, carpeta=PDF_DIR if a.descargar else None)
        h = limitar([f for f in base if necesita_texto(f) and not con_pdf(f) and f["url"]])
        if h:
            descargar_html_textos(con, h, a.puerto)
    elif a.descargar:
        c = limitar([f for f in base if sin_bajar(f)])
        if c:
            descargar_pdfs(con, c, a.puerto, convertir=False, carpeta=PDF_DIR)


# ----------------------------------------------------------------------------
# Busqueda
# ----------------------------------------------------------------------------

def coincide_emisor(emisor_norm, consultas, exacto):
    for q in consultas:
        if exacto:
            # se admite un sufijo de provincia entre parentesis: "ayuntamiento de la adrada (avila)"
            e = re.sub(r"\s*\([^)]*\)\s*$", "", emisor_norm)
            if e == q or e == "ayuntamiento de " + q:
                return True
        elif q in emisor_norm:
            return True
    return False


def coincide_texto(texto_norm, terminos, frase, palabra):
    if not terminos:
        return True
    if frase:
        objetivos = [" ".join(terminos)]
        todos = objetivos
    else:
        todos = terminos
    for t in todos:
        if palabra:
            if not re.search(r"(?<!\w)" + re.escape(t) + r"(?!\w)", texto_norm):
                return False
        elif t not in texto_norm:
            return False
    return True


COLS = ("id, fecha, numero, emisor, emisor_norm, titulo, titulo_norm, url, url_pdf, pdf_estado, "
        "fuente, seccion, ref, num_boletin, resumen_norm")


def fragmento(texto, terminos):
    for t in terminos:
        k = texto.find(t)
        if k >= 0:
            return "..." + texto[max(0, k - 70):k + 110].strip() + "..."
    return ""


def filtrar(con, a, solo_fecha_emisor=False):
    sql = "SELECT " + COLS + " FROM anuncios WHERE 1=1"
    par = []
    if a.desde:
        sql += " AND fecha >= ?"
        par.append(a.desde.isoformat())
    if a.hasta:
        sql += " AND fecha <= ?"
        par.append(a.hasta.isoformat())
    fuentes = getattr(a, "fuentes", None)
    if fuentes:
        sql += " AND fuente IN (%s)" % ",".join("?" * len(fuentes))
        par.extend(fuentes)
    filas = con.execute(sql, par).fetchall()
    consultas = [norm(e) for e in (a.emisor or [])]
    if consultas:
        filas = [f for f in filas if coincide_emisor(f["emisor_norm"], consultas, a.exacto)]
    if solo_fecha_emisor:
        return filas
    terminos = norm(a.texto).split() if a.texto else []
    res, resto = [], []
    for f in filas:
        if coincide_texto(f["titulo_norm"] + " " + (f["resumen_norm"] or ""), terminos, a.frase, a.palabra):
            res.append((f, "titulo", ""))
        elif f["pdf_estado"] == 1:
            resto.append(f)
    for i in range(0, len(resto), 200):
        lote = resto[i:i + 200]
        ids = [f["id"] for f in lote]
        mapa = {r["id"]: r["texto_norm"] for r in con.execute(
            "SELECT id, texto_norm FROM anuncios WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)}
        for f in lote:
            tn = mapa.get(f["id"]) or ""
            if coincide_texto(f["titulo_norm"] + " " + tn, terminos, a.frase, a.palabra):
                res.append((f, "pdf", fragmento(tn, terminos)))
    return res


def imprimir(resultados, ancho, salida):
    def out(s=""):
        print(s)
        salida.append(s)
    for f, donde, frag in resultados:
        cab = "%s  [%s]  nº %s  %s" % (f_es(f["fecha"]), (f["fuente"] or "avila").upper(),
                                       f["numero"] or "?", f["emisor"] or "")
        for linea in textwrap.wrap(cab, ancho) or [cab]:
            out(linea)
        for linea in textwrap.wrap(f["titulo"], ancho - 3):
            out("   " + linea)
        if donde == "pdf":
            out("   (coincidencia en el texto del PDF)")
            for linea in textwrap.wrap(frag, ancho - 3):
                out("   " + linea)
        out("   " + f["url"])
        if f["url_pdf"] and f["url_pdf"] != f["url"]:
            out("   PDF: " + f["url_pdf"])
        out()


def cmd_buscar(a):
    con = abrir_db()
    a.fuentes = [x.lower() for x in _lista(getattr(a, "fuente", None))] or None
    if a.fuentes:
        mal = [x for x in a.fuentes if x not in TODAS_FUENTES]
        if mal:
            print("Fuente no valida: %s. Opciones: %s" % (", ".join(mal), ", ".join(TODAS_FUENTES)))
            return 2
    sync_avila = (not a.fuentes) or ("avila" in a.fuentes)
    if a.fecha:
        a.desde = a.hasta = a.fecha
    if a.ultimos:
        a.hasta = a.hasta or dt.date.today()
        a.desde = a.desde or (dt.date.today() - dt.timedelta(days=a.ultimos))
    if a.desde and not a.hasta:
        a.hasta = dt.date.today()
    if a.hasta and not a.desde:
        a.desde = a.hasta - dt.timedelta(days=30)
    if a.desde and a.hasta and a.desde > a.hasta:
        print("La fecha inicial es posterior a la final.")
        return 2
    if not (a.emisor or a.texto):
        print("Indica al menos -e EMISOR o -c TEXTO (ver: bop.py buscar --help).")
        return 2

    if a.desde and not a.offline:
        if sync_avila:
            fechas = list(rango_fechas(a.desde, min(a.hasta, dt.date.today()), a.fines_semana))
            descargar_dias(con, fechas, a.puerto, a.actualizar)
        else:
            print("Nota: %s solo se consulta en la base local. Para traer datos nuevos usa:  bop.py web <fuente> ..."
                  % ", ".join(a.fuentes))
    elif not a.desde:
        if a.fuentes:
            n = con.execute("SELECT COUNT(*) c FROM anuncios WHERE fuente IN (%s)" % ",".join("?" * len(a.fuentes)),
                            a.fuentes).fetchone()["c"]
            print("Sin fechas: busco solo en la base local (%d anuncios de %s)." % (n, ", ".join(a.fuentes)))
        else:
            n = con.execute("SELECT COUNT(*) c FROM anuncios").fetchone()["c"]
            print("Sin fechas: busco solo en la base local (%d anuncios guardados)." % n)
        if n == 0:
            print("Todavia no hay datos. Usa --ultimos N, --fecha o --desde/--hasta (Avila), o: bop.py web <fuente> ...")
            return 1

    if (a.pdf or a.txt or a.descargar) and not a.offline:
        if not (a.emisor or a.desde):
            print("--pdf/--txt/--descargar necesitan limitar por emisor (-e) o por fechas.")
        else:
            procesar_textos(con, filtrar(con, a, solo_fecha_emisor=True), a)

    res = filtrar(con, a)
    res.sort(key=lambda x: (x[0]["fecha"], x[0]["id"]), reverse=not a.antiguos_primero)
    if a.limite:
        res = res[:a.limite]

    salida = []
    print()
    if a.emisor and not a.exacto:
        previos = filtrar(con, a, solo_fecha_emisor=True)
        nombres = sorted({f["emisor"] for f in previos})
        if len(nombres) > 1:
            print("AVISO: '%s' coincide con %d emisores distintos:" % (", ".join(a.emisor), len(nombres)))
            for n in nombres:
                print("   - " + n)
            print("   Usa --exacto para quedarte solo con el que se llama igual.\n")
    imprimir(res, ancho_terminal(), salida)
    resumen = "%d anuncio(s) encontrado(s)." % len(res)
    print(resumen)
    if a.guardar:
        with open(a.guardar, "w", encoding="utf-8") as fh:
            fh.write("\n".join(salida) + "\n" + resumen + "\n")
        print("Guardado en %s" % a.guardar)
    return 0


def cmd_sincronizar(a):
    con = abrir_db()
    hoy = dt.date.today()
    if a.ultimos:
        desde, hasta = hoy - dt.timedelta(days=a.ultimos), hoy
    else:
        desde = a.desde or (hoy - dt.timedelta(days=30))
        hasta = a.hasta or hoy
    hasta = min(hasta, hoy)
    fechas = list(rango_fechas(desde, hasta, a.fines_semana))
    ok = descargar_dias(con, fechas, a.puerto, a.actualizar)
    if ok and (a.pdf or a.txt or a.descargar):
        base = con.execute("SELECT " + COLS + " FROM anuncios WHERE fecha >= ? AND fecha <= ? ORDER BY fecha DESC",
                           (desde.isoformat(), hasta.isoformat())).fetchall()
        procesar_textos(con, base, a)
    return 0 if ok else 1


def cmd_emisores(a):
    con = abrir_db()
    fuentes = [x.lower() for x in _lista(a.fuente)] if getattr(a, "fuente", None) else []
    donde = (" WHERE fuente IN (%s)" % ",".join("?" * len(fuentes))) if fuentes else ""
    filas = con.execute("""SELECT emisor, emisor_norm, COUNT(*) n, MIN(fecha) d, MAX(fecha) h
                           FROM anuncios""" + donde + """ GROUP BY emisor_norm ORDER BY emisor_norm""", fuentes).fetchall()
    q = norm(a.buscar) if a.buscar else ""
    n = 0
    for f in filas:
        if q and q not in f["emisor_norm"]:
            continue
        print("%5d  %s  (%s - %s)" % (f["n"], f["emisor"], f_es(f["d"]), f_es(f["h"])))
        n += 1
    print("%d emisor(es)." % n)
    return 0


# ---------------------------------------------------------------------------
# Informes de diagnostico (para enviarmelos cuando algo falla)
# ---------------------------------------------------------------------------

def _escribir(z, nombre, texto):
    z.writestr(nombre, texto if isinstance(texto, str) else json.dumps(texto, ensure_ascii=False, indent=1))


def _informe_txt(f, argv, campos, avisos_py, resultado, ruta_bundle):
    tz = resultado.get("traza") or {}
    res = tz.get("resumen") or {}
    L = []
    L.append("INFORME DE DIAGNOSTICO bop.py %s - fuente %s" % (VERSION, f))
    L.append("Generado: %s" % dt.datetime.now().isoformat(timespec="seconds"))
    L.append("Python %s en %s" % (platform.python_version(), platform.platform()))
    L.append("Version extension: %s" % tz.get("version_extension", "(no informada: extension antigua?)"))
    L.append("Comando: %s" % " ".join(argv))
    L.append("")
    L.append("RESUMEN")
    L.append("  Leidos: %s   Paginas: %s   Total que indica la web: %s" % (res.get("leidos"), res.get("paginas"), res.get("total")))
    L.append("  La web dice 'sin resultados': %s   Hay mas paginas sin leer: %s   HTTP: %s" % (res.get("vacio"), res.get("hay_mas"), res.get("status")))
    L.append("  Error de la extension: %s" % (resultado.get("error") or "-"))
    L.append("  Duracion: %s ms" % tz.get("ms_total"))
    for av in (avisos_py or []) + (res.get("avisos") or []):
        L.append("  Aviso: %s" % av)
    L.append("")
    L.append("CAMPOS QUE bop.py PIDIO (None = quitar el campo)")
    for k, v in sorted((campos or {}).items()):
        L.append("  %s = %r" % (k, v))
    fo = tz.get("formulario") or {}
    L.append("")
    L.append("FORMULARIO DE LA WEB")
    for it in fo.get("intentos") or []:
        L.append("  intento: %s" % json.dumps(it, ensure_ascii=False)[:400])
    L.append("  usado_fallback=%s  accion=%s  metodo=%s" % (fo.get("usado_fallback"), fo.get("accion"), fo.get("metodo")))
    L.append("  controles dentro del <form>: %s   en toda la pagina: %s" % (len(fo.get("controles_en_formulario") or []), fo.get("controles_en_pagina")))
    L.append("  Pares enviados (nombre = valor):")
    for k, v in fo.get("pares_enviados") or []:
        L.append("    %s = %s" % (k, v))
    pe = tz.get("peticion") or {}
    L.append("")
    L.append("PETICION: %s %s" % (pe.get("metodo"), pe.get("url")))
    if pe.get("cuerpo"):
        L.append("  cuerpo: %s" % pe["cuerpo"][:3000])
    for p in tz.get("paginas") or []:
        an = p.get("analisis") or {}
        L.append("")
        L.append("PAGINA %s  HTTP %s  %s bytes  %s ms  redirigida=%s" % (p.get("n"), p.get("status"), p.get("longitud"), p.get("ms"), p.get("redirigida")))
        L.append("  pedida: %s" % p.get("url_pedida"))
        L.append("  final : %s" % p.get("url_final"))
        L.append("  tipo  : %s  charset %s" % (p.get("content_type"), p.get("charset")))
        L.append("  titulo: %s" % an.get("titulo_pagina"))
        L.append("  h1/h2 : %s" % " | ".join(an.get("h1_h2") or []))
        L.append("  total detectado: %s   frase 'vacio': %s" % (an.get("total_detectado"), (an.get("frase_vacio") or "-")[:300]))
        L.append("  enlaces totales: %s   que parecen resultado: %s   grupos: %s   leidos: %s   nuevos: %s" % (
            an.get("n_enlaces"), an.get("n_enlaces_resultado"), p.get("grupos"), p.get("leidos"), p.get("nuevos")))
        L.append("  siguiente pagina elegida: %s" % (p.get("siguiente") or "(ninguna)"))
        if p.get("parada"):
            L.append("  PARADA: %s" % p["parada"])
        L.append("  tablas: %s  listas: %s  formularios: %s" % (an.get("n_tablas"), an.get("n_listas"), json.dumps(an.get("formularios"), ensure_ascii=False)[:500]))
        L.append("  Enlaces que parecen resultado (hasta 15):")
        for e in (an.get("enlaces_resultado") or [])[:15]:
            L.append("    %s | %s | %s" % (e.get("texto"), e.get("href"), e.get("ruta")))
        L.append("  Candidatos a paginacion:")
        for e in (an.get("candidatos_paginacion") or [])[:25]:
            L.append("    [%s] %s | clase=%s | %s" % (e.get("texto"), e.get("href"), e.get("clase"), e.get("ruta")))
        L.append("  Registros leidos (hasta 8):")
        for r in (p.get("registros") or [])[:8]:
            L.append("    %s | %s | %s | %s" % (r.get("fecha"), r.get("ref"), (r.get("emisor") or "")[:50], (r.get("titulo") or "")[:90]))
        L.append("  Texto de la pagina (inicio): %s" % (an.get("texto_inicio") or "")[:1200])
    if tz.get("excepcion"):
        L.append("")
        L.append("EXCEPCION EN LA EXTENSION:\n%s" % tz["excepcion"])
    L.append("")
    L.append("Contenido del paquete: informe.txt (este), traza.json (todo en bruto), formulario.html,")
    L.append("pagina_N.html (HTML crudo de cada pagina de resultados), bop.py.version")
    return "\n".join(L) + "\n"


def guardar_bundle_web(f, argv, campos, avisos_py, resultado):
    """Guarda un .zip con TODO lo necesario para depurar una busqueda web. Devuelve la ruta."""
    try:
        os.makedirs(DIAG_DIR, exist_ok=True)
        marca = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        ruta = os.path.join(DIAG_DIR, "diag_%s_%s.zip" % (f, marca))
        tz = dict(resultado.get("traza") or {})
        paginas = tz.get("paginas") or []
        with zipfile.ZipFile(ruta, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            _escribir(z, "informe.txt", _informe_txt(f, argv, campos, avisos_py, resultado, ruta))
            htmls = []
            for p in paginas:
                h = p.pop("html", None)
                htmls.append(h)
            fo = tz.get("formulario") or {}
            if fo.get("html"):
                _escribir(z, "formulario.html", fo.pop("html"))
            _escribir(z, "traza.json", tz)
            for i, h in enumerate(htmls, 1):
                if h:
                    _escribir(z, "pagina_%d.html" % i, h)
            _escribir(z, "bop.py.version", "bop.py %s\n%s\n" % (VERSION, " ".join(argv)))
        # conservar solo los ultimos 30 paquetes
        zs = sorted(x for x in os.listdir(DIAG_DIR) if x.startswith("diag_") and x.endswith(".zip"))
        for viejo in zs[:-30]:
            try:
                os.remove(os.path.join(DIAG_DIR, viejo))
            except OSError:
                pass
        return ruta
    except Exception as e:  # el diagnostico nunca debe romper la busqueda
        print("(No pude guardar el informe de diagnostico: %s)" % e)
        return ""


def cmd_informe(a):
    """Junta en un solo .zip los ultimos informes + estado + historial para enviarlo."""
    os.makedirs(DIAG_DIR, exist_ok=True)
    zs = sorted(x for x in os.listdir(DIAG_DIR) if x.startswith("diag_") and x.endswith(".zip"))
    marca = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    ruta = os.path.join(DIR, "informe_%s.zip" % marca)
    con = abrir_db()
    hist = []
    try:
        for r in con.execute("SELECT * FROM busquedas ORDER BY id DESC LIMIT 40"):
            hist.append(dict(zip([c[0] for c in con.execute("SELECT * FROM busquedas LIMIT 0").description], r)))
        porf = [tuple(r) for r in con.execute("SELECT fuente, COUNT(*), SUM(pdf_estado=1) FROM anuncios GROUP BY fuente")]
    except Exception as e:
        porf = [("error", str(e), 0)]
    with zipfile.ZipFile(ruta, "w", zipfile.ZIP_DEFLATED) as z:
        _escribir(z, "entorno.txt", "bop.py %s\nPython %s\n%s\nDIR=%s\nHOY=%s\nFuentes (fuente, anuncios, con texto): %r\n" % (
            VERSION, platform.python_version(), platform.platform(), DIR, dt.date.today(), porf))
        _escribir(z, "historial.json", hist)
        n = a.ultimos
        for nombre in zs[-n:]:
            z.write(os.path.join(DIAG_DIR, nombre), "informes/" + nombre)
        for extra in os.listdir(DIAG_DIR):
            if extra.endswith(".html") and not extra.startswith("diag_"):
                z.write(os.path.join(DIAG_DIR, extra), "otros/" + extra)
        try:
            z.write(os.path.abspath(__file__), "bop.py")
        except Exception:
            pass
    print("Informe listo: %s" % ruta)
    print("Incluye los ultimos %d informes de busqueda (de %d guardados), historial y entorno." % (min(n, len(zs)), len(zs)))
    print("Envia ese archivo .zip y revisare el fallo. No contiene tus contrasenas.")
    return 0



def cmd_estado(a):
    con = abrir_db()
    r = con.execute("""SELECT COUNT(*) dias, MIN(fecha) d, MAX(fecha) h FROM dias WHERE estado='ok'""").fetchone()
    s_ = con.execute("SELECT COUNT(*) c FROM dias WHERE estado='sin_boletin'").fetchone()["c"]
    an = con.execute("SELECT COUNT(*) c FROM anuncios").fetchone()["c"]
    pdf = con.execute("SELECT COUNT(*) c FROM anuncios WHERE pdf_estado=1").fetchone()["c"]
    em = con.execute("SELECT COUNT(DISTINCT emisor_norm) c FROM anuncios").fetchone()["c"]
    print("Base de datos : %s" % DB_PATH)
    if os.path.exists(DB_PATH):
        print("Tamano        : %.1f KB" % (os.path.getsize(DB_PATH) / 1024.0))
    print("Anuncios      : %d" % an)
    pend = con.execute("SELECT COUNT(*) c FROM anuncios WHERE pdf_estado=0").fetchone()["c"]
    print("Con texto PDF : %d  (pendientes de bajar: %d)" % (pdf, pend))
    print("Emisores      : %d" % em)
    print("BOP Avila     : %d dias con boletin" % r["dias"]
          + ((" (de %s a %s)" % (f_es(r["d"]), f_es(r["h"]))) if r["dias"] else "")
          + ", %d sin boletin" % s_)
    print("Por fuente    :")
    for f in con.execute("""SELECT fuente, COUNT(*) n, SUM(pdf_estado=1) t, MIN(NULLIF(fecha,'')) d, MAX(fecha) h
                            FROM anuncios GROUP BY fuente ORDER BY fuente"""):
        rango = (" (%s - %s)" % (f_es(f["d"]), f_es(f["h"]))) if f["d"] else ""
        print("   %-6s %7d anuncios, %6d con texto%s" % (f["fuente"], f["n"], f["t"] or 0, rango))
    nb = con.execute("SELECT COUNT(*) c FROM busquedas").fetchone()["c"]
    print("Busquedas web : %d registradas (bop.py historial)" % nb)
    return 0


def cmd_diagnostico(a):
    d = parse_fecha(a.fecha)
    url = a.url or url_dia(d)
    os.makedirs(DIR, exist_ok=True)
    job = {"id": uuid.uuid4().hex, "tipo": "html", "url": url, "fecha": f_url(d), "fecha_iso": d.isoformat()}
    recibido = {}

    def procesar(r, hechos, total):
        recibido.update(r.get("datos") or {})

    print("Pidiendo a la extension: %s" % url)
    ejecutar_jobs([job], a.puerto, procesar)
    if not recibido:
        return 1
    if recibido.get("error"):
        print("Error: %s" % recibido["error"])
        return 1
    ruta = os.path.join(DIR, "diagnostico_%s.html" % f_url(d))
    with open(ruta, "w", encoding="utf-8") as fh:
        fh.write(recibido.get("html", ""))
    html = recibido.get("html", "")
    print("HTTP %s, %d caracteres. Guardado en %s" % (recibido.get("status"), len(html), ruta))
    print("Enlaces a /bops/ encontrados: %d" % len(re.findall(r'/bops/\d{4}/', html)))
    return 0


# ----------------------------------------------------------------------------
# Buscadores avanzados web: BOE, notificaciones BOE, BOCYL, BOCM, BOAM
# ----------------------------------------------------------------------------

FUENTES_WEB = {
    "boe":   "BOE - Buscar en todo el BOE (https://boe.es/buscar/boe.php)",
    "notif": "BOE - Notificaciones (https://boe.es/buscar/notificaciones.php)",
    "bocyl": "BOCYL - Boletin Oficial de Castilla y Leon (https://bocyl.jcyl.es/busquedaCampos.do)",
    "bocm":  "BOCM - Boletin Oficial de la Comunidad de Madrid (https://bocm.es/advanced-search)",
    "boam":  "BOAM - Boletin del Ayuntamiento de Madrid (sede.madrid.es)",
    "bopa":  "BOPA - Boletin Oficial del Principado de Asturias (miprincipado.asturias.es/bopa)",
}
TODAS_FUENTES = ["avila"] + list(FUENTES_WEB)

# Listas de opciones de los formularios (copiadas de los buscadores).
BOE_SECCIONES = [("1", "Seccion I. Disposiciones generales"), ("2", "Seccion II. Autoridades y personal"),
                 ("3", "Seccion III. Otras disposiciones"), ("4", "Seccion IV. Administracion de Justicia"),
                 ("5", "Seccion V. Anuncios"), ("T", "Seccion TC. Tribunal Constitucional")]
NOTIF_MATERIAS = [("1", "AGRICULTURA Y GANADERÍA"), ("2", "AGUAS"), ("3", "CATASTRO"), ("4", "CAZA Y PESCA"),
    ("5", "CONSUMO"), ("6", "CRÉDITO, BANCA Y SEGUROS"), ("7", "CULTURA Y ESPECTÁCULOS"), ("8", "DEPORTES"),
    ("9", "ECONOMÍA Y COMERCIO"), ("13", "EDUCACIÓN"), ("10", "EMPLEADOS PÚBLICOS"), ("11", "EMPLEO"),
    ("12", "ENERGÍA"), ("14", "ENSEÑANZA UNIVERSITARIA"), ("15", "ESTADÍSTICA"), ("16", "EXPROPIACIONES"),
    ("17", "EXTRANJERÍA"), ("18", "FUERZAS ARMADAS"), ("19", "HACIENDA"), ("20", "INDUSTRIA Y ARTESANÍA"),
    ("47", "INSCRIPCIÓN, AFILIACIÓN Y RECAUDACIÓN DE LA SEGURIDAD SOCIAL"), ("21", "INSPECCIÓN DE TRABAJO"),
    ("22", "JUEGO"), ("23", "MEDIO AMBIENTE Y SOSTENIBILIDAD"), ("24", "METROLOGÍA"), ("25", "NAVEGACIÓN AÉREA"),
    ("26", "NAVEGACIÓN MARÍTIMA, PUERTOS Y MARINA MERCANTE"), ("27", "NOTARÍAS"), ("29", "PADRÓN MUNICIPAL"),
    ("30", "PATRIMONIO Y PROPIEDADES PÚBLICAS"),
    ("38", "PRESTACIONES DE SEGURIDAD SOCIAL Y DE ACCIÓN SOCIAL MARÍTIMA"), ("32", "PROPIEDAD INDUSTRIAL"),
    ("31", "PROPIEDAD INTELECTUAL"), ("33", "PROTECCIÓN DE DATOS DE CARÁCTER PERSONAL"), ("48", "REGISTROS CIVILES"),
    ("34", "REGISTROS DE LA PROPIEDAD"), ("35", "RESPONSABILIDAD PATRIMONIAL"), ("36", "SANIDAD Y FARMACIA"),
    ("37", "SEGURIDAD CIUDADANA Y PROTECCIÓN CIVIL"), ("39", "SERVICIOS FUNERARIOS"), ("40", "SERVICIOS SOCIALES"),
    ("41", "SUBVENCIONES, BECAS Y OTRAS AYUDAS"), ("42", "TELECOMUNICACIONES Y NUEVAS TECNOLOGÍAS"),
    ("43", "TRÁFICO, CIRCULACIÓN Y SEGURIDAD VIAL"), ("44", "TRANSPORTES TERRESTRES"), ("45", "TURISMO"),
    ("28", "URBANISMO Y VIVIENDA"), ("46", "OTRAS")]
BOCYL_APARTADOS = [("13", "I. Comunidad de Castilla y Leon (todo)"), ("13:16", "I.A Disposiciones generales"),
    ("13:17", "I.B Autoridades y personal"), ("13:18", "I.C Subvenciones"), ("13:19", "I.D Otras disposiciones"),
    ("13:20", "I.E Anuncios"), ("14", "II. Estado y otras comunidades autonomas"),
    ("15", "III. Administracion local"), ("16", "IV. Administracion de justicia"), ("17", "V. Otros anuncios")]
BOCYL_ALIAS = {"I": "13", "IA": "13:16", "IB": "13:17", "IC": "13:18", "ID": "13:19", "IE": "13:20",
               "II": "14", "III": "15", "IV": "16", "V": "17"}
BOCYL_RANGOS = ["ACUERDO", "ANUNCIO", "CEDULA", "CIRCULAR", "CONVENIO", "DECRETO", "DECRETO LEGISLATIVO",
    "DECRETO-LEY", "EDICTO", "EXTRACTO", "INFORMACIÓN", "INSTRUCCIÓN", "LEY", "LEY ORGÁNICA", "NOTIFICACIÓN",
    "ORDEN", "ORDENANZA", "OTROS", "PROTOCOLO", "REAL DECRETO", "REAL DECRETO-LEGISLATIVO", "REAL DECRETO-LEY",
    "RESOLUCIÓN"]
BOCM_SECCIONES = [("8384", "I. Comunidad de Madrid"), ("8417", "II. Disposiciones y anuncios del Estado"),
                  ("8387", "III. Administracion local (ayuntamientos)"), ("8388", "IV. Administracion de justicia"),
                  ("8404", "V. Otros anuncios")]
BOCM_ALIAS = {"I": "8384", "II": "8417", "III": "8387", "IV": "8388", "V": "8404",
              "1": "8384", "2": "8417", "3": "8387", "4": "8388", "5": "8404"}
BOAM_TIPOS = [("92b9bea4ac3f9010VgnVCM1000009b25680aTAXC", "Adjudicaciones de contratos"),
    ("ced9bea4ac3f9010VgnVCM1000009b25680aTAXC", "Almacén de Villa"),
    ("13b286433d26c010VgnVCM1000009b25680aTAXC", "Bolsa de trabajo"),
    ("2ab286433d26c010VgnVCM1000009b25680aTAXC", "Concursos de méritos y libre designación"),
    ("69b9bea4ac3f9010VgnVCM1000009b25680aTAXC", "Disposiciones y Actos"),
    ("afc286433d26c010VgnVCM1000009b25680aTAXC", "Edictos y Notificaciones"),
    ("819d2cdea93e9010VgnVCM1000009b25680aTAXC", "Licitaciones de contratos"),
    ("8ab480c6c96a0110VgnVCM2000000c205a0aTAXC", "Nombramientos y Ceses"),
    ("4227ed5d2353f210VgnVCM100000171f5a0aTAXC", "Notificaciones por comparecencia"),
    ("b6d286433d26c010VgnVCM1000009b25680aTAXC", "Objetos Perdidos"),
    ("e4e286433d26c010VgnVCM1000009b25680aTAXC", "Oposiciones"),
    ("b445c635d2c00110VgnVCM2000000c205a0aTAXC", "Otros"),
    ("f7d9bea4ac3f9010VgnVCM1000009b25680aTAXC", "Sesiones Distritos"),
    ("fbe286433d26c010VgnVCM1000009b25680aTAXC", "Sesiones Junta de Gobierno"),
    ("03f286433d26c010VgnVCM1000009b25680aTAXC", "Sesiones Pleno del Ayuntamiento"),
    ("95e9bea4ac3f9010VgnVCM1000009b25680aTAXC", "Situados"),
    ("cba9bea4ac3f9010VgnVCM1000009b25680aTAXC", "Subvenciones")]


def _lista(s):
    return [x.strip() for x in re.split(r"[,;]", s) if x.strip()] if s else []


def resolver_opcion(valor, opciones, que):
    """valor puede ser el codigo o parte del nombre (sin tildes ni mayusculas)."""
    v = norm(valor)
    for cod, nombre in opciones:
        if norm(cod) == v:
            return cod
    for cod, nombre in opciones:
        if norm(nombre) == v:
            return cod
    coinc = [(c, n) for c, n in opciones if v and v in norm(n)]
    if len(coinc) == 1:
        return coinc[0][0]
    if not coinc:
        raise ValueError("%s no valido: %r. Mira las opciones con:  bop.py campos <fuente>" % (que, valor))
    raise ValueError("%s ambiguo: %r coincide con: %s" % (que, valor, "; ".join(n for _, n in coinc[:6])))


def rango_remoto(a):
    hoy = dt.date.today()
    desde, hasta = a.desde, a.hasta
    if a.fecha:
        desde = hasta = a.fecha
    if a.anio:
        desde = desde or dt.date(a.anio, 1, 1)
        hasta = hasta or dt.date(a.anio, 12, 31)
    if a.ultimos:
        hasta = hasta or hoy
        desde = desde or (hoy - dt.timedelta(days=a.ultimos))
    if desde and hasta and desde > hasta:
        raise ValueError("la fecha inicial es posterior a la final")
    return desde, hasta


def _ops(a):
    res = {}
    for x in (a.op or []):
        if "=" not in x:
            raise ValueError("--op espera CAMPO=and|or|not (por ejemplo --op texto=or)")
        k, v = x.split("=", 1)
        k, v = k.strip().lower(), v.strip().lower()
        if v not in ("and", "or", "not"):
            raise ValueError("--op: el operador debe ser and, or o not")
        res[k] = v
    return res


def campos_boe(a, desde, hasta, avisos):
    c = {}
    secs = [x.upper() for x in _lista(a.seccion)] or [c0 for c0, _ in BOE_SECCIONES]
    validas = {c0 for c0, _ in BOE_SECCIONES}
    mal = [x for x in secs if x not in validas]
    if mal:
        raise ValueError("seccion BOE no valida: %s (usa 1,2,3,4,5,T)" % ",".join(mal))
    for c0, _ in BOE_SECCIONES:
        c["dato[0][%s]" % c0] = c0 if c0 in secs else None
    if a.titulo:
        c["dato[1]"] = a.titulo
    if a.emisor:
        c["dato[2]"] = a.emisor
    if a.texto:
        c["dato[3]"] = a.texto
    if a.num_boletin:
        c["dato[4]"] = a.num_boletin
    if a.num_oficial:
        c["dato[5]"] = a.num_oficial
    if desde:
        c["dato[6][0]"] = desde.isoformat()
    if hasta:
        c["dato[6][1]"] = hasta.isoformat()
    c["page_hits"] = str(a.page_hits)
    c["sort_order[0]"] = "asc" if a.orden == "antiguos" else "desc"
    for nombre, n in (("titulo", 1), ("emisor", 2), ("departamento", 2), ("texto", 3),
                      ("num_boletin", 4), ("num_oficial", 5)):
        if nombre in _ops(a):
            c["operador[%d]" % n] = _ops(a)[nombre]
    for x in (a.tipo, a.materia, a.num_anuncio):
        if x:
            avisos.append("El BOE no tiene ese campo (--tipo/--materia/--num-anuncio): ignorado.")
            break
    return c


def campos_notif(a, desde, hasta, avisos):
    c = {}
    if a.texto:
        c["dato[0]"] = a.texto
    if a.emisor:
        c["dato[1]"] = a.emisor
    if a.materia:
        c["dato[2]"] = resolver_opcion(a.materia, NOTIF_MATERIAS, "materia")
    if a.num_boletin:
        c["dato[3]"] = a.num_boletin
    if desde:
        c["dato[4][0]"] = desde.isoformat()
    if hasta:
        c["dato[4][1]"] = hasta.isoformat()
    c["page_hits"] = str(a.page_hits)
    c["sort_order[0]"] = "asc" if a.orden == "antiguos" else "desc"
    for nombre, n in (("texto", 0), ("emisor", 1), ("departamento", 1), ("materia", 2)):
        if nombre in _ops(a):
            c["operador[%d]" % n] = _ops(a)[nombre]
    if a.titulo or a.seccion or a.tipo or a.num_oficial or a.num_anuncio:
        avisos.append("Notificaciones solo admite: texto, departamento, materia, nº de boletin y fechas.")
    return c


def campos_bocyl(a, desde, hasta, avisos):
    c = {}
    if a.texto:
        if len(a.texto) > 100:
            raise ValueError("BOCYL limita el texto a 100 caracteres")
        c["texto"] = a.texto
    if a.titulo:
        c["titulo"] = a.titulo
    if a.emisor:
        c["organismo"] = a.emisor
    if a.num_boletin:
        c["numBoletin"] = a.num_boletin
    if a.num_oficial:
        c["numOficial"] = a.num_oficial
    if desde:
        c["fechaPublicacionDesde"] = desde.strftime("%d/%m/%Y")
    if hasta:
        c["fechaPublicacionHasta"] = hasta.strftime("%d/%m/%Y")
    if a.seccion:
        res = []
        for x in _lista(a.seccion):
            res.append(BOCYL_ALIAS.get(x.upper().replace(".", "")) or resolver_opcion(x, BOCYL_APARTADOS, "seccion"))
        c["apartadosSeleccionados"] = res
    if a.tipo:
        c["rangosSeleccionados"] = [resolver_opcion(x, [(r, r) for r in BOCYL_RANGOS], "tipo (rango)")
                                    for x in _lista(a.tipo)]
    c["ordenacion"] = {"antiguos": "1 asc, 3 asc", "relevancia": "4 desc, 3 asc"}.get(a.orden, "1 desc, 3 asc")
    if a.materia or a.num_anuncio:
        avisos.append("BOCYL no tiene ese campo (--materia/--num-anuncio): ignorado.")
    return c


def campos_bocm(a, desde, hasta, avisos):
    c = {}
    if a.texto:
        c["search_api_views_fulltext_1"] = a.texto
    if a.emisor:
        em = re.sub(r"^\s*(excmo\.?\s+)?ayuntamiento\s+de\s+(la\s+|el\s+|los\s+|las\s+)?", "", a.emisor, flags=re.I).strip() or a.emisor
        if em != a.emisor.strip():
            avisos.append("BOCM publica el emisor sin 'Ayuntamiento de' (p. ej. 'Alcobendas'): busco '%s'." % em)
        c["field_issuing_authority"] = em
    if a.num_boletin:
        c["field_bulletin_field_bocm_number"] = a.num_boletin
    if a.num_oficial:
        c["field_internal_reference"] = a.num_oficial
    if a.anio:
        c["field_bulletin_field_date_y_hidden[year]"] = str(a.anio)
    if desde:
        c["field_bulletin_field_date[date]"] = desde.strftime("%d-%m-%Y")
    if hasta:
        c["field_bulletin_field_date_1[date]"] = hasta.strftime("%d-%m-%Y")
    if a.seccion:
        v = a.seccion.strip()
        c["field_orden_seccion"] = BOCM_ALIAS.get(v.upper()) or resolver_opcion(v, BOCM_SECCIONES, "seccion")
    if a.titulo:
        avisos.append("BOCM no tiene campo de titulo: usa -c para buscar en todo el texto. --titulo ignorado.")
    if a.tipo or a.materia or a.num_anuncio:
        avisos.append("BOCM: --tipo/--materia/--num-anuncio ignorados (usa --campo si conoces el nombre del campo).")
    return c


def campos_boam(a, desde, hasta, avisos):
    c = {}
    if a.texto:
        c["textoBOAM"] = a.texto
    if a.titulo:
        c["tituloText"] = a.titulo
    if desde:
        c["desdeFecha"] = desde.strftime("%d/%m/%Y")
    if hasta:
        c["hastaFecha"] = hasta.strftime("%d/%m/%Y")
    if a.num_boletin:
        partes = [p.strip() for p in re.split(r"[-:]", a.num_boletin) if p.strip()]
        c["desdeNum"] = partes[0]
        c["hastaNum"] = partes[-1]
    if a.num_anuncio:
        c["numAnuncio"] = a.num_anuncio
    if a.tipo:
        c["tipoContenido"] = resolver_opcion(a.tipo, BOAM_TIPOS, "tipo de contenido")
    if a.emisor or a.seccion or a.materia or a.num_oficial:
        avisos.append("BOAM no tiene campos de emisor/seccion/materia/nº oficial: ignorados "
                      "(puedes filtrar luego en local con: bop.py buscar --fuente boam -e ...).")
    return c


def campos_bopa(a, desde, hasta, avisos):
    """BOPA: dos vias. Texto (-c): buscador del BOPA, mas nuevo primero, sin rango de fechas en la web (se filtra
    aqui). Emisor (-e) o solo fechas: se leen los sumarios de cada dia (consejeria, organismo, ayuntamiento)."""
    c = {}
    if a.seccion or a.materia or a.num_oficial or a.num_boletin or a.num_anuncio or a.tipo:
        avisos.append("BOPA: --seccion/--materia/--tipo/--num-* no existen en su buscador: ignorados.")
    texto, titulo, emisor = a.texto, a.titulo, a.emisor
    sumario = bool(emisor) or (not texto and not titulo)
    if titulo and not texto and not emisor:
        texto = titulo
        titulo = None
        avisos.append("BOPA: no tiene busqueda por titulo; uso -t como texto.")
    if sumario:
        if not desde:
            raise ValueError("BOPA con -e (o sin texto) lee los boletines dia a dia: indica --desde/--hasta, --fecha, --anio o --ultimos N")
        h = hasta or dt.date.today()
        dias = (h - desde).days + 1
        if dias > 400:
            raise ValueError("Rango demasiado grande (%d dias): maximo 400 por busqueda. Divide el rango." % dias)
        c["__fechas"] = [(desde + dt.timedelta(days=i)).isoformat() for i in range(dias)]
        if texto or titulo:
            avisos.append("BOPA: con -e se leen los sumarios; -c/-t solo filtra por el TITULO. Para buscar en el texto completo "
                          "usa -c sin -e (buscador del BOPA) o descarga y convierte los PDF (--txt) y busca con 'buscar'.")
    else:
        c["p_r_p_dispositionText"] = texto
        c["p_r_p_dispositionDate"] = ""
        if desde or hasta:
            c["__filtro"] = {"desde": desde.isoformat() if desde else "", "hasta": hasta.isoformat() if hasta else ""}
            avisos.append("BOPA: su buscador no tiene rango de fechas; lee los resultados del mas nuevo al mas antiguo y filtra aqui.")
    return c


CONSTRUCTORES = {"boe": campos_boe, "notif": campos_notif, "bocyl": campos_bocyl,
                 "bocm": campos_bocm, "boam": campos_boam, "bopa": campos_bopa}


def parse_campos_libres(lista):
    res = {}
    for x in (lista or []):
        if "=" not in x:
            raise ValueError("--campo espera NOMBRE=VALOR (por ejemplo --campo 'sort_field[0]=DEM_CONCATENADO')")
        k, v = x.split("=", 1)
        k = k.strip()
        if k in res:
            res[k] = (res[k] if isinstance(res[k], list) else [res[k]]) + [v]
        else:
            res[k] = v
    return res


def cmd_web(a):
    f = a.fuente
    con = abrir_db()
    try:
        desde, hasta = rango_remoto(a)
        avisos = []
        campos = CONSTRUCTORES[f](a, desde, hasta, avisos)
        campos.update(parse_campos_libres(a.campo))
        _ops(a)
    except ValueError as e:
        print("Error: %s" % e)
        return 2
    filtro_fecha = campos.pop("__filtro", None)
    criterios = [k for k, v in campos.items()
                 if v not in (None, "", []) and not k.startswith(("page_hits", "sort_", "operador", "dato[0][", "ordenacion"))]
    if not criterios:
        for av in avisos:
            print("Aviso: " + av)
        print("Indica al menos un criterio (-c texto, -e emisor, fechas...). Mira: bop.py web %s --help" % f)
        return 2
    for av in avisos:
        print("Aviso: " + av)

    job = {"id": uuid.uuid4().hex, "tipo": "busqueda", "fuente": f, "campos": campos,
           "max_paginas": max(1, a.max_paginas), "limite": a.limite or 0,
           "diag": bool(a.diagnostico), "timeout": 300}
    if filtro_fecha:
        job["filtro_fecha"] = filtro_fecha
    if campos.get("__fechas"):
        job["timeout"] = 120 + 3 * len(campos["__fechas"])
        job["max_paginas"] = max(job["max_paginas"], len(campos["__fechas"]))
    resultado = {}

    def procesar(r, hechos, total):
        resultado.update(r.get("datos") or {})

    def progreso(d):
        tot = (" de %s" % d["total"]) if d.get("total") is not None else ""
        if d.get("paginas_totales"):
            tot += " (pagina %s de %s)" % (d.get("pagina"), d.get("paginas_totales"))
        print("  pagina %s: +%s (acumulados %s%s)" % (d.get("pagina"), d.get("nuevos"), d.get("acumulados"), tot))
        sys.stdout.flush()

    print("Consultando %s ..." % FUENTES_WEB[f].split(" (")[0])
    ejecutar_jobs([job], a.puerto, procesar, al_progreso=progreso)
    if not resultado:
        return 1
    if resultado.get("error"):
        print("Error de la extension: %s" % resultado["error"])
        pq = guardar_bundle_web(f, sys.argv, campos, avisos, resultado)
        if pq:
            print("Informe de diagnostico guardado: %s  (enviamelo)" % pq)
        return 1
    for av in resultado.get("avisos") or []:
        print("Aviso: " + av)

    regs = resultado.get("registros") or []
    resultado.pop("html_diag", None)
    paquete = guardar_bundle_web(f, sys.argv, campos, avisos, resultado)
    if not regs:
        if resultado.get("vacio"):
            print("La web indica que no hay resultados para esa busqueda (si crees que si los hay, envia el informe).")
            if a.emisor and not a.texto and f in ("boe", "notif", "bocm"):
                print("Pista: en %s el campo emisor busca por el nombre exacto del organo tal como lo publica la web" % f.upper())
                print("(en el BOE son departamentos/ministerios). Para un ayuntamiento prueba con -c, por ejemplo:")
                print("  python bop.py web %s -c \"%s\"" % (f, a.emisor))
        else:
            print("No pude leer ningun resultado de la pagina. Si la web si mostraba resultados, envia el informe.")
        if paquete:
            print("Informe de diagnostico guardado: %s" % paquete)
        registrar_busqueda(con, f, campos, 0, resultado.get("total"), True)
        return 0

    nuevos, actual = guardar_registros(con, f, regs)
    completa = not resultado.get("hay_mas")
    registrar_busqueda(con, f, campos, len(regs), resultado.get("total"), completa)
    urls = [(r.get("url") or r.get("url_pdf")) for r in regs]
    filas = []
    for i in range(0, len(urls), 400):
        lote = urls[i:i + 400]
        filas.extend(con.execute("SELECT " + COLS + " FROM anuncios WHERE url IN (%s)" % ",".join("?" * len(lote)),
                                 lote).fetchall())
    filas.sort(key=lambda r: (r["fecha"], r["id"]), reverse=(a.orden != "antiguos"))
    if campos.get("__fechas"):
        # sumarios: se guarda todo el boletin, pero se muestra solo lo pedido
        if a.emisor:
            consultas = [norm(x) for x in _lista(a.emisor)]
            filas = [r for r in filas if coincide_emisor(r["emisor_norm"], consultas, a.exacto)]
        for t_ in [x for x in (a.texto, a.titulo) if x]:
            palabras = norm(t_).split()
            filas = [r for r in filas if all(p in (r["titulo_norm"] or "") for p in palabras)]
        regs_mostrados = len(filas)
        if not filas:
            print("Ningun anuncio de los %d leidos cumple -e/-c/-t. (Se han guardado en la base para busquedas locales.)" % len(regs))

    if a.pdf or a.txt or a.descargar:
        procesar_textos(con, filas, a)
        filas = [con.execute("SELECT " + COLS + " FROM anuncios WHERE id=?", (r["id"],)).fetchone() for r in filas]

    salida = []
    print()
    imprimir([(r, "titulo", "") for r in filas], ancho_terminal(), salida)
    total = resultado.get("total")
    resumen = "%d resultado(s) leidos (%d nuevos en la base, %d ya estaban)." % (len(regs), nuevos, actual)
    if campos.get("__fechas"):
        resumen += " Dias consultados: %d (sin boletin: %s)." % (len(campos["__fechas"]), resultado.get("dias_sin_boletin", "?"))
    if total is not None:
        resumen += " La web indica %d en total." % total
    print(resumen)
    if resultado.get("hay_mas"):
        print("HAY MAS RESULTADOS que no se han leido: sube --max-paginas (ahora %d) o acota con fechas/filtros." % a.max_paginas)
    elif total is not None and total > len(regs) and not a.limite and not filtro_fecha:
        print("Aviso: la web habla de %d resultados pero solo he podido leer %d: el lector no reconoce toda la pagina." % (total, len(regs)))
    if paquete:
        print("Informe de diagnostico: %s" % paquete)
    if resultado.get("usado_fallback"):
        print("Aviso: no pude leer el formulario de la web y use valores por defecto; los resultados podrian estar incompletos.")
    if a.guardar:
        with open(a.guardar, "w", encoding="utf-8") as fh:
            fh.write("\n".join(salida) + "\n" + resumen + "\n")
        print("Guardado en %s" % a.guardar)
    return 0


def cmd_textos(a):
    """Descarga/convierte los textos de anuncios YA guardados (cualquier fuente)."""
    con = abrir_db()
    if not (a.txt or a.pdf or a.descargar):
        print("Indica que hacer: --txt (pdf2txt.py), --pdf (pdftotext) y/o --descargar (solo guardar los PDF).")
        return 2
    a.fuentes = [x.lower() for x in _lista(a.fuente)] if a.fuente else None
    if a.fecha:
        a.desde = a.hasta = a.fecha
    if a.ultimos:
        a.hasta = a.hasta or dt.date.today()
        a.desde = a.desde or (a.hasta - dt.timedelta(days=a.ultimos))
    a.exacto = a.exacto
    a.frase = a.palabra = False
    if a.texto:
        base = [f for f, _, _ in filtrar(con, a)]
    else:
        base = filtrar(con, a, solo_fecha_emisor=True)
    base.sort(key=lambda r: (r["fecha"], r["id"]), reverse=True)
    print("%d anuncio(s) cumplen los filtros." % len(base))
    procesar_textos(con, base, a)
    return 0


def cmd_campos(a):
    f = a.fuente
    print(FUENTES_WEB[f])
    ayuda = {
        "boe": [("-c, --texto", "Texto (todo el contenido)"), ("-t, --titulo", "Titulo"), ("-e, --emisor", "Departamento"),
                ("--num-boletin", "Nº de boletin (formato n/aaaa)"), ("--num-oficial", "Nº oficial (n/aaaa o DPTO/n/aaaa)"),
                ("--desde/--hasta", "Fecha de publicacion"), ("--seccion", "1,2,3,4,5,T (por defecto todas)"),
                ("--page-hits", "50, 200, 500, 1000 o 2000 resultados por pagina (200)"),
                ("--orden", "nuevos (por defecto) o antiguos"),
                ("--op CAMPO=or", "Operador con el campo siguiente: titulo, emisor, texto, num_boletin, num_oficial")],
        "notif": [("-c, --texto", "Texto: NIF, nombre..."), ("-e, --emisor", "Departamento"), ("--materia", "Materia (codigo o nombre)"),
                  ("--num-boletin", "Nº de boletin"), ("--desde/--hasta", "Fecha de publicacion"),
                  ("--page-hits", "50, 200, 500, 1000 o 2000 (200)"), ("--orden", "nuevos o antiguos")],
        "bocyl": [("-c, --texto", "Texto (max. 100 caracteres; admite y / o / no entre palabras)"), ("-t, --titulo", "Titulo"),
                  ("-e, --emisor", "Organismo"), ("--num-boletin", "Nº de boletin"), ("--num-oficial", "Nº oficial"),
                  ("--desde/--hasta", "Fecha de publicacion"), ("--seccion", "I, IA..IE, II, III, IV, V (o codigo)"),
                  ("--tipo", "Rango: ANUNCIO, EDICTO, ORDEN... (varios con comas)"),
                  ("--orden", "nuevos, antiguos o relevancia")],
        "bocm": [("-c, --texto", "Buscar en todo el texto"), ("-e, --emisor", "Organo emisor"), ("--num-boletin", "Nº del boletin"),
                 ("--num-oficial", "Signatura"), ("--anio", "Año del boletin"), ("--desde/--hasta", "Fecha del boletin"),
                 ("--seccion", "I, II, III (administracion local), IV, V")],
        "bopa": [("-c, --texto", "Buscador de texto del BOPA (mas nuevo primero; --desde/--hasta se filtran aqui)"),
                 ("-e, --emisor", "Consejeria, organismo o ayuntamiento: lee los sumarios dia a dia (maximo 400 dias)"),
                 ("--desde/--hasta, -f, --anio, --ultimos", "Fechas (sin -c ni -e: todos los anuncios de esos dias)"),
                 ("--max-paginas", "Paginas de resultados de 20 (40 por defecto) en la busqueda de texto")],
        "boam": [("-c, --texto", "Buscar en texto"), ("-t, --titulo", "Buscar en titulo"), ("--desde/--hasta", "Fecha del boletin"),
                 ("--num-boletin", "Nº de BOAM, o rango 10200-10230"), ("--num-anuncio", "Nº de anuncio"),
                 ("--tipo", "Tipo de contenido (nombre o parte)")],
    }[f]
    print("\nOpciones de bop.py web %s:" % f)
    for k, v in ayuda:
        print("  %-18s %s" % (k, v))
    print("  %-18s %s" % ("--campo N=V", "cualquier campo del formulario con su nombre real (se puede repetir)"))
    listas = {"boe": [("Secciones", BOE_SECCIONES)], "notif": [("Materias (--materia)", NOTIF_MATERIAS)],
              "bocyl": [("Secciones (--seccion)", BOCYL_APARTADOS), ("Tipos (--tipo)", [(r, r) for r in BOCYL_RANGOS])],
              "bocm": [("Secciones (--seccion)", BOCM_SECCIONES)], "boam": [("Tipos de contenido (--tipo)", BOAM_TIPOS)]}[f]
    for titulo, ops in listas:
        print("\n%s:" % titulo)
        for cod, nombre in ops:
            print("  %-12s %s" % (cod if len(cod) <= 12 else cod[:9] + "...", nombre))
    return 0


def cmd_fuentes(a):
    con = abrir_db()
    print("Fuentes disponibles:\n")
    cuentas = {r["fuente"]: r["n"] for r in con.execute("SELECT fuente, COUNT(*) n FROM anuncios GROUP BY fuente")}
    print("  %-7s %6d guardados   BOP de Avila (bop.py buscar / sincronizar)" % ("avila", cuentas.get("avila", 0)))
    for k, v in FUENTES_WEB.items():
        print("  %-7s %6d guardados   %s  (bop.py web %s)" % (k, cuentas.get(k, 0), v, k))
    return 0


def cmd_historial(a):
    con = abrir_db()
    filas = con.execute("SELECT * FROM busquedas ORDER BY id DESC LIMIT ?", (a.n,)).fetchall()
    if not filas:
        print("Todavia no hay busquedas web registradas.")
        return 0
    for r in filas:
        p = json.loads(r["parametros"])
        legible = ", ".join("%s=%s" % (k, v) for k, v in p.items()
                            if v not in (None, "", []) and not k.startswith(("page_hits", "sort_", "operador", "dato[0]["))
                            or k in ("page_hits",))
        print("#%d  %s  [%s]  %d resultado(s)%s%s" % (
            r["id"], r["realizada"].replace("T", " "), r["fuente"].upper(), r["n_resultados"],
            (" de %s" % r["total_servidor"]) if r["total_servidor"] is not None else "",
            "" if r["completa"] else "  (incompleta)"))
        for linea in textwrap.wrap(legible, ancho_terminal() - 4):
            print("     " + linea)
    return 0


# ----------------------------------------------------------------------------
# Principal
# ----------------------------------------------------------------------------

def fecha_arg(s):
    try:
        return parse_fecha(s)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))


def main():
    p = argparse.ArgumentParser(
        prog="bop.py",
        description="Buscador local de boletines oficiales: BOP de Avila, BOE, notificaciones BOE, BOCYL, BOCM y BOAM "
                    "(con extension de Firefox).")
    p.add_argument("--version", action="version", version="bop.py " + VERSION)
    sub = p.add_subparsers(dest="cmd")

    def comunes(sp):
        sp.add_argument("--puerto", type=int, default=PUERTO_DEF, help="puerto local (8768)")

    b = sub.add_parser("buscar", help="buscar anuncios por emisor, texto y/o fechas")
    b.add_argument("-e", "--emisor", action="append",
                   help="ayuntamiento u organo emisor (se puede repetir). Ej: -e 'la adrada'")
    b.add_argument("--fuente", metavar="LISTA",
                   help="limitar a estas fuentes (coma): avila, boe, notif, bocyl, bocm, boam. "
                        "Sin esto se busca en todo lo guardado; las fuentes distintas de avila solo se leen en local")
    b.add_argument("--exacto", action="store_true",
                   help="el emisor debe llamarse igual (o 'AYUNTAMIENTO DE <texto>'); evita Sotillo de la Adrada al buscar La Adrada")
    b.add_argument("-c", "--texto", help="palabra(s) a buscar en el anuncio; con varias, deben estar todas")
    b.add_argument("--frase", action="store_true", help="buscar las palabras de -c como frase exacta")
    b.add_argument("--palabra", action="store_true", help="palabra completa (ordenanza no encuentra ordenanzas)")
    b.add_argument("-f", "--fecha", type=fecha_arg, help="un solo dia")
    b.add_argument("--desde", type=fecha_arg, help="fecha inicial")
    b.add_argument("--hasta", type=fecha_arg, help="fecha final (por defecto hoy)")
    b.add_argument("--ultimos", type=int, help="los ultimos N dias")
    b.add_argument("--pdf", action="store_true",
                   help="baja tambien el PDF de cada anuncio candidato y busca dentro (necesita poppler)")
    b.add_argument("--txt", action="store_true",
                   help="convierte cada anuncio con TU programa pdf2txt.py (por URL) y busca en el .txt")
    b.add_argument("--pdf2txt", metavar="RUTA", help="ruta de pdf2txt.py (se recuerda para la proxima vez)")
    b.add_argument("--descargar", action="store_true",
                   help="descarga los PDF a ~/bop/pdf (sin convertir, salvo que uses tambien --txt o --pdf)")
    b.add_argument("--max-pdf", type=int, default=80, help="maximo de anuncios a procesar de una vez (80; 0 = todos)")
    b.add_argument("--offline", action="store_true", help="no consultar la web, solo la base local")
    b.add_argument("--actualizar", action="store_true", help="volver a consultar los dias aunque ya esten guardados")
    b.add_argument("--fines-semana", action="store_true", help="consultar tambien sabados y domingos")
    b.add_argument("--limite", type=int, help="maximo de resultados a mostrar")
    b.add_argument("--antiguos-primero", action="store_true", help="orden cronologico (por defecto, lo mas nuevo primero)")
    b.add_argument("--guardar", metavar="ARCHIVO.txt", help="guardar los resultados en un archivo de texto")
    comunes(b)
    b.set_defaults(fn=cmd_buscar)

    s = sub.add_parser("sincronizar", help="descargar boletines a la base local sin buscar")
    s.add_argument("--desde", type=fecha_arg)
    s.add_argument("--hasta", type=fecha_arg)
    s.add_argument("--ultimos", type=int)
    s.add_argument("--actualizar", action="store_true")
    s.add_argument("--fines-semana", action="store_true")
    s.add_argument("--pdf", action="store_true",
                   help="baja tambien el texto de TODOS los PDF del rango (lento la primera vez, se puede reanudar)")
    s.add_argument("--txt", action="store_true", help="convierte todos los anuncios del rango con pdf2txt.py")
    s.add_argument("--pdf2txt", metavar="RUTA", help="ruta de pdf2txt.py")
    s.add_argument("--descargar", action="store_true", help="descarga los PDF a ~/bop/pdf (sin convertir)")
    s.add_argument("--max-pdf", type=int, default=0, help="limite de PDF a bajar en esta pasada (0 = todos)")
    comunes(s)
    s.set_defaults(fn=cmd_sincronizar)

    e = sub.add_parser("emisores", help="listar los emisores guardados")
    e.add_argument("-b", "--buscar", help="filtrar por texto")
    e.add_argument("--fuente", metavar="LISTA", help="limitar a estas fuentes (coma)")
    e.set_defaults(fn=cmd_emisores)

    st = sub.add_parser("estado", help="resumen de la base local")
    st.set_defaults(fn=cmd_estado)

    w = sub.add_parser("web", help="busqueda avanzada en BOE, notificaciones BOE, BOCYL, BOCM o BOAM; guarda los resultados",
                       description="Usa el buscador avanzado de la web (a traves de la extension de Firefox), guarda los "
                                   "resultados en la base local y, si quieres, descarga los PDF y los convierte a texto. "
                                   "Mira los campos de cada fuente con:  bop.py campos <fuente>")
    w.add_argument("fuente", choices=list(FUENTES_WEB), help="boe, notif, bocyl, bocm, boam o bopa")
    w.add_argument("-c", "--texto", help="texto a buscar")
    w.add_argument("-t", "--titulo", help="buscar en el titulo (BOE, BOCYL, BOAM)")
    w.add_argument("-e", "--emisor", help="departamento / organismo / organo emisor")
    w.add_argument("-f", "--fecha", type=fecha_arg, help="un solo dia de publicacion")
    w.add_argument("--desde", type=fecha_arg, help="fecha de publicacion inicial")
    w.add_argument("--hasta", type=fecha_arg, help="fecha de publicacion final")
    w.add_argument("--anio", type=int, help="un año completo")
    w.add_argument("--ultimos", type=int, help="los ultimos N dias")
    w.add_argument("--num-boletin", help="numero de boletin")
    w.add_argument("--num-oficial", help="numero oficial / signatura")
    w.add_argument("--num-anuncio", help="numero de anuncio (BOAM)")
    w.add_argument("--seccion", help="seccion(es), segun la fuente (ver: bop.py campos <fuente>)")
    w.add_argument("--tipo", help="tipo / rango / tipo de contenido, segun la fuente")
    w.add_argument("--materia", help="materia (notificaciones BOE)")
    w.add_argument("--orden", choices=["nuevos", "antiguos", "relevancia"], default="nuevos", help="orden (nuevos)")
    w.add_argument("--op", action="append", metavar="CAMPO=and|or|not",
                   help="operador entre campos del BOE/notificaciones, p. ej. --op texto=or")
    w.add_argument("--page-hits", type=int, choices=[50, 200, 500, 1000, 2000], default=200,
                   help="resultados por pagina en BOE/notificaciones (200)")
    w.add_argument("--campo", action="append", metavar="NOMBRE=VALOR",
                   help="cualquier campo del formulario con su nombre real (repetible)")
    w.add_argument("--max-paginas", type=int, default=40, help="paginas de resultados a leer como maximo (40)")
    w.add_argument("--limite", type=int, help="maximo de resultados a leer")
    w.add_argument("--exacto", action="store_true", help="BOPA con -e: el emisor debe llamarse igual (o 'AYUNTAMIENTO DE <texto>')")
    w.add_argument("--pdf", action="store_true", help="baja el PDF de los resultados y guarda su texto (pdftotext)")
    w.add_argument("--txt", action="store_true", help="convierte los PDF con TU programa pdf2txt.py")
    w.add_argument("--pdf2txt", metavar="RUTA", help="ruta de pdf2txt.py (se recuerda)")
    w.add_argument("--descargar", action="store_true", help="guarda los PDF en ~/bop/pdf")
    w.add_argument("--max-pdf", type=int, default=80, help="maximo de anuncios a procesar para texto/PDF (80; 0 = todos)")
    w.add_argument("--guardar", metavar="ARCHIVO.txt", help="guardar la lista de resultados en un archivo de texto")
    w.add_argument("--diagnostico", action="store_true",
                   help="guarda el HTML de la primera pagina de resultados en ~/bop/diag (para depurar)")
    comunes(w)
    w.set_defaults(fn=cmd_web)

    tx = sub.add_parser("textos", help="descargar/convertir los textos de anuncios ya guardados (cualquier fuente)")
    tx.add_argument("--fuente", metavar="LISTA", help="fuentes (coma)")
    tx.add_argument("-e", "--emisor", action="append", help="emisor (repetible)")
    tx.add_argument("--exacto", action="store_true")
    tx.add_argument("-c", "--texto", help="solo anuncios cuyo titulo/texto contenga estas palabras")
    tx.add_argument("-f", "--fecha", type=fecha_arg)
    tx.add_argument("--desde", type=fecha_arg)
    tx.add_argument("--hasta", type=fecha_arg)
    tx.add_argument("--ultimos", type=int)
    tx.add_argument("--pdf", action="store_true")
    tx.add_argument("--txt", action="store_true")
    tx.add_argument("--pdf2txt", metavar="RUTA")
    tx.add_argument("--descargar", action="store_true")
    tx.add_argument("--max-pdf", type=int, default=0, help="maximo a procesar (0 = todos)")
    comunes(tx)
    tx.set_defaults(fn=cmd_textos)

    cp = sub.add_parser("campos", help="campos y opciones del buscador de una fuente")
    cp.add_argument("fuente", choices=list(FUENTES_WEB))
    cp.set_defaults(fn=cmd_campos)

    fu = sub.add_parser("fuentes", help="listar las fuentes y cuantos anuncios hay de cada una")
    fu.set_defaults(fn=cmd_fuentes)

    hi = sub.add_parser("historial", help="ultimas busquedas web realizadas")
    hi.add_argument("-n", type=int, default=15, help="cuantas mostrar (15)")
    hi.set_defaults(fn=cmd_historial)

    inf = sub.add_parser("informe", help="juntar en un .zip los ultimos informes de diagnostico para enviarlos")
    inf.add_argument("--ultimos", type=int, default=5, help="cuantos informes de busqueda incluir (5)")
    inf.set_defaults(fn=cmd_informe)

    d = sub.add_parser("diagnostico", help="guardar el HTML crudo de un dia (para depurar el analizador)")
    d.add_argument("fecha", help="DD-MM-AAAA")
    d.add_argument("--url", help="otra URL en lugar de la del dia")
    comunes(d)
    d.set_defaults(fn=cmd_diagnostico)

    a = p.parse_args()
    if not a.cmd:
        p.print_help()
        return 0
    try:
        return a.fn(a)
    except BrokenPipeError:
        try:
            sys.stdout = open(os.devnull, "w")
        except Exception:
            pass
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCancelado.")
        sys.exit(130)
