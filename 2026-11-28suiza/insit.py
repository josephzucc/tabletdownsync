#!/usr/bin/env python3
"""insit: indice, descarga y busqueda local del archivo de Inside IT (inside-it.ch).

Como funciona:
  1. 'indice' baja los sitemaps publicos de inside-it.ch y guarda en local la lista
     de TODOS los articulos del archivo (direccion, fecha y titulo sacado de la direccion).
  2. 'titulos' busca en ese indice por palabras, sin red.
  3. 'bajar' descarga el texto completo de los articulos elegidos usando la extension
     insit de Firefox, que usa TU sesion de abonado (cookies de Firefox).
  4. 'buscar' busca por palabras o frases dentro del texto ya descargado, sin red.
  5. 'paquete' junta articulos en .txt de 121 columnas para subirselos a Claude.

Uso rapido (ver insitManualBono.txt):
  python3 insit.py servidor            (en una sesion de Termux aparte; dejar abierto)
  python3 insit.py indice
  python3 insit.py titulos gemeinde informatik --desde 2023-01-01
  python3 insit.py bajar gemeinde informatik --desde 2023-01-01 --max 20
  python3 insit.py buscar "access datenbank" --exportar
  python3 insit.py plan insitPlan.txt --desde 2022-01-01
"""
import argparse
import atexit
import datetime
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
import unicodedata
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import error, parse, request

VERSION = "1.0.0"
# Puerto provisional: comprobar en puertos.txt que esta libre (empsui usa 8774).
PUERTO_DEFECTO = int(os.environ.get("INSIT_PUERTO", "8791"))
ANCHO = 121
TIMEOUT = 40
USER_AGENT = ("Mozilla/5.0 (Android 14; Mobile; rv:140.0) Gecko/140.0 Firefox/140.0")
BASE = "https://www.inside-it.ch"
SITEMAP_INDICE = BASE + "/sitemap.xml"
DIR = os.environ.get("INSIT_DIR") or os.path.join(os.path.expanduser("~"), "insit")
RUTA_DB = os.path.join(DIR, "insit.db")
DIR_LOGS = os.path.join(DIR, "logs")
_DESCARGAS = os.path.join(os.path.expanduser("~"), "storage", "downloads")
DIR_SALIDA = (os.environ.get("INSIT_SALIDA")
              or (os.path.join(_DESCARGAS, "insit") if os.path.isdir(_DESCARGAS)
                  else os.path.join(DIR, "salidas")))

MARCAS_MURO = ("Lesen Sie den vollständigen Artikel", "Abo auswählen und weiterlesen",
               "Lesen Sie den vollst&auml;ndigen Artikel")
RE_ID = re.compile(r"([a-z0-9][a-z0-9-]*?)-(\d{8})(?:-(\d+))?$")
RE_ENLACE = re.compile(r'href="((?:https://(?:www\.)?inside-it\.ch)?/(?:post/)?[a-z0-9][a-z0-9-]*-\d{8}(?:-\d+)?)"')


# ---------------------------------------------------------------------------
# Utilidades y registro
# ---------------------------------------------------------------------------

def sello(t=None):
    return (t or datetime.datetime.now()).strftime("%Y-%m-%d-%H-%M-%S")


class Registro:
    """Log por ejecucion; al terminar se renombra con la fecha/hora de su ultima escritura."""

    def __init__(self):
        self.ruta = None
        self.ultima = None

    def escribe(self, linea):
        try:
            if self.ruta is None:
                os.makedirs(DIR_LOGS, exist_ok=True)
                self.ruta = os.path.join(DIR_LOGS, f"en-curso-{os.getpid()}.log")
            with open(self.ruta, "a", encoding="utf-8") as f:
                f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {linea}\n")
            self.ultima = datetime.datetime.now()
        except OSError:
            pass

    def cierra(self):
        if self.ruta and self.ultima and os.path.exists(self.ruta):
            destino = os.path.join(DIR_LOGS, f"{sello(self.ultima)}logInsit.log")
            n = 1
            while os.path.exists(destino):
                destino = os.path.join(DIR_LOGS, f"{sello(self.ultima)}logInsit-{n}.log")
                n += 1
            try:
                os.replace(self.ruta, destino)
            except OSError:
                pass


REG = Registro()
atexit.register(REG.cierra)


def out(msg=""):
    print(msg, flush=True)


def log(msg):
    print(msg, flush=True)
    REG.escribe(msg)


def err(msg):
    print(msg, file=sys.stderr, flush=True)
    REG.escribe("ERROR " + msg)


def normaliza(s):
    """minusculas, umlauts como en las direcciones (ae, oe, ue), sin acentos."""
    s = (s or "").lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        s = s.replace(a, b)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[-_\u2010-\u2014/]", " ", s)  # guiones = espacio (Access-Datenbank = access datenbank)
    return re.sub(r"\s+", " ", s)


def envolver(txt, indent="", resto=None, ancho=ANCHO):
    resto = indent if resto is None else resto
    lineas = []
    for parrafo in (txt or "").split("\n"):
        if not parrafo.strip():
            lineas.append("")
            continue
        lineas += textwrap.wrap(parrafo.strip(), ancho, initial_indent=indent, subsequent_indent=resto,
                                break_long_words=True, break_on_hyphens=False) or [""]
    return "\n".join(lineas)


def nombre_archivo(txt, maximo=50):
    t = re.sub(r"[^a-z0-9]+", "_", normaliza(txt)).strip("_")
    return (t or "sin_nombre")[:maximo]


def fecha_arg(valor):
    if not valor:
        return None
    v = valor.strip()
    if re.fullmatch(r"\d{4}", v):
        return v + "-01-01"
    if re.fullmatch(r"\d{4}-\d{2}", v):
        return v + "-01"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        return v
    raise argparse.ArgumentTypeError("fecha: AAAA, AAAA-MM o AAAA-MM-DD")


def id_de_url(url):
    camino = parse.urlparse(url).path.rstrip("/")
    ult = camino.rsplit("/", 1)[-1]
    m = RE_ID.fullmatch(ult)
    if not m:
        return None, None, None
    slug, f8 = m.group(1), m.group(2)
    try:
        fecha = datetime.date(int(f8[:4]), int(f8[4:6]), int(f8[6:])).isoformat()
    except ValueError:
        fecha = None
    return ult, slug, fecha


def url_canonica(id_):
    return f"{BASE}/{id_}"


# ---------------------------------------------------------------------------
# Expresion de busqueda: palabras (todas), "frases", -excluir, palabra|otra
# ---------------------------------------------------------------------------

class Consulta:
    def __init__(self, terminos, alguna=False, palabra=False):
        self.incluir, self.excluir = [], []
        for t in terminos:
            t = t.strip()
            if not t:
                continue
            if t.startswith("no:") and len(t) > 3:
                self.excluir.append(normaliza(t[3:]))
            elif t.startswith("-") and len(t) > 1:
                self.excluir.append(normaliza(t[1:]))
            else:
                self.incluir.append([normaliza(x) for x in t.split("|") if x.strip()])
        self.alguna = alguna
        self.palabra = palabra

    def _esta(self, texto, t):
        if t.startswith("="):
            t = t[1:]
            return re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", texto) is not None
        if self.palabra:
            return re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", texto) is not None
        return t in texto

    def coincide(self, texto):
        texto = normaliza(texto)
        if any(self._esta(texto, x) for x in self.excluir):
            return False
        if not self.incluir:
            return True
        grupos = [any(self._esta(texto, alt) for alt in g) for g in self.incluir]
        return any(grupos) if self.alguna else all(grupos)

    def todas_las_palabras(self):
        return [alt.lstrip("=") for g in self.incluir for alt in g]

    def para_slug(self):
        """Las direcciones se normalizan igual (guiones = espacios)."""
        return self


# ---------------------------------------------------------------------------
# Base de datos
# ---------------------------------------------------------------------------

def abre_db():
    os.makedirs(DIR, exist_ok=True)
    db = sqlite3.connect(RUTA_DB)
    db.row_factory = sqlite3.Row
    db.executescript("""
    CREATE TABLE IF NOT EXISTS articulos (
        id TEXT PRIMARY KEY, url TEXT, fecha TEXT, slug TEXT, origen TEXT,
        titulo TEXT, autor TEXT, lead TEXT, cuerpo TEXT, etiquetas TEXT,
        estado TEXT DEFAULT 'indice', bajado TEXT, chars INTEGER DEFAULT 0, nota TEXT);
    CREATE INDEX IF NOT EXISTS i_fecha ON articulos(fecha);
    CREATE INDEX IF NOT EXISTS i_estado ON articulos(estado);
    CREATE TABLE IF NOT EXISTS sitemaps (url TEXT PRIMARY KEY, n INTEGER, bajado TEXT);
    """)
    return db


def alta_indice(db, url, origen):
    id_, slug, fecha = id_de_url(url)
    if not id_:
        return False
    cur = db.execute("INSERT OR IGNORE INTO articulos(id, url, fecha, slug, origen) VALUES(?,?,?,?,?)",
                     (id_, url_canonica(id_), fecha, slug, origen))
    return cur.rowcount > 0


def filtra(db, consulta=None, desde=None, hasta=None, estado=None, en="todo", limite=None):
    sql, par = "SELECT * FROM articulos WHERE 1=1", []
    if desde:
        sql += " AND fecha >= ?"
        par.append(desde)
    if hasta:
        sql += " AND fecha <= ?"
        par.append(hasta)
    if estado:
        sql += " AND estado = ?"
        par.append(estado)
    sql += " ORDER BY fecha DESC, id"
    res = []
    cs = consulta.para_slug() if consulta else None
    for f in db.execute(sql, par):
        if consulta:
            if en == "slug":
                ok = cs.coincide(f["slug"] or "")
            elif en == "titulo":
                ok = cs.coincide(f["slug"] or "") or consulta.coincide(f["titulo"] or "")
            else:
                texto = " \n ".join(x or "" for x in (f["titulo"], f["lead"], f["cuerpo"], f["etiquetas"]))
                ok = cs.coincide(f["slug"] or "") or consulta.coincide(texto)
            if not ok:
                continue
        res.append(f)
        if limite and len(res) >= limite:
            break
    return res


def titulo_de(f):
    if f["titulo"]:
        return f["titulo"]
    return (f["slug"] or f["id"]).replace("-", " ").capitalize()


# ---------------------------------------------------------------------------
# Cliente HTTP: directo (Termux) o a traves de la extension (sesion de Firefox)
# ---------------------------------------------------------------------------

def http_directo(url):
    req = request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "de-CH,de;q=0.9"})
    try:
        with request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read()
            return r.status, raw.decode(r.headers.get_content_charset() or "utf-8", errors="replace")
    except error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except error.URLError as e:
        raise RuntimeError(f"No se pudo conectar con {url}: {e.reason}")


def bajar_texto(url, puerto, via="auto"):
    """Sitemaps (publicos): directo; si falla, por la extension."""
    if via in ("auto", "directo"):
        try:
            st, txt = http_directo(url)
            if st < 400:
                return txt
            if via == "directo":
                raise RuntimeError(f"HTTP {st} en {url}")
        except RuntimeError:
            if via == "directo":
                raise
    r = pedir_trabajo({"op": "fetch", "url": url}, puerto)
    if int(r.get("status", 0)) >= 400:
        raise RuntimeError(f"HTTP {r.get('status')} en {url}")
    return r.get("text", "")


# ---------------------------------------------------------------------------
# Analisis del HTML de un articulo
# ---------------------------------------------------------------------------

BLOQUES = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "blockquote", "tr", "section", "article"}


class ATexto(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.partes, self.salta = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.salta += 1
        elif tag in BLOQUES:
            self.partes.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self.salta = max(0, self.salta - 1)
        elif tag in BLOQUES:
            self.partes.append("\n")

    def handle_data(self, d):
        if not self.salta:
            self.partes.append(d)


def html_a_texto(frag):
    p = ATexto()
    try:
        p.feed(frag or "")
    except Exception:
        return re.sub(r"<[^>]+>", " ", frag or "")
    t = "".join(p.partes)
    t = re.sub(r"[ \t\xa0]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n\n", t)
    return "\n".join(x.strip() for x in t.split("\n")).strip()


class Parrafos(HTMLParser):
    """Recoge <p>, <h2>, <h3>, <li> dentro de <article> (o <main>), fuera de nav/footer/aside."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.pila, self.bloques, self.actual = [], [], None
        self.en_art = 0
        self.fuera = 0
        self.tiene_article = False

    def handle_starttag(self, tag, attrs):
        if tag in ("br", "img", "hr", "meta", "link", "input"):
            return
        self.pila.append(tag)
        if tag == "article":
            self.en_art += 1
            self.tiene_article = True
        if tag in ("nav", "footer", "aside", "script", "style", "form", "button"):
            self.fuera += 1
        if tag in ("p", "h2", "h3", "li", "blockquote") and self.actual is None:
            self.actual = [tag, [], self.en_art > 0]

    def handle_endtag(self, tag):
        if tag not in self.pila:
            return
        while self.pila:
            t = self.pila.pop()
            if t == "article":
                self.en_art = max(0, self.en_art - 1)
            if t in ("nav", "footer", "aside", "script", "style", "form", "button"):
                self.fuera = max(0, self.fuera - 1)
            if self.actual is not None and t == self.actual[0]:
                txt = re.sub(r"\s+", " ", "".join(self.actual[1])).strip()
                if txt:
                    self.bloques.append((self.actual[0], txt, self.actual[2]))
                self.actual = None
            if t == tag:
                break

    def handle_data(self, d):
        if self.actual is not None and not self.fuera:
            self.actual[1].append(d)


def _corta_muro(t):
    for m in MARCAS_MURO:
        i = t.find(html.unescape(m))
        if i >= 0:
            t = t[:i]
    return t.strip()


def _meta(html_txt, nombre):
    for patron in (r'<meta[^>]+(?:property|name)="%s"[^>]+content="([^"]*)"',
                   r'<meta[^>]+content="([^"]*)"[^>]+(?:property|name)="%s"'):
        m = re.search(patron % re.escape(nombre), html_txt, re.I)
        if m:
            return html.unescape(m.group(1)).strip()
    return ""


def _jsonld_cuerpo(html_txt):
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html_txt, re.S | re.I):
        try:
            datos = json.loads(m.group(1))
        except ValueError:
            continue
        pila = [datos]
        while pila:
            x = pila.pop()
            if isinstance(x, dict):
                if isinstance(x.get("articleBody"), str) and len(x["articleBody"]) > 200:
                    return html_a_texto(x["articleBody"])
                pila += list(x.values())
            elif isinstance(x, list):
                pila += x
    return ""


def _next_data_cuerpo(html_txt):
    m = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html_txt, re.S)
    if not m:
        return ""
    try:
        datos = json.loads(m.group(1))
    except ValueError:
        return ""
    mejor = ""

    def textos(x):
        if isinstance(x, str):
            return [x] if len(x) >= 40 and not re.match(r"^(https?:|/|data:)", x) else []
        if isinstance(x, dict):
            r = []
            for k, v in x.items():
                if k.lower() in ("teaser", "related", "relatedarticles", "seo", "meta", "image", "images"):
                    continue
                r += textos(v)
            return r
        if isinstance(x, list):
            r = []
            for v in x:
                r += textos(v)
            return r
        return []

    pila = [datos]
    while pila:
        x = pila.pop()
        if isinstance(x, dict):
            for k, v in x.items():
                if re.search(r"body|content|blocks|richtext|text", k, re.I) and isinstance(v, (str, list, dict)):
                    cand = "\n\n".join(html_a_texto(s) for s in textos(v))
                    if len(cand) > len(mejor):
                        mejor = cand
                if isinstance(v, (dict, list)):
                    pila.append(v)
        elif isinstance(x, list):
            pila += x
    return _corta_muro(mejor)


def analiza_articulo(html_txt, id_):
    titulo = _meta(html_txt, "og:title")
    if not titulo:
        m = re.search(r"<title[^>]*>(.*?)</title>", html_txt, re.S | re.I)
        titulo = html_a_texto(m.group(1)) if m else ""
    titulo = re.sub(r"\s*[|–-]\s*Inside IT\s*$", "", titulo).strip()
    lead = _meta(html_txt, "og:description") or _meta(html_txt, "description")
    autor = _meta(html_txt, "author")
    if not autor:
        m = re.search(r">\s*Von\s+([^<]{3,80}?)\s*<", html_txt)
        autor = html.unescape(m.group(1)).strip() if m else ""
    fecha = (_meta(html_txt, "article:published_time") or "")[:10]
    etiquetas = []
    for t in re.findall(r'/suche\?t=([^"&<>\s]+)', html_txt):
        t = parse.unquote_plus(html.unescape(t))
        if t not in etiquetas:
            etiquetas.append(t)

    p = Parrafos()
    try:
        p.feed(html_txt)
    except Exception:
        pass
    bloques = [b for b in p.bloques if b[2]] if p.tiene_article else p.bloques
    dom = []
    for tag, txt, _ in bloques:
        if any(html.unescape(mm) in txt for mm in MARCAS_MURO):
            break
        if txt in ("Mehr zum Thema", "Mehr anzeigen") or txt == titulo:
            continue
        dom.append(("## " + txt) if tag in ("h2", "h3") else txt)
    dom_txt = _corta_muro("\n\n".join(dom))

    candidatos = [("jsonld", _jsonld_cuerpo(html_txt)), ("dom", dom_txt), ("next", _next_data_cuerpo(html_txt))]
    cuerpo, metodo = "", ""
    for nombre, c in candidatos:
        if nombre == "jsonld" and len(c) >= 500:
            cuerpo, metodo = c, nombre
            break
        if nombre == "dom" and len(c) >= 600:
            cuerpo, metodo = c, nombre
            break
        if len(c) > len(cuerpo):
            cuerpo, metodo = c, nombre
    if lead and cuerpo.startswith(lead):
        cuerpo = cuerpo[len(lead):].strip()
    muro = any(m in html_txt for m in MARCAS_MURO)
    recortado = muro and len(cuerpo) < 1500
    return {"titulo": titulo, "lead": lead, "autor": autor, "fecha": fecha, "cuerpo": cuerpo,
            "etiquetas": ", ".join(etiquetas), "recortado": recortado, "metodo": metodo, "muro": muro}


# ---------------------------------------------------------------------------
# Comandos
# ---------------------------------------------------------------------------

def cmd_indice(a):
    db = abre_db()
    log(f"Bajando el indice de sitemaps: {SITEMAP_INDICE}")
    txt = bajar_texto(SITEMAP_INDICE, a.puerto, a.via)
    try:
        raiz = ET.fromstring(txt.encode("utf-8"))
    except ET.ParseError as e:
        raise RuntimeError(f"El indice no es XML valido ({e}). Prueba: --via extension")
    ns = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
    hijos = [e.text.strip() for e in raiz.iter(ns + "loc") if e.text]
    numerados = sorted([h for h in hijos if re.search(r"sitemap-\d+\.xml$", h)],
                       key=lambda u: int(re.search(r"(\d+)\.xml$", u).group(1)))
    otros = [h for h in hijos if h not in numerados]
    hechos = {f["url"] for f in db.execute("SELECT url FROM sitemaps")}
    # Siempre se repasan los 2 ultimos numerados (articulos recientes) y los no bajados.
    recientes = set(numerados[-2:])
    lista = [h for h in otros + numerados if a.todo or h not in hechos or h in recientes]
    log(f"Sitemaps en el indice: {len(hijos)}; a bajar ahora: {len(lista)}")
    nuevos_total = 0
    for i, u in enumerate(lista, 1):
        try:
            x = bajar_texto(u, a.puerto, a.via)
            r = ET.fromstring(x.encode("utf-8"))
        except (RuntimeError, ET.ParseError) as e:
            err(f"  [{i}/{len(lista)}] {u}: {e}")
            continue
        n = nuevos = 0
        for e in r.iter(ns + "loc"):
            if e.text:
                n += 1
                if alta_indice(db, e.text.strip(), "sitemap"):
                    nuevos += 1
        db.execute("INSERT OR REPLACE INTO sitemaps(url, n, bajado) VALUES(?,?,?)", (u, n, sello()))
        db.commit()
        nuevos_total += nuevos
        log(f"  [{i}/{len(lista)}] {u.rsplit('/', 1)[-1]}: {n} direcciones, {nuevos} nuevas")
        time.sleep(a.pausa)
    tot = db.execute("SELECT COUNT(*), MIN(fecha), MAX(fecha) FROM articulos").fetchone()
    log(f"Indice: {tot[0]} articulos ({tot[1]} a {tot[2]}); nuevos en esta pasada: {nuevos_total}")
    return 0


def linea_resumen(f, n):
    est = {"indice": " ", "completo": "T", "recortado": "R", "error": "E"}.get(f["estado"], "?")
    cab = f"{n:>4}. {f['fecha'] or '????-??-??'} [{est}] "
    return envolver(titulo_de(f), cab, " " * len(cab))


def muestra_lista(filas, mostrar, titulo):
    out(titulo)
    out("-" * ANCHO)
    for n, f in enumerate(filas[:mostrar], 1):
        out(linea_resumen(f, n))
    if len(filas) > mostrar:
        out(f"... y {len(filas) - mostrar} mas (usa --mostrar N o --exportar)")
    out("Estado: [ ] solo en indice  [T] texto completo  [R] recortado por el muro  [E] error")


def exporta_lista(filas, nombre, cabecera):
    os.makedirs(DIR_SALIDA, exist_ok=True)
    ruta = os.path.join(DIR_SALIDA, f"{sello()}{nombre}.txt")
    with open(ruta, "w", encoding="utf-8") as fh:
        fh.write(envolver(cabecera) + "\n" + "=" * ANCHO + "\n")
        for n, f in enumerate(filas, 1):
            fh.write(linea_resumen(f, n) + "\n" + envolver(f["url"], " " * 6) + "\n")
    log(f"Guardado: {ruta}")
    return ruta


def cmd_titulos(a):
    db = abre_db()
    if not db.execute("SELECT 1 FROM articulos LIMIT 1").fetchone():
        raise RuntimeError("El indice esta vacio. Ejecuta primero: python3 insit.py indice")
    c = Consulta(a.terminos, a.alguna, a.palabra)
    filas = filtra(db, c, a.desde, a.hasta, en="titulo")
    muestra_lista(filas, a.mostrar, f"Titulos con: {' '.join(a.terminos)}  ({len(filas)} articulos)")
    if a.exportar:
        exporta_lista(filas, "insitTitulos_" + nombre_archivo(" ".join(a.terminos)),
                      f"insit {VERSION} - titulos con: {' '.join(a.terminos)} - desde {a.desde or '-'} "
                      f"hasta {a.hasta or '-'} - {len(filas)} articulos")
    return 0


def descarga_uno(db, f, a):
    url = f["url"]
    modo = a.modo
    datos, html_txt = None, ""
    intentos = ["fetch", "pestana"] if modo == "auto" else [modo]
    for m in intentos:
        if m == "fetch":
            r = pedir_trabajo({"op": "fetch", "url": url}, a.puerto)
        else:
            r = pedir_trabajo({"op": "pestana", "url": url, "espera": a.espera}, a.puerto, espera=180)
        st = int(r.get("status", 200) or 200)
        if st >= 400:
            raise RuntimeError(f"HTTP {st}")
        html_txt = r.get("text", "")
        datos = analiza_articulo(html_txt, f["id"])
        datos["modo"] = m
        if not datos["recortado"] and len(datos["cuerpo"]) >= 200:
            break
    estado = "recortado" if datos["recortado"] else "completo"
    db.execute("""UPDATE articulos SET titulo=?, autor=?, lead=?, cuerpo=?, etiquetas=?, estado=?, bajado=?,
                  chars=?, nota=?, fecha=COALESCE(NULLIF(?, ''), fecha) WHERE id=?""",
               (datos["titulo"], datos["autor"], datos["lead"], datos["cuerpo"], datos["etiquetas"], estado,
                sello(), len(datos["cuerpo"]), f"modo={datos['modo']} metodo={datos['metodo']}",
                datos["fecha"], f["id"]))
    db.commit()
    return estado, datos


def cmd_bajar(a):
    db = abre_db()
    if a.url:
        for u in a.url:
            if not alta_indice(db, u, "manual"):
                if not id_de_url(u)[0]:
                    raise RuntimeError(f"Direccion no reconocida como articulo: {u}")
        db.commit()
        filas = [db.execute("SELECT * FROM articulos WHERE id=?", (id_de_url(u)[0],)).fetchone() for u in a.url]
    else:
        if not a.terminos and not a.todo_rango:
            raise RuntimeError("Indica palabras, --url o --todo-rango (con --desde/--hasta)")
        c = Consulta(a.terminos, a.alguna, a.palabra) if a.terminos else None
        filas = filtra(db, c, a.desde, a.hasta, en="titulo")
    if not a.repetir:
        filas = [f for f in filas if f["estado"] in ("indice", "error") or (a.reintentar and f["estado"] == "recortado")]
    filas = filas[:a.max]
    if not filas:
        log("Nada que bajar (ya descargado o sin coincidencias). Usa --repetir para volver a bajar.")
        return 0
    log(f"A descargar: {len(filas)} articulos (pausa {a.pausa} s, modo {a.modo})")
    cont = {"completo": 0, "recortado": 0, "error": 0}
    for i, f in enumerate(filas, 1):
        try:
            est, d = descarga_uno(db, f, a)
            cont[est] += 1
            log(f"  [{i}/{len(filas)}] {est:9} {len(d['cuerpo']):>6} car.  {f['fecha']} {d['titulo'][:70]}")
        except RuntimeError as e:
            cont["error"] += 1
            db.execute("UPDATE articulos SET estado='error', nota=? WHERE id=?", (str(e)[:300], f["id"]))
            db.commit()
            err(f"  [{i}/{len(filas)}] error  {f['id']}: {e}")
        if i < len(filas):
            time.sleep(a.pausa)
    log(f"Hecho: {cont['completo']} completos, {cont['recortado']} recortados, {cont['error']} errores")
    if cont["recortado"]:
        log("Recortados = Firefox no tiene la sesion de abonado activa. Entra en inside-it.ch con tu cuenta")
        log("en Firefox y repite con: --reintentar")
    return 0


def _fragmento(texto, palabras, ancho=300):
    tn = normaliza(texto)
    for p in palabras:
        i = tn.find(p)
        if i >= 0:
            ini = max(0, i - ancho // 3)
            return ("..." if ini else "") + re.sub(r"\s+", " ", texto[ini:ini + ancho]).strip() + "..."
    return re.sub(r"\s+", " ", texto[:ancho]).strip()


def bloque_articulo(f):
    partes = ["=" * ANCHO, envolver(titulo_de(f).upper()),
              envolver(f"Fecha: {f['fecha'] or '-'}   Autor: {f['autor'] or '-'}   Estado: {f['estado']}"),
              envolver(f"Direccion: {f['url']}")]
    if f["etiquetas"]:
        partes.append(envolver(f"Etiquetas: {f['etiquetas']}"))
    partes.append("-" * ANCHO)
    if f["lead"]:
        partes.append(envolver(f["lead"]))
        partes.append("")
    partes.append(envolver(f["cuerpo"] or "(sin texto descargado: usa 'bajar')"))
    return "\n".join(partes) + "\n"


def cmd_buscar(a):
    db = abre_db()
    c = Consulta(a.terminos, a.alguna, a.palabra)
    estado = None if a.incluir_indice else "completo"
    filas = filtra(db, c, a.desde, a.hasta, estado=estado, en=a.en)
    if not a.incluir_indice:
        filas += [f for f in filtra(db, c, a.desde, a.hasta, estado="recortado", en=a.en)]
        filas.sort(key=lambda f: f["fecha"] or "", reverse=True)
    out(f"Resultados para: {' '.join(a.terminos)}  ({len(filas)} articulos descargados)")
    out("-" * ANCHO)
    palabras = c.todas_las_palabras()
    for n, f in enumerate(filas[:a.mostrar], 1):
        out(linea_resumen(f, n))
        texto = " ".join(x or "" for x in (f["lead"], f["cuerpo"]))
        if texto.strip():
            out(envolver(_fragmento(texto, palabras), " " * 6))
    if len(filas) > a.mostrar:
        out(f"... y {len(filas) - a.mostrar} mas")
    if a.exportar:
        return _escribe_paquete(filas, "insitBusqueda_" + nombre_archivo(" ".join(a.terminos)),
                                f"Busqueda: {' '.join(a.terminos)}", a.max_car)
    return 0


def _escribe_paquete(filas, nombre, titulo, max_car):
    os.makedirs(DIR_SALIDA, exist_ok=True)
    marca = sello()
    rutas, buf, parte = [], [], 1
    cab = (f"insit {VERSION} - {titulo} - {len(filas)} articulos - generado {marca}\n"
           "Fuente: Inside IT (inside-it.ch). Uso personal del abonado; no republicar.\n")

    def vuelca():
        nonlocal buf, parte
        if not buf:
            return
        ruta = os.path.join(DIR_SALIDA, f"{marca}{nombre}" + (f"_parte{parte}" if parte > 1 or len(rutas) else "") + ".txt")
        with open(ruta, "w", encoding="utf-8") as fh:
            fh.write(envolver(cab) + "\n" + "".join(buf))
        rutas.append(ruta)
        buf, parte = [], parte + 1

    tam = 0
    for f in filas:
        b = bloque_articulo(f)
        if buf and tam + len(b) > max_car:
            vuelca()
            tam = 0
        buf.append(b)
        tam += len(b)
    vuelca()
    for r in rutas:
        log(f"Guardado: {r}")
    return 0


def cmd_paquete(a):
    db = abre_db()
    c = Consulta(a.terminos, a.alguna, a.palabra) if a.terminos else None
    filas = filtra(db, c, a.desde, a.hasta, en=a.en)
    filas = [f for f in filas if f["estado"] in ("completo", "recortado")][:a.max]
    if not filas:
        raise RuntimeError("No hay articulos descargados que cumplan el filtro")
    return _escribe_paquete(filas, "insitPaquete_" + nombre_archivo(" ".join(a.terminos) or "todo"),
                            f"Paquete: {' '.join(a.terminos) or 'todo'} desde {a.desde or '-'}", a.max_car)


def cmd_ver(a):
    db = abre_db()
    id_ = id_de_url(a.id)[0] or a.id
    f = db.execute("SELECT * FROM articulos WHERE id=? OR id LIKE ?", (id_, f"%{id_}%")).fetchone()
    if not f:
        raise RuntimeError("No encontrado")
    out(bloque_articulo(f))
    return 0


def lee_plan(ruta):
    lineas, seccion = [], ""
    with open(ruta, encoding="utf-8") as fh:
        for n, l in enumerate(fh, 1):
            l = l.strip()
            if not l or l.startswith("#"):
                continue
            if l.startswith("[") and l.endswith("]"):
                seccion = l[1:-1]
                continue
            terminos = [t.strip('"') for t in re.findall(r'"[^"]+"|\S+', l)]
            palabra = "@palabra" in terminos
            terminos = [t for t in terminos if t != "@palabra"]
            lineas.append((seccion, l, terminos, palabra))
    return lineas


def cmd_plan(a):
    db = abre_db()
    if not db.execute("SELECT 1 FROM articulos LIMIT 1").fetchone():
        raise RuntimeError("El indice esta vacio. Ejecuta primero: python3 insit.py indice")
    plan = lee_plan(a.archivo)
    os.makedirs(DIR_SALIDA, exist_ok=True)
    ruta = os.path.join(DIR_SALIDA, f"{sello()}insitPlan.txt")
    vistos, informe, a_bajar = set(), [], []
    for seccion, linea, terminos, palabra in plan:
        c = Consulta(terminos, palabra=a.palabra or palabra)
        filas = filtra(db, c, a.desde, a.hasta, en="titulo")
        nuevos = [f for f in filas if f["id"] not in vistos]
        vistos.update(f["id"] for f in filas)
        informe.append((seccion, linea, filas, len(nuevos)))
        a_bajar += [f for f in nuevos[:a.por_linea]]
        log(f"  {len(filas):>5} ({len(nuevos):>4} nuevos)  [{seccion}] {linea}")
    with open(ruta, "w", encoding="utf-8") as fh:
        fh.write(envolver(f"insit {VERSION} - informe del plan {os.path.basename(a.archivo)} - desde "
                          f"{a.desde or '-'} hasta {a.hasta or '-'} - {len(vistos)} articulos distintos") + "\n")
        fh.write("=" * ANCHO + "\n")
        sec_ant = None
        for seccion, linea, filas, nuevos in informe:
            if seccion != sec_ant:
                fh.write("\n" + envolver(seccion.upper()) + "\n" + "-" * ANCHO + "\n")
                sec_ant = seccion
            fh.write(envolver(f"{len(filas):>5} articulos ({nuevos} no vistos antes): {linea}") + "\n")
            for n, f in enumerate(filas[:a.mostrar], 1):
                fh.write(linea_resumen(f, n) + "\n")
    log(f"Informe: {ruta}")
    if a.bajar:
        a_bajar = a_bajar[:a.max]
        log(f"Bajando {len(a_bajar)} articulos del plan (max {a.por_linea} por linea, {a.max} en total)...")
        a.url, a.terminos, a.todo_rango, a.repetir, a.reintentar = None, [], False, False, False
        ids = [f["id"] for f in a_bajar]
        filas = [db.execute("SELECT * FROM articulos WHERE id=?", (i,)).fetchone() for i in ids]
        filas = [f for f in filas if f["estado"] in ("indice", "error")]
        for i, f in enumerate(filas, 1):
            try:
                est, d = descarga_uno(db, f, a)
                log(f"  [{i}/{len(filas)}] {est:9} {f['fecha']} {d['titulo'][:80]}")
            except RuntimeError as e:
                db.execute("UPDATE articulos SET estado='error', nota=? WHERE id=?", (str(e)[:300], f["id"]))
                db.commit()
                err(f"  [{i}/{len(filas)}] error {f['id']}: {e}")
            time.sleep(a.pausa)
    return 0


def cmd_etiqueta(a):
    """Abre /suche?t=ETIQUETA en Firefox (sesion propia), pulsa 'Mehr anzeigen' y anade los articulos."""
    db = abre_db()
    url = f"{BASE}/suche?t={parse.quote(a.etiqueta)}"
    log(f"Leyendo {url} en Firefox (pulsando 'Mehr anzeigen' hasta {a.clics} veces)...")
    r = pedir_trabajo({"op": "pestana", "url": url, "espera": a.espera, "clics": a.clics}, a.puerto,
                      espera=60 + a.clics * 15)
    enlaces = []
    for e in RE_ENLACE.findall(r.get("text", "")):
        u = e if e.startswith("http") else BASE + e
        if u not in enlaces:
            enlaces.append(u)
    nuevos = sum(1 for u in enlaces if alta_indice(db, u, "etiqueta:" + a.etiqueta))
    db.commit()
    log(f"Etiqueta '{a.etiqueta}': {len(enlaces)} articulos encontrados, {nuevos} nuevos en el indice")
    ids = [id_de_url(u)[0] for u in enlaces]
    filas = [db.execute("SELECT * FROM articulos WHERE id=?", (i,)).fetchone() for i in ids if i]
    muestra_lista([f for f in filas if f], a.mostrar, f"Articulos con la etiqueta {a.etiqueta}")
    if a.exportar:
        exporta_lista([f for f in filas if f], "insitEtiqueta_" + nombre_archivo(a.etiqueta),
                      f"Etiqueta {a.etiqueta}: {len(filas)} articulos")
    return 0


def cmd_volcar(a):
    r = pedir_trabajo({"op": "pestana" if a.pestana else "fetch", "url": a.url, "espera": a.espera},
                      a.puerto, espera=180)
    os.makedirs(DIR_SALIDA, exist_ok=True)
    id_ = id_de_url(a.url)[0] or "pagina"
    ruta = os.path.join(DIR_SALIDA, f"{sello()}volcadoInsit_{id_[:60]}.html")
    with open(ruta, "w", encoding="utf-8") as fh:
        fh.write(r.get("text", ""))
    d = analiza_articulo(r.get("text", ""), id_)
    log(f"HTTP {r.get('status')}  {len(r.get('text', ''))} car. de HTML -> {ruta}")
    log(f"Analisis: titulo='{d['titulo'][:80]}' cuerpo={len(d['cuerpo'])} car. metodo={d['metodo']} "
        f"muro_visible={d['muro']} recortado={d['recortado']}")
    return 0


def cmd_estado(a):
    db = abre_db()
    t = db.execute("SELECT COUNT(*), MIN(fecha), MAX(fecha) FROM articulos").fetchone()
    out(f"insit {VERSION}   carpeta: {DIR}   salidas: {DIR_SALIDA}")
    out(f"Indice: {t[0]} articulos, de {t[1]} a {t[2]}")
    for f in db.execute("SELECT estado, COUNT(*) n FROM articulos GROUP BY estado ORDER BY n DESC"):
        out(f"  {f['estado']:10} {f['n']}")
    out(f"Sitemaps bajados: {db.execute('SELECT COUNT(*) FROM sitemaps').fetchone()[0]}")
    try:
        e = pedir_estado(a.puerto)
        h = e.get("sondeo_hace")
        out(f"Puente: servidor activo en {a.puerto}; ultima senal de la extension hace "
            f"{'nunca' if h is None else f'{h:.0f} s'}")
    except Exception:
        out(f"Puente: no hay servidor en el puerto {a.puerto} (python3 insit.py servidor)")
    return 0


def cmd_diag(a):
    db = abre_db()
    os.makedirs(DIR_SALIDA, exist_ok=True)
    ruta = os.path.join(DIR_SALIDA, f"{sello()}diagInsit.txt")
    lin = [f"insit {VERSION} diagnostico {sello()}", f"python {sys.version.split()[0]}  sqlite {sqlite3.sqlite_version}",
           f"DIR={DIR}  SALIDA={DIR_SALIDA}  puerto={a.puerto}"]
    t = db.execute("SELECT COUNT(*), MIN(fecha), MAX(fecha) FROM articulos").fetchone()
    lin.append(f"articulos={t[0]} rango={t[1]}..{t[2]}")
    for f in db.execute("SELECT estado, COUNT(*) n FROM articulos GROUP BY estado"):
        lin.append(f"  estado {f['estado']}: {f['n']}")
    for f in db.execute("SELECT origen, COUNT(*) n FROM articulos GROUP BY origen"):
        lin.append(f"  origen {f['origen']}: {f['n']}")
    lin.append("Ultimos 15 descargados (id, estado, car., nota):")
    for f in db.execute("SELECT id, estado, chars, nota FROM articulos WHERE bajado IS NOT NULL "
                        "ORDER BY bajado DESC LIMIT 15"):
        lin.append(f"  {f['id'][:70]} {f['estado']} {f['chars']} {f['nota']}")
    lin.append("Ultimos errores:")
    for f in db.execute("SELECT id, nota FROM articulos WHERE estado='error' LIMIT 10"):
        lin.append(f"  {f['id'][:70]}: {f['nota']}")
    try:
        st, txt = http_directo(SITEMAP_INDICE)
        lin.append(f"Prueba directa sitemap: HTTP {st}, {len(txt)} car.")
    except RuntimeError as e:
        lin.append(f"Prueba directa sitemap: {e}")
    try:
        e = pedir_estado(a.puerto)
        lin.append(f"Puente: activo; senal hace {e.get('sondeo_hace')} s; ext_url={e.get('ext_url')}; "
                   f"version_ext={e.get('version_ext')}")
    except Exception as ex:
        lin.append(f"Puente: no responde ({ex})")
    logs = sorted(os.listdir(DIR_LOGS)) if os.path.isdir(DIR_LOGS) else []
    lin.append(f"Logs: {len(logs)}; ultimo: {logs[-1] if logs else '-'}")
    if logs:
        with open(os.path.join(DIR_LOGS, logs[-1]), encoding="utf-8", errors="replace") as fh:
            lin += ["  | " + x.rstrip() for x in fh.readlines()[-40:]]
    with open(ruta, "w", encoding="utf-8") as fh:
        fh.write("\n".join(envolver(x) for x in lin) + "\n")
    log(f"Diagnostico: {ruta}")
    return 0


# ---------------------------------------------------------------------------
# Puente con la extension (servidor local 127.0.0.1)
# ---------------------------------------------------------------------------

class EstadoPuente:
    def __init__(self):
        self.pendientes, self.resultados = [], {}
        self.lock = threading.Lock()
        self.n = 0
        self.ultimo_sondeo = None
        self.ext_url = None
        self.version_ext = None


def crear_handler(estado):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _cors(self):
            origen = self.headers.get("Origin", "")
            if origen.startswith("moz-extension://"):
                self.send_header("Access-Control-Allow-Origin", origen)
                self.send_header("Access-Control-Allow-Headers", "X-Insit, Content-Type")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

        def _resp(self, codigo, obj=None):
            cuerpo = json.dumps(obj).encode("utf-8") if obj is not None else b""
            self.send_response(codigo)
            self._cors()
            if obj is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(cuerpo)))
            self.end_headers()
            self.wfile.write(cuerpo)

        def _autorizado(self):
            if self.headers.get("X-Insit", "") != "1":
                return False
            if self.headers.get("Host", "").split(":")[0] not in ("127.0.0.1", "localhost"):
                return False
            origen = self.headers.get("Origin", "")
            return not origen or origen.startswith("moz-extension://")

        def _cuerpo(self):
            n = int(self.headers.get("Content-Length", 0) or 0)
            return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}

        def do_OPTIONS(self):
            if not self.headers.get("Origin", "").startswith("moz-extension://"):
                return self._resp(403, {"error": "origen"})
            self._resp(204)

        def do_GET(self):
            if not self._autorizado():
                return self._resp(403, {"error": "no autorizado"})
            if self.path == "/estado":
                with estado.lock:
                    hace = None if estado.ultimo_sondeo is None else time.time() - estado.ultimo_sondeo
                    return self._resp(200, {"sondeo_hace": hace, "ext_url": estado.ext_url,
                                            "version_ext": estado.version_ext,
                                            "pendientes": len(estado.pendientes)})
            if self.path == "/trabajo":
                with estado.lock:
                    estado.ultimo_sondeo = time.time()
                    if estado.pendientes:
                        return self._resp(200, estado.pendientes.pop(0))
                return self._resp(204)
            if self.path.startswith("/resultado/"):
                rid = self.path.rsplit("/", 1)[1]
                with estado.lock:
                    if rid in estado.resultados:
                        return self._resp(200, estado.resultados.pop(rid))
                return self._resp(202, {"estado": "pendiente"})
            self._resp(404, {"error": "no existe"})

        def do_POST(self):
            if not self._autorizado():
                return self._resp(403, {"error": "no autorizado"})
            try:
                datos = self._cuerpo()
            except ValueError:
                return self._resp(400, {"error": "json"})
            if self.path == "/trabajo":
                with estado.lock:
                    estado.n += 1
                    datos["id"] = str(estado.n)
                    estado.pendientes.append(datos)
                return self._resp(200, {"id": datos["id"]})
            if self.path == "/resultado":
                with estado.lock:
                    estado.resultados[str(datos.get("id"))] = datos
                return self._resp(200, {"ok": True})
            if self.path == "/hola":
                with estado.lock:
                    estado.ext_url = str(datos.get("url", ""))[:300] or estado.ext_url
                    estado.version_ext = str(datos.get("version", ""))[:20] or estado.version_ext
                    estado.ultimo_sondeo = time.time()
                return self._resp(200, {"ok": True})
            if self.path == "/registro":
                nombre = str(datos.get("nombre", ""))
                if not re.fullmatch(r"[0-9]{4}(-[0-9]{2}){5}logInsitExt\.log", nombre):
                    return self._resp(400, {"error": "nombre"})
                os.makedirs(DIR_LOGS, exist_ok=True)
                with open(os.path.join(DIR_LOGS, nombre), "w", encoding="utf-8") as fh:
                    fh.write(str(datos.get("texto", ""))[:200000])
                return self._resp(200, {"ok": True})
            self._resp(404, {"error": "no existe"})

    return H


def cmd_servidor(a):
    estado = EstadoPuente()
    srv = ThreadingHTTPServer(("127.0.0.1", a.puerto), crear_handler(estado))
    log(f"Servidor del puente insit en 127.0.0.1:{a.puerto} (solo este telefono). Ctrl+C para parar.")
    log("La extension insit escucha sola en segundo plano mientras Firefox este abierto.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("Parado.")
    return 0


def _base(puerto):
    return f"http://127.0.0.1:{puerto}"


def _cab():
    return {"X-Insit": "1", "Content-Type": "application/json"}


def pedir_estado(puerto):
    req = request.Request(_base(puerto) + "/estado", headers=_cab())
    with request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


def despertar_extension(puerto):
    """Si la extension no da senales, intenta abrir su pagina en Firefox con 'am start'."""
    try:
        est = pedir_estado(puerto)
    except Exception:
        return
    hace, url = est.get("sondeo_hace"), est.get("ext_url")
    if hace is not None and hace < 15:
        return
    if not url or os.environ.get("INSIT_NO_ABRIR") or not shutil.which("am"):
        if not url:
            log("(La extension aun no se ha presentado: abre Firefox; si sigue sin responder, pulsa su icono.)")
        return
    log("La extension no responde: intentando despertar Firefox...")
    try:
        subprocess.run(["am", "start", "-a", "android.intent.action.VIEW", "-d", url],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
    except Exception as e:
        err(f"(am start fallo: {e})")


def pedir_trabajo(params, puerto=PUERTO_DEFECTO, espera=120):
    try:
        req = request.Request(_base(puerto) + "/trabajo", data=json.dumps(params).encode("utf-8"),
                              headers=_cab(), method="POST")
        with request.urlopen(req, timeout=10) as r:
            rid = json.loads(r.read().decode("utf-8"))["id"]
    except (error.URLError, OSError):
        raise RuntimeError("No hay servidor del puente. Arrancalo en otra sesion de Termux: "
                           "python3 insit.py servidor")
    despertar_extension(puerto)
    t0, avisado = time.time(), False
    while time.time() - t0 < espera:
        try:
            req = request.Request(f"{_base(puerto)}/resultado/{rid}", headers=_cab())
            with request.urlopen(req, timeout=10) as r:
                if r.status == 200:
                    res = json.loads(r.read().decode("utf-8"))
                    if res.get("error"):
                        raise RuntimeError("La extension informo de un error: " + res["error"])
                    return res
        except error.URLError:
            raise RuntimeError("Se perdio la conexion con el servidor del puente.")
        if not avisado and time.time() - t0 > 20:
            log("  esperando a la extension (Firefox abierto con la extension insit instalada)...")
            avisado = True
        time.sleep(1.0)
    raise RuntimeError("La extension no respondio a tiempo.")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv=None):
    comun = argparse.ArgumentParser(add_help=False)
    comun.add_argument("--puerto", type=int, default=PUERTO_DEFECTO)
    filtro = argparse.ArgumentParser(add_help=False)
    filtro.add_argument("--desde", type=fecha_arg, help="Fecha minima AAAA, AAAA-MM o AAAA-MM-DD")
    filtro.add_argument("--hasta", type=fecha_arg, help="Fecha maxima")
    filtro.add_argument("--alguna", action="store_true", help="Basta con que aparezca una de las palabras")
    filtro.add_argument("--palabra", action="store_true", help="Palabra exacta (no dentro de compuestos)")
    filtro.add_argument("--mostrar", type=int, default=30, help="Resultados en pantalla (30)")
    descarga = argparse.ArgumentParser(add_help=False)
    descarga.add_argument("--modo", choices=["auto", "fetch", "pestana"], default="auto",
                          help="auto: rapido y, si sale recortado, abre pestana")
    descarga.add_argument("--pausa", type=float, default=4.0, help="Segundos entre articulos (4)")
    descarga.add_argument("--espera", type=float, default=4.0, help="Segundos de espera en modo pestana (4)")
    descarga.add_argument("--max", type=int, default=30, help="Maximo de articulos por ejecucion (30)")

    p = argparse.ArgumentParser(prog="insit.py", description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"insit {VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("servidor", parents=[comun], help="Servidor del puente con la extension (dejar abierto)")
    s.set_defaults(func=cmd_servidor)

    i = sub.add_parser("indice", parents=[comun], help="Bajar/actualizar la lista de todo el archivo")
    i.add_argument("--todo", action="store_true", help="Volver a bajar todos los sitemaps")
    i.add_argument("--via", choices=["auto", "directo", "extension"], default="auto")
    i.add_argument("--pausa", type=float, default=1.5)
    i.set_defaults(func=cmd_indice)

    t = sub.add_parser("titulos", parents=[comun, filtro], help="Buscar en los titulos del indice (sin red)")
    t.add_argument("terminos", nargs="+")
    t.add_argument("--exportar", action="store_true")
    t.set_defaults(func=cmd_titulos)

    b = sub.add_parser("bajar", parents=[comun, filtro, descarga], help="Descargar texto completo (con tu sesion)")
    b.add_argument("terminos", nargs="*")
    b.add_argument("--url", action="append", help="Direccion concreta (repetible)")
    b.add_argument("--todo-rango", action="store_true", dest="todo_rango",
                   help="Todos los articulos entre --desde y --hasta")
    b.add_argument("--repetir", action="store_true", help="Volver a bajar aunque ya este")
    b.add_argument("--reintentar", action="store_true", help="Volver a bajar los recortados")
    b.set_defaults(func=cmd_bajar)

    bu = sub.add_parser("buscar", parents=[comun, filtro], help="Buscar en el texto descargado (sin red)")
    bu.add_argument("terminos", nargs="+")
    bu.add_argument("--en", choices=["todo", "titulo"], default="todo")
    bu.add_argument("--incluir-indice", action="store_true", dest="incluir_indice",
                    help="Incluir tambien los no descargados (solo por titulo)")
    bu.add_argument("--exportar", action="store_true", help="Guardar los articulos completos en .txt")
    bu.add_argument("--max-car", type=int, default=250000, dest="max_car", help="Tamano maximo por .txt")
    bu.set_defaults(func=cmd_buscar)

    pq = sub.add_parser("paquete", parents=[comun, filtro], help="Juntar articulos descargados en .txt para Claude")
    pq.add_argument("terminos", nargs="*")
    pq.add_argument("--en", choices=["todo", "titulo"], default="todo")
    pq.add_argument("--max", type=int, default=200)
    pq.add_argument("--max-car", type=int, default=250000, dest="max_car")
    pq.set_defaults(func=cmd_paquete)

    pl = sub.add_parser("plan", parents=[comun, filtro, descarga], help="Ejecutar un plan de busquedas (insitPlan.txt)")
    pl.add_argument("archivo")
    pl.add_argument("--bajar", action="store_true", help="Descargar tambien los encontrados")
    pl.add_argument("--por-linea", type=int, default=10, dest="por_linea", help="Max a bajar por linea (10)")
    pl.set_defaults(func=cmd_plan)

    e = sub.add_parser("etiqueta", parents=[comun], help="Leer una etiqueta del sitio (p. ej. e-government)")
    e.add_argument("etiqueta")
    e.add_argument("--clics", type=int, default=10, help="Veces que pulsa 'Mehr anzeigen' (10)")
    e.add_argument("--espera", type=float, default=4.0)
    e.add_argument("--mostrar", type=int, default=30)
    e.add_argument("--exportar", action="store_true")
    e.set_defaults(func=cmd_etiqueta)

    v = sub.add_parser("ver", parents=[comun], help="Mostrar un articulo guardado")
    v.add_argument("id")
    v.set_defaults(func=cmd_ver)

    vo = sub.add_parser("volcar", parents=[comun], help="Guardar el HTML de una pagina (diagnostico)")
    vo.add_argument("url")
    vo.add_argument("--pestana", action="store_true", help="Leerla abriendo una pestana")
    vo.add_argument("--espera", type=float, default=4.0)
    vo.set_defaults(func=cmd_volcar)

    es = sub.add_parser("estado", parents=[comun], help="Resumen de lo guardado y del puente")
    es.set_defaults(func=cmd_estado)

    d = sub.add_parser("diag", parents=[comun], help="Crear archivo de diagnostico para Claude")
    d.set_defaults(func=cmd_diag)

    argv = list(sys.argv[1:] if argv is None else argv)
    # '-palabra' (excluir) no es una opcion: se convierte en 'no:palabra'
    argv = [("no:" + x[1:]) if re.fullmatch(r"-[^-\d\s][^\s]*", x) and x != "-h" else x for x in argv]
    a = p.parse_args(argv)
    REG.escribe("orden: " + " ".join(argv))
    try:
        return a.func(a) or 0
    except RuntimeError as ex:
        err(f"Error: {ex}")
        return 1
    except KeyboardInterrupt:
        err("Interrumpido.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
