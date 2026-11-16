#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
webcambios.py - Vigila webs, avisa si han cambiado y las convierte a TXT o PDF.

Pensado para Termux (solo usa la libreria estandar; para PDF necesita fpdf2).

Instalacion en Termux:
    pkg install python
    pip install fpdf2          (solo si quieres PDF)

Uso rapido:
    python webcambios.py vigilar URL [-n nombre] [-s seccion]
    python webcambios.py comprobar [nombre | URL]
    python webcambios.py lista
    python webcambios.py quitar nombre
    python webcambios.py txt URL [-s seccion] [-o fichero]
    python webcambios.py pdf URL [-s seccion] [-o fichero]

Codigos de salida de "comprobar": 0 = sin cambios, 1 = hay cambios, 2 = error.
"""

import argparse
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
import unicodedata
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.request import Request, urlopen

BASE = os.path.join(os.path.expanduser("~"), ".webcambios")
CFG = os.path.join(BASE, "webs.json")
SNAP = os.path.join(BASE, "snapshots")
UA = "Mozilla/5.0 (Android 14; Mobile; rv:130.0) Gecko/130.0 Firefox/130.0"
ANCHO = 80
MAX_SNAP = 20
# Lineas que cambian solas en cada visita y no cuentan como cambio
IGNORAR_DEF = [r"^Fecha y hora oficial", r"^Fecha:?$", r"^Hora:?$"]

VACIAS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
          "meta", "param", "source", "track", "wbr"}
IGNORADAS = {"script", "style", "noscript", "template", "svg", "iframe",
             "select", "button", "textarea", "head"}
SIN_RAIZ = {"nav", "header", "footer", "aside"}
CABECERAS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
BLOQUE = {"p", "div", "section", "article", "ul", "ol", "li", "tr", "table",
          "blockquote", "dd", "dt", "dl", "pre", "main", "figure",
          "figcaption", "details", "summary", "fieldset", "legend"}


# ----------------------------------------------------------------- descarga
def descargar(url, reintentos=2):
    url = urldefrag(url)[0]
    error = None
    for _ in range(reintentos + 1):
        try:
            req = Request(url, headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "es-ES,es;q=0.9"})
            with urlopen(req, timeout=30) as r:
                datos = r.read()
                cs = r.headers.get_content_charset()
                final = r.geturl()
            break
        except Exception as e:  # noqa
            error = e
            time.sleep(2)
    else:
        raise RuntimeError("No se pudo descargar %s: %s" % (url, error))
    if not cs:
        m = re.search(rb'<meta[^>]+charset=["\']?([\w-]+)', datos[:4096], re.I)
        cs = m.group(1).decode() if m else "utf-8"
    try:
        html = datos.decode(cs, errors="replace")
    except LookupError:
        html = datos.decode("utf-8", errors="replace")
    return html, final


# --------------------------------------------------------------- extraccion
def detectar_raiz(html):
    if re.search(r'id=["\']main-content["\']', html):
        return ("id", "main-content")
    if re.search(r"<main[\s>]", html, re.I):
        return ("tag", "main")
    return None


class Extractor(HTMLParser):
    """Convierte HTML en lista de (nivel, texto, id_propio, id_previo)."""

    def __init__(self, base, raiz, enlaces):
        super().__init__(convert_charrefs=True)
        self.base, self.raiz, self.enlaces = base, raiz, enlaces
        self.pila = []
        self.skip_at = None
        self.root_at = None
        self.fin = False
        self.buf = []
        self.lineas = []
        self.nivel = 0
        self.hid = None
        self.pid = None
        self.href = None
        self.a_ini = 0
        self.prefijo = None

    @property
    def activo(self):
        if self.fin or self.skip_at is not None:
            return False
        return self.raiz is None or self.root_at is not None

    def cortar(self):
        texto = re.sub(r"\s+", " ", "".join(self.buf)).strip()
        self.buf = []
        self.prefijo = None
        if texto:
            self.lineas.append((self.nivel, texto, self.hid, self.pid))
            self.pid = None

    def handle_starttag(self, tag, attrs):
        if self.fin:
            return
        a = dict(attrs)
        if tag in VACIAS:
            if self.activo and tag in ("br", "hr"):
                self.cortar()
            return
        self.pila.append(tag)
        pos = len(self.pila) - 1
        if self.raiz and self.root_at is None:
            if (self.raiz[0] == "id" and a.get("id") == self.raiz[1]) or \
               (self.raiz[0] == "tag" and tag == self.raiz[1]):
                self.root_at = pos
        if self.skip_at is None and (
                tag in IGNORADAS or (self.raiz is None and tag in SIN_RAIZ)):
            self.skip_at = pos
        if not self.activo:
            return
        if tag in CABECERAS:
            self.cortar()
            self.nivel = CABECERAS[tag]
            self.hid = a.get("id")
            return
        if a.get("id"):
            self.pid = a["id"]
        if tag in BLOQUE:
            self.cortar()
            if tag == "li":
                self.prefijo = "- "
        elif tag in ("td", "th"):
            if "".join(self.buf).strip():
                self.buf.append(" | ")
        elif tag == "a" and self.enlaces:
            h = a.get("href")
            if h and not h.lower().startswith(("#", "javascript:", "tel:")):
                self.href = urljoin(self.base, h)
                self.a_ini = len(self.buf)

    def handle_endtag(self, tag):
        if self.fin or tag in VACIAS or tag not in self.pila:
            return
        idx = len(self.pila) - 1 - self.pila[::-1].index(tag)
        if self.activo:
            if tag == "a" and self.href:
                t = "".join(self.buf[self.a_ini:]).strip()
                if t and self.href not in t:
                    self.buf.append(" [%s]" % self.href)
                self.href = None
            if tag in CABECERAS:
                self.cortar()
                self.nivel = 0
                self.hid = None
            elif tag in BLOQUE:
                self.cortar()
        del self.pila[idx:]
        if self.skip_at is not None and len(self.pila) <= self.skip_at:
            self.skip_at = None
        if self.root_at is not None and len(self.pila) <= self.root_at:
            self.cortar()
            self.root_at = None
            self.fin = True

    def handle_data(self, data):
        if not self.activo:
            return
        if self.prefijo and data.strip():
            self.buf.append(self.prefijo)
            self.prefijo = None
        self.buf.append(data)

    def close(self):
        super().close()
        self.cortar()


def extraer(html, base, enlaces=True):
    raiz = detectar_raiz(html)
    ex = Extractor(base, raiz, enlaces)
    ex.feed(html)
    ex.close()
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    titulo = re.sub(r"\s+", " ", m.group(1)).strip() if m else base
    import html as _h
    return _h.unescape(titulo), ex.lineas


def norm(s):
    s = unicodedata.normalize("NFD", s.lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def seleccionar_seccion(lineas, clave):
    """Devuelve las lineas de la seccion pedida, o None si no se encuentra."""
    k = norm(clave).strip()
    raiz = k[:max(5, len(k) - 2)]
    cand = None
    for i, (n, t, h, p) in enumerate(lineas):          # id del titular
        if n and h and norm(h) == k:
            cand = i
            break
    if cand is None:                                   # texto del titular
        for i, (n, t, h, p) in enumerate(lineas):
            if n and (k in norm(t) or raiz in norm(t)):
                cand = i
                break
    if cand is None:                                   # id de un contenedor
        for i, (n, t, h, p) in enumerate(lineas):
            if n and p and norm(p) == k:
                cand = i
                break
    if cand is None:
        return None
    nivel = lineas[cand][0]
    fin = len(lineas)
    for j in range(cand + 1, len(lineas)):
        if 0 < lineas[j][0] <= nivel:
            fin = j
            break
    return lineas[cand:fin]


def filtrar(lineas, patrones):
    res = [re.compile(p, re.I) for p in patrones]
    return [l for l in lineas if not any(r.search(l[1]) for r in res)]


def preparar(url, seccion, enlaces, ignorar):
    html, final = descargar(url)
    titulo, lineas = extraer(html, final, enlaces)
    clave = seccion or urlparse(url).fragment or None
    aviso = None
    if clave:
        sel = seleccionar_seccion(lineas, clave)
        if sel is None:
            aviso = "Aviso: no encuentro la seccion '%s'; uso toda la pagina." % clave
        else:
            lineas = sel
    lineas = filtrar(lineas, IGNORAR_DEF + list(ignorar or []))
    if not lineas:
        raise RuntimeError("La pagina no tiene texto util (puede cargarse con "
                           "JavaScript o estar bloqueada).")
    return titulo, final, lineas, aviso


# ------------------------------------------------------------------ formato
def formatear(lineas, titulo=None, url=None, seccion=None):
    out = []
    if titulo:
        out += textwrap.wrap(titulo.upper(), ANCHO) or [""]
        out += ["=" * ANCHO]
        out += ["Origen : " + url] if url else []
        if seccion:
            out += ["Seccion: " + seccion]
        out += ["Fecha  : " + datetime.now().strftime("%d/%m/%Y %H:%M"), ""]
    ajustar = dict(width=ANCHO, break_long_words=False, break_on_hyphens=False)
    for n, t, h, p in lineas:
        if n in (1, 2):
            w = textwrap.wrap(t.upper(), **ajustar)
            out += [""] + w + [("=" if n == 1 else "-") * min(
                max(len(x) for x in w), ANCHO)]
        elif n >= 3:
            out += [""] + textwrap.wrap(t, **ajustar)
        elif t.startswith("- "):
            out += textwrap.wrap(t, subsequent_indent="  ", **ajustar)
        else:
            out += textwrap.wrap(t, **ajustar)
    return "\n".join(out).strip("\n") + "\n"


def carpeta_salida():
    for p in ("~/storage/downloads", "~/storage/shared/Download"):
        p = os.path.expanduser(p)
        if os.path.isdir(p):
            return p
    return os.getcwd()


def slug(url):
    u = urlparse(url)
    s = (u.path.strip("/").split("/")[-1] or u.netloc)
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-")
    return s or "web"


def ruta_salida(url, ext, salida):
    if salida:
        return os.path.expanduser(salida)
    nombre = "%s_%s.%s" % (slug(url), datetime.now().strftime("%Y%m%d_%H%M"), ext)
    return os.path.join(carpeta_salida(), nombre)


def escribir_txt(lineas, titulo, url, seccion, ruta):
    with open(ruta, "w", encoding="utf-8") as f:
        f.write(formatear(lineas, titulo, url, seccion))
    return ruta


def escribir_pdf(lineas, titulo, url, seccion, ruta):
    try:
        from fpdf import FPDF
        from fpdf.enums import XPos, YPos
    except ImportError:
        print("Para crear PDF hace falta:  pip install fpdf2")
        return None
    cambios = {"\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'",
               "\u2013": "-", "\u2014": "-", "\u2026": "...", "\u2022": "-",
               "\u20ac": "EUR", "\u00a0": " ", "\u200b": ""}

    def L(s):
        for a, b in cambios.items():
            s = s.replace(a, b)
        s = re.sub(r"(\S{45})(?=\S)", r"\1 ", s)     # URLs largas
        return s.encode("latin-1", "replace").decode("latin-1")

    pdf = FPDF(format="A4")
    pdf.set_margins(15, 15, 15)
    pdf.set_auto_page_break(True, 15)
    pdf.add_page()

    def bloque(txt, estilo, tam, alto, color=(0, 0, 0), antes=0):
        if antes:
            pdf.ln(antes)
        pdf.set_font("Helvetica", estilo, tam)
        pdf.set_text_color(*color)
        pdf.multi_cell(0, alto, L(txt), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    bloque(titulo, "B", 15, 7)
    bloque(url, "", 8, 4, (90, 90, 90))
    info = "Generado: " + datetime.now().strftime("%d/%m/%Y %H:%M")
    if seccion:
        info += "   Seccion: " + seccion
    bloque(info, "", 8, 4, (90, 90, 90))
    pdf.ln(3)
    tams = {1: 15, 2: 13, 3: 11}
    for n, t, h, p in lineas:
        if n:
            bloque(t, "B", tams.get(n, 10), 6, antes=3)
        elif t.startswith("- "):
            pdf.set_x(20)
            pdf.set_font("Helvetica", "", 10)
            pdf.set_text_color(0, 0, 0)
            pdf.multi_cell(0, 5, L(t), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        else:
            bloque(t, "", 10, 5, antes=1)
    pdf.output(ruta)
    return ruta


# -------------------------------------------------------------------- estado
def cargar():
    try:
        with open(CFG, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def guardar(cfg):
    os.makedirs(BASE, exist_ok=True)
    tmp = CFG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CFG)


def carpeta_snap(nombre):
    d = os.path.join(SNAP, nombre)
    os.makedirs(d, exist_ok=True)
    return d


def ultimo_snapshot(nombre):
    d = carpeta_snap(nombre)
    fich = sorted(f for f in os.listdir(d) if f.endswith(".snap"))
    if not fich:
        return ""
    with open(os.path.join(d, fich[-1]), encoding="utf-8") as f:
        return f.read()


def guardar_snapshot(nombre, texto):
    d = carpeta_snap(nombre)
    ruta = os.path.join(d, datetime.now().strftime("%Y%m%d_%H%M%S") + ".snap")
    with open(ruta, "w", encoding="utf-8") as f:
        f.write(texto)
    fich = sorted(f for f in os.listdir(d) if f.endswith(".snap"))
    for viejo in fich[:-MAX_SNAP]:
        os.remove(os.path.join(d, viejo))


def diferencias(antes, ahora):
    a, b = antes.split("\n"), ahora.split("\n")
    res = []
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        res += [("-", x) for x in a[i1:i2]]
        res += [("+", x) for x in b[j1:j2]]
    return res


def notificar(titulo, texto):
    if shutil.which("termux-notification"):
        try:
            subprocess.run(["termux-notification", "--title", titulo,
                            "--content", texto], timeout=15)
        except Exception:  # noqa
            pass


def nombre_unico(cfg, url):
    base = slug(url)
    n, i = base, 2
    while n in cfg and cfg[n]["url"] != url:
        n = "%s-%d" % (base, i)
        i += 1
    return n


# ------------------------------------------------------------------- acciones
def comprobar(nombre, cfg, args):
    w = cfg[nombre]
    try:
        titulo, final, lineas, aviso = preparar(
            w["url"], w.get("seccion"), w.get("enlaces", True),
            w.get("ignorar", []))
    except Exception as e:  # noqa
        print("[%s] ERROR: %s" % (nombre, e))
        return 2
    if aviso:
        print("[%s] %s" % (nombre, aviso))
    texto = "\n".join(l[1] for l in lineas)
    huella = hashlib.sha256(texto.encode("utf-8")).hexdigest()
    ahora = datetime.now().strftime("%d/%m/%Y %H:%M")
    w["titulo"] = titulo
    w["ultima_vez"] = ahora
    estado = 0
    if not w.get("hash"):
        guardar_snapshot(nombre, texto)
        w["hash"] = huella
        w["ultimo_cambio"] = ahora
        print("[%s] Vigilancia iniciada (%d bloques de texto)." % (nombre, len(lineas)))
    elif huella == w["hash"]:
        print("[%s] Sin cambios desde %s." % (nombre, w.get("ultimo_cambio", "?")))
    else:
        dif = diferencias(ultimo_snapshot(nombre), texto)
        print("[%s] HAY CAMBIOS (visto antes: %s)" % (nombre, w.get("ultimo_cambio", "?")))
        registro = ["", "=== %s ===" % ahora]
        for sig, linea in dif:
            registro.append("%s %s" % (sig, linea))
            print(textwrap.fill(linea, ANCHO, initial_indent=sig + " ",
                                subsequent_indent="  ", break_long_words=False,
                                break_on_hyphens=False))
        with open(os.path.join(carpeta_snap(nombre), "cambios.txt"), "a",
                  encoding="utf-8") as f:
            f.write("\n".join(registro) + "\n")
        guardar_snapshot(nombre, texto)
        w["hash"] = huella
        w["ultimo_cambio"] = ahora
        notificar("Web cambiada: " + nombre, "%d lineas distintas" % len(dif))
        estado = 1
        if args.txt:
            print("TXT guardado:", escribir_txt(
                lineas, titulo, final, w.get("seccion"),
                ruta_salida(w["url"], "txt", None)))
        if args.pdf:
            r = escribir_pdf(lineas, titulo, final, w.get("seccion"),
                             ruta_salida(w["url"], "pdf", None))
            if r:
                print("PDF guardado:", r)
    return estado


def cmd_vigilar_comprobar(args):
    cfg = cargar()
    objetivo = getattr(args, "objetivo", None)
    if objetivo and objetivo.lower().startswith(("http://", "https://")):
        existente = [n for n, w in cfg.items() if w["url"] == objetivo]
        if existente:
            nombres = existente
        else:
            n = args.nombre or nombre_unico(cfg, objetivo)
            cfg[n] = {"url": objetivo, "seccion": args.seccion,
                      "enlaces": not args.sin_enlaces, "ignorar": args.ignorar or []}
            nombres = [n]
    elif objetivo:
        if objetivo not in cfg:
            print("No existe '%s'. Usa 'lista' para ver las webs." % objetivo)
            return 2
        nombres = [objetivo]
    else:
        nombres = list(cfg)
        if not nombres:
            print("No hay webs vigiladas. Usa: vigilar URL")
            return 0
    peor = 0
    for n in nombres:
        r = comprobar(n, cfg, args)
        peor = max(peor, r) if r != 1 or peor != 2 else peor
        guardar(cfg)
    return peor


def cmd_lista(args):
    cfg = cargar()
    if not cfg:
        print("No hay webs vigiladas.")
        return 0
    for n, w in cfg.items():
        print(n)
        print("  URL          :", w["url"])
        if w.get("seccion"):
            print("  Seccion      :", w["seccion"])
        print("  Ultima vez   :", w.get("ultima_vez", "-"))
        print("  Ultimo cambio:", w.get("ultimo_cambio", "-"))
    return 0


def cmd_quitar(args):
    cfg = cargar()
    if args.nombre not in cfg:
        print("No existe '%s'." % args.nombre)
        return 2
    del cfg[args.nombre]
    guardar(cfg)
    shutil.rmtree(os.path.join(SNAP, args.nombre), ignore_errors=True)
    print("Eliminada:", args.nombre)
    return 0


def cmd_convertir(args, formato):
    try:
        titulo, final, lineas, aviso = preparar(
            args.url, args.seccion, not args.sin_enlaces, args.ignorar)
    except Exception as e:  # noqa
        print("ERROR:", e)
        return 2
    if aviso:
        print(aviso)
    ruta = ruta_salida(args.url, formato, args.salida)
    if formato == "txt":
        escribir_txt(lineas, titulo, final, args.seccion or urlparse(args.url).fragment, ruta)
    else:
        if not escribir_pdf(lineas, titulo, final,
                            args.seccion or urlparse(args.url).fragment, ruta):
            return 2
    print("Guardado:", ruta)
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="Vigila webs y las convierte a TXT/PDF.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def comunes(p):
        p.add_argument("-s", "--seccion",
                       help="solo esta seccion (id o parte del titulo, "
                            "tambien vale #fragmento en la URL)")
        p.add_argument("-i", "--ignorar", action="append",
                       help="expresion regular de lineas a ignorar (repetible)")
        p.add_argument("--sin-enlaces", action="store_true",
                       help="no incluir las direcciones de los enlaces")

    for nombre in ("vigilar", "comprobar"):
        p = sub.add_parser(nombre)
        p.add_argument("objetivo", nargs="?" if nombre == "comprobar" else None,
                       help="URL o nombre de una web ya vigilada")
        p.add_argument("-n", "--nombre")
        p.add_argument("--txt", action="store_true",
                       help="guardar TXT si hay cambios")
        p.add_argument("--pdf", action="store_true",
                       help="guardar PDF si hay cambios")
        comunes(p)
        p.set_defaults(f=cmd_vigilar_comprobar)
    sub.add_parser("lista").set_defaults(f=cmd_lista)
    p = sub.add_parser("quitar")
    p.add_argument("nombre")
    p.set_defaults(f=cmd_quitar)
    for fmt in ("txt", "pdf"):
        p = sub.add_parser(fmt)
        p.add_argument("url")
        p.add_argument("-o", "--salida", help="fichero de salida")
        comunes(p)
        p.set_defaults(f=lambda a, fmt=fmt: cmd_convertir(a, fmt))
    args = ap.parse_args()
    sys.exit(args.f(args))


if __name__ == "__main__":
    main()
