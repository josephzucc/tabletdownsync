#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
empsui.py - Buscar, leer y guardar en local ofertas de empleo y proyectos de
informatica en Suiza (y EURES) desde Termux. Solo biblioteca estandar.

Fuentes:  jobsch  jobup  jobroom  swissdev  freelancermap  eures  (y 'url' para
          cualquier otra web con datos JobPosting)

Ejemplos:
  python3 empsui.py buscar jobsch "python" --ti --desde 7
  python3 empsui.py buscar todas "devops" --max 30 --detalle
  python3 empsui.py buscar freelancermap "cyber security" --via-extension
  python3 empsui.py leer jobsch 2ce68883-cd7c-4c7a-8daf-dc65ffe38ead
  python3 empsui.py url https://www.jobscout24.ch/de/job/...
  python3 empsui.py local "datenschutz" --fuente eures
  python3 empsui.py servidor          (puente con la extension empsui.xpi)

La salida normal va a stdout; solo los errores van a stderr.
Todo lo encontrado se guarda en ~/empsui/empsui.db y en archivos .txt.
"""
import argparse
import platform
import datetime
import html
import json
import os
import re
import secrets
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

VERSION = "1.3.0"
PUERTO_DEFECTO = 8774
ANCHO = 121
TIMEOUT = 40
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 "
              "Firefox/130.0")
DIR = os.environ.get("EMPSUI_DIR") or os.path.join(os.path.expanduser("~"), "empsui")
RUTA_DB = os.path.join(DIR, "empsui.db")
DIR_CACHE = os.path.join(DIR, "cache")

FUENTES = ["jobsch", "jobup", "jobroom", "swissdev", "freelancermap", "eures"]
NOMBRES = {"jobsch": "jobs.ch", "jobup": "jobup.ch", "jobroom": "job-room.ch",
           "swissdev": "swissdevjobs.ch", "freelancermap": "freelancermap.ch",
           "eures": "EURES", "web": "web (JobPosting)"}


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

SELLO = time.strftime("%Y-%m-%d-%H-%M-%S")
DIR_LOGS = os.path.join(DIR, "logs")
DIR_RESP = os.path.join(DIR, "respuestas")


def envuelve(texto, ancho=121, sangria="    "):
    """Lineas de como mucho `ancho` caracteres (las largas se cortan con sangria de continuacion)."""
    res = []
    for linea in str(texto).replace("\r", "").split("\n"):
        if len(linea) <= ancho:
            res.append(linea)
            continue
        res.append(linea[:ancho])
        resto = linea[ancho:]
        n = ancho - len(sangria)
        while resto:
            res.append(sangria + resto[:n])
            resto = resto[n:]
    return res


class Diag:
    """Registro de la ejecucion: lineas, peticiones, avisos y errores (para el informe de diagnostico)."""

    def __init__(self):
        self.lineas, self.peticiones, self.avisos, self.errores = [], [], [], []
        self.ruta = None
        self.comando = ""
        self.resultado = "(sin terminar)"
        self.puente = {}
        self.n_resp = 0
        self.informe_ruta = None

    def linea(self, nivel, msg):
        ms = int(time.time() * 1000) % 1000
        txt = f"[{time.strftime('%H:%M:%S')}.{ms:03d}] {nivel:5} {msg}"
        self.lineas.append(txt)
        if self.ruta is None:
            try:
                os.makedirs(DIR_LOGS, exist_ok=True)
                self.ruta = os.path.join(DIR_LOGS, SELLO + "logEmpsuiPy.log")
            except OSError:
                self.ruta = ""
        if self.ruta:
            try:
                with open(self.ruta, "a", encoding="utf-8") as f:
                    f.write("\n".join(envuelve(txt)) + "\n")
            except OSError:
                self.ruta = ""

    def aviso(self, msg):
        self.avisos.append(msg)
        log("AVISO: " + msg)

    def guarda_respuesta(self, url, status, via, texto, etiqueta):
        self.n_resp += 1
        try:
            os.makedirs(DIR_RESP, exist_ok=True)
            ruta = os.path.join(DIR_RESP, f"{SELLO}-resp{self.n_resp:02d}-{etiqueta}.txt")
            cab = f"URL: {url}\nEstado: {status}\nVia: {via}\nBytes: {len(texto)}\n" + "-" * 60 + "\n"
            with open(ruta, "w", encoding="utf-8") as f:
                f.write("\n".join(envuelve(cab + texto[:150000])) + "\n")
            self.linea("DEBUG", f"respuesta guardada en {ruta}")
            return ruta
        except OSError:
            return ""


DIAG = Diag()


def out(msg=""):
    print(msg, flush=True)
    DIAG.linea("INFO", msg)


def log(msg):
    # mensajes de progreso: tambien por stdout (la salida normal)
    print(msg, flush=True)
    DIAG.linea("INFO", msg)


def err(msg):
    print(msg, file=sys.stderr, flush=True)
    DIAG.linea("ERROR", msg)
    DIAG.errores.append(str(msg))


def dbg(msg):
    DIAG.linea("DEBUG", msg)


def normaliza(s):
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def asegura_dirs():
    os.makedirs(DIR, exist_ok=True)
    os.makedirs(DIR_CACHE, exist_ok=True)


def ahora():
    return datetime.datetime.now().strftime("%Y-%m-%d-%H-%M-%S")


def nombre_archivo(txt):
    t = re.sub(r"[^a-z0-9]+", "_", normaliza(txt)).strip("_")
    return t[:40] or "busqueda"


def limpia_url(u):
    """Quita parametros de seguimiento (utm_*)."""
    p = parse.urlsplit(u)
    q = [(k, v) for k, v in parse.parse_qsl(p.query, keep_blank_values=True)
         if not k.lower().startswith("utm_")]
    return parse.urlunsplit((p.scheme, p.netloc, p.path, parse.urlencode(q), ""))


def fecha_iso(valor):
    """Convierte varias formas de fecha a 'AAAA-MM-DD' (o '' si no se puede)."""
    if valor in (None, ""):
        return ""
    if isinstance(valor, (int, float)) or (isinstance(valor, str) and valor.isdigit()
                                           and len(valor) >= 10):
        v = float(valor)
        if v > 1e11:
            v /= 1000.0
        return datetime.datetime.fromtimestamp(v).strftime("%Y-%m-%d")
    s = str(valor).strip()
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    if m:
        return m.group(1)
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z"):
        try:
            return datetime.datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return s[:20]


# ---------------------------------------------------------------------------
# HTML -> texto
# ---------------------------------------------------------------------------

class HTMLTexto(HTMLParser):
    BLOQUES = {"p", "div", "br", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
               "ul", "ol", "section", "article", "table", "header", "footer"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.partes = []
        self.omitir = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "template"):
            self.omitir += 1
        elif tag == "li":
            self.partes.append("\n- ")
        elif tag in self.BLOQUES:
            self.partes.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "template"):
            self.omitir = max(0, self.omitir - 1)
        elif tag in self.BLOQUES or tag == "li":
            self.partes.append("\n")

    def handle_data(self, data):
        if not self.omitir:
            self.partes.append(data)

    def texto(self):
        t = "".join(self.partes)
        t = re.sub(r"[ \t\xa0]+", " ", t)
        t = re.sub(r" *\n *", "\n", t)
        t = re.sub(r"\n{3,}", "\n\n", t)
        return t.strip()


def html_a_texto(fragmento):
    if not fragmento:
        return ""
    p = HTMLTexto()
    try:
        p.feed(fragmento)
        p.close()
    except Exception:
        return re.sub(r"<[^>]+>", " ", fragmento)
    return p.texto()


def desescapa_md(t):
    """EURES devuelve texto con barras de escape de Markdown (\\* \\- \\&amp;)."""
    t = html.unescape(t or "")
    t = re.sub(r"\\+([\\*_\-\.\(\)\[\]#&+!|>~`])", r"\1", t)
    t = t.replace("&amp;", "&")
    # Markdown aplastado en una sola linea -> parrafos y vinetas legibles
    t = t.replace("**", "")
    t = re.sub(r"={3,}", "\n", t)
    t = re.sub(r"\s*#{1,6}\s*", "\n\n", t)
    t = re.sub(r"(?<!\*)\*\s+(?=\S)", "\n- ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


# ---------------------------------------------------------------------------
# Acceso a la red: directo o a traves de la extension
# ---------------------------------------------------------------------------

class Cliente:
    """Hace peticiones HTTP: directas desde Termux, o a traves de la extension
    (que las ejecuta con la sesion de Firefox)."""

    def __init__(self, via_extension=False, puerto=PUERTO_DEFECTO, modo="auto"):
        self.via = via_extension
        self.puerto = puerto
        self.modo = modo

    def pide(self, url, metodo="GET", cabeceras=None, cuerpo=None, json_cuerpo=None):
        via = "extension" if self.via else "directo"
        n = len(DIAG.peticiones) + 1
        dbg(f"PETICION {n}: {metodo} {url} (via {via})")
        t0 = time.time()
        try:
            st, txt = self._pide(url, metodo, cabeceras, cuerpo, json_cuerpo)
        except Exception as e:
            DIAG.peticiones.append({"n": n, "metodo": metodo, "url": url, "via": via, "status": None, "bytes": 0,
                                    "clase": "sin-respuesta", "titulo": "", "muestra": str(e)[:300], "seg": round(time.time() - t0, 1),
                                    "archivo": ""})
            dbg(f"RESPUESTA {n}: sin respuesta ({e})")
            raise
        clase = clasifica_resp(st, txt)
        titulo = (re.search(r"<title[^>]*>([\s\S]*?)</title>", txt[:200000], re.I) or [None, ""])[1]
        titulo = re.sub(r"\s+", " ", html.unescape(titulo)).strip()[:70]
        dbg(f"RESPUESTA {n}: estado {st}, {len(txt)} car., clase {clase}, {time.time() - t0:.1f}s"
            + (f", titulo {titulo!r}" if titulo else ""))
        archivo = ""
        if clase.startswith(("http-", "bloqueo", "vacio")):
            archivo = DIAG.guarda_respuesta(url, st, via, txt, clase)
        muestra = txt[:300] if clase in ("json", "xml") else re.sub(
            r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"<(script|style)[\s\S]*?</\1>", " ", txt[:400000], flags=re.I)))[:500].strip()
        DIAG.peticiones.append({"n": n, "metodo": metodo, "url": url, "via": via, "status": st, "bytes": len(txt),
                                "clase": clase, "titulo": titulo, "muestra": muestra, "seg": round(time.time() - t0, 1),
                                "archivo": archivo})
        return st, txt

    def _pide(self, url, metodo, cabeceras, cuerpo, json_cuerpo):
        cab = {"User-Agent": USER_AGENT, "Accept": "*/*",
               "Accept-Language": "de-CH,de;q=0.9,en;q=0.8"}
        if cabeceras:
            cab.update(cabeceras)
        if json_cuerpo is not None:
            cuerpo = json.dumps(json_cuerpo)
            cab["Content-Type"] = "application/json"
        if self.via:
            r = pedir_trabajo({"op": "fetch", "url": url, "metodo": metodo,
                               "cabeceras": {k: v for k, v in cab.items()
                                             if k.lower() not in ("user-agent",)},
                               "cuerpo": cuerpo, "modo": self.modo}, self.puerto)
            if r.get("error"):
                raise RuntimeError(r["error"])
            return int(r.get("status", 0)), r.get("text", "")
        datos = cuerpo.encode("utf-8") if isinstance(cuerpo, str) else cuerpo
        req = request.Request(url, data=datos, headers=cab, method=metodo)
        try:
            with request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read()
                cs = resp.headers.get_content_charset() or "utf-8"
                return resp.status, raw.decode(cs, errors="replace")
        except error.HTTPError as e:
            cuerpo_err = e.read().decode("utf-8", errors="replace")
            return e.code, cuerpo_err
        except error.URLError as e:
            raise RuntimeError(f"No se pudo conectar con {url}: {e.reason}")

    def json(self, url, **kw):
        st, txt = self.pide(url, **kw)
        if st >= 400:
            raise RuntimeError(f"HTTP {st} en {url}: {txt[:200]}")
        try:
            return json.loads(txt)
        except ValueError:
            if DIAG.peticiones:
                DIAG.peticiones[-1]["clase"] = "no-json"
                DIAG.peticiones[-1]["archivo"] = DIAG.guarda_respuesta(
                    url, st, DIAG.peticiones[-1]["via"], txt, "no-json")
            raise RuntimeError(f"Respuesta no JSON en {url} (primeros caracteres: "
                               f"{txt[:120]!r}). Prueba con --via-extension.")

    def texto(self, url, **kw):
        st, txt = self.pide(url, **kw)
        if st >= 400:
            raise RuntimeError(f"HTTP {st} en {url}")
        return txt


# ---------------------------------------------------------------------------
# Modelo de oferta
# ---------------------------------------------------------------------------

def oferta(fuente, id_, titulo, **kw):
    o = {"fuente": fuente, "id": str(id_), "titulo": (titulo or "").strip(),
         "empresa": "", "lugar": "", "fecha": "", "url": "", "contrato": "",
         "carga": "", "salario": "", "remoto": "", "idioma": "",
         "tecnologias": [], "descripcion": "", "completa": False, "extra": {}}
    o.update(kw)
    return o


def coincide(o, terminos):
    """Todos los terminos (sin acentos) deben aparecer en titulo/empresa/descripcion."""
    base = normaliza(" ".join([o["titulo"], o["empresa"], o["lugar"], o["descripcion"],
                               " ".join(o["tecnologias"])]))
    return all(normaliza(t) in base for t in terminos)


# ---------------------------------------------------------------------------
# jobs.ch / jobup.ch  (API publica JSON de la propia web)
# ---------------------------------------------------------------------------

JOBSCH_CATEGORIA_TI = "106"   # grupo de informatica (rama /98/106, deducido de resultados reales)


def _grado(doc):
    g = doc.get("employment_grades") or []
    if g:
        return f"{min(g)}-{max(g)}%" if min(g) != max(g) else f"{g[0]}%"
    return ""


def jobsch_doc_a_oferta(doc, fuente, host):
    enlaces = doc.get("_links", {})
    clave = "detail_fr" if fuente == "jobup" else "detail_de"
    url = (enlaces.get(clave) or enlaces.get("detail_en") or {}).get("href", "")
    return oferta(fuente, doc.get("job_id", ""), doc.get("title", ""),
                  empresa=doc.get("company_name", ""), lugar=doc.get("place", ""),
                  fecha=fecha_iso(doc.get("publication_date")), url=url,
                  carga=_grado(doc), descripcion=html_a_texto(doc.get("preview", "")),
                  extra={"coordenadas": doc.get("coordinates"),
                         "segmento": doc.get("company_segmentation")})


def buscar_jobsch(c, a, fuente="jobsch"):
    host = "www.jobup.ch" if fuente == "jobup" else "www.jobs.ch"
    res, pagina = [], 1
    desde = None
    if a.desde:
        d = datetime.datetime.now() - datetime.timedelta(days=a.desde)
        desde = d.strftime("%Y-%m-%d 00:00:00")
    while len(res) < a.max:
        p = [("rows", "20"), ("page", str(pagina)), ("sort", "date")]
        if a.terminos:
            p.append(("query", " ".join(a.terminos)))
        if a.ubicacion:
            p.append(("location", a.ubicacion))
        if a.ti:
            p.append(("category-ids[]", JOBSCH_CATEGORIA_TI))
        if desde:
            hasta = (datetime.datetime.now() + datetime.timedelta(days=2)).strftime("%Y-%m-%d 00:00:00")
            p.append(("publication-date-from", desde))
            p.append(("publication-date-to", hasta))
        if a.carga_min:
            p.append(("employment-grade-min", str(a.carga_min)))
        if getattr(a, "carga_max", None):
            p.append(("employment-grade-max", str(a.carga_max)))
        for idioma in (a.idioma or []):
            p.append(("language-skills[]", idioma))
        p += _pares_filtro(getattr(a, "filtro", None))
        d = c.json(f"https://{host}/api/v1/public/search?" + parse.urlencode(p))
        docs = d.get("documents") or []
        if pagina == 1:
            log(f"[{fuente}] {d.get('total_hits', '?')} resultados en total")
        for doc in docs:
            res.append(jobsch_doc_a_oferta(doc, fuente, host))
        if not docs or pagina >= int(d.get("num_pages") or 1):
            break
        pagina += 1
    return res[:a.max]


def detalle_jobsch(c, o):
    host = "www.jobup.ch" if o["fuente"] == "jobup" else "www.jobs.ch"
    d = c.json(f"https://{host}/api/v1/public/search/job/{o['id']}")
    partes = [html_a_texto(d.get("template_lead_text", "")),
              html_a_texto(d.get("template_text", ""))]
    desc = "\n\n".join(p for p in partes if p)
    if not desc:
        desc = html_a_texto(d.get("template", ""))
    o["descripcion"] = desc
    o["completa"] = bool(desc)
    o["lugar"] = ", ".join(x for x in [d.get("place"), (d.get("locations") or [{}])[0].get("cantonCode")] if x)
    o["extra"].update({"email_candidatura": d.get("application_email", ""),
                       "url_externa": d.get("external_url", ""),
                       "calle": d.get("street", ""), "cp": d.get("zipcode", ""),
                       "web_empresa": d.get("template_job_url", ""),
                       "publicado_hasta": d.get("publication_end_date", "")})
    return o


# ---------------------------------------------------------------------------
# job-room.ch (oficial, SECO)
# ---------------------------------------------------------------------------

def _jr_a_oferta(ad):
    jc = ad.get("jobContent", {})
    desc = (jc.get("jobDescriptions") or [{}])[0]
    emp = jc.get("employment", {})
    loc = jc.get("location", {})
    comp = jc.get("company", {})
    carga = ""
    if emp.get("workloadPercentageMin"):
        a, b = emp["workloadPercentageMin"], emp.get("workloadPercentageMax") or emp["workloadPercentageMin"]
        carga = f"{a}%" if a == b else f"{a}-{b}%"
    contrato = "indefinido" if emp.get("permanent") else ("temporal" if emp.get("permanent") is False else "")
    lugar = ", ".join(x for x in [loc.get("city"), loc.get("cantonCode")] if x)
    return oferta("jobroom", ad.get("id", ""), desc.get("title", ""),
                  empresa=comp.get("name", ""), lugar=lugar,
                  fecha=fecha_iso((ad.get("publication") or {}).get("startDate")),
                  url="https://www.job-room.ch/job-search/" + ad.get("id", ""),
                  contrato=contrato, carga=carga, idioma=desc.get("languageIsoCode", ""),
                  descripcion=html_a_texto(desc.get("description", "")),
                  extra={"url_externa": jc.get("externalUrl", ""),
                         "aplicar": (jc.get("applyChannel") or {}),
                         "n_egov": ad.get("stellennummerEgov")})


def buscar_jobroom(c, a):
    res, pagina = [], 0
    cantones = [a.ubicacion.upper()] if a.ubicacion and len(a.ubicacion) == 2 else []
    while len(res) < a.max:
        body = {"workloadPercentageMin": a.carga_min or 10, "workloadPercentageMax": 100,
                "permanent": None, "companyName": None, "onlineSince": a.desde or 60,
                "displayRestricted": False, "professionCodes": [],
                "keywords": a.terminos, "communalCodes": [], "cantonCodes": cantones}
        st, txt = c.pide("https://www.job-room.ch/jobadservice/api/jobAdvertisements/_search?"
                         + parse.urlencode({"page": pagina, "size": 20, "sort": "date_desc"}),
                         metodo="POST", json_cuerpo=body)
        if st >= 400:
            raise RuntimeError(f"job-room.ch HTTP {st}: {txt[:200]}")
        lista = json.loads(txt)
        if not lista:
            break
        for it in lista:
            res.append(_jr_a_oferta(it["jobAdvertisement"]))
        if len(lista) < 20:
            break
        pagina += 1
    out_ = res[:a.max]
    if a.ubicacion and len(a.ubicacion) != 2:
        out_ = [o for o in out_ if normaliza(a.ubicacion) in normaliza(o["lugar"])]
    return out_


def detalle_jobroom(c, o):
    ad = c.json(f"https://www.job-room.ch/jobadservice/api/jobAdvertisements/{o['id']}")
    nuevo = _jr_a_oferta(ad)
    o.update({k: v for k, v in nuevo.items() if v})
    o["completa"] = True
    return o


# ---------------------------------------------------------------------------
# swissdevjobs.ch (feed RSS; se filtra en local)
# ---------------------------------------------------------------------------

def swissdev_feed(c, refrescar=False):
    asegura_dirs()
    ruta = os.path.join(DIR_CACHE, "swissdev_rss.xml")
    if not refrescar and os.path.exists(ruta) and time.time() - os.path.getmtime(ruta) < 3600:
        with open(ruta, encoding="utf-8") as f:
            return f.read()
    txt = c.texto("https://swissdevjobs.ch/rss")
    with open(ruta, "w", encoding="utf-8") as f:
        f.write(txt)
    return txt


def swissdev_parsea(xml_txt):
    raiz = ET.fromstring(xml_txt.encode("utf-8"))
    res = []
    for it in raiz.iter("item"):
        titulo = (it.findtext("title") or "").strip()
        link = limpia_url(it.findtext("link") or "")
        desc_html = it.findtext("description") or ""
        m = re.match(r"(.*?)\s*@\s*(.*?)(?:\s*\[(CHF[^\]]*)\])?$", titulo, re.S)
        cargo, empresa, sal = (m.group(1), m.group(2), m.group(3) or "") if m else (titulo, "", "")
        if not sal:
            ms = re.search(r"Salary:\s*([^<\n]+)", desc_html)
            sal = ms.group(1).strip() if ms else ""
        tec = []
        mt = re.search(r"Technologies:</b>\s*<ul>(.*?)</ul>", desc_html, re.S)
        if mt:
            tec = [html_a_texto(x) for x in re.findall(r"<li>(.*?)</li>", mt.group(1), re.S)]
        slug = link.rsplit("/", 1)[-1]
        res.append(oferta("swissdev", slug, cargo, empresa=empresa, fecha=fecha_iso(it.findtext("pubDate")),
                          url=link, salario=sal, tecnologias=tec,
                          descripcion=html_a_texto(desc_html), completa=True))
    return res


def buscar_swissdev(c, a):
    todas = swissdev_parsea(swissdev_feed(c, getattr(a, "refrescar", False)))
    log(f"[swissdev] {len(todas)} ofertas en el feed")
    res = [o for o in todas if coincide(o, a.terminos)]
    if a.ubicacion:
        res = [o for o in res if normaliza(a.ubicacion) in normaliza(o["descripcion"] + o["titulo"])]
    for t in (getattr(a, "tech", None) or []):
        n = normaliza(t)
        res = [o for o in res if n in normaliza(" ".join(o["tecnologias"]) + " " + o["titulo"])]
    if getattr(a, "salario_min", None):
        def _sal(o):
            nums = [int(x) * (1000 if "k" in o["salario"].lower() else 1)
                    for x in re.findall(r"(\d+)", o["salario"].replace("'", "").replace(",", ""))]
            return max(nums) if nums else 0
        res = [o for o in res if _sal(o) >= a.salario_min]
    if a.desde:
        limite = (datetime.date.today() - datetime.timedelta(days=a.desde)).isoformat()
        res = [o for o in res if o["fecha"] >= limite]
    res.sort(key=lambda o: o["fecha"], reverse=True)
    return res[:a.max]


# ---------------------------------------------------------------------------
# freelancermap.ch (HTML renderizado en servidor + JSON-LD en el detalle)
# ---------------------------------------------------------------------------

FM_BASE = "https://www.freelancermap.ch"


def _fm_fecha(txt):
    txt = (txt or "").strip()
    if re.fullmatch(r"\d{1,2}:\d{2}", txt):          # publicado hoy a esa hora
        return datetime.date.today().isoformat()
    return txt


def comilla_atributos(h):
    """Pone comillas a los atributos sin ellas (paginas guardadas con SingleFile): class=x -> class="x"."""
    return re.sub(r'(\s[\w:-]+)=([^\s"\'>][^\s>]*)', lambda m: f'{m.group(1)}="{m.group(2)}"', h)


def _campo(b, testid, cierre="div"):
    m = re.search(r'data-testid="?' + testid + r'"?[^>]*>(.*?)</' + cierre + '>', b, re.S)
    return html_a_texto(m.group(1)).strip(" |") if m else ""


def fm_parsea_lista(pagina_html):
    res = []
    pagina_html = comilla_atributos(pagina_html) if re.search(r'<div class=project-card>', pagina_html) else pagina_html
    bloques = re.split(r'<div class="?project-card"?>', pagina_html)[1:]
    for b in bloques:
        mt = re.search(r'<a[^>]*data-testid="?title"?[^>]*href="([^"]+)"[^>]*>(.*?)</a>', b, re.S) or \
            re.search(r'<a[^>]*href="([^"]+)"[^>]*data-testid="?title"?[^>]*>(.*?)</a>', b, re.S)
        if not mt:
            continue
        href, titulo = mt.group(1), html_a_texto(mt.group(2)).strip()
        emp = re.search(r'project-card-company[^>]*>\s*<span>(.*?)</span>', b, re.S)
        ciudad = re.search(r'data-id="?project-card-city"?[^>]*>(.*?)</a>', b, re.S)
        pais = re.search(r'data-id="?project-card-country"?[^>]*>(.*?)</a>', b, re.S)
        tec = [html_a_texto(x) for x in re.findall(r'data-id="?project-card-keyword-link"?[^>]*>(.*?)</a>', b, re.S)]
        slug = href.rstrip("/").rsplit("/", 1)[-1]
        lugar = ", ".join(x for x in [html_a_texto(ciudad.group(1)).strip(" ,") if ciudad else "",
                                      html_a_texto(pais.group(1)) if pais else ""] if x)
        res.append(oferta("freelancermap", slug, titulo,
                          empresa=html_a_texto(emp.group(1)) if emp else "", lugar=lugar,
                          fecha=_fm_fecha(_campo(b, "created", "span")),
                          url=parse.urljoin(FM_BASE, href),
                          contrato=_campo(b, "type"), remoto=_campo(b, "remoteInPercent"),
                          tecnologias=tec,
                          extra={"inicio": _campo(b, "beginningText"), "duracion": _campo(b, "duration")}))
    return res


def fm_parsea_enlaces(pagina_html):
    """Plan B: si no hay tarjetas conocidas, toma los enlaces a /projekt/<slug> como ofertas."""
    res, vistos = [], set()
    pagina_html = comilla_atributos(pagina_html)
    for m in re.finditer(r'<a[^>]*href="((?:https?://[^"/]*freelancermap\.[a-z]+)?/projekt/[^"?#]+)[^"]*"[^>]*>(.*?)</a>',
                         pagina_html, re.S):
        href, titulo = m.group(1), html_a_texto(m.group(2)).strip()
        slug = href.rstrip("/").rsplit("/", 1)[-1]
        if not titulo or slug in vistos:
            continue
        vistos.add(slug)
        res.append(oferta("freelancermap", slug, titulo, url=parse.urljoin(FM_BASE, href)))
    return res


def fm_total(txt):
    """Numero de proyectos que anuncia la propia web ('Projekte finden 52 Projekte ...'), o None."""
    v = re.sub(r"<(script|style)[\s\S]*?</\1>", " ", txt, flags=re.I)
    v = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", v))
    m = re.search(r"Projekte finden\s+([\d.']+)\s+Projekt", v)
    if not m:
        m = re.search(r"(?:^|\s)([\d.']+)\s+Projekte\s", v)   # cabecera de resultados: '679 Projekte'
    return int(re.sub(r"[.']", "", m.group(1))) if m else None


FM_PAISES = {"de": "1", "at": "2", "ch": "3", "uk": "4", "us": "5", "pl": "114"}
FM_REMOTO = {"presencial": "on_site", "hibrido": "hybrid", "remoto": "remote"}
FM_CONTRATO = {"freelance": "contracting", "leasing": "employee_leasing", "fijo": "permanent_position"}
FM_CATEGORIA = {"software": "1", "texto": "2", "diseno": "3", "marketing": "4", "ingenieria": "6", "finanzas": "7"}
FM_SECTOR = {"ti": "24", "energia": "7", "finanzas": "9", "transporte": "18", "telecom": "16", "maquinaria": "1",
             "hochschulen": "11", "pharma": "14", "administracion": "20", "construccion": "3"}


def _ids(valores, tabla, nombre):
    """Convierte nombres (o ids numericos) de un filtro a los ids de la web."""
    res = []
    for v in valores or []:
        for x in str(v).split(","):
            x = x.strip().lower()
            if not x:
                continue
            if x in tabla:
                res.append(tabla[x])
            elif x in tabla.values() or x.isdigit():
                res.append(x)
            else:
                raise RuntimeError(f"Valor '{x}' no valido para {nombre}. Validos: {', '.join(sorted(tabla))} "
                                   f"(mira 'empsui.py filtros freelancermap').")
    return res


def _pares_filtro(lista):
    res = []
    for kv in lista or []:
        if "=" not in kv:
            raise RuntimeError(f"--filtro espera CLAVE=VALOR, recibi '{kv}'.")
        k, v = kv.split("=", 1)
        res.append((k.strip(), v.strip()))
    return res


def fm_params(a, termino, pagina):
    p = [("query", termino)]
    paises = [FM_PAISES.get(x.lower(), x) for x in (getattr(a, "fm_pais", None) or ["ch"])]
    for i, x in enumerate(paises):
        p.append((f"countries[{i}]", x))
    for nombre, tabla, valores in (("remoteInPercent", FM_REMOTO, getattr(a, "remoto", None)),
                                   ("contractTypes", FM_CONTRATO, getattr(a, "contrato", None))):
        for i, x in enumerate(_ids(valores, tabla, nombre)):
            p.append((f"{nombre}[{i}]", x))
    for i, x in enumerate(_ids(getattr(a, "categoria", None), FM_CATEGORIA, "categoria")):
        p.append((f"categories[{i}]", x))
    for i, x in enumerate(_ids(getattr(a, "sector", None), FM_SECTOR, "sector")):
        p.append((f"industry[{i}]", x))
    if getattr(a, "desde", None):
        p.append(("created", str(min(max(int(a.desde), 1), 30))))
    if getattr(a, "cliente_final", False):
        p.append(("endcustomer", "1"))
    if getattr(a, "ocultar_beworben", False):
        p.append(("hideAppliedProjects", "1"))
    if a.ubicacion:
        p.append(("city", a.ubicacion))
    if getattr(a, "radio", None):
        p.append(("radius", str(a.radio)))
    p += _pares_filtro(getattr(a, "filtro", None))
    p += [("sort", "1"), ("pagenr", str(pagina))]
    return p


def buscar_freelancermap(c, a):
    terminos = [" ".join(a.terminos)]
    if getattr(a, "cualquiera", False) and len(a.terminos) > 1:
        terminos = list(a.terminos)          # cada palabra es una busqueda aparte (union de resultados)
    res, vistos = [], set()
    for termino in terminos:
        parcial, pagina, total = [], 1, None
        while len(res) + len(parcial) < a.max:
            url = f"{FM_BASE}/projekte?" + parse.urlencode(fm_params(a, termino, pagina))
            txt = c.texto(url)
            if pagina == 1:
                total = fm_total(txt)
                sesion = "Anmelden" in re.sub(r"<[^>]+>", " ", txt) and "mein_account" not in txt
                log(f"[freelancermap] '{termino}': la web indica "
                    f"{'?' if total is None else total} proyecto(s)"
                    f"{' (sin sesion iniciada: la web limita a 22 por busqueda)' if sesion else ''}.")
                if c.via and sesion:
                    DIAG.aviso("freelancermap: Firefox no tiene la sesion iniciada; inicia sesion en freelancermap.ch "
                               "para ver todas las paginas.")
                if getattr(a, "mostrar_url", False):
                    log(f"  URL: {url}")
            lista = fm_parsea_lista(txt) or fm_parsea_enlaces(txt)
            nuevos = [o for o in lista if o["id"] not in vistos]
            if not nuevos:
                if pagina == 1 and not lista:
                    DIAG.aviso(f"freelancermap: 0 proyectos en la pagina 1 para '{termino}' (la web indica "
                               f"{'nada' if total is None else total}; {len(txt)} caracteres recibidos, "
                               f"{len(re.findall('/projekt/', txt))} menciones de /projekt/). Pagina guardada.")
                    DIAG.guarda_respuesta(url, 200, "extension" if c.via else "directo", txt,
                                          "freelancermap-sin-proyectos")
                elif pagina > 1 and total and len(res) + len(parcial) < total:
                    DIAG.aviso(f"freelancermap devolvio la pagina {pagina} igual que la anterior: solo se pueden leer "
                               f"{len(res) + len(parcial)} de {total} proyectos (sin sesion la web limita las paginas; "
                               f"inicia sesion en Firefox y usa --via-extension).")
                    DIAG.guarda_respuesta(url, 200, "extension" if c.via else "directo", txt,
                                          "freelancermap-pagina-repetida")
                break
            for o in nuevos:
                vistos.add(o["id"])
                parcial.append(o)
            if total is not None and len(parcial) >= total:
                break
            pagina += 1
        res += parcial
    return res[:a.max]


def jsonld_ofertas(pagina_html):
    """Devuelve la lista de objetos JobPosting (schema.org) de una pagina."""
    res = []
    for m in re.finditer(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
                         pagina_html, re.S):
        try:
            d = json.loads(m.group(1))
        except ValueError:
            continue
        pila = d if isinstance(d, list) else [d]
        while pila:
            x = pila.pop(0)
            if isinstance(x, dict):
                if "@graph" in x:
                    pila.extend(x["@graph"])
                t = x.get("@type")
                if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
                    res.append(x)
    return res


def _jp_lugar(jp):
    loc = jp.get("jobLocation")
    if isinstance(loc, list):
        loc = loc[0] if loc else {}
    adr = (loc or {}).get("address", {}) if isinstance(loc, dict) else {}
    if isinstance(adr, dict):
        return ", ".join(x for x in [adr.get("addressLocality"), adr.get("addressRegion"),
                                     adr.get("addressCountry") if isinstance(adr.get("addressCountry"), str) else ""] if x)
    return str(adr or "")


def jp_a_oferta(jp, fuente, url="", id_=None):
    org = jp.get("hiringOrganization") or {}
    empresa = org.get("name", "") if isinstance(org, dict) else str(org)
    sal = jp.get("baseSalary")
    salario = ""
    if isinstance(sal, dict):
        v = sal.get("value", {})
        if isinstance(v, dict):
            salario = " - ".join(str(x) for x in [v.get("minValue"), v.get("maxValue")] if x)
            if salario:
                salario = (salario + " " + (sal.get("currency") or "")).strip()
    et = jp.get("employmentType")
    return oferta(fuente, id_ or url or jp.get("identifier", ""), html.unescape(jp.get("title", "")),
                  empresa=html.unescape(empresa), lugar=_jp_lugar(jp),
                  fecha=fecha_iso(jp.get("datePosted")), url=url or jp.get("url", ""),
                  contrato=", ".join(et) if isinstance(et, list) else (et or ""),
                  salario=salario, descripcion=html_a_texto(html.unescape(jp.get("description", ""))),
                  completa=True, extra={"valido_hasta": jp.get("validThrough", "")})


def detalle_freelancermap(c, o):
    txt = c.texto(o["url"])
    jps = jsonld_ofertas(txt)
    if jps:
        n = jp_a_oferta(jps[0], "freelancermap", o["url"], o["id"])
        for k in ("descripcion", "fecha", "salario"):
            if n[k]:
                o[k] = n[k]
        if n["empresa"] and not o["empresa"]:
            o["empresa"] = n["empresa"]
        o["extra"]["valido_hasta"] = n["extra"].get("valido_hasta", "")
    else:
        m = re.search(r'project-body-description">(.*?)</div>\s*</div>', txt, re.S)
        if m:
            o["descripcion"] = html_a_texto(m.group(1))
    mi = re.search(r'Projekt-ID[^0-9]*(\d+)', html_a_texto(txt))
    if mi:
        o["extra"]["projekt_id"] = mi.group(1)
    o["completa"] = bool(o["descripcion"])
    return o


# ---------------------------------------------------------------------------
# EURES (API interna del portal europeo)
# ---------------------------------------------------------------------------

EURES_API = "https://europa.eu/eures/api/jv-searchengine/public"
EURES_ISCO_TIC = "http://data.europa.eu/esco/isco/C25"   # profesionales de las TIC
EURES_PERIODOS = [(1, None), (3, "LAST_THREE_DAYS"), (7, "LAST_WEEK"), (31, "LAST_MONTH")]


def _eures_a_oferta(jv):
    emp = jv.get("employer") or {}
    return oferta("eures", jv.get("id", ""), html.unescape(jv.get("title", "")),
                  empresa=emp.get("name", "") if isinstance(emp, dict) else "",
                  lugar=", ".join(sorted((jv.get("locationMap") or {}).keys())).upper(),
                  fecha=fecha_iso(jv.get("creationDate")),
                  url=f"https://europa.eu/eures/portal/jv-se/jv-details/{jv.get('id', '')}?lang=en",
                  contrato=", ".join(jv.get("positionScheduleCodes") or []),
                  descripcion=desescapa_md(html_a_texto(jv.get("description", ""))),
                  completa=False,
                  extra={"nuts": jv.get("locationMap"), "isco": jv.get("jobCategoriesCodes")})


def buscar_eures(c, a):
    paises = [p.strip().lower() for p in (a.pais or "ch").split(",") if p.strip()]
    periodo = None
    if a.desde:
        for dias, valor in EURES_PERIODOS:
            if a.desde <= dias:
                periodo = valor
                break
        else:
            periodo = None
    res, pagina = [], 1
    while len(res) < a.max:
        body = {"resultsPerPage": 50, "page": pagina, "sortSearch": "MOST_RECENT",
                "keywords": [{"keyword": t, "specificSearchCode": "EVERYWHERE"} for t in a.terminos],
                "publicationPeriod": periodo, "occupationUris": [EURES_ISCO_TIC] if a.ti else [],
                "skillUris": [], "requiredExperienceCodes": [], "positionScheduleCodes": [],
                "sectorCodes": [], "educationAndQualificationLevelCodes": [],
                "positionOfferingCodes": [], "locationCodes": paises, "euresFlagCodes": [],
                "otherBenefitsCodes": [], "requiredLanguages": [], "minNumberPost": None,
                "sessionId": secrets.token_hex(6), "requestLanguage": "en"}
        d = c.json(EURES_API + "/jv-search/search", metodo="POST", json_cuerpo=body)
        if pagina == 1:
            log(f"[eures] {d.get('numberRecords', '?')} resultados en total")
        jvs = d.get("jvs") or []
        for jv in jvs:
            res.append(_eures_a_oferta(jv))
        if len(jvs) < 50:
            break
        pagina += 1
    res = res[:a.max]
    if a.ubicacion:
        res = [o for o in res if normaliza(a.ubicacion) in normaliza(o["lugar"] + o["descripcion"])]
    return res


def detalle_eures(c, o):
    d = c.json(f"{EURES_API}/jv/id/{o['id']}?requestLang=en")
    perfiles = d.get("jvProfiles") or {}
    p = perfiles.get("en") or (next(iter(perfiles.values())) if perfiles else {})
    locs = p.get("locations") or []
    if locs:
        o["lugar"] = ", ".join(x for x in [locs[0].get("cityName"), (locs[0].get("countryCode") or "").upper(),
                                            locs[0].get("postalCode")] if x)
    if p.get("description"):
        o["descripcion"] = desescapa_md(html_a_texto(p["description"]))
    emp = p.get("employer") or {}
    if emp.get("name"):
        o["empresa"] = emp["name"]
    instr = [html_a_texto(x) for x in (p.get("applicationInstructions") or [])]
    o["extra"].update({"como_aplicar": instr, "fuente_original": d.get("source"),
                       "referencia": d.get("reference"),
                       "habilidades": p.get("requiredSkills"),
                       "idiomas": p.get("positionLanguages")})
    o["completa"] = True
    return o


# ---------------------------------------------------------------------------
# Cualquier otra web: JSON-LD JobPosting (ictjobs.ch, jobscout24.ch, ...)
# ---------------------------------------------------------------------------

def ofertas_de_html(txt, url):
    jps = jsonld_ofertas(txt)
    if jps:
        return [jp_a_oferta(jp, "web", url) for jp in jps]
    m = re.search(r"<title>(.*?)</title>", txt, re.S)
    titulo = html_a_texto(m.group(1)) if m else url
    cuerpo = re.search(r"<body.*?>(.*)</body>", txt, re.S)
    return [oferta("web", url, titulo, url=url,
                   descripcion=html_a_texto(cuerpo.group(1) if cuerpo else txt), completa=True,
                   extra={"aviso": "No habia datos JobPosting: se guarda el texto de la pagina"})]


def clasifica_resp(status, txt):
    if status is None:
        return "sin-respuesta"
    if not (txt or "").strip():
        return "vacio" if status < 400 else f"http-{status}"
    if status >= 400:
        return f"http-{status}"
    b = detecta_bloqueo(txt)
    if b:
        return "bloqueo:" + b
    s = txt.lstrip()[:20]
    if s[:1] in "{[":
        return "json"
    if s.startswith(("<?xml", "<rss", "<feed")):
        return "xml"
    return "html"


def detecta_bloqueo(txt):
    t = normaliza(txt[:6000])
    for f in ("just a moment", "checking your browser", "attention required", "access denied",
              "verifying you are human", "enable javascript and cookies"):
        if f in t:
            return f
    return ""


def leer_url_generica(c, url, render=False):
    if render:
        r = pedir_trabajo({"op": "pagina", "url": url}, c.puerto)
        txt = r.get("text", "")
    else:
        txt = c.texto(url)
    b = detecta_bloqueo(txt)
    if b:
        raise RuntimeError(f"La web devolvio una pagina de proteccion ('{b}'). Abre la oferta tu mismo en Firefox "
                           "y usa: python3 empsui.py pestana")
    return ofertas_de_html(txt, url)


# ---------------------------------------------------------------------------
# Base de datos local
# ---------------------------------------------------------------------------

def abre_db():
    asegura_dirs()
    db = sqlite3.connect(RUTA_DB)
    db.execute("""CREATE TABLE IF NOT EXISTS ofertas (
        fuente TEXT, id TEXT, titulo TEXT, empresa TEXT, lugar TEXT, fecha TEXT,
        url TEXT, contrato TEXT, carga TEXT, salario TEXT, remoto TEXT, idioma TEXT,
        tecnologias TEXT, descripcion TEXT, completa INTEGER, extra TEXT,
        visto TEXT, actualizado TEXT, PRIMARY KEY (fuente, id))""")
    db.commit()
    return db


def guarda(db, ofertas):
    nuevas = 0
    ahora_ = datetime.datetime.now().isoformat(timespec="seconds")
    for o in ofertas:
        previa = db.execute("SELECT completa, descripcion, visto FROM ofertas WHERE fuente=? AND id=?",
                            (o["fuente"], o["id"])).fetchone()
        if previa and previa[0] and not o["completa"]:
            # no pisar una descripcion completa con un resumen
            o["descripcion"], o["completa"] = previa[1], True
        visto = previa[2] if previa else ahora_
        if not previa:
            nuevas += 1
        db.execute("INSERT OR REPLACE INTO ofertas VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (o["fuente"], o["id"], o["titulo"], o["empresa"], o["lugar"], o["fecha"],
                    o["url"], o["contrato"], o["carga"], o["salario"], o["remoto"], o["idioma"],
                    json.dumps(o["tecnologias"], ensure_ascii=False), o["descripcion"],
                    1 if o["completa"] else 0, json.dumps(o["extra"], ensure_ascii=False, default=str),
                    visto, ahora_))
    db.commit()
    return nuevas


def fila_a_oferta(f):
    return oferta(f[0], f[1], f[2], empresa=f[3], lugar=f[4], fecha=f[5], url=f[6], contrato=f[7],
                  carga=f[8], salario=f[9], remoto=f[10], idioma=f[11],
                  tecnologias=json.loads(f[12] or "[]"), descripcion=f[13],
                  completa=bool(f[14]), extra=json.loads(f[15] or "{}"))


# ---------------------------------------------------------------------------
# Salida en texto (121 columnas)
# ---------------------------------------------------------------------------

def envolver(txt, indent="", resto=None):
    resto = indent if resto is None else resto
    return textwrap.wrap(txt, width=ANCHO, initial_indent=indent, subsequent_indent=resto,
                         break_long_words=True, break_on_hyphens=False) or [indent.rstrip()]


def bloque_oferta(o, n=None, completo=True):
    L = []
    cab = f"{n}. " if n else ""
    L += envolver(f"{cab}{o['titulo']}")
    campos = [("Fuente", NOMBRES.get(o["fuente"], o["fuente"])), ("Empresa", o["empresa"]),
              ("Lugar", o["lugar"]), ("Fecha", o["fecha"]), ("Contrato", o["contrato"]),
              ("Carga", o["carga"]), ("Remoto", o["remoto"]), ("Salario", o["salario"]),
              ("Idioma", o["idioma"]), ("Tecnologias", ", ".join(o["tecnologias"])),
              ("Enlace", o["url"])]
    for k, v in campos:
        if v:
            L += envolver(f"{k}: {v}", "  ", "      ")
    ex = o.get("extra") or {}
    for k in ("inicio", "email_candidatura", "url_externa", "valido_hasta", "projekt_id"):
        if ex.get(k):
            L += envolver(f"{k}: {ex[k]}", "  ", "      ")
    if completo and o["descripcion"]:
        L.append("")
        for parrafo in o["descripcion"].split("\n"):
            L += envolver(parrafo, "  ", "    ") if parrafo.strip() else [""]
        if not o["completa"]:
            L.append("  [solo resumen: usa --detalle o el comando 'leer' para el texto completo]")
    return L


def escribe_txt(ruta, titulo, ofertas, completo=True):
    L = [titulo, f"Generado: {datetime.datetime.now():%Y-%m-%d %H:%M}   Ofertas: {len(ofertas)}",
         "=" * ANCHO, ""]
    for i, o in enumerate(ofertas, 1):
        L += bloque_oferta(o, i, completo)
        L += ["", "-" * ANCHO, ""]
    with open(ruta, "w", encoding="utf-8") as f:
        f.write("\n".join(L).rstrip() + "\n")


def resumen_linea(o, n):
    partes = [o["titulo"], o["empresa"], o["lugar"], o["fecha"], o["carga"] or o["salario"]]
    t = " | ".join(p for p in partes if p)
    return "\n".join(envolver(f"{n:3}. [{o['fuente']}] {t}", "", "      "))


# ---------------------------------------------------------------------------
# Comandos de busqueda / lectura
# ---------------------------------------------------------------------------

BUSCADORES = {"jobsch": lambda c, a: buscar_jobsch(c, a, "jobsch"),
              "jobup": lambda c, a: buscar_jobsch(c, a, "jobup"),
              "jobroom": buscar_jobroom, "swissdev": buscar_swissdev,
              "freelancermap": buscar_freelancermap, "eures": buscar_eures}
DETALLES = {"jobsch": detalle_jobsch, "jobup": detalle_jobsch, "jobroom": detalle_jobroom,
            "freelancermap": detalle_freelancermap, "eures": detalle_eures}


def completa_detalles(c, ofertas, pausa=1.0):
    for i, o in enumerate(ofertas, 1):
        if o["completa"] or o["fuente"] not in DETALLES:
            continue
        try:
            DETALLES[o["fuente"]](c, o)
            log(f"  detalle {i}/{len(ofertas)}: {o['titulo'][:60]}")
        except Exception as e:
            err(f"  no se pudo leer el detalle de {o['titulo'][:50]!r}: {e}")
        time.sleep(pausa)


def cmd_buscar(a):
    c = Cliente(a.via_extension, a.puerto)
    fuentes = FUENTES if a.fuente == "todas" else [a.fuente]
    db = abre_db()
    todas = []
    for f in fuentes:
        log(f"[{f}] buscando: {' '.join(a.terminos) or '(todo)'}")
        try:
            lista = BUSCADORES[f](c, a)
        except Exception as e:
            err(f"[{f}] error: {e}")
            continue
        log(f"[{f}] {len(lista)} oferta(s)")
        if a.detalle:
            completa_detalles(c, lista, a.pausa)
        elif a.solo_coincidentes and f != "swissdev":
            pass
        nuevas = guarda(db, lista)
        log(f"[{f}] guardadas en la base local ({nuevas} nuevas)")
        todas += lista
    if not todas:
        out("Sin resultados.")
        return 0
    asegura_dirs()
    base = "_".join([a.fuente, nombre_archivo(" ".join(a.terminos))])
    ruta = a.salida or os.path.join(DIR, f"empsui_{base}_{ahora()}.txt")
    escribe_txt(ruta, f"EMPSUI - {a.fuente} - {' '.join(a.terminos)}", todas, completo=True)
    out("")
    for i, o in enumerate(todas[:a.mostrar], 1):
        out(resumen_linea(o, i))
    if len(todas) > a.mostrar:
        out(f"... y {len(todas) - a.mostrar} mas (estan en el archivo).")
    out(f"\nArchivo: {ruta}")
    out(f"Base local: {RUTA_DB}")
    return 0


def cmd_leer(a):
    c = Cliente(a.via_extension, a.puerto)
    db = abre_db()
    f = a.fuente
    id_ = a.id
    if f not in DETALLES:
        err("La fuente debe ser una de: " + ", ".join(DETALLES))
        return 2
    fila = db.execute("SELECT * FROM ofertas WHERE fuente=? AND id=?", (f, id_)).fetchone()
    if fila:
        o = fila_a_oferta(fila)
    else:
        m = re.search(r"([0-9a-f]{8}-[0-9a-f-]{27})", id_)
        o = oferta(f, m.group(1) if m else id_, "", url=id_ if id_.startswith("http") else "")
        if f in ("jobsch", "jobup") and m:
            o["id"] = m.group(1)
        if f == "freelancermap" and id_.startswith("http"):
            o["id"] = id_.rstrip("/").rsplit("/", 1)[-1]
    DETALLES[f](c, o)
    guarda(db, [o])
    ruta = a.salida or os.path.join(DIR, f"empsui_{f}_{nombre_archivo(o['titulo'] or o['id'])}_{ahora()}.txt")
    escribe_txt(ruta, f"EMPSUI - {f}", [o])
    out("\n".join(bloque_oferta(o)))
    out(f"\nArchivo: {ruta}")
    return 0


def cmd_url(a):
    c = Cliente(a.via_extension, a.puerto)
    db = abre_db()
    lista = leer_url_generica(c, a.url, a.render)
    guarda(db, lista)
    ruta = a.salida or os.path.join(DIR, f"empsui_web_{nombre_archivo(lista[0]['titulo'])}_{ahora()}.txt")
    escribe_txt(ruta, "EMPSUI - web", lista)
    for o in lista:
        out("\n".join(bloque_oferta(o)))
    out(f"\nArchivo: {ruta}")
    return 0


def cmd_pestana(a):
    r = pedir_trabajo({"op": "pestana", "contiene": a.contiene or ""}, a.puerto)
    url = r.get("url", "")
    lista = ofertas_de_html(r.get("text", ""), url)
    for o in lista:
        if not o["titulo"] and r.get("titulo"):
            o["titulo"] = r["titulo"]
    db = abre_db()
    guarda(db, lista)
    ruta = a.salida or os.path.join(DIR, f"empsui_pestana_{nombre_archivo(lista[0]['titulo'])}_{ahora()}.txt")
    escribe_txt(ruta, "EMPSUI - pestana de Firefox", lista)
    for o in lista:
        out("\n".join(bloque_oferta(o)))
    out(f"\nArchivo: {ruta}")
    return 0


def cmd_local(a):
    db = abre_db()
    filas = db.execute("SELECT * FROM ofertas ORDER BY fecha DESC, visto DESC").fetchall()
    res = []
    for f in filas:
        o = fila_a_oferta(f)
        if a.fuente and o["fuente"] != a.fuente:
            continue
        if coincide(o, a.terminos):
            res.append(o)
    if a.desde:
        limite = (datetime.date.today() - datetime.timedelta(days=a.desde)).isoformat()
        res = [o for o in res if o["fecha"] >= limite]
    if not res:
        out("No hay ofertas guardadas que coincidan.")
        return 0
    for i, o in enumerate(res[:a.mostrar], 1):
        out(resumen_linea(o, i))
    if a.salida or a.exportar:
        ruta = a.salida or os.path.join(DIR, f"empsui_local_{nombre_archivo(' '.join(a.terminos))}_{ahora()}.txt")
        escribe_txt(ruta, "EMPSUI - busqueda en la base local", res)
        out(f"\nArchivo: {ruta}")
    out(f"\n{len(res)} coincidencia(s) de {len(filas)} guardadas.")
    return 0


FILTROS_TXT = {
    "freelancermap": [
        "Se envian como parametros de https://www.freelancermap.ch/projekte (nombres tomados de la propia web).",
        "--fm-pais ch|de|at|uk|us|pl       countries[0..]  (defecto ch = Schweiz, id 3)",
        "--remoto presencial|hibrido|remoto remoteInPercent[0..] (on_site, hybrid, remote)",
        "--contrato freelance|leasing|fijo contractTypes[0..] (contracting, employee_leasing, permanent_position)",
        "--categoria software|texto|diseno|marketing|ingenieria|finanzas   categories[0..] (1,2,3,4,6,7)",
        "--sector ti|energia|finanzas|transporte|telecom|maquinaria|hochschulen|pharma|administracion|construccion",
        "                                   industry[0..] (24,7,9,18,16,1,11,14,20,3)",
        "--desde N                          created=N (proyectos de los ultimos N dias, 1 a 30)",
        "--cliente-final                    endcustomer=1",
        "--ocultar-beworben                 hideAppliedProjects=1",
        "--ubicacion X --radio KM           city=X, radius=KM",
        "--cualquiera                       una busqueda por palabra y union de resultados",
        "--filtro CLAVE=VALOR               cualquier otro parametro (queryParts, keywords, subCategories, ...)",
        "Con --via-extension se usa tu sesion de Firefox: la web da todas las paginas (sin sesion solo 22).",
        "El total que anuncia la web se muestra en pantalla: comparalo para comprobar que un filtro se aplico."],
    "jobsch": [
        "API https://www.jobs.ch/api/v1/public/search (y www.jobup.ch):",
        "--ubicacion X            location=X",
        "--ti                     category-ids[]=106 (informatica)",
        "--desde N                publication-date-from / publication-date-to",
        "--carga-min / --carga-max employment-grade-min / employment-grade-max",
        "--idioma de|en|fr|it     language-skills[]=...",
        "--filtro CLAVE=VALOR     cualquier otro (la web tiene ademas: tipo de contrato, posicion, sector,",
        "                         tipo de empresa y empresa; usa --mostrar-url y compara el total)"],
    "jobroom": ["--ubicacion ZH (codigo de canton), --desde N."],
    "swissdev": [
        "Lee el feed RSS (unas 270 ofertas; la web anuncia mas).",
        "--tech X (repetible)     la tecnologia debe aparecer en la oferta",
        "--ubicacion X            ciudad (Zurich, Basel, Bern, Geneva, Lausanne, Zug, Luzern, ...)",
        "--salario-min CHF        salario anual minimo anunciado",
        "--desde N                ultimos N dias"],
    "eures": ["--pais ch,de,... (codigos), --ti (ocupaciones TIC), --desde N."],
}


def cmd_filtros(a):
    claves = [a.fuente] if a.fuente else list(FILTROS_TXT)
    for k in claves:
        out(f"== {k}")
        for linea in FILTROS_TXT.get(k, ["(sin filtros especiales)"]):
            out("  " + linea)
    return 0


def cmd_ver(a):
    db = abre_db()
    f = db.execute("SELECT * FROM ofertas WHERE id=? OR url LIKE ?", (a.id, "%" + a.id + "%")).fetchone()
    if not f:
        err("No esta en la base local.")
        return 1
    out("\n".join(bloque_oferta(fila_a_oferta(f))))
    return 0


def cmd_estado(a):
    db = abre_db()
    out(f"empsui {VERSION}   carpeta: {DIR}")
    for f, n, c in db.execute("SELECT fuente, COUNT(*), SUM(completa) FROM ofertas GROUP BY fuente"):
        out(f"  {NOMBRES.get(f, f):22} {n:6} ofertas ({c or 0} con texto completo)")
    try:
        r = pedir_estado(a.puerto)
        out(f"Puente: servidor activo; extension "
            + ("conectada" if r.get("sondeo_hace") is not None and r["sondeo_hace"] < 30 else "sin senales"))
    except Exception:
        out("Puente: no hay servidor activo (empsui.py lo arranca solo al usar --via-extension)")
    return 0


# ---------------------------------------------------------------------------
# Puente con la extension (servidor local 127.0.0.1)
# ---------------------------------------------------------------------------

class EstadoPuente:
    def __init__(self):
        self.pendientes = []
        self.resultados = {}
        self.lock = threading.Lock()
        self.n = 0
        self.ultimo_sondeo = None
        self.ultimo_abrir = None
        self.ext_url = None
        self.ext_version = None
        self.log_ext = None


PAGINA_ABRIR = ("<!doctype html><meta charset=utf-8><meta name=\"empsui-despertar\" content=\"1\">"
                "<title>empsui</title><p>empsui: despertando la extension. Puedes cerrar esta pestana.</p>")


def crear_handler(estado):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _cors(self):
            origen = self.headers.get("Origin", "")
            if origen.startswith("moz-extension://"):
                self.send_header("Access-Control-Allow-Origin", origen)
                self.send_header("Access-Control-Allow-Headers", "X-Empsui, Content-Type")
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
            if self.headers.get("X-Empsui", "") != "1":
                return False
            host = self.headers.get("Host", "").split(":")[0]
            if host not in ("127.0.0.1", "localhost"):
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
            if self.path.split("?")[0] == "/abrir" and self.headers.get("Host", "").split(":")[0] in ("127.0.0.1", "localhost"):
                with estado.lock:
                    estado.ultimo_abrir = time.time()
                cuerpo = PAGINA_ABRIR.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(cuerpo)))
                self.end_headers()
                self.wfile.write(cuerpo)
                return
            if not self._autorizado():
                return self._resp(403, {"error": "no autorizado"})
            if self.path == "/estado":
                with estado.lock:
                    ahora = time.time()
                    hace = None if estado.ultimo_sondeo is None else ahora - estado.ultimo_sondeo
                    return self._resp(200, {"empsui": True, "sondeo_hace": hace, "ext_url": estado.ext_url,
                                            "ext_version": estado.ext_version, "log_ext": estado.log_ext,
                                            "abrir_hace": None if estado.ultimo_abrir is None else ahora - estado.ultimo_abrir})
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
                    estado.ext_version = str(datos.get("version", ""))[:20] or estado.ext_version
                    estado.ultimo_sondeo = time.time()
                return self._resp(200, {"ok": True})
            if self.path == "/registro":
                nombre = str(datos.get("nombre", ""))
                if not re.fullmatch(r"[0-9]{4}(-[0-9]{2}){5}logEmpsui\.log", nombre):
                    return self._resp(400, {"error": "nombre"})
                asegura_dirs()
                os.makedirs(DIR_LOGS, exist_ok=True)
                with open(os.path.join(DIR_LOGS, nombre), "w", encoding="utf-8") as f:
                    f.write("\n".join(envuelve(str(datos.get("texto", "")))) + "\n")
                with estado.lock:
                    estado.log_ext = os.path.join(DIR_LOGS, nombre)
                return self._resp(200, {"ok": True})
            self._resp(404, {"error": "no existe"})

    return H


def cmd_servidor(a):
    estado = EstadoPuente()
    srv = ThreadingHTTPServer(("127.0.0.1", a.puerto), crear_handler(estado))
    log(f"Servidor del puente en 127.0.0.1:{a.puerto} (solo este telefono; sin token)")
    log("La extension empsui se conecta sola si Firefox esta abierto. Deja esto abierto. Ctrl+C para parar.")
    log("(No es obligatorio: con --via-extension, empsui.py arranca su propio servidor y despierta la extension.)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("Parado.")
    return 0


def _base(puerto):
    return f"http://127.0.0.1:{puerto}"


def _cab():
    return {"X-Empsui": "1", "Content-Type": "application/json"}


def pedir_estado(puerto, espera=5):
    req = request.Request(_base(puerto) + "/estado", headers=_cab())
    with request.urlopen(req, timeout=espera) as r:
        return json.loads(r.read().decode("utf-8"))


PAQUETES_FIREFOX = ["org.mozilla.fenix", "org.mozilla.firefox", "org.mozilla.firefox_beta"]
_PUENTE = {"listo": False, "srv": None}


def espera_sondeo(puerto, segundos):
    fin = time.time() + segundos
    while True:
        try:
            h = pedir_estado(puerto).get("sondeo_hace")
            if h is not None and h < 10:
                return True
        except Exception:
            pass
        if time.time() >= fin:
            return False
        time.sleep(0.5)


def paquetes_firefox():
    forzado = os.environ.get("EMPSUI_FIREFOX")
    if forzado:
        return [p.strip() for p in forzado.split(",") if p.strip()]
    instalados = []
    if shutil.which("pm"):
        try:
            r = subprocess.run(["pm", "list", "packages"], capture_output=True, text=True, timeout=20)
            instalados = [p for p in PAQUETES_FIREFOX if f"package:{p}" in r.stdout.split()]
        except Exception:
            pass
    return [p for p in PAQUETES_FIREFOX if p in instalados] or PAQUETES_FIREFOX


def despierta_extension(puerto):
    """True si la extension esta sondeando el servidor; si no, intenta abrir Firefox con 'am start'."""
    if espera_sondeo(puerto, 8):
        return True
    log("Extension: sin senales; intento abrir Firefox para despertarla...")
    if os.environ.get("EMPSUI_NO_ABRIR") or not shutil.which("am"):
        return False
    url = f"{_base(puerto)}/abrir"
    for pkg in paquetes_firefox():
        try:
            r = subprocess.run(["am", "start", "-a", "android.intent.action.VIEW", "-d", url, "-p", pkg],
                               capture_output=True, text=True, timeout=25)
            salida = (r.stdout + " " + r.stderr).strip().replace("\n", " | ")
            ok = r.returncode == 0 and "Error" not in salida
        except Exception as e:
            ok, salida = False, str(e)
        log(f"  am start en {pkg}: " + ("lanzado" if ok else f"fallo ({salida[:120]})"))
        if ok and espera_sondeo(puerto, 15):
            return True
        try:
            h = pedir_estado(puerto).get("abrir_hace")
            if h is not None and h < 40:
                DIAG.puente["abrir_visto"] = True
                err("  Firefox abrio la pagina de despertar, pero la extension empsui no respondio: esta instalada y activa?")
                break
        except Exception:
            pass
    return False


def asegura_puente(puerto):
    """Arranca (o reutiliza) el servidor local y despierta la extension. Se hace una vez por ejecucion."""
    if _PUENTE["listo"]:
        return
    try:
        est = pedir_estado(puerto)
        if est.get("empsui"):
            DIAG.puente["servidor"] = "reutilizado"
            log(f"Puente: reutilizo el servidor empsui que ya esta activo en el puerto {puerto}.")
        else:
            DIAG.puente["servidor"] = "antiguo (sin soporte de despertar)"
            raise RuntimeError(f"En el puerto {puerto} hay un servidor empsui ANTIGUO (por ejemplo 'empsui.py "
                               f"servidor' lanzado con la version 1.0.0) que no sabe despertar la extension ni "
                               f"guardar diagnostico. Cierralo (Ctrl+C en su sesion de Termux, o 'pkill -f "
                               f"empsui.py') y vuelve a lanzar el comando.")
    except RuntimeError:
        raise
    except Exception:
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", puerto), crear_handler(EstadoPuente()))
        except OSError as e:
            raise RuntimeError(f"No se pudo abrir el puerto {puerto} (otro programa lo usa?): {e}. "
                               f"Usa --puerto con otro numero (el mismo en la extension).")
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        _PUENTE["srv"] = srv
        DIAG.puente["servidor"] = "propio (hilo en este proceso)"
        log(f"Puente: servidor local en 127.0.0.1:{puerto} (solo este telefono).")
    if despierta_extension(puerto):
        DIAG.puente["extension"] = "conectada"
        log("Extension: conectada.")
    else:
        DIAG.puente["extension"] = "sin respuesta"
        raise RuntimeError("La extension no responde. Abre Firefox y comprueba que empsui esta instalada y que "
                           "la casilla 'Conectar con Termux' de su pagina esta marcada (el icono la abre).")
    _PUENTE["listo"] = True


def pedir_trabajo(params, puerto=PUERTO_DEFECTO, espera=600):
    """Deja un trabajo para la extension y devuelve su respuesta completa."""
    asegura_puente(puerto)
    try:
        req = request.Request(_base(puerto) + "/trabajo", data=json.dumps(params).encode("utf-8"),
                              headers=_cab(), method="POST")
        with request.urlopen(req, timeout=10) as r:
            rid = json.loads(r.read().decode("utf-8"))["id"]
    except (error.URLError, OSError):
        raise RuntimeError("No hay servidor del puente.")
    t0 = time.time()
    avisado = 0
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
        if time.time() - t0 - avisado > 20:
            avisado = time.time() - t0
            log("  esperando a la extension (Firefox abierto?)...")
        time.sleep(1.0)
    raise RuntimeError("La extension no respondio a tiempo.")


# ---------------------------------------------------------------------------
# Informe de diagnostico (un solo .txt de 121 columnas que se puede mandar entero)
# ---------------------------------------------------------------------------

def entorno():
    e = [f"empsui.py {VERSION}", f"Python {platform.python_version()} en {platform.system()} {platform.machine()}",
         f"Termux: {'si' if os.environ.get('PREFIX', '').startswith('/data/data/com.termux') else 'no detectado'}",
         f"Carpeta de trabajo: {os.getcwd()}", f"Carpeta empsui: {DIR}",
         f"Comando 'am': {shutil.which('am') or 'no'}; 'pm': {shutil.which('pm') or 'no'}"]
    for v in ("EMPSUI_DIR", "EMPSUI_FIREFOX", "EMPSUI_NO_ABRIR"):
        if os.environ.get(v):
            e.append(f"{v}={os.environ[v]}")
    return e


def sugerencias():
    sug = []
    clases = [p["clase"] for p in DIAG.peticiones]
    vias = {p["via"] for p in DIAG.peticiones}
    if any(c.startswith("bloqueo") for c in clases):
        sug.append("Una web mostro una verificacion anti-bot: abre esa web en Firefox, resuelvela, y repite con "
                   "--via-extension (o usa el comando pestana).")
    if "no-json" in clases:
        sug.append("Una web devolvio una pagina HTML en vez de datos (proteccion anti-bot, aviso o cambio de la web). Si fue "
                   "directo, repite con --via-extension; si ya fue por la extension, envia este informe.")
    if any(c in ("http-403", "http-429") for c in clases) and "directo" in vias:
        sug.append("La web rechaza las peticiones directas desde Termux (403/429): repite con --via-extension.")
    if any("freelancermap devolvio la pagina" in a for a in DIAG.avisos):
        sug.append("freelancermap limita las paginas sin sesion: inicia sesion en freelancermap.ch en el Firefox de la "
                   "extension y repite con --via-extension. Si ya la tenias iniciada y sigue igual, la limitacion es de la web.")
    if any("no dio proyectos reconocibles" in a for a in DIAG.avisos):
        sug.append("freelancermap cambio su pagina o mostro otra cosa: envia este informe (la pagina esta en la carpeta respuestas).")
    if DIAG.puente.get("extension") == "sin respuesta":
        if DIAG.puente.get("abrir_visto"):
            sug.append("Firefox abrio la pagina de despertar pero la extension no contesto: comprueba que empsui.xpi 1.1.0 "
                       "(o posterior) esta instalada y habilitada. La version 1.0.0 NO se despierta sola.")
        else:
            sug.append("La extension no responde y Firefox no llego a abrir la pagina de despertar. Abre Firefox a mano; si "
                       "'am start' fallo, da a Termux el permiso 'Mostrar sobre otras aplicaciones' en Android.")
    if any("No se pudo abrir el puerto" in e for e in DIAG.errores):
        sug.append("El puerto esta ocupado: usa --puerto con otro numero (el mismo en la extension).")
    if any("No se pudo conectar" in e for e in DIAG.errores):
        sug.append("Sin red desde Termux: comprueba la conexion a internet.")
    if not sug and DIAG.errores:
        sug.append("Mira las lineas ERROR del registro y las respuestas guardadas (carpeta 'respuestas').")
    return sug or ["No se detectaron problemas."]


def escribe_informe(destino=None):
    destino = destino or os.getcwd()
    ruta = os.path.join(destino, f"{SELLO}diagEmpsui.txt")
    L = ["=" * 121, f"INFORME DE DIAGNOSTICO DE EMPSUI {VERSION}", f"Generado: {time.strftime('%Y-%m-%d %H:%M:%S')}", "=" * 121, ""]
    L += ["1. RESUMEN", f"Comando: {DIAG.comando}", f"Resultado: {DIAG.resultado}",
          f"Errores: {len(DIAG.errores)}   Avisos: {len(DIAG.avisos)}", ""]
    if DIAG.avisos:
        L += ["Avisos:"] + [f"- {a}" for a in DIAG.avisos] + [""]
    L += ["2. QUE HACER (sugerencias automaticas)"] + [f"- {x}" for x in sugerencias()] + [""]
    L += ["3. ENTORNO"] + entorno() + [""]
    L.append("4. PUENTE CON LA EXTENSION")
    L += [f"{k}: {v}" for k, v in DIAG.puente.items()] or ["(no se uso el puente)"]
    ext_log = None
    try:
        est = pedir_estado(PUERTO_USADO[0], 3)
        L.append(f"Version de la extension: {est.get('ext_version') or 'desconocida (1.0.0 no la envia)'}")
        ext_log = est.get("log_ext")
    except Exception:
        pass
    L.append("")
    L.append("5. PETICIONES (n, metodo, estado, clase, via, bytes, segundos, url)")
    for p in DIAG.peticiones or []:
        L.append(f"{p['n']:>3} {p['metodo']:4} {str(p['status']):>4} {p['clase']:<22} {p['via']:<10} {p['bytes']:>8} "
                 f"{p['seg']:>5}s {p['url']}")
    if not DIAG.peticiones:
        L.append("(ninguna)")
    L.append("")
    L.append("6. RESPUESTAS INESPERADAS (muestra del texto)")
    malas = [p for p in DIAG.peticiones if p["clase"].startswith(("http-", "bloqueo", "vacio", "sin-", "no-json"))]
    for p in malas:
        L.append(f"Peticion {p['n']}: {p['url']}")
        L.append(f"  estado {p['status']}, clase {p['clase']}, titulo {p['titulo']!r}")
        if p["muestra"]:
            L.append("  muestra: " + p["muestra"][:400])
        if p["archivo"]:
            L.append(f"  cuerpo completo guardado en: {p['archivo']}")
    if not malas:
        L.append("(ninguna)")
    L.append("")
    L.append("7. REGISTRO DE ESTA EJECUCION (empsui.py)")
    L += DIAG.lineas[-400:]
    L.append("")
    L.append("8. REGISTRO DE LA EXTENSION (Firefox)")
    if ext_log and os.path.exists(ext_log):
        try:
            with open(ext_log, encoding="utf-8") as f:
                L += f.read().splitlines()[-150:]
        except OSError as e:
            L.append(f"(no se pudo leer {ext_log}: {e})")
    else:
        L.append("(la extension no envio registro en esta ejecucion)")
    L += ["", f"Registro completo de esta ejecucion: {DIAG.ruta or '(no se pudo escribir)'}"]
    try:
        with open(ruta, "w", encoding="utf-8") as f:
            f.write("\n".join(x for l in L for x in envuelve(l)) + "\n")
    except OSError as e:
        print(f"No se pudo escribir el informe de diagnostico: {e}", file=sys.stderr)
        return None
    DIAG.informe_ruta = ruta
    return ruta


PUERTO_USADO = [PUERTO_DEFECTO]


def cmd_diagnostico(a):
    """Prueba cada fuente (directo y, si se puede, por la extension) y escribe el informe."""
    import argparse as _ap
    out(f"Diagnostico de empsui {VERSION}")
    for linea in entorno():
        out("  " + linea)
    ns = _ap.Namespace(terminos=["python"], ubicacion=None, max=3, desde=None, ti=False, idioma=None,
                       carga_min=None, pais=None, detalle=False, pausa=0.0, mostrar=3, refrescar=True,
                       solo_coincidentes=False, via_extension=False, puerto=a.puerto)
    modos = [("directo", False)]
    try:
        asegura_puente(a.puerto)
        modos.append(("extension", True))
    except RuntimeError as e:
        err(f"Extension no disponible: {e}")
    fallos = 0
    for nombre, via in modos:
        out(f"Pruebas en modo {nombre}:")
        c = Cliente(via, a.puerto)
        for f in FUENTES:
            try:
                n = len(BUSCADORES[f](c, ns))
                out(f"  {'OK   ' if n else 'FALLA'} {f:<14} {n} oferta(s)")
                fallos += 0 if n else 1
            except Exception as e:
                err(f"  FALLA {f:<14} {str(e)[:160]}")
                fallos += 1
    DIAG.resultado = f"diagnostico: {fallos} prueba(s) fallida(s)" if fallos else "diagnostico: todo bien"
    out(f"Resultado: {DIAG.resultado}.")
    return 0 if not fallos else 1


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv=None):
    comun = argparse.ArgumentParser(add_help=False)
    comun.add_argument("--via-extension", action="store_true", dest="via_extension",
                       help="Hacer las peticiones desde Firefox (con tu sesion) en vez de desde Termux")
    comun.add_argument("--puerto", type=int, default=PUERTO_DEFECTO)
    comun.add_argument("--informe", action="store_true", help="Escribir siempre el informe de diagnostico")
    comun.add_argument("--sin-informe", action="store_true", dest="sin_informe",
                       help="No escribir informe de diagnostico automatico")
    comun.add_argument("-o", "--salida", help="Ruta del archivo .txt de salida")

    p = argparse.ArgumentParser(prog="empsui.py", description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"empsui {VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("buscar", parents=[comun], help="Buscar ofertas en una fuente (o en todas)")
    b.add_argument("fuente", choices=FUENTES + ["todas"])
    b.add_argument("terminos", nargs="*", help="Palabras a buscar (todas deben aparecer)")
    b.add_argument("--ubicacion", "-u", help="Ciudad o canton (jobroom: codigo de 2 letras, p. ej. ZH)")
    b.add_argument("--max", type=int, default=40, help="Maximo de ofertas por fuente (40)")
    b.add_argument("--desde", type=int, help="Solo ofertas de los ultimos N dias")
    b.add_argument("--ti", action="store_true", help="Solo informatica (jobs.ch/jobup y EURES)")
    b.add_argument("--idioma", action="append", help="jobs.ch: idioma exigido (de, en, fr, it); repetible")
    b.add_argument("--carga-min", type=int, dest="carga_min", help="Porcentaje minimo de jornada")
    b.add_argument("--pais", help="EURES: paises separados por comas (por defecto ch)")
    b.add_argument("--carga-max", type=int, dest="carga_max", help="jobs.ch/jobup: porcentaje maximo de jornada")
    b.add_argument("--filtro", action="append", metavar="CLAVE=VALOR",
                   help="Parametro extra tal cual para la web/API (repetible; mira 'empsui.py filtros')")
    b.add_argument("--tech", action="append", help="swissdev: tecnologia que debe aparecer (repetible)")
    b.add_argument("--salario-min", type=int, dest="salario_min", help="swissdev: salario anual minimo en CHF")
    b.add_argument("--remoto", action="append", metavar="MODO",
                   help="freelancermap: presencial, hibrido o remoto (repetible o separado por comas)")
    b.add_argument("--contrato", action="append", metavar="TIPO",
                   help="freelancermap: freelance, leasing o fijo (repetible)")
    b.add_argument("--categoria", action="append", help="freelancermap: software, texto, diseno, marketing, ingenieria, finanzas")
    b.add_argument("--sector", action="append", help="freelancermap: ti, energia, finanzas, transporte, telecom, ... ")
    b.add_argument("--fm-pais", action="append", dest="fm_pais", help="freelancermap: ch (defecto), de, at, uk, us, pl")
    b.add_argument("--radio", type=int, help="freelancermap: radio en km alrededor de --ubicacion")
    b.add_argument("--cliente-final", action="store_true", dest="cliente_final", help="freelancermap: solo cliente final")
    b.add_argument("--ocultar-beworben", action="store_true", dest="ocultar_beworben",
                   help="freelancermap: ocultar proyectos a los que ya te presentaste")
    b.add_argument("--cualquiera", action="store_true", help="freelancermap: cada palabra es una busqueda aparte y se unen los resultados")
    b.add_argument("--mostrar-url", action="store_true", dest="mostrar_url", help="Mostrar la direccion exacta pedida")
    b.add_argument("--detalle", action="store_true", help="Leer tambien el texto completo de cada oferta")
    b.add_argument("--pausa", type=float, default=1.0, help="Segundos entre detalles (1)")
    b.add_argument("--mostrar", type=int, default=25, help="Lineas de resumen a mostrar (25)")
    b.add_argument("--refrescar", action="store_true", help="swissdev: volver a bajar el feed")
    b.add_argument("--solo-coincidentes", action="store_true", dest="solo_coincidentes", help=argparse.SUPPRESS)
    b.set_defaults(func=cmd_buscar)

    l = sub.add_parser("leer", parents=[comun], help="Leer una oferta completa por su id o enlace")
    l.add_argument("fuente", choices=list(DETALLES))
    l.add_argument("id", help="Identificador o enlace de la oferta")
    l.set_defaults(func=cmd_leer)

    u = sub.add_parser("url", parents=[comun], help="Leer una pagina cualquiera (JobPosting o texto)")
    u.add_argument("url")
    u.add_argument("--render", action="store_true",
                   help="Con --via-extension: abrir la pagina en Firefox y leerla ya pintada (webs con JavaScript)")
    u.set_defaults(func=cmd_url)

    pe = sub.add_parser("pestana", parents=[comun], help="Leer la pestana de Firefox que tengas abierta (via extension)")
    pe.add_argument("--contiene", help="Elegir la pestana cuya direccion contenga este texto")
    pe.set_defaults(func=cmd_pestana)

    lo = sub.add_parser("local", parents=[comun], help="Buscar en lo ya guardado (sin red)")
    lo.add_argument("terminos", nargs="*")
    lo.add_argument("--fuente", choices=FUENTES + ["web"])
    lo.add_argument("--desde", type=int)
    lo.add_argument("--mostrar", type=int, default=40)
    lo.add_argument("--exportar", action="store_true", help="Guardar el resultado en un .txt")
    lo.set_defaults(func=cmd_local)

    v = sub.add_parser("ver", parents=[comun], help="Mostrar una oferta guardada")
    v.add_argument("id")
    v.set_defaults(func=cmd_ver)

    fi = sub.add_parser("filtros", parents=[comun], help="Listar los filtros disponibles de cada fuente")
    fi.add_argument("fuente", nargs="?", choices=list(FILTROS_TXT))
    fi.set_defaults(func=cmd_filtros)

    e = sub.add_parser("estado", parents=[comun], help="Resumen de lo guardado y estado del puente")
    e.set_defaults(func=cmd_estado)

    s = sub.add_parser("servidor", parents=[comun], help="Servidor del puente con la extension")
    s.set_defaults(func=cmd_servidor)

    d = sub.add_parser("diagnostico", parents=[comun], help="Prueba cada fuente (directo y por la extension) y escribe un informe")
    d.set_defaults(func=cmd_diagnostico)

    a = p.parse_args(argv)
    PUERTO_USADO[0] = getattr(a, "puerto", PUERTO_DEFECTO)
    DIAG.comando = "empsui.py " + " ".join(argv if argv is not None else sys.argv[1:])
    codigo = 0
    try:
        codigo = a.func(a) or 0
    except RuntimeError as ex:
        err(f"Error: {ex}")
        codigo = 1
    except KeyboardInterrupt:
        err("Interrumpido.")
        codigo = 130
    except Exception as ex:  # error inesperado: queda todo en el informe
        import traceback
        err(f"Error inesperado: {ex}")
        DIAG.linea("TRACE", traceback.format_exc())
        codigo = 1
    DIAG.resultado = DIAG.resultado if DIAG.resultado != "(sin terminar)" else ("OK" if codigo == 0 else f"ERROR (codigo {codigo})")
    if a.cmd != "servidor" and not getattr(a, "sin_informe", False) and (
            getattr(a, "informe", False) or codigo != 0 or DIAG.errores or DIAG.avisos or a.cmd == "diagnostico"):
        if DIAG.puente.get("servidor"):
            time.sleep(1.5)  # deja que la extension envie su registro
        ruta = escribe_informe()
        if ruta:
            (err if codigo else out)(f"Informe de diagnostico: {ruta}")
    return codigo


if __name__ == "__main__":
    sys.exit(main())
