#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
netz.py - Buscar, leer y guardar en local articulos de Netzwoche (aleman) e
ICTjournal (frances) desde Termux. Solo biblioteca estandar.

Ejemplos:
  python3 netz.py buscar "cybersecurity" --sitio netzwoche --max 20
  python3 netz.py buscar "intelligence artificielle" --sitio ictjournal --leer
  python3 netz.py listar news --sitio ictjournal
  python3 netz.py leer https://www.netzwoche.ch/news/2026-10-01/ejemplo
  python3 netz.py local "KI"
  python3 netz.py servidor          (puente con la extension netz.xpi)

La salida normal va a stdout; solo los errores van a stderr.
Todo lo encontrado se guarda en ~/netz/netz.db y en archivos .txt.
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
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import error, parse, request

VERSION = "1.0.0"
PUERTO_DEFECTO = 8776
ANCHO = 121
TIMEOUT = 40
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 "
              "Firefox/130.0")
DIR = os.environ.get("NETZ_DIR") or os.path.join(os.path.expanduser("~"), "netz")
DIR_CACHE = os.path.join(DIR, "cache")
SELLO = time.strftime("%Y-%m-%d-%H-%M-%S")
DIR_LOGS = os.path.join(DIR, "logs")
DIR_RESP = os.path.join(DIR, "respuestas")


# ---------------------------------------------------------------------------
# Infraestructura (comun con empsui): registro, red, puente con la extension, informe
# ---------------------------------------------------------------------------

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
                self.ruta = os.path.join(DIR_LOGS, SELLO + "logNetzPy.log")
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


PAGINA_ABRIR = ("<!doctype html><meta charset=utf-8><meta name=\"netz-despertar\" content=\"1\">"
                "<title>netz</title><p>netz: despertando la extension. Puedes cerrar esta pestana.</p>")


def crear_handler(estado):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _cors(self):
            origen = self.headers.get("Origin", "")
            if origen.startswith("moz-extension://"):
                self.send_header("Access-Control-Allow-Origin", origen)
                self.send_header("Access-Control-Allow-Headers", "X-Netz, Content-Type")
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
            if self.headers.get("X-Netz", "") != "1":
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
                    return self._resp(200, {"netz": True, "sondeo_hace": hace, "ext_url": estado.ext_url,
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
                if not re.fullmatch(r"[0-9]{4}(-[0-9]{2}){5}logNetz\.log", nombre):
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
    log("La extension netz se conecta sola si Firefox esta abierto. Deja esto abierto. Ctrl+C para parar.")
    log("(No es obligatorio: con --via-extension, netz.py arranca su propio servidor y despierta la extension.)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("Parado.")
    return 0


def _base(puerto):
    return f"http://127.0.0.1:{puerto}"


def _cab():
    return {"X-Netz": "1", "Content-Type": "application/json"}


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
    forzado = os.environ.get("NETZ_FIREFOX")
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
    if os.environ.get("NETZ_NO_ABRIR") or not shutil.which("am"):
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
                err("  Firefox abrio la pagina de despertar, pero la extension netz no respondio: esta instalada y activa?")
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
        if est.get("netz"):
            DIAG.puente["servidor"] = "reutilizado"
            log(f"Puente: reutilizo el servidor netz que ya esta activo en el puerto {puerto}.")
        else:
            DIAG.puente["servidor"] = "antiguo (sin soporte de despertar)"
            raise RuntimeError(f"En el puerto {puerto} hay un servidor netz ANTIGUO (por ejemplo 'netz.py "
                               f"servidor' lanzado con la version 1.0.0) que no sabe despertar la extension ni "
                               f"guardar diagnostico. Cierralo (Ctrl+C en su sesion de Termux, o 'pkill -f "
                               f"netz.py') y vuelve a lanzar el comando.")
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
        raise RuntimeError("La extension no responde. Abre Firefox y comprueba que netz esta instalada y que "
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


def entorno():
    e = [f"netz.py {VERSION}", f"Python {platform.python_version()} en {platform.system()} {platform.machine()}",
         f"Termux: {'si' if os.environ.get('PREFIX', '').startswith('/data/data/com.termux') else 'no detectado'}",
         f"Carpeta de trabajo: {os.getcwd()}", f"Carpeta netz: {DIR}",
         f"Comando 'am': {shutil.which('am') or 'no'}; 'pm': {shutil.which('pm') or 'no'}"]
    for v in ("NETZ_DIR", "NETZ_FIREFOX", "NETZ_NO_ABRIR"):
        if os.environ.get(v):
            e.append(f"{v}={os.environ[v]}")
    return e


def sugerencias():
    sug = []
    clases = [p["clase"] for p in DIAG.peticiones]
    vias = {p["via"] for p in DIAG.peticiones}
    if any(c.startswith("bloqueo") for c in clases):
        sug.append("Una web mostro una verificacion anti-bot: abre esa web en Firefox, resuelvela, y repite con "
                   "--via-extension.")
    if any(c in ("http-403", "http-429") for c in clases) and "directo" in vias:
        sug.append("La web rechaza las peticiones directas desde Termux (403/429): repite con --via-extension.")
    if any("no dio articulos reconocibles" in a or "sin articulos" in a for a in DIAG.avisos):
        sug.append("La web cambio su maquetacion o mostro otra cosa: envia este informe (la pagina esta en la carpeta "
                   "respuestas).")
    if any("contenido de pago" in a for a in DIAG.avisos):
        sug.append("Hay articulos de pago: inicia sesion en netzwoche.ch / ictjournal.ch en el Firefox de la extension y "
                   "repite con --via-extension.")
    if DIAG.puente.get("extension") == "sin respuesta":
        if DIAG.puente.get("abrir_visto"):
            sug.append("Firefox abrio la pagina de despertar pero la extension no contesto: comprueba que netz.xpi esta "
                       "instalada y habilitada.")
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
    ruta = os.path.join(destino, f"{SELLO}diagNetz.txt")
    L = ["=" * 121, f"INFORME DE DIAGNOSTICO DE NETZ {VERSION}", f"Generado: {time.strftime('%Y-%m-%d %H:%M:%S')}", "=" * 121, ""]
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
    L.append("7. REGISTRO DE ESTA EJECUCION (netz.py)")
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


# ---------------------------------------------------------------------------
# Sitios: Netzwoche (alemán) e ICTjournal (francés) comparten el mismo CMS (Drupal "np8")
# ---------------------------------------------------------------------------

SITIOS = {
    "netzwoche": {"base": "https://www.netzwoche.ch", "nombre": "Netzwoche", "idioma": "de"},
    "ictjournal": {"base": "https://www.ictjournal.ch", "nombre": "ICTjournal", "idioma": "fr"},
}
# Ressorts de Netzwoche vistos en su formulario de busqueda (los de ICTjournal se leen de su web: 'secciones').
SECCIONES_FIJAS = {"netzwoche": {"cybersecurity": "3300", "elektro": "3302", "interviews": "3295", "koepfe": "3322",
                                 "news": "31", "storys": "40", "meinungen": "41", "studien": "42",
                                 "hands-on": "43", "netzwitzig": "44"}}
RUTA_DB = os.path.join(DIR, "netz.db")
VOID = {"br", "img", "input", "meta", "link", "hr", "source", "wbr", "area", "base", "col", "embed", "param", "track"}
PALABRAS_MURO = re.compile(
    r"(nur f[uü]r abonnent|jetzt (?:einloggen|anmelden|abonnieren)|plus-artikel|weiterlesen mit|zugang zu diesem artikel|"
    r"r[eé]serv[eé] aux abonn[eé]s|abonnez-vous|cet article est r[eé]serv[eé]|se connecter pour lire|"
    r"login erforderlich|mit abo weiterlesen)", re.I)


# ---------------------------------------------------------------------------
# Mini DOM (solo biblioteca estandar)
# ---------------------------------------------------------------------------

class Nodo:
    __slots__ = ("tag", "attrs", "hijos", "padre")

    def __init__(self, tag, attrs=None, padre=None):
        self.tag, self.attrs, self.hijos, self.padre = tag, dict(attrs or {}), [], padre

    def clases(self):
        return (self.attrs.get("class") or "").split()

    def tiene_clase(self, parte):
        return any(parte in c for c in self.clases())

    def texto(self):
        partes = []

        def rec(n):
            for h in n.hijos:
                if isinstance(h, str):
                    partes.append(h)
                elif h.tag in ("script", "style"):
                    continue
                elif h.tag == "br":
                    partes.append("\n")
                else:
                    rec(h)
        rec(self)
        return re.sub(r"[ \t\r\f\v ]+", " ", re.sub(r"\s*\n\s*", "\n", "".join(partes))).strip()

    def descendientes(self):
        for h in self.hijos:
            if not isinstance(h, str):
                yield h
                yield from h.descendientes()

    def busca(self, pred):
        return [n for n in self.descendientes() if pred(n)]

    def primero(self, pred):
        for n in self.descendientes():
            if pred(n):
                return n
        return None

    def ancestro(self, pred):
        p = self.padre
        while p is not None:
            if pred(p):
                return True
            p = p.padre
        return False


class _ConstruyeDOM(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.raiz = Nodo("#raiz")
        self.actual = self.raiz

    def handle_starttag(self, tag, attrs):
        if tag in ("p", "option", "li") and self.actual.tag == tag:
            self.actual = self.actual.padre
        n = Nodo(tag, {k: (v or "") for k, v in attrs}, self.actual)
        self.actual.hijos.append(n)
        if tag not in VOID:
            self.actual = n

    def handle_startendtag(self, tag, attrs):
        n = Nodo(tag, {k: (v or "") for k, v in attrs}, self.actual)
        self.actual.hijos.append(n)

    def handle_endtag(self, tag):
        p = self.actual
        while p is not None and p.tag != tag:
            p = p.padre
        if p is not None and p.padre is not None:
            self.actual = p.padre

    def handle_data(self, data):
        if data:
            self.actual.hijos.append(data)


def parsea_dom(txt):
    txt = re.sub(r"<!--.*?-->", "", txt, flags=re.S)
    txt = re.sub(r"(<(?:script|style)\b[^>]*>).*?(</(?:script|style)>)", r"\1\2", txt, flags=re.S | re.I)
    b = _ConstruyeDOM()
    b.feed(txt)
    return b.raiz


# ---------------------------------------------------------------------------
# Listados (busqueda, portada, secciones)
# ---------------------------------------------------------------------------

def articulo(sitio, url, titulo, **kw):
    a = {"sitio": sitio, "url": url, "titulo": (titulo or "").strip(), "antetitulo": "", "fecha": "", "autor": "",
         "seccion": "", "tipo": "", "texto": "", "completa": False, "acceso": "", "patrocinado": False}
    a.update(kw)
    return a


def seccion_de_url(url):
    p = parse.urlparse(url).path.strip("/").split("/")
    return p[0] if p and p[0] else ""


def parsea_teasers(txt, sitio):
    base = SITIOS[sitio]["base"]
    raiz = parsea_dom(txt)
    res, vistos = [], set()
    for ar in raiz.busca(lambda n: n.tag == "article" and n.tiene_clase("node")):
        a = ar.primero(lambda n: n.tag == "a" and n.attrs.get("href"))
        if a is None:
            continue
        url = limpia_url(parse.urljoin(base, a.attrs["href"]))
        if url in vistos:
            continue
        t = ar.primero(lambda n: n.tiene_clase("node__title")) or ar.primero(lambda n: n.tag in ("h2", "h3", "h4"))
        titulo = t.texto() if t is not None else a.texto()
        if not titulo:
            continue
        ante = ar.primero(lambda n: n.tiene_clase("np8-dateline"))
        tm = ar.primero(lambda n: n.tag == "time")
        fecha = ""
        if tm is not None:
            fecha = (tm.attrs.get("datetime") or tm.texto())[:10]
            if re.match(r"\d{2}\.\d{2}\.\d{4}", fecha):
                fecha = f"{fecha[6:10]}-{fecha[3:5]}-{fecha[0:2]}"
        cl = " ".join(ar.clases())
        tipo = (re.search(r"node--type-([\w-]+)", cl) or [None, ""])[1].replace("np8-", "")
        vistos.add(url)
        res.append(articulo(sitio, url, titulo, antetitulo=ante.texto() if ante is not None else "",
                            fecha=fecha, tipo=tipo, seccion=seccion_de_url(url), patrocinado="sponsored" in cl))
    return res


def parsea_secciones(txt):
    raiz = parsea_dom(txt)
    sel = raiz.primero(lambda n: n.tag == "select" and n.attrs.get("name") == "entity_node_np8_section")
    if sel is None:
        return {}
    return {o.texto(): o.attrs.get("value", "") for o in sel.busca(lambda n: n.tag == "option")
            if o.attrs.get("value") not in (None, "", "All")}


def siguiente_pagina(txt):
    return bool(re.search(r'rel=["\']?next\b', txt)) and bool(re.search(r'[?&](?:amp;)?page=\d+', txt))


def resuelve_seccion(c, sitio, valor):
    """Nombre de ressort -> id (usa los fijos de Netzwoche o los lee de la web)."""
    if not valor:
        return "All"
    v = normaliza(valor).replace(" ", "-")
    if valor.isdigit():
        return valor
    fijas = SECCIONES_FIJAS.get(sitio, {})
    if v in fijas:
        return fijas[v]
    secs = parsea_secciones(c.texto(SITIOS[sitio]["base"] + "/search"))
    for nombre, idv in secs.items():
        if normaliza(nombre).replace(" ", "-") == v:
            return idv
    raise RuntimeError(f"Seccion '{valor}' desconocida en {sitio}. Disponibles: "
                       f"{', '.join(sorted(secs) or sorted(fijas))} (comando 'secciones').")


def url_busqueda(sitio, terminos, seccion="All", autor="", desde="", hasta="", pagina=0):
    p = [("keywords", " ".join(terminos)), ("entity_node_np8_section", seccion), ("author", autor),
         ("entity_node_np8_publication_date[from]", desde), ("entity_node_np8_publication_date[until]", hasta)]
    if pagina:
        p.append(("page", str(pagina)))
    return SITIOS[sitio]["base"] + "/search?" + parse.urlencode(p)


def lista_paginas(c, url_fn, maximo, vacio_aviso=""):
    """Recorre paginas (page=0,1,2...) hasta tener 'maximo' articulos o no haber mas."""
    res, vistos, pagina = [], set(), 0
    while len(res) < maximo and pagina < 60:
        url = url_fn(pagina)
        txt = c.texto(url)
        sitio = url_fn.sitio
        nuevos = [a for a in parsea_teasers(txt, sitio) if a["url"] not in vistos]
        if not nuevos:
            if pagina == 0 and vacio_aviso:
                DIAG.aviso(vacio_aviso + f" Pagina guardada en {DIR_RESP}.")
                DIAG.guarda_respuesta(url, 200, "extension" if c.via else "directo", txt, "netz-sin-articulos")
            break
        for a in nuevos:
            vistos.add(a["url"])
            res.append(a)
        if not siguiente_pagina(txt):
            break
        pagina += 1
    return res[:maximo]


class _UrlFn:
    def __init__(self, sitio, fn):
        self.sitio, self.fn = sitio, fn

    def __call__(self, pagina):
        return self.fn(pagina)


def buscar_sitio(c, sitio, a):
    sec = resuelve_seccion(c, sitio, getattr(a, "seccion", None))
    f = _UrlFn(sitio, lambda pg: url_busqueda(sitio, a.terminos, sec, getattr(a, "autor", "") or "",
                                              getattr(a, "desde", "") or "", getattr(a, "hasta", "") or "", pg))
    if getattr(a, "mostrar_url", False):
        log(f"[{sitio}] URL: {f(0)}")
    res = lista_paginas(c, f, a.max, f"{sitio}: la busqueda no dio articulos reconocibles.")
    return res


def listar_ruta(c, sitio, ruta, maximo):
    base = SITIOS[sitio]["base"]
    ruta = ruta.strip()
    url0 = ruta if ruta.startswith("http") else base + "/" + ruta.lstrip("/")
    f = _UrlFn(sitio, lambda pg: url0 if not pg else url0 + ("&" if "?" in url0 else "?") + f"page={pg}")
    return lista_paginas(c, f, maximo, f"{sitio}: la pagina {url0} no dio articulos reconocibles.")


# ---------------------------------------------------------------------------
# Articulo completo
# ---------------------------------------------------------------------------

def _meta(raiz, clave):
    m = raiz.primero(lambda n: n.tag == "meta" and (n.attrs.get("property") == clave or n.attrs.get("name") == clave))
    return (m.attrs.get("content") or "").strip() if m is not None else ""


def _jsonld(txt):
    res = []
    for m in re.finditer(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', txt, re.S | re.I):
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
                if t in ("NewsArticle", "Article", "BlogPosting", "ReportageNewsArticle") or \
                        (isinstance(t, list) and any(k in t for k in ("NewsArticle", "Article"))):
                    res.append(x)
    return res


_SALTAR = re.compile(r"(^|[-_ ])(ad|ads|adnz|ad-swap|teaser|related|social|share|newsletter|comment|banner|promo|"
                     r"breadcrumb|pager|tags?|menu|sidebar|audienzz|adition)([-_ ]|$)", re.I)


def _saltar(n):
    if n.tag in ("nav", "aside", "footer", "header", "form", "figure", "script", "style", "noscript", "button", "svg"):
        return True
    return any(_SALTAR.search(c) for c in n.clases()) or bool(_SALTAR.search(n.attrs.get("id", "")))


def _bloques(nodo):
    out_ = []

    def rec(n):
        if _saltar(n):
            return
        if n.tag in ("p", "h2", "h3", "h4", "blockquote"):
            t = n.texto()
            if t:
                out_.append(("== " + t + " ==") if n.tag in ("h2", "h3", "h4") else t)
            return
        if n.tag == "li":
            t = n.texto()
            if t:
                out_.append("- " + t)
            return
        if n.tag == "div" and (n.tiene_clase("lead") or n.tiene_clase("subtitle") or n.tiene_clase("np8-teaser")) \
                and not n.primero(lambda x: x.tag in ("p", "li", "h2", "h3")):
            t = n.texto()
            if t:
                out_.append(t)
            return
        for h in n.hijos:
            if not isinstance(h, str):
                rec(h)
    rec(nodo)
    # quita duplicados consecutivos
    res = []
    for b in out_:
        if not res or res[-1] != b:
            res.append(b)
    return res


def parsea_articulo(txt, url, sitio):
    raiz = parsea_dom(txt)
    ld = (_jsonld(txt) or [{}])[0]
    titulo = ld.get("headline") or ""
    h1 = raiz.primero(lambda n: n.tag == "h1")
    if h1 is not None and h1.texto():
        titulo = h1.texto()
    titulo = titulo or _meta(raiz, "og:title")
    fecha = (ld.get("datePublished") or _meta(raiz, "article:published_time") or "")[:10]
    tm = raiz.primero(lambda n: n.tag == "time" and n.attrs.get("datetime"))
    if not fecha and tm is not None:
        fecha = tm.attrs["datetime"][:10]
    autor = ""
    au = ld.get("author")
    if isinstance(au, list) and au:
        au = au[0]
    if isinstance(au, dict):
        autor = au.get("name", "")
    elif isinstance(au, str):
        autor = au
    if not autor:
        autor = _meta(raiz, "author")
    if not autor:
        n = raiz.primero(lambda x: x.tiene_clase("author") or x.tiene_clase("byline") or x.attrs.get("rel") == "author")
        autor = n.texto()[:120] if n is not None else ""
    ante = raiz.primero(lambda n: n.tiene_clase("np8-dateline"))
    # cuerpo: el articulo en vista completa, si no <main>, si no todo el documento
    cont = raiz.primero(lambda n: n.tag == "article" and n.tiene_clase("node--view-mode-full")) or \
        raiz.primero(lambda n: n.tag == "article" and not n.tiene_clase("node--view-mode-teaser-standard")) or \
        raiz.primero(lambda n: n.tag == "main") or raiz
    bloques = _bloques(cont)
    cuerpo = "\n\n".join(bloques)
    if len(cuerpo) < 300 and ld.get("articleBody"):
        cuerpo = html_a_texto(ld["articleBody"])
    visible = re.sub(r"\s+", " ", raiz.texto())
    muro = bool(PALABRAS_MURO.search(visible))
    acceso = "completo"
    if muro and len(cuerpo) < 2500:
        acceso = "parcial (contenido de pago: inicia sesion en Firefox y usa --via-extension)"
    elif len(cuerpo) < 200:
        acceso = "no se reconocio el texto"
    return articulo(sitio, url, titulo, antetitulo=ante.texto() if ante is not None else "", fecha=fecha, autor=autor,
                    seccion=seccion_de_url(url), texto=cuerpo, completa=len(cuerpo) >= 200, acceso=acceso)


def completa_articulo(c, a):
    txt = c.texto(a["url"])
    d = parsea_articulo(txt, a["url"], a["sitio"])
    for k in ("titulo", "antetitulo", "fecha", "autor", "texto", "completa", "acceso"):
        if d.get(k):
            a[k] = d[k]
    if d["acceso"] != "completo":
        DIAG.aviso(f"{a['url']}: acceso {d['acceso']}.")
        if not d["completa"]:
            DIAG.guarda_respuesta(a["url"], 200, "extension" if c.via else "directo", txt, "netz-articulo-sin-texto")
    return a


def sitio_de_url(url):
    h = parse.urlparse(url).netloc.lower()
    for k, v in SITIOS.items():
        if parse.urlparse(v["base"]).netloc.replace("www.", "") in h:
            return k
    return None


# ---------------------------------------------------------------------------
# Base local y salida
# ---------------------------------------------------------------------------

def abre_db():
    asegura_dirs()
    db = sqlite3.connect(RUTA_DB)
    db.execute("""CREATE TABLE IF NOT EXISTS articulos (url TEXT PRIMARY KEY, sitio TEXT, titulo TEXT, antetitulo TEXT,
                  fecha TEXT, autor TEXT, seccion TEXT, tipo TEXT, texto TEXT, completa INTEGER, acceso TEXT,
                  guardado TEXT)""")
    return db


def guarda(db, arts):
    nuevos = 0
    for a in arts:
        ant = db.execute("SELECT completa FROM articulos WHERE url=?", (a["url"],)).fetchone()
        if ant is None:
            nuevos += 1
        elif ant[0] and not a["completa"]:
            continue
        db.execute("INSERT OR REPLACE INTO articulos VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (a["url"], a["sitio"], a["titulo"], a["antetitulo"], a["fecha"], a["autor"], a["seccion"], a["tipo"],
                    a["texto"], 1 if a["completa"] else 0, a["acceso"], ahora()))
    db.commit()
    return nuevos


def fila_a_art(f):
    k = ["url", "sitio", "titulo", "antetitulo", "fecha", "autor", "seccion", "tipo", "texto", "completa", "acceso"]
    d = dict(zip(k, f))
    d["completa"] = bool(d["completa"])
    d["patrocinado"] = False
    return d


def envolver(txt, sangria=""):
    res = []
    for linea in (txt or "").split("\n"):
        res += textwrap.wrap(linea, ANCHO, initial_indent=sangria, subsequent_indent=sangria,
                             break_long_words=True, break_on_hyphens=False) or [""]
    return res


def bloque_art(a, n=None):
    L = ["-" * ANCHO]
    L += envolver(f"{n}. {a['titulo']}" if n else a["titulo"])
    if a.get("antetitulo"):
        L += envolver(a["antetitulo"], "  ")
    L.append(f"  Sitio: {SITIOS.get(a['sitio'], {}).get('nombre', a['sitio'])}   Fecha: {a.get('fecha') or '-'}"
             f"   Seccion: {a.get('seccion') or '-'}" + (f"   Autor: {a['autor']}" if a.get("autor") else ""))
    L += envolver("Enlace: " + a["url"], "  ")
    if a.get("acceso") and a["acceso"] != "completo":
        L += envolver("Acceso: " + a["acceso"], "  ")
    if a.get("texto"):
        L.append("")
        L += envolver(a["texto"], "  ")
    L.append("")
    return L


def escribe_txt(ruta, titulo, arts):
    L = [titulo, f"Generado: {time.strftime('%Y-%m-%d %H:%M')}   Articulos: {len(arts)}", "=" * ANCHO, ""]
    for i, a in enumerate(arts, 1):
        L += bloque_art(a, i)
    with open(ruta, "w", encoding="utf-8") as f:
        f.write("\n".join(x for l in L for x in envuelve(l, ANCHO)) + "\n")


def resumen_linea(a, n):
    s = f"{n:>3}. {a.get('fecha') or '----------'} {SITIOS.get(a['sitio'], {}).get('nombre', a['sitio'])[:10]:<10} {a['titulo']}"
    return s if len(s) <= ANCHO else s[:ANCHO - 3] + "..."


# ---------------------------------------------------------------------------
# Comandos
# ---------------------------------------------------------------------------

def sitios_de(a):
    return list(SITIOS) if getattr(a, "sitio", "ambos") == "ambos" else [a.sitio]


def _guarda_y_muestra(a, arts, nombre):
    if not arts:
        out("Sin resultados.")
        return 0
    db = abre_db()
    nuevos = guarda(db, arts)
    log(f"Guardados en la base local ({nuevos} nuevos).")
    asegura_dirs()
    ruta = a.salida or os.path.join(DIR, f"netz_{nombre}_{ahora()}.txt")
    escribe_txt(ruta, f"NETZ - {nombre}", arts)
    out("")
    for i, x in enumerate(arts[:a.mostrar], 1):
        out(resumen_linea(x, i))
    if len(arts) > a.mostrar:
        out(f"... y {len(arts) - a.mostrar} mas (estan en el archivo).")
    out(f"\nArchivo: {ruta}")
    out(f"Base local: {RUTA_DB}")
    return 0


def cmd_buscar(a):
    if not a.terminos and not (a.seccion or a.autor or a.desde or a.hasta):
        raise RuntimeError("Indica palabras a buscar o algun filtro (--seccion, --autor, --desde, --hasta).")
    c = Cliente(a.via_extension, a.puerto)
    todos = []
    for s in sitios_de(a):
        log(f"[{s}] buscando: {' '.join(a.terminos) or '(todo)'}")
        try:
            lista = buscar_sitio(c, s, a)
        except Exception as e:
            err(f"[{s}] error: {e}")
            continue
        log(f"[{s}] {len(lista)} articulo(s)")
        if a.leer:
            for i, x in enumerate(lista, 1):
                try:
                    completa_articulo(c, x)
                except Exception as e:
                    err(f"[{s}] no se pudo leer {x['url']}: {e}")
                if a.pausa and i < len(lista):
                    time.sleep(a.pausa)
        todos += lista
    return _guarda_y_muestra(a, todos, "_".join([a.sitio, nombre_archivo(" ".join(a.terminos) or "todo")]))


def cmd_listar(a):
    c = Cliente(a.via_extension, a.puerto)
    todos = []
    for s in sitios_de(a):
        try:
            lista = listar_ruta(c, s, a.ruta or "", a.max)
        except Exception as e:
            err(f"[{s}] error: {e}")
            continue
        log(f"[{s}] {len(lista)} articulo(s) en '{a.ruta or 'portada'}'")
        if a.leer:
            for x in lista:
                try:
                    completa_articulo(c, x)
                except Exception as e:
                    err(f"[{s}] no se pudo leer {x['url']}: {e}")
                time.sleep(a.pausa or 0)
        todos += lista
    return _guarda_y_muestra(a, todos, "_".join(["lista", a.sitio, nombre_archivo(a.ruta or "portada")]))


def cmd_leer(a):
    c = Cliente(a.via_extension, a.puerto)
    arts = []
    for u in a.urls:
        s = sitio_de_url(u) if u.startswith("http") else a.sitio if a.sitio != "ambos" else None
        if not s:
            raise RuntimeError(f"No reconozco el sitio de '{u}': usa un enlace de netzwoche.ch o ictjournal.ch, "
                               f"o indica --sitio con una ruta.")
        url = u if u.startswith("http") else SITIOS[s]["base"] + "/" + u.lstrip("/")
        d = articulo(s, url, "")
        completa_articulo(c, d)
        arts.append(d)
    if not arts:
        return 1
    db = abre_db()
    guarda(db, arts)
    asegura_dirs()
    ruta = a.salida or os.path.join(DIR, f"netz_{nombre_archivo(arts[0]['titulo'] or 'articulo')}_{ahora()}.txt")
    escribe_txt(ruta, "NETZ - lectura de articulos", arts)
    for x in arts:
        out(f"{x['fecha'] or '----------'} {x['titulo']}  [{len(x['texto'])} caracteres, acceso: {x['acceso']}]")
    out(f"\nArchivo: {ruta}")
    return 0


def cmd_secciones(a):
    c = Cliente(a.via_extension, a.puerto)
    for s in sitios_de(a):
        try:
            secs = parsea_secciones(c.texto(SITIOS[s]["base"] + "/search"))
        except Exception as e:
            err(f"[{s}] error: {e}")
            secs = {}
        secs = secs or {k: v for k, v in SECCIONES_FIJAS.get(s, {}).items()}
        out(f"== {SITIOS[s]['nombre']}")
        for nombre, idv in secs.items():
            out(f"  {idv:>6}  {nombre}")
    return 0


def cmd_local(a):
    db = abre_db()
    q, par = "SELECT url, sitio, titulo, antetitulo, fecha, autor, seccion, tipo, texto, completa, acceso FROM articulos WHERE 1=1", []
    for t in a.terminos:
        q += " AND (titulo LIKE ? OR texto LIKE ? OR antetitulo LIKE ?)"
        par += [f"%{t}%"] * 3
    if a.sitio != "ambos":
        q += " AND sitio=?"
        par.append(a.sitio)
    if a.desde:
        q += " AND fecha>=?"
        par.append(a.desde)
    q += " ORDER BY fecha DESC LIMIT ?"
    par.append(a.mostrar)
    filas = [fila_a_art(f) for f in db.execute(q, par)]
    if not filas:
        out("Nada en la base local.")
        return 0
    for i, x in enumerate(filas, 1):
        out(resumen_linea(x, i))
    if a.exportar:
        ruta = a.salida or os.path.join(DIR, f"netz_local_{ahora()}.txt")
        escribe_txt(ruta, "NETZ - base local", filas)
        out(f"\nArchivo: {ruta}")
    return 0


def cmd_ver(a):
    db = abre_db()
    f = db.execute("SELECT url, sitio, titulo, antetitulo, fecha, autor, seccion, tipo, texto, completa, acceso "
                   "FROM articulos WHERE url LIKE ? LIMIT 1", (f"%{a.id}%",)).fetchone()
    if not f:
        raise RuntimeError("No encuentro ese articulo en la base local.")
    for l in bloque_art(fila_a_art(f)):
        out(l)
    return 0


def cmd_estado(a):
    db = abre_db()
    out(f"netz {VERSION}   carpeta: {DIR}")
    for s, n, c in db.execute("SELECT sitio, COUNT(*), SUM(completa) FROM articulos GROUP BY sitio"):
        out(f"  {SITIOS.get(s, {}).get('nombre', s):12} {n:6} articulos ({c or 0} con texto completo)")
    try:
        r = pedir_estado(a.puerto)
        out("Puente: servidor activo; extension "
            + ("conectada" if r.get("sondeo_hace") is not None and r["sondeo_hace"] < 30 else "sin senales"))
    except Exception:
        out("Puente: no hay servidor activo (netz.py lo arranca solo al usar --via-extension)")
    return 0


def cmd_diagnostico(a):
    """Prueba busqueda y portada de cada sitio (directo y por la extension) y escribe el informe."""
    out(f"Diagnostico de netz {VERSION}")
    for linea in entorno():
        out("  " + linea)
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
        for s in SITIOS:
            for prueba in ("busqueda", "portada", "articulo"):
                try:
                    if prueba == "busqueda":
                        n = len(lista_paginas(c, _UrlFn(s, lambda pg, s=s: url_busqueda(s, ["ki"], pagina=pg)), 3,
                                              f"{s}: busqueda sin articulos."))
                    elif prueba == "portada":
                        n = len(listar_ruta(c, s, "", 3))
                    else:
                        lst = listar_ruta(c, s, "", 1)
                        d = completa_articulo(c, lst[0]) if lst else None
                        n = len(d["texto"]) if d else 0
                    out(f"  {'OK   ' if n else 'FALLA'} {s:<11} {prueba:<9} {n}")
                    fallos += 0 if n else 1
                except Exception as e:
                    err(f"  FALLA {s:<11} {prueba:<9} {str(e)[:160]}")
                    fallos += 1
    DIAG.resultado = f"diagnostico: {fallos} prueba(s) fallida(s)" if fallos else "diagnostico: todo bien"
    out(f"Resultado: {DIAG.resultado}.")
    return 0 if not fallos else 1


def main(argv=None):
    p = argparse.ArgumentParser(prog="netz.py", description="netz.py - Buscar, leer y guardar en local articulos de "
                                "Netzwoche (DE) e ICTjournal (FR) desde Termux. Solo biblioteca estandar.")
    comun = argparse.ArgumentParser(add_help=False)
    comun.add_argument("--via-extension", action="store_true", dest="via_extension",
                       help="Hacer las peticiones desde Firefox con tu sesion (articulos de pago)")
    comun.add_argument("--puerto", type=int, default=PUERTO_DEFECTO)
    comun.add_argument("--informe", action="store_true", help="Escribir siempre el informe de diagnostico")
    comun.add_argument("--sin-informe", action="store_true", dest="sin_informe", help="No escribir informe aunque haya errores")
    comun.add_argument("-o", "--salida", help="Ruta del archivo .txt de salida")
    comun.add_argument("--sitio", choices=list(SITIOS) + ["ambos"], default="ambos")
    p.add_argument("--version", action="version", version=f"netz {VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("buscar", parents=[comun], help="Buscar articulos")
    b.add_argument("terminos", nargs="*")
    b.add_argument("--seccion", help="Ressort/rubrique (nombre o numero; mira 'secciones')")
    b.add_argument("--autor")
    b.add_argument("--desde", help="Fecha inicial AAAA-MM-DD")
    b.add_argument("--hasta", help="Fecha final AAAA-MM-DD")
    b.add_argument("--max", type=int, default=24)
    b.add_argument("--leer", action="store_true", help="Leer tambien el texto completo de cada articulo")
    b.add_argument("--pausa", type=float, default=1.0)
    b.add_argument("--mostrar", type=int, default=25)
    b.add_argument("--mostrar-url", action="store_true", dest="mostrar_url")
    b.set_defaults(func=cmd_buscar)

    li = sub.add_parser("listar", parents=[comun], help="Listar los articulos de la portada o de una seccion (ruta)")
    li.add_argument("ruta", nargs="?", default="", help="p. ej. news, dossiers, hands-on, interviews, articles")
    li.add_argument("--max", type=int, default=24)
    li.add_argument("--leer", action="store_true")
    li.add_argument("--pausa", type=float, default=1.0)
    li.add_argument("--mostrar", type=int, default=25)
    li.set_defaults(func=cmd_listar)

    le = sub.add_parser("leer", parents=[comun], help="Leer uno o varios articulos por enlace")
    le.add_argument("urls", nargs="+")
    le.set_defaults(func=cmd_leer)

    se = sub.add_parser("secciones", parents=[comun], help="Listar los ressorts de busqueda")
    se.set_defaults(func=cmd_secciones)

    lo = sub.add_parser("local", parents=[comun], help="Buscar en lo ya guardado (sin red)")
    lo.add_argument("terminos", nargs="*")
    lo.add_argument("--desde")
    lo.add_argument("--mostrar", type=int, default=40)
    lo.add_argument("--exportar", action="store_true")
    lo.set_defaults(func=cmd_local)

    v = sub.add_parser("ver", parents=[comun], help="Mostrar un articulo guardado (parte del enlace)")
    v.add_argument("id")
    v.set_defaults(func=cmd_ver)

    e = sub.add_parser("estado", parents=[comun], help="Resumen de lo guardado y estado del puente")
    e.set_defaults(func=cmd_estado)

    s = sub.add_parser("servidor", parents=[comun], help="Servidor del puente con la extension")
    s.set_defaults(func=cmd_servidor)

    d = sub.add_parser("diagnostico", parents=[comun], help="Prueba cada sitio (directo y por la extension) y escribe un informe")
    d.set_defaults(func=cmd_diagnostico)

    a = p.parse_args(argv)
    PUERTO_USADO[0] = getattr(a, "puerto", PUERTO_DEFECTO)
    DIAG.comando = "netz.py " + " ".join(argv if argv is not None else sys.argv[1:])
    codigo = 0
    try:
        codigo = a.func(a) or 0
    except RuntimeError as ex:
        err(f"Error: {ex}")
        codigo = 1
    except KeyboardInterrupt:
        err("Interrumpido.")
        codigo = 130
    except Exception as ex:
        import traceback
        err(f"Error inesperado: {ex}")
        DIAG.linea("TRACE", traceback.format_exc())
        codigo = 1
    DIAG.resultado = DIAG.resultado if DIAG.resultado != "(sin terminar)" else ("OK" if codigo == 0 else f"ERROR (codigo {codigo})")
    if not getattr(a, "sin_informe", False) and (getattr(a, "informe", False) or DIAG.errores or DIAG.avisos or a.cmd == "diagnostico"):
        ruta = escribe_informe()
        if ruta:
            out(f"Informe de diagnostico: {ruta}")
    return codigo


if __name__ == "__main__":
    sys.exit(main())
