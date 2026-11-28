#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""swn.py - Busca y lee noticias de Swissinfo (varios idiomas), SRF y licitaciones de simap desde Termux.

Funciona junto a la extension de Firefox "swn" (swn.xpi): este programa abre un servidor local en
127.0.0.1:<puerto>; la extension, que esta en tu navegador, recoge los trabajos, abre/lee las paginas y
devuelve los resultados. Solo libreria estandar de Python 3.

Uso rapido:
    python swn.py estado
    python swn.py buscar "inteligencia artificial" -s swissinfo,srf -i es,en,de -n 20
    python swn.py buscar "\"Sperrzeit\"" -s todos --leer
    python swn.py leer 1 3 5
    python swn.py local "digital"
Manual completo: swnManualBono.txt
"""
import argparse
import datetime
import json
import os
import re
import shutil
import sqlite3
import sys
import textwrap
import threading
import time
import unicodedata
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.0.0"
DIR = os.environ.get("SWN_DIR") or os.path.join(os.path.expanduser("~"), "swn")
PUERTO_DEF = int(os.environ.get("SWN_PUERTO", "8777"))

IDIOMAS = {  # alias corto -> codigo de Swissinfo
    "es": "spa", "en": "eng", "de": "ger", "fr": "fre", "it": "ita",
    "pt": "por", "ar": "ara", "zh": "chi", "ja": "jpn", "ru": "rus",
}
NOMBRES = {"spa": "espanol", "eng": "ingles", "ger": "aleman", "fre": "frances", "ita": "italiano",
           "por": "portugues", "ara": "arabe", "chi": "chino", "jpn": "japones", "rus": "ruso"}
SITIOS = ("swissinfo", "srf", "simap")
# periodo -> (filtro swissinfo, filtro SRF, dias para el recorte local)
PERIODOS = {
    "hoy": ("last_week", "day", 1), "3dias": ("last_week", "threeDays", 3), "semana": ("last_week", "week", 7),
    "mes": ("last_month", "month", 31), "6meses": ("last_year", "sixMonths", 183), "anio": ("last_year", "year", 366),
}
TIPOS = {"articulo": "article", "noticia": "article", "ticker": "ticker", "opinion": "opinion", "video": "video",
         "briefing": "briefing", "debate": "debate", "galeria": "gallery", "newsletter": "newsletter"}

_plock = threading.Lock()
_diag_fh = None
_diag_ruta = None
_ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


# ----------------------------------------------------------------------------- salida
def out(msg=""):
    with _plock:
        print(msg)
        sys.stdout.flush()
    dlog("OUT " + str(msg))


def err(msg):
    with _plock:
        print(msg, file=sys.stderr)
        sys.stderr.flush()
    dlog("ERR " + str(msg))


def dlog(msg):
    """Registro de diagnostico: todo lo que pasa, con hora."""
    global _diag_fh, _diag_ruta
    try:
        if _diag_fh is None:
            os.makedirs(os.path.join(DIR, "diag"), exist_ok=True)
            _diag_ruta = os.path.join(DIR, "diag", "swn_%s.log" % _ts)
            _diag_fh = open(_diag_ruta, "a", encoding="utf-8")
            _diag_fh.write("swn.py %s | python %s | args: %s\n" % (VERSION, sys.version.split()[0], " ".join(sys.argv[1:])))
        _diag_fh.write("%s %s\n" % (datetime.datetime.now().strftime("%H:%M:%S"), str(msg).replace("\n", "\n    ")))
        _diag_fh.flush()
    except OSError:
        pass


# ----------------------------------------------------------------------------- texto
def norm(s):
    """Minusculas, sin acentos y con ae/oe/ue = a/o/u (asi 'kuenstliche' == 'Kunstliche' == 'Künstliche')."""
    s = unicodedata.normalize("NFD", (s or "").lower().replace("\u00df", "ss"))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"([aou])e", r"\1", s)


def terminos(q):
    return [norm(a or b) for a, b in re.findall(r'"([^"]+)"|(\S+)', q or "")]


def coincide(texto, terms):
    t = norm(texto)
    return bool(terms) and all(x in t for x in terms)


def fecha_corta(d):
    return (d or "")[:16].replace("T", " ")


def ancho_term(args=None):
    if args is not None and getattr(args, "ancho", None):
        return args.ancho
    try:
        return max(40, min(100, shutil.get_terminal_size((78, 20)).columns - 1))
    except OSError:
        return 78


def envolver(texto, sangria, ancho):
    pad = " " * sangria
    return "\n".join(textwrap.fill(p, width=ancho, initial_indent=pad, subsequent_indent=pad) if p.strip() else ""
                     for p in (texto or "").split("\n"))


def slug(s, n=50):
    s = re.sub(r"[^A-Za-z0-9]+", "_", norm(s)).strip("_")
    return s[:n] or "busqueda"


def id_item(it):
    url = it.get("url", "") or ""
    if it.get("site") == "swissinfo":
        m = re.search(r"/(\d{6,})(?:[?#].*)?$", url)
        return it.get("id") or (m.group(1) if m else slug(url.rsplit("/", 1)[-1], 40))
    if it.get("site") == "simap":
        m = re.search(r"project-detail/([0-9a-f-]{8,})", url)
        return (it.get("id") or (m.group(1) if m else ""))[:8] or slug(url, 20)
    return slug((it.get("id") or url).rsplit("/", 1)[-1], 60)


# ----------------------------------------------------------------------------- base de datos
def abrir_db():
    os.makedirs(DIR, exist_ok=True)
    db = sqlite3.connect(os.path.join(DIR, "swn.db"))
    db.execute("""CREATE TABLE IF NOT EXISTS resultados(url TEXT PRIMARY KEY, site TEXT, lang TEXT, id TEXT, title TEXT,
                  date TEXT, lead TEXT, kicker TEXT, office TEXT, place TEXT, extra TEXT, termino TEXT, visto TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS articulos(url TEXT PRIMARY KEY, site TEXT, lang TEXT, title TEXT, date TEXT,
                  author TEXT, lead TEXT, text TEXT, access TEXT, words INTEGER, ruta TEXT, guardado TEXT)""")
    return db


def guardar_resultado(db, it, termino):
    db.execute("INSERT OR REPLACE INTO resultados VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (it.get("url"), it.get("site"), it.get("lang"), it.get("id"), it.get("title"), it.get("date"), it.get("lead"),
                it.get("kicker"), it.get("office"), it.get("place"), it.get("extra"), termino,
                datetime.datetime.now().isoformat(timespec="seconds")))


def guardar_articulo(db, a):
    """Se llama desde el hilo del servidor: SQLite exige una conexion propia por hilo."""
    db = abrir_db()
    carpeta = os.path.join(DIR, "articulos")
    os.makedirs(carpeta, exist_ok=True)
    ruta = os.path.join(carpeta, "%s_%s_%s.txt" % (a.get("site", "x"), a.get("lang") or "xx", id_item(a)))
    cab = [
        "TITULO:     " + (a.get("title") or ""),
        "SITIO:      %s (%s)" % (a.get("site"), a.get("lang") or "-"),
        "FECHA:      " + fecha_corta(a.get("date")),
        "MODIFICADO: " + fecha_corta(a.get("modified")),
        "AUTOR:      " + (a.get("author") or ""),
        "ACCESO:     " + (a.get("access") or ""),
        "PALABRAS:   %s" % a.get("words", 0),
        "URL:        " + (a.get("url") or ""),
        "RESUMEN:    " + (a.get("lead") or ""),
        "-" * 70,
    ]
    with open(ruta, "w", encoding="utf-8") as f:
        f.write("\n".join(cab) + "\n" + (a.get("text") or "") + "\n")
    db.execute("INSERT OR REPLACE INTO articulos VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
               (a.get("url"), a.get("site"), a.get("lang"), a.get("title"), a.get("date"), a.get("author"), a.get("lead"),
                a.get("text"), a.get("access"), a.get("words"), ruta, datetime.datetime.now().isoformat(timespec="seconds")))
    db.commit()
    db.close()
    return ruta


# ----------------------------------------------------------------------------- puente con la extension
class Puente:
    def __init__(self, puerto):
        self.puerto = puerto
        self.lock = threading.Lock()
        self.job = None
        self.entregado = False
        self.items = []
        self.resultado = None
        self.hecho = threading.Event()
        self.actividad = time.time()
        self.cancelar = False
        self.ultimo_contacto = 0
        self.al_item = None          # funcion opcional que se llama por cada item recibido
        self.server = None

    def iniciar(self):
        puente = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *a):
                dlog("HTTP " + (fmt % a))

            def _ok(self):
                host = (self.headers.get("Host") or "").split(":")[0]
                if host not in ("127.0.0.1", "localhost"):
                    self._send(403, {"error": "host"})
                    return False
                if self.headers.get("X-SWN") != "1":
                    self._send(403, {"error": "cabecera"})
                    return False
                return True

            def _send(self, code, obj=None):
                body = b"" if obj is None else json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                if obj is not None:
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_OPTIONS(self):
                self._send(403, {"error": "sin CORS"})

            def do_GET(self):
                if not self._ok():
                    return
                if self.path.startswith("/job"):
                    puente.ultimo_contacto = time.time()
                    with puente.lock:
                        if puente.job is not None and not puente.entregado:
                            puente.entregado = True
                            puente.actividad = time.time()
                            self._send(200, puente.job)
                            dlog("Trabajo entregado a la extension: " + json.dumps(puente.job, ensure_ascii=False))
                            return
                    self._send(204)
                else:
                    self._send(404, {"error": "ruta"})

            def do_POST(self):
                if not self._ok():
                    return
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    d = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
                except ValueError:
                    self._send(400, {"error": "json"})
                    return
                puente.actividad = time.time()
                if puente.job is None or d.get("id") != puente.job.get("id"):
                    self._send(200, {"cancel": True})
                    return
                if self.path == "/log":
                    out("  [ext] " + str(d.get("msg", "")))
                elif self.path == "/item":
                    it = d.get("item") or {}
                    puente.items.append(it)
                    if puente.al_item:
                        try:
                            puente.al_item(it)
                        except Exception as e:  # nunca romper el servidor por un fallo de formato
                            import traceback
                            dlog("al_item fallo:\n" + traceback.format_exc())
                            err("ERROR procesando un resultado de la extension: %r (detalle en el registro de diagnostico)" % e)
                elif self.path == "/diag":
                    guardar_diag(d.get("name", "pagina.html"), d.get("html", ""))
                elif self.path == "/result":
                    puente.resultado = d
                    puente.hecho.set()
                self._send(200, {"cancel": puente.cancelar})

        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", self.puerto), H)
        except OSError as e:
            raise SystemExit("ERROR: no puedo abrir el puerto %d (%s). Quiza hay otro swn.py abierto; "
                             "usa otro con --puerto y el mismo en el panel de la extension." % (self.puerto, e))
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        dlog("Servidor local en 127.0.0.1:%d" % self.puerto)

    def ejecutar(self, job, espera=60, inactividad=240):
        """Devuelve (ok, data, items, error)."""
        job = dict(job)
        job["id"] = uuid.uuid4().hex[:8]
        with self.lock:
            self.job, self.entregado, self.items, self.resultado = job, False, [], None
            self.cancelar = False
            self.hecho.clear()
            self.actividad = time.time()
        t0 = time.time()
        avisado = False
        try:
            while not self.hecho.wait(0.5):
                if not self.entregado:
                    if time.time() - t0 > espera:
                        return False, None, [], ("La extension no ha recogido el trabajo en %d s. Comprueba: Firefox abierto "
                                                 "(en primer plano o en pantalla dividida), extension swn activa, 'Escuchar a Termux' "
                                                 "marcado y el mismo puerto (%d)." % (espera, self.puerto))
                    if not avisado and time.time() - t0 > 6:
                        out("  Esperando a la extension de Firefox... (abre Firefox y deja el panel de swn a mano)")
                        avisado = True
                elif time.time() - self.actividad > inactividad:
                    return False, None, list(self.items), "La extension dejo de dar senales durante %d s." % inactividad
        except KeyboardInterrupt:
            self.cancelar = True
            err("Cancelando... (la extension parara en cuanto reciba el aviso)")
            time.sleep(3)
            raise
        finally:
            pass
        r = self.resultado or {}
        items = list(self.items)
        with self.lock:
            self.job = None
        if r.get("ok"):
            return True, r.get("data"), items, ""
        return False, None, items, r.get("error", "error desconocido")


def guardar_diag(nombre, html):
    carpeta = os.path.join(DIR, "diag")
    os.makedirs(carpeta, exist_ok=True)
    ruta = os.path.join(carpeta, "%s_%s" % (_ts, re.sub(r"[^A-Za-z0-9_.-]", "_", nombre)))
    with open(ruta, "w", encoding="utf-8") as f:
        f.write(html)
    out("  [diagnostico] pagina guardada en %s (%d KB)" % (ruta, len(html) // 1024))
    dlog("DIAG HTML guardado: " + ruta)
    return ruta


def conectar(args):
    p = Puente(args.puerto)
    p.iniciar()
    return p


def pausa_arg(args):
    try:
        a, b = [float(x) for x in args.pausa.split(",")]
        return [max(1.0, a), max(a, b)]
    except ValueError:
        raise SystemExit("ERROR: --pausa debe ser MIN,MAX en segundos (por ejemplo 2,5)")


# ----------------------------------------------------------------------------- comandos
def lista_idiomas(txt):
    res = []
    for x in [t.strip().lower() for t in txt.split(",") if t.strip()]:
        c = IDIOMAS.get(x, x)
        if c not in NOMBRES:
            raise SystemExit("ERROR: idioma no valido '%s'. Usa: %s" % (x, ", ".join("%s(%s)" % (k, v) for k, v in IDIOMAS.items())))
        if c not in res:
            res.append(c)
    return res


def lista_sitios(txt):
    t = txt.lower().strip()
    if t in ("todos", "todo", "all"):
        return list(SITIOS)
    res = []
    for x in [s.strip() for s in t.split(",") if s.strip()]:
        if x not in SITIOS:
            raise SystemExit("ERROR: sitio no valido '%s'. Usa: %s o todos" % (x, ", ".join(SITIOS)))
        res.append(x)
    return res


def pintar_item(n, it, ancho):
    sitio = it.get("site", "?") + ("/" + it["lang"] if it.get("lang") and it.get("site") != "srf" else "")
    extra = ""
    if it.get("site") == "simap":
        out("[%3d] %-10s %-16s %s" % (n, fecha_corta(it.get("date"))[:10], sitio, it.get("title", "")))
        out(envolver("Organismo: %s | %s" % (it.get("office", ""), it.get("kicker", "")), 6, ancho))
        out(envolver("Lugar: %s | %s" % (it.get("place", ""), it.get("extra", "")), 6, ancho))
    else:
        if it.get("kicker"):
            extra = " [%s]" % it["kicker"]
        out("[%3d] %-16s %-16s %s%s" % (n, fecha_corta(it.get("date")), sitio, it.get("title", ""), extra))
        if it.get("lead"):
            out(envolver(it["lead"] if len(it["lead"]) <= 240 else it["lead"][:240].rsplit(" ", 1)[0] + " ...", 6, ancho))
    out("      " + it.get("url", ""))


def cmd_estado(args):
    p = conectar(args)
    out("swn.py %s | datos en %s | puerto %d" % (VERSION, DIR, args.puerto))
    ok, data, _, e = p.ejecutar({"op": "ping"}, espera=args.espera)
    if not ok:
        err("ERROR: " + e)
        return 1
    out("Extension conectada: swn %s" % data.get("version"))
    out("Navegador: %s" % data.get("ua"))
    return 0


def cmd_idiomas(args):
    out("Idiomas de Swissinfo (alias -> codigo del sitio):")
    for k, v in IDIOMAS.items():
        out("  %-3s -> %-4s %s" % (k, v, NOMBRES[v]))
    out("SRF solo existe en aleman. simap acepta interfaz en de, fr, it, en (--ui).")
    return 0


def construir_jobs(args, q, sitios, idiomas):
    pausa = pausa_arg(args)
    per = PERIODOS.get(args.periodo) if args.periodo else None
    jobs = []
    for s in sitios:
        if s == "swissinfo":
            for lg in idiomas:
                jobs.append({"op": "buscar", "site": "swissinfo", "lang": lg, "q": q, "n": args.n, "pausa": pausa,
                             "orden": args.orden, "tipo": TIPOS.get(args.tipo, args.tipo) if args.tipo else "",
                             "tema": args.tema or "", "periodo_si": per[0] if per else ""})
        elif s == "srf":
            jobs.append({"op": "buscar", "site": "srf", "lang": "ger", "q": q, "n": args.n, "pausa": pausa,
                         "periodo_srf": per[1] if per else "", "medios": bool(args.medios)})
        elif s == "simap":
            jobs.append({"op": "buscar", "site": "simap", "lang": args.ui, "ui": args.ui, "q": q, "n": args.n, "pausa": pausa})
    return jobs


def filtrar(items, args, terms):
    res = []
    corte = None
    if args.periodo:
        corte = datetime.datetime.now() - datetime.timedelta(days=PERIODOS[args.periodo][2])
    for it in items:
        if corte and it.get("date"):
            try:
                d = datetime.datetime.fromisoformat(it["date"][:19])
                if d.replace(tzinfo=None) < corte:
                    continue
            except ValueError:
                pass
        if args.exacto:
            blob = " ".join(str(it.get(k, "")) for k in ("title", "lead", "kicker", "office", "extra"))
            if not coincide(blob, terms):
                continue
        res.append(it)
    return res


def cmd_buscar(args):
    q = args.termino.strip()
    if len(q) < 2:
        err("ERROR: el termino debe tener al menos 2 caracteres")
        return 1
    sitios = lista_sitios(args.sitios)
    idiomas = lista_idiomas(args.idiomas)
    jobs = construir_jobs(args, q, sitios, idiomas)
    if args.periodo and "simap" in sitios:
        out("Aviso: simap no tiene filtro de periodo en esta herramienta; solo se recorta por fecha en local.")
    p = conectar(args)
    out("Buscando: %s | sitios: %s | idiomas Swissinfo: %s | max %d por sitio/idioma"
        % (q, ",".join(sitios), ",".join(idiomas) if "swissinfo" in sitios else "-", args.n))
    todos, resumen, errores = [], [], 0
    for j in jobs:
        etiqueta = j["site"] + ("/" + j["lang"] if j["site"] != "srf" else "")
        out("== %s ==" % etiqueta)
        ok, data, items, e = p.ejecutar(j, espera=args.espera)
        its = [i for i in items if i.get("kind") == "item"]
        if not ok:
            errores += 1
            err("ERROR en %s: %s" % (etiqueta, e))
            if its:
                out("  (se conservan los %d resultados recibidos antes del error)" % len(its))
        else:
            tot = (data or {}).get("total")
            out("  %d resultados%s" % (len(its), (" de %s en total" % tot) if tot else ""))
        resumen.append("%s %d" % (etiqueta, len(its)))
        todos.extend(its)
    vistos, unicos = set(), []
    for it in todos:
        if it.get("url") and it["url"] not in vistos:
            vistos.add(it["url"])
            unicos.append(it)
    terms = terminos(q)
    final = filtrar(unicos, args, terms)
    if len(final) != len(unicos):
        out("Filtrado local: %d de %d resultados cumplen (--exacto / --periodo)." % (len(final), len(unicos)))
    if args.orden == "fecha" or not args.orden:
        final.sort(key=lambda i: i.get("date") or "", reverse=True)
    ancho = ancho_term(args)
    out("")
    db = abrir_db()
    for n, it in enumerate(final, 1):
        it["n"] = n
        pintar_item(n, it, ancho)
        guardar_resultado(db, it, q)
    db.commit()
    os.makedirs(DIR, exist_ok=True)
    with open(os.path.join(DIR, "ultima.json"), "w", encoding="utf-8") as f:
        json.dump({"termino": q, "items": final}, f, ensure_ascii=False)
    os.makedirs(os.path.join(DIR, "busquedas"), exist_ok=True)
    ruta = os.path.join(DIR, "busquedas", "%s_%s.txt" % (_ts, slug(q)))
    with open(ruta, "w", encoding="utf-8") as f:
        f.write("BUSQUEDA: %s\nFECHA: %s\nSITIOS: %s\n%s\n" % (q, datetime.datetime.now().isoformat(timespec="minutes"), ", ".join(resumen), "-" * 70))
        for it in final:
            f.write("[%d] %s | %s | %s\n    %s\n    %s\n" % (it["n"], fecha_corta(it.get("date")), it.get("site"), it.get("title", ""),
                                                          (it.get("lead") or it.get("office") or "")[:300], it.get("url", "")))
    out("")
    out("Total: %d resultados (%s)" % (len(final), "; ".join(resumen)))
    out("Lista guardada en: " + ruta)
    out("Para leer: swn.py leer 1 2 3   (numeros de la lista)")
    if args.leer and final:
        leibles = [i for i in final if i.get("extra") != "medio"]
        out("")
        out("Leyendo %d articulos..." % len(leibles))
        args.refs = [str(i["n"]) for i in leibles]
        args.sin_texto = not args.mostrar
        args.sin_guardar = False
        return leer_urls(p, args, [i["url"] for i in leibles], terms if args.exacto else None, db) or (1 if errores and not final else 0)
    return 1 if errores and not final else 0


def resolver_refs(refs):
    urls = []
    ultima = None
    for r in refs:
        if re.match(r"^https?://", r):
            urls.append(r)
            continue
        if not r.isdigit():
            raise SystemExit("ERROR: '%s' no es un numero de la ultima lista ni una URL" % r)
        if ultima is None:
            ruta = os.path.join(DIR, "ultima.json")
            if not os.path.exists(ruta):
                raise SystemExit("ERROR: no hay una ultima busqueda guardada (%s). Ejecuta antes 'swn.py buscar ...'." % ruta)
            with open(ruta, encoding="utf-8") as f:
                ultima = json.load(f)["items"]
        k = int(r)
        sel = [i for i in ultima if i.get("n") == k]
        if not sel:
            raise SystemExit("ERROR: el numero %d no esta en la ultima lista (1-%d)" % (k, len(ultima)))
        urls.append(sel[0]["url"])
    return urls


def leer_urls(p, args, urls, terms, db):
    ancho = ancho_term(args)
    recibidos = {"ok": 0, "err": 0, "rutas": []}

    def al_item(it):
        if it.get("kind") == "error":
            recibidos["err"] += 1
            err("ERROR leyendo %s: %s" % (it.get("url"), it.get("error")))
            return
        if it.get("kind") != "articulo":
            return
        recibidos["ok"] += 1
        ruta = ""
        if not args.sin_guardar:
            ruta = guardar_articulo(db, it)
            recibidos["rutas"].append(ruta)
        out("-" * 70)
        out("%s" % it.get("title", ""))
        out("%s (%s) | %s | %s | %s palabras | acceso: %s" % (it.get("site"), it.get("lang") or "-", fecha_corta(it.get("date")),
                                                             it.get("author") or "sin autor", it.get("words", 0), it.get("access")))
        out(it.get("url", ""))
        if terms is not None:
            out("Contiene el termino en el texto completo: %s" % ("SI" if coincide((it.get("title") or "") + " " + (it.get("text") or ""), terms) else "NO"))
        if ruta:
            out("Guardado en: " + ruta)
        if not args.sin_texto:
            out("")
            out(envolver(it.get("text") or "(sin texto extraido; mira el diagnostico)", 0, ancho))
        elif it.get("lead"):
            out(envolver("Resumen: " + it["lead"][:300], 0, ancho))

    p.al_item = al_item
    job = {"op": "leer", "urls": urls, "pausa": pausa_arg(args)}
    ok, data, items, e = p.ejecutar(job, espera=args.espera, inactividad=300)
    p.al_item = None
    out("-" * 70)
    out("Leidos: %d | errores: %d" % (recibidos["ok"], recibidos["err"]))
    if not ok:
        err("ERROR: " + e)
        return 1
    return 0


def cmd_leer(args):
    db = abrir_db()
    urls = resolver_refs(args.refs)
    p = conectar(args)
    out("Leyendo %d paginas..." % len(urls))
    terms = None
    return leer_urls(p, args, urls, terms, db)


def cmd_local(args):
    terms = terminos(args.termino)
    db = abrir_db()
    ancho = ancho_term(args)
    encontrados = []
    for r in db.execute("SELECT url,site,lang,title,date,author,lead,text,ruta FROM articulos"):
        blob = " ".join(x or "" for x in r[3:8])
        if coincide(blob, terms):
            encontrados.append(("articulo", r))
    if not args.solo_articulos:
        urls = {r[1][0] for r in encontrados}
        for r in db.execute("SELECT url,site,lang,title,date,lead,kicker,office,extra FROM resultados"):
            if r[0] in urls:
                continue
            if coincide(" ".join(x or "" for x in r[3:9]), terms):
                encontrados.append(("resultado", r))
    encontrados.sort(key=lambda x: (x[1][4] or ""), reverse=True)
    if not encontrados:
        out("Sin coincidencias en la base local (%s). Busca antes con 'swn.py buscar'." % os.path.join(DIR, "swn.db"))
        return 0
    for n, (tipo, r) in enumerate(encontrados[:args.n], 1):
        out("[%3d] %-16s %-14s %s%s" % (n, fecha_corta(r[4]), "%s/%s" % (r[1], r[2] or "-"), r[3], "  (texto guardado)" if tipo == "articulo" else ""))
        if tipo == "articulo":
            texto = r[7] or ""
            i = norm(texto).find(terms[0]) if terms else -1
            if i >= 0:
                ini = max(0, i - 80)
                out(envolver("..." + texto[ini:i + 160].replace("\n", " ") + "...", 6, ancho))
            out("      " + r[8])
        out("      " + r[0])
    out("")
    out("%d coincidencias (mostradas %d)" % (len(encontrados), min(len(encontrados), args.n)))
    return 0


def cmd_lista(args):
    ruta = os.path.join(DIR, "ultima.json")
    if not os.path.exists(ruta):
        err("ERROR: no hay una ultima busqueda guardada en " + ruta)
        return 1
    with open(ruta, encoding="utf-8") as f:
        d = json.load(f)
    out("Ultima busqueda: %s (%d resultados)" % (d["termino"], len(d["items"])))
    ancho = ancho_term(args)
    for it in d["items"]:
        pintar_item(it["n"], it, ancho)
    return 0


def cmd_volcar(args):
    p = conectar(args)
    out("Pidiendo a la extension el HTML de %s (%s)" % (args.url, "renderizado en pestana" if args.render else "descarga directa"))
    ok, data, items, e = p.ejecutar({"op": "volcar", "url": args.url, "render": bool(args.render)}, espera=args.espera)
    if not ok:
        err("ERROR: " + e)
        return 1
    out("Hecho. Envia el fichero de la carpeta %s a Claude si necesitas que ajuste la lectura de esa pagina." % os.path.join(DIR, "diag"))
    return 0


# ----------------------------------------------------------------------------- argumentos
def main(argv=None):
    comun = argparse.ArgumentParser(add_help=False)
    comun.add_argument("--puerto", type=int, default=PUERTO_DEF, help="puerto local (defecto %d)" % PUERTO_DEF)
    comun.add_argument("--espera", type=int, default=60, help="segundos esperando a que la extension recoja el trabajo")
    comun.add_argument("--pausa", default="2,5", help="pausa aleatoria MIN,MAX segundos entre paginas (defecto 2,5)")
    comun.add_argument("--ancho", type=int, help="ancho de linea para ajustar el texto")

    ap = argparse.ArgumentParser(prog="swn.py", description="Noticias de Swissinfo, SRF y licitaciones simap desde Termux",
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="Manual: swnManualBono.txt\nDatos en %s (cambiable con la variable SWN_DIR)" % DIR)
    sub = ap.add_subparsers(dest="cmd")

    s = sub.add_parser("estado", parents=[comun], help="comprueba que la extension esta conectada")
    s.set_defaults(f=cmd_estado)
    s = sub.add_parser("idiomas", parents=[comun], help="lista los idiomas de Swissinfo")
    s.set_defaults(f=cmd_idiomas)

    s = sub.add_parser("buscar", parents=[comun], help="busca un termino o \"frase\"")
    s.add_argument("termino", help='palabra(s) o "frase exacta" entre comillas')
    s.add_argument("-s", "--sitios", default="swissinfo,srf", help="swissinfo,srf,simap o todos (defecto swissinfo,srf)")
    s.add_argument("-i", "--idiomas", default="en", help="idiomas de Swissinfo: es,en,de,fr,it,pt,ar,zh,ja,ru (defecto en)")
    s.add_argument("-n", type=int, default=20, help="maximo de resultados por sitio e idioma (defecto 20)")
    s.add_argument("--orden", choices=["fecha", "relevancia"], default="fecha", help="orden en Swissinfo (defecto fecha)")
    s.add_argument("--periodo", choices=list(PERIODOS), help="hoy, 3dias, semana, mes, 6meses, anio")
    s.add_argument("--tipo", help="solo Swissinfo: articulo, ticker, opinion, video, briefing, debate, galeria, newsletter")
    s.add_argument("--tema", help="solo Swissinfo: identificador de tema (ej. swiss-ai, ai-governance)")
    s.add_argument("--medios", action="store_true", help="SRF: incluir tambien programas de TV/radio (solo ficha)")
    s.add_argument("--ui", choices=["de", "fr", "it", "en"], default="en", help="simap: idioma de la interfaz (defecto en)")
    s.add_argument("--exacto", action="store_true", help="descarta resultados cuyo titulo/resumen no contenga todos los terminos")
    s.add_argument("--leer", action="store_true", help="tras buscar, descarga y guarda cada articulo")
    s.add_argument("--mostrar", action="store_true", help="con --leer, imprime tambien el texto completo")
    s.set_defaults(f=cmd_buscar)

    s = sub.add_parser("leer", parents=[comun], help="lee articulos por numero de la ultima lista o por URL")
    s.add_argument("refs", nargs="+", help="numeros de la ultima busqueda y/o URLs")
    s.add_argument("--sin-texto", action="store_true", help="no imprimir el texto (solo guardarlo)")
    s.add_argument("--sin-guardar", action="store_true", help="no guardar el articulo en disco")
    s.set_defaults(f=cmd_leer)

    s = sub.add_parser("local", parents=[comun], help="busca en lo ya guardado (sin extension ni red)")
    s.add_argument("termino")
    s.add_argument("-n", type=int, default=30)
    s.add_argument("--solo-articulos", action="store_true", help="solo articulos con texto guardado")
    s.set_defaults(f=cmd_local)

    s = sub.add_parser("lista", parents=[comun], help="vuelve a mostrar la ultima lista")
    s.set_defaults(f=cmd_lista)

    s = sub.add_parser("volcar", parents=[comun], help="guarda el HTML de una pagina (para diagnostico)")
    s.add_argument("url")
    s.add_argument("--render", action="store_true", help="HTML ya pintado por JavaScript (abre una pestana)")
    s.set_defaults(f=cmd_volcar)

    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 0
    dlog("inicio comando " + args.cmd)
    try:
        rc = args.f(args)
    except KeyboardInterrupt:
        err("Interrumpido.")
        rc = 130
    except SystemExit as e:
        if e.code not in (None, 0):
            err(str(e.code))
        rc = e.code if isinstance(e.code, int) else 1
    except Exception as e:  # ultimo recurso: dejar rastro completo en el diagnostico
        import traceback
        dlog("EXCEPCION\n" + traceback.format_exc())
        err("ERROR inesperado: %r. Detalle en %s" % (e, _diag_ruta))
        rc = 1
    if _diag_ruta and rc not in (0, None):
        err("Registro de diagnostico: " + _diag_ruta)
    return rc or 0


if __name__ == "__main__":
    sys.exit(main())
