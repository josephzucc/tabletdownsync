#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
upw.py - Buscar, leer, contar y guardar en local ofertas de Upwork desde Termux. Solo biblioteca estandar.
Upwork es una aplicacion de pagina unica con proteccion anti-bot: las paginas las abre y pinta Firefox (extension
upw.xpi, con tu sesion) y upw.py lee el HTML ya construido.

Ejemplos:
  python3 upw.py buscar vb6                          (ofertas de clientes de Suiza)
  python3 upw.py buscar "visual basic" --mundo --paginas 3 --leer
  python3 upw.py estadisticas --lista "vb6,altsystem,cobol,as/400"
  python3 upw.py estadisticas --historia
  python3 upw.py local vb6
  python3 upw.py diagnostico

La salida normal va a stdout; solo los errores van a stderr.
Todo lo encontrado se guarda en ~/upw/upw.db y en archivos .txt (AAAA-MM-DD-HH-MM-SSupwork_...txt).
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
PUERTO_DEFECTO = 8777
ANCHO = 121
TIMEOUT = 40
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 "
              "Firefox/130.0")
DIR = os.environ.get("UPW_DIR") or os.path.join(os.path.expanduser("~"), "upw")
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
                self.ruta = os.path.join(DIR_LOGS, SELLO + "logUpwPy.log")
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

    def pagina(self, url, esperar=6000, desplazar=0):
        """Abre la direccion en una pestana en segundo plano de Firefox, espera, desplaza y devuelve (200, HTML pintado)."""
        n = len(DIAG.peticiones) + 1
        dbg(f"PETICION {n}: PAGINA {url} (espera {esperar} ms, desplazar {desplazar})")
        t0 = time.time()
        r = pedir_trabajo({"op": "pagina", "url": url, "esperar": esperar, "desplazar": desplazar,
                           "expande": bool(desplazar)}, self.puerto, 300)
        if r.get("error"):
            DIAG.peticiones.append({"n": n, "metodo": "PAGINA", "url": url, "via": "extension", "status": None, "bytes": 0,
                                    "clase": "sin-respuesta", "titulo": "", "muestra": str(r["error"])[:300],
                                    "seg": round(time.time() - t0, 1), "archivo": ""})
            raise RuntimeError("la extension informo: " + str(r["error"]))
        txt = r.get("text", "")
        titulo = (re.search(r"<title[^>]*>([\s\S]*?)</title>", txt[:200000], re.I) or [None, ""])[1]
        titulo = re.sub(r"\s+", " ", html.unescape(titulo)).strip()[:70]
        dbg(f"RESPUESTA {n}: {len(txt)} car., {time.time() - t0:.1f}s, titulo {titulo!r}")
        DIAG.peticiones.append({"n": n, "metodo": "PAGINA", "url": url, "via": "extension", "status": 200, "bytes": len(txt),
                                "clase": "html", "titulo": titulo, "muestra": re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(
                                    r"<(script|style)[\s\S]*?</\1>", " ", txt[:300000], flags=re.I)))[:500].strip(),
                                "seg": round(time.time() - t0, 1), "archivo": ""})
        return 200, txt

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


PAGINA_ABRIR = ("<!doctype html><meta charset=utf-8><meta name=\"upw-despertar\" content=\"1\">"
                "<title>upw</title><p>upw: despertando la extension. Puedes cerrar esta pestana.</p>")


def crear_handler(estado):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _cors(self):
            origen = self.headers.get("Origin", "")
            if origen.startswith("moz-extension://"):
                self.send_header("Access-Control-Allow-Origin", origen)
                self.send_header("Access-Control-Allow-Headers", "X-Upw, Content-Type")
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
            if self.headers.get("X-Upw", "") != "1":
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
                    return self._resp(200, {"upw": True, "sondeo_hace": hace, "ext_url": estado.ext_url,
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
                if not re.fullmatch(r"[0-9]{4}(-[0-9]{2}){5}logUpw\.log", nombre):
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
    log("La extension upw se conecta sola si Firefox esta abierto. Deja esto abierto. Ctrl+C para parar.")
    log("(No es obligatorio: con --via-extension, upw.py arranca su propio servidor y despierta la extension.)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("Parado.")
    return 0


def _base(puerto):
    return f"http://127.0.0.1:{puerto}"


def _cab():
    return {"X-Upw": "1", "Content-Type": "application/json"}


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
    forzado = os.environ.get("UPW_FIREFOX")
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
    if os.environ.get("UPW_NO_ABRIR") or not shutil.which("am"):
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
                err("  Firefox abrio la pagina de despertar, pero la extension upw no respondio: esta instalada y activa?")
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
        if est.get("upw"):
            DIAG.puente["servidor"] = "reutilizado"
            log(f"Puente: reutilizo el servidor upw que ya esta activo en el puerto {puerto}.")
        else:
            DIAG.puente["servidor"] = "antiguo (sin soporte de despertar)"
            raise RuntimeError(f"En el puerto {puerto} hay un servidor upw ANTIGUO (por ejemplo 'upw.py "
                               f"servidor' lanzado con la version 1.0.0) que no sabe despertar la extension ni "
                               f"guardar diagnostico. Cierralo (Ctrl+C en su sesion de Termux, o 'pkill -f "
                               f"upw.py') y vuelve a lanzar el comando.")
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
        raise RuntimeError("La extension no responde. Abre Firefox y comprueba que upw esta instalada y que "
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
    e = [f"upw.py {VERSION}", f"Python {platform.python_version()} en {platform.system()} {platform.machine()}",
         f"Termux: {'si' if os.environ.get('PREFIX', '').startswith('/data/data/com.termux') else 'no detectado'}",
         f"Carpeta de trabajo: {os.getcwd()}", f"Carpeta upw: {DIR}",
         f"Comando 'am': {shutil.which('am') or 'no'}; 'pm': {shutil.which('pm') or 'no'}"]
    for v in ("UPW_DIR", "UPW_FIREFOX", "UPW_NO_ABRIR"):
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
        sug.append("Hay articulos de pago: inicia sesion en upwwoche.ch / ictjournal.ch en el Firefox de la extension y "
                   "repite con --via-extension.")
    if DIAG.puente.get("extension") == "sin respuesta":
        if DIAG.puente.get("abrir_visto"):
            sug.append("Firefox abrio la pagina de despertar pero la extension no contesto: comprueba que upw.xpi esta "
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
    ruta = os.path.join(destino, f"{SELLO}diagUpw.txt")
    L = ["=" * 121, f"INFORME DE DIAGNOSTICO DE UPW {VERSION}", f"Generado: {time.strftime('%Y-%m-%d %H:%M:%S')}", "=" * 121, ""]
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
    L.append("7. REGISTRO DE ESTA EJECUCION (upw.py)")
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
# ---------------------------------------------------------------------------
# Upwork: Firefox pinta la pagina (Upwork es una aplicacion de pagina unica) y se lee el HTML ya construido
# ---------------------------------------------------------------------------

BASE = "https://www.upwork.com"
RUTA_DB = os.path.join(DIR, "upw.db")
VOID = {"br", "img", "input", "meta", "link", "hr", "source", "wbr", "area", "base", "col", "embed", "param", "track"}
PALABRAS_TECNICAS = ["vb6", "visual basic", "vb.net", "altsystem", "legacy", "as/400", "cobol", "delphi", "powerbuilder",
                     "ms access", "vba", "classic asp", "foxpro", "abap", "migration"]
CLASES_BLOQUEO = re.compile(r"(just a moment|verifying you are human|attention required|access denied|"
                            r"challenge-platform|cf-chl)", re.I)
RE_ID = re.compile(r"/jobs/(?:[^/?#]*?)?(~0[0-9a-f]{10,})", re.I)


class Nodo:
    __slots__ = ("tag", "attrs", "hijos", "padre")

    def __init__(self, tag, attrs=None, padre=None):
        self.tag, self.attrs, self.hijos, self.padre = tag, dict(attrs or {}), [], padre

    def clases(self):
        return (self.attrs.get("class") or "").split()

    def texto(self):
        partes = []

        def rec(n):
            for h in n.hijos:
                if isinstance(h, str):
                    partes.append(h)
                elif h.tag in ("script", "style"):
                    continue
                elif h.tag in ("br", "p", "div", "li", "h1", "h2", "h3", "h4", "section", "article", "tr"):
                    partes.append("\n")
                    rec(h)
                    partes.append("\n")
                else:
                    rec(h)
        rec(self)
        t = re.sub(r"[ \t\r\f\v ]+", " ", "".join(partes))
        return re.sub(r"\n\s*\n+", "\n", re.sub(r" *\n *", "\n", t)).strip()

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
        self.actual.hijos.append(Nodo(tag, {k: (v or "") for k, v in attrs}, self.actual))

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
    txt = re.sub(r"(<(?:script|style|svg)\b[^>]*>).*?(</(?:script|style|svg)>)", r"\1\2", txt, flags=re.S | re.I)
    b = _ConstruyeDOM()
    b.feed(txt)
    return b.raiz


def dt(n):
    return (n.attrs.get("data-test") or n.attrs.get("data-qa") or "").lower()


def id_de_url(url):
    m = RE_ID.search(url or "")
    return m.group(1) if m else ""


def url_busqueda(consulta, pagina=1, pais="", orden="recency", por_pagina=50):
    p = {"q": consulta, "sort": orden, "per_page": por_pagina}
    if pagina > 1:
        p["page"] = pagina
    if pais:
        p["location"] = pais
    return f"{BASE}/nx/search/jobs/?" + parse.urlencode(p)


def total_declarado(texto_plano):
    """'1,234 jobs found' / '1,234 results' si la pagina lo dice."""
    m = re.search(r"([\d][\d,.  ]*)\s+(?:jobs?|results?|trabajos|resultados)\b", texto_plano[:6000], re.I)
    if m:
        try:
            return int(re.sub(r"\D", "", m.group(1)))
        except ValueError:
            return None
    return None


def parsea_tarjetas(txt):
    """Lista de ofertas de una pagina de resultados. Se localiza cada enlace a /jobs/~ID y se sube por el DOM hasta el
    contenedor mas pequeno que contenga datos de la oferta (Posted / Hourly / Fixed) sin mezclar otra oferta."""
    raiz = parsea_dom(txt)
    enlaces = raiz.busca(lambda n: n.tag == "a" and id_de_url(n.attrs.get("href", "")))
    por_id = {}
    for a in enlaces:
        i = id_de_url(a.attrs["href"])
        por_id.setdefault(i, []).append(a)
    res = []
    for i, lst in por_id.items():
        titulo_a = max(lst, key=lambda n: len(n.texto()))
        nodo = titulo_a
        mejor = None
        for _ in range(9):
            if nodo.padre is None:
                break
            nodo = nodo.padre
            ids = {id_de_url(x.attrs["href"]) for x in nodo.busca(lambda n: n.tag == "a" and id_de_url(n.attrs.get("href", "")))}
            if len(ids) > 1:
                break
            mejor = nodo
            if re.search(r"(Posted|Hourly|Fixed.?price|Est\.? ?budget)", nodo.texto()) and len(nodo.texto()) > 80:
                break
        tarjeta = mejor or titulo_a
        res.append(tarjeta_a_oferta(i, titulo_a, tarjeta))
    return res


def tarjeta_a_oferta(i, titulo_a, tarjeta):
    t = tarjeta.texto()
    href = titulo_a.attrs.get("href", "")
    url = href if href.startswith("http") else BASE + href
    o = {"id": i, "url": limpia_url(url).split("?")[0], "titulo": titulo_a.texto().replace("\n", " ").strip(),
         "desc": "", "publicado": "", "tipo": "", "presupuesto": "", "nivel": "", "duracion": "", "propuestas": "",
         "pais": "", "skills": "", "completa": 0}
    m = re.search(r"Posted\s*([^\n]+)", t, re.I)
    o["publicado"] = m.group(1).strip() if m else ""
    m = re.search(r"Hourly(?::|\s)?\s*\$?\s*([\d.,]+)\s*-\s*\$?\s*([\d.,]+)", t, re.I)
    if m:
        o["tipo"], o["presupuesto"] = "hora", f"${m.group(1)}-{m.group(2)}/h"
    elif re.search(r"Hourly", t, re.I):
        o["tipo"] = "hora"
    m = re.search(r"Fixed.?price", t, re.I)
    if m:
        o["tipo"] = "fijo"
    m = re.search(r"Est\.? ?budget:?\s*\$?\s*([\d.,]+)", t, re.I)
    if m:
        o["presupuesto"] = "$" + m.group(1)
    m = re.search(r"\b(Entry level|Intermediate|Expert)\b", t, re.I)
    o["nivel"] = m.group(1) if m else ""
    m = re.search(r"Est\.? ?time:?\s*([^\n]+)", t, re.I)
    o["duracion"] = m.group(1).strip() if m else ""
    m = re.search(r"Proposals:?\s*([^\n]+)", t, re.I)
    o["propuestas"] = m.group(1).strip() if m else ""
    pais = tarjeta.primero(lambda n: any(k in dt(n) for k in ("client-country", "location", "country")))
    o["pais"] = pais.texto().replace("\n", " ").strip() if pais else ""
    sk = tarjeta.busca(lambda n: any(k in dt(n) for k in ("token", "skill", "attr-item")) and 0 < len(n.texto()) < 40)
    o["skills"] = ", ".join(dict.fromkeys(x.texto().replace("\n", " ") for x in sk))
    d = tarjeta.primero(lambda n: "description" in dt(n))
    if d:
        o["desc"] = d.texto()
    else:   # el parrafo mas largo que no sea el titulo
        cand = [x for x in t.split("\n") if len(x) > 120 and x.strip() != o["titulo"]]
        o["desc"] = max(cand, key=len) if cand else ""
    return o


def parsea_detalle(txt, o):
    raiz = parsea_dom(txt)
    d = raiz.primero(lambda n: dt(n) in ("description", "job-description", "job-description-text") or "job-description" in dt(n))
    cuerpo = d.texto() if d else ""
    plano = raiz.texto()
    if not cuerpo:
        m = re.search(r"Summary\s*\n([\s\S]+?)(?:\n(?:About the client|Activity on this job|Skills and expertise|Client's recent)|\Z)", plano)
        cuerpo = m.group(1).strip() if m else ""
    h1 = raiz.primero(lambda n: n.tag == "h1")
    if h1 and not o["titulo"]:
        o["titulo"] = h1.texto().replace("\n", " ")
    extra = []
    for pat in (r"(Proposals:?[^\n]*)", r"(Interviewing:?[^\n]*)", r"(Invites sent:?[^\n]*)", r"(Last viewed by client:?[^\n]*)",
                r"(Member since[^\n]*)", r"(\$[\d.,KkMm]+\s*total spent)", r"(\d+% hire rate[^\n]*)", r"(Payment method verified)"):
        m = re.search(pat, plano, re.I)
        if m:
            extra.append(m.group(1).strip())
    if cuerpo:
        o["desc"] = cuerpo
    if extra:
        o["desc"] = (o["desc"] + "\n\nDatos de la oferta/cliente: " + "; ".join(extra)).strip()
    if not o["pais"]:
        m = re.search(r"About the client[\s\S]{0,300}?\n([A-Z][A-Za-z .'-]{2,40})\n", plano)
        o["pais"] = m.group(1) if m else ""
    o["completa"] = 1 if cuerpo else 0
    return o


# ---- red -----------------------------------------------------------------

def pagina_pintada(c, url, esperar=7000, desplazar=2, etiqueta=""):
    """HTML de la pagina ya pintada por Firefox. Detecta la verificacion anti-bot de Cloudflare."""
    if not c.via:
        raise RuntimeError("Upwork solo se puede leer con la extension de Firefox (pagina pintada).")
    st, txt = c.pagina(url, esperar, desplazar)
    if CLASES_BLOQUEO.search(re.sub(r"<[^>]+>", " ", txt[:8000])) and "/jobs/~" not in txt:
        DIAG.guarda_respuesta(url, st, "extension", txt, "bloqueo-cloudflare")
        raise RuntimeError("Upwork mostro una verificacion anti-bot: abre upwork.com en ese Firefox, resuelvela e inicia "
                           "sesion, y repite.")
    return txt


def busca_ofertas(c, consulta, pais="", paginas=1, maximo=50, orden="recency"):
    todas, vistos, total = [], set(), None
    for pg in range(1, paginas + 1):
        url = url_busqueda(consulta, pg, pais, orden)
        txt = pagina_pintada(c, url, 7000, 3, "busqueda")
        if total is None:
            total = total_declarado(parsea_dom(txt).texto())
        lote = [o for o in parsea_tarjetas(txt) if o["id"] not in vistos]
        if not lote:
            if pg == 1:
                DIAG.guarda_respuesta(url, 200, "extension", txt, "sin-ofertas")
                DIAG.avisos.append(f"{consulta}: la pagina no dio ofertas reconocibles")
                raise RuntimeError(f"La pagina de busqueda de {consulta!r} no tiene ofertas reconocibles "
                                   f"(titulo {titulo_de(txt)[:50]!r}). Si no has iniciado sesion en upwork.com en ese "
                                   f"Firefox, hazlo; si ya la tienes, la pagina cambio: manda el informe.")
            break
        for o in lote:
            vistos.add(o["id"])
            o["consulta"] = consulta
        todas += lote
        out(f"  pagina {pg}: {len(lote)} oferta(s)" + (f" (Upwork declara {total} en total)" if total and pg == 1 else ""))
        if len(todas) >= maximo:
            break
        if pg < paginas:
            pausa_corta()
    return todas[:maximo], total


def titulo_de(txt):
    m = re.search(r"<title[^>]*>([\s\S]*?)</title>", txt[:200000], re.I)
    return re.sub(r"\s+", " ", html.unescape(m.group(1))).strip() if m else ""


def pausa_corta(a=1.5, b=3.0):
    import random
    time.sleep(random.uniform(a, b))


# ---- base de datos y salida ---------------------------------------------------

def abre_db():
    asegura_dirs()
    db = sqlite3.connect(RUTA_DB)
    db.execute("""CREATE TABLE IF NOT EXISTS ofertas (id TEXT PRIMARY KEY, url TEXT, titulo TEXT, descr TEXT, publicado TEXT,
                  tipo TEXT, presupuesto TEXT, nivel TEXT, duracion TEXT, propuestas TEXT, pais TEXT, skills TEXT,
                  consulta TEXT, primera TEXT, ultima TEXT, completa INTEGER)""")
    db.execute("""CREATE TABLE IF NOT EXISTS historia (fecha TEXT, consulta TEXT, pais TEXT, total INTEGER, vistas INTEGER)""")
    return db


def guarda(db, ofertas):
    nuevas = 0
    for o in ofertas:
        f = db.execute("SELECT completa, primera FROM ofertas WHERE id=?", (o["id"],)).fetchone()
        if f is None:
            nuevas += 1
        elif f[0] and not o["completa"]:
            db.execute("UPDATE ofertas SET ultima=?, publicado=? WHERE id=?", (ahora(), o["publicado"], o["id"]))
            continue
        db.execute("INSERT OR REPLACE INTO ofertas VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (o["id"], o["url"], o["titulo"], o["desc"], o["publicado"], o["tipo"], o["presupuesto"], o["nivel"],
                    o["duracion"], o["propuestas"], o["pais"], o["skills"], o.get("consulta", ""),
                    f[1] if f else ahora(), ahora(), o["completa"]))
    db.commit()
    return nuevas


COLS = ["id", "url", "titulo", "descr", "publicado", "tipo", "presupuesto", "nivel", "duracion", "propuestas", "pais",
        "skills", "consulta", "primera", "ultima", "completa"]


def fila_a_oferta(f):
    d = dict(zip(COLS, f))
    d["desc"] = d.pop("descr")
    return d


def envolver(txt, sangria=""):
    res = []
    for linea in (txt or "").split("\n"):
        res += textwrap.wrap(linea, ANCHO, initial_indent=sangria, subsequent_indent=sangria, break_long_words=True,
                             break_on_hyphens=False) or [""]
    return res


def bloque_oferta(o, n=None):
    L = ["-" * ANCHO]
    L += envolver(f"{n}. {o['titulo']}" if n else o["titulo"])
    datos = [f"Publicada: {o.get('publicado') or '-'}", f"Tipo: {o.get('tipo') or '-'}",
             f"Importe: {o.get('presupuesto') or '-'}", f"Nivel: {o.get('nivel') or '-'}"]
    L += envolver("  " + "   ".join(datos))
    datos2 = [f"Duracion: {o.get('duracion') or '-'}", f"Propuestas: {o.get('propuestas') or '-'}",
              f"Pais del cliente: {o.get('pais') or '-'}"]
    L += envolver("  " + "   ".join(datos2))
    if o.get("skills"):
        L += envolver("  Habilidades: " + o["skills"])
    L += envolver("  Enlace: " + o["url"])
    if o.get("desc"):
        L.append("")
        L += envolver(o["desc"], "  ")
    L.append("")
    return L


def escribe_txt(ruta, titulo, ofertas):
    L = [titulo, f"Generado: {time.strftime('%Y-%m-%d %H:%M')}   Ofertas: {len(ofertas)}", "=" * ANCHO, ""]
    for i, o in enumerate(ofertas, 1):
        L += bloque_oferta(o, i)
    with open(ruta, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")


def resumen_linea(o, n):
    s = f"{n:>3}. {o.get('publicado') or '-':<16.16} {o.get('tipo') or '-':<5} {o.get('presupuesto') or '-':<14.14} {o['titulo']}"
    return s if len(s) <= ANCHO else s[:ANCHO - 3] + "..."


# ---- comandos ------------------------------------------------------------------

def pais_de(a):
    if getattr(a, "mundo", False):
        return ""
    return a.pais


def cmd_buscar(a):
    if not a.terminos:
        raise RuntimeError("Indica que buscar, p. ej.: python3 upw.py buscar vb6")
    c = Cliente(True, a.puerto)
    asegura_puente(a.puerto)
    consulta = " ".join(a.terminos)
    pais = pais_de(a)
    log(f"[upwork] buscando: {consulta!r}" + (f" (clientes de {pais})" if pais else " (todo el mundo)"))
    ofertas, total = busca_ofertas(c, consulta, pais, a.paginas, a.max, a.orden)
    if a.leer:
        for i, o in enumerate(ofertas, 1):
            log(f"  leyendo {i}/{len(ofertas)}: {o['titulo'][:80]}")
            try:
                parsea_detalle(pagina_pintada(c, o["url"], 6000, 0, "detalle"), o)
            except RuntimeError as e:
                err(f"    no se pudo leer {o['url']}: {e}")
            pausa_corta()
    db = abre_db()
    nuevas = guarda(db, ofertas)
    db.execute("INSERT INTO historia VALUES (?,?,?,?,?)", (ahora(), consulta, pais, total, len(ofertas)))
    db.commit()
    log(f"Guardadas en la base local ({nuevas} nuevas de {len(ofertas)}).")
    ruta = a.salida or os.path.join(DIR, f"{ahora()}upwork_{nombre_archivo(consulta)}.txt")
    escribe_txt(ruta, f"UPW - Upwork: {consulta}" + (f" [{pais}]" if pais else ""), ofertas)
    out("")
    for i, o in enumerate(ofertas[:a.mostrar], 1):
        out(resumen_linea(o, i))
    if len(ofertas) > a.mostrar:
        out(f"... y {len(ofertas) - a.mostrar} mas (estan en el archivo).")
    out(f"\nArchivo: {ruta}")
    out(f"Base local: {RUTA_DB}")
    return 0


def cmd_leer(a):
    c = Cliente(True, a.puerto)
    asegura_puente(a.puerto)
    db = abre_db()
    ofertas = []
    for u in a.urls:
        i = id_de_url(u) or u
        f = db.execute("SELECT * FROM ofertas WHERE id=?", (i,)).fetchone()
        o = fila_a_oferta(f) if f else {"id": i, "url": u if u.startswith("http") else f"{BASE}/jobs/{u}", "titulo": "",
                                       "desc": "", "publicado": "", "tipo": "", "presupuesto": "", "nivel": "",
                                       "duracion": "", "propuestas": "", "pais": "", "skills": "", "completa": 0,
                                       "consulta": ""}
        try:
            parsea_detalle(pagina_pintada(c, o["url"], 6000, 0, "detalle"), o)
        except RuntimeError as e:
            err(f"No se pudo leer {o['url']}: {e}")
            continue
        ofertas.append(o)
        pausa_corta()
    guarda(db, ofertas)
    ruta = a.salida or os.path.join(DIR, f"{ahora()}upwork_lectura.txt")
    escribe_txt(ruta, "UPW - Upwork: lectura de ofertas", ofertas)
    for o in ofertas:
        for l in bloque_oferta(o):
            out(l)
    out(f"Archivo: {ruta}")
    return 0 if ofertas else 1


def cmd_estadisticas(a):
    """Cuantas ofertas de Upwork hay por cada palabra clave (y pais) y como evoluciona en el tiempo."""
    palabras = [p.strip() for p in (a.lista or ",".join(PALABRAS_TECNICAS)).split(",") if p.strip()]
    db = abre_db()
    if a.historia:
        filas = db.execute("SELECT fecha, consulta, pais, total, vistas FROM historia ORDER BY consulta, fecha").fetchall()
        if not filas:
            out("Todavia no hay historia: ejecuta 'estadisticas' o 'buscar' primero.")
            return 0
        out(f"{'Fecha':<19} {'Palabra':<22} {'Pais':<14} {'Total Upwork':>12} {'Leidas':>7}")
        for f, q, p, t, v in filas:
            out(f"{f:<19} {q[:22]:<22} {(p or 'mundo')[:14]:<14} {t if t is not None else '-':>12} {v:>7}")
        return 0
    c = Cliente(True, a.puerto)
    asegura_puente(a.puerto)
    pais = pais_de(a)
    fecha = ahora()
    filas = []
    for q in palabras:
        log(f"[upwork] contando: {q!r}")
        try:
            txt = pagina_pintada(c, url_busqueda(q, 1, pais, "recency", 10), 7000, 1, "estadistica")
            tarjetas = parsea_tarjetas(txt)
            total = total_declarado(parsea_dom(txt).texto())
            if total is None:
                total = len(tarjetas)
                aprox = "(solo las visibles)"
            else:
                aprox = ""
            horas = [t for t in tarjetas if re.search(r"(minute|hour)s? ago", t["publicado"] or "", re.I)]
            filas.append((q, total, len(tarjetas), len(horas), aprox))
            db.execute("INSERT INTO historia VALUES (?,?,?,?,?)", (fecha, q, pais, total, len(tarjetas)))
            db.commit()
            guarda(db, [{**t, "consulta": q} for t in tarjetas])
        except RuntimeError as e:
            err(f"  {q}: {e}")
            filas.append((q, None, 0, 0, "ERROR"))
        pausa_corta()
    L = [f"UPW - estadisticas de ofertas en Upwork   {time.strftime('%Y-%m-%d %H:%M')}   "
         f"{'clientes de ' + pais if pais else 'todo el mundo'}", "=" * ANCHO,
         f"{'Palabra':<24} {'Ofertas (total)':>16} {'Leidas':>7} {'Ultimas horas':>14}  Nota", "-" * ANCHO]
    for q, t, v, h, n in filas:
        L.append(f"{q:<24} {t if t is not None else '-':>16} {v:>7} {h:>14}  {n}")
    L += ["-" * ANCHO, "Total = cifra que declara Upwork en la pagina de resultados; si no la declara, solo se cuentan las "
          "ofertas visibles.", "'Ultimas horas' = ofertas leidas publicadas hace minutos u horas."]
    ruta = a.salida or os.path.join(DIR, f"{fecha}upwork_estadisticas.txt")
    with open(ruta, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    for l in L:
        out(l)
    out(f"\nArchivo: {ruta}")
    return 0


def cmd_local(a):
    db = abre_db()
    cond, par = [], []
    for t in a.terminos:
        cond.append("(titulo LIKE ? OR descr LIKE ? OR skills LIKE ?)")
        par += [f"%{t}%"] * 3
    if a.desde:
        cond.append("primera >= ?")
        par.append(a.desde)
    f = db.execute("SELECT * FROM ofertas" + (" WHERE " + " AND ".join(cond) if cond else "") +
                   " ORDER BY ultima DESC LIMIT ?", par + [a.mostrar]).fetchall()
    ofertas = [fila_a_oferta(x) for x in f]
    if not ofertas:
        out("Sin resultados en la base local.")
        return 0
    for i, o in enumerate(ofertas, 1):
        out(resumen_linea(o, i))
    if a.exportar:
        ruta = a.salida or os.path.join(DIR, f"{ahora()}upwork_local.txt")
        escribe_txt(ruta, "UPW - Upwork: base local", ofertas)
        out(f"Archivo: {ruta}")
    return 0


def cmd_ver(a):
    db = abre_db()
    f = db.execute("SELECT * FROM ofertas WHERE id LIKE ? OR url LIKE ? LIMIT 1", (f"%{a.id}%", f"%{a.id}%")).fetchone()
    if not f:
        raise RuntimeError("No encuentro esa oferta en la base local.")
    for l in bloque_oferta(fila_a_oferta(f)):
        out(l)
    return 0


def cmd_estado(a):
    db = abre_db()
    n = db.execute("SELECT COUNT(*), SUM(completa) FROM ofertas").fetchone()
    out(f"upw {VERSION}   carpeta: {DIR}")
    out(f"  {n[0]} oferta(s) guardadas ({n[1] or 0} con descripcion completa)")
    try:
        r = pedir_estado(a.puerto)
        out("Puente: servidor activo; extension "
            + ("conectada" if r.get("sondeo_hace") is not None and r["sondeo_hace"] < 30 else "sin senales"))
    except Exception:
        out("Puente: no hay servidor activo (upw.py lo arranca solo al buscar)")
    return 0


def cmd_diagnostico(a):
    out(f"Diagnostico de upw {VERSION}")
    for linea in entorno():
        out("  " + linea)
    fallos = 0
    try:
        asegura_puente(a.puerto)
    except RuntimeError as e:
        err(f"Extension no disponible: {e}")
        DIAG.resultado = "diagnostico: sin extension"
        return 1
    c = Cliente(True, a.puerto)
    for nombre, url in (("busqueda vb6 (mundo)", url_busqueda("vb6", 1, "", "recency", 10)),
                        ("busqueda vb6 (Suiza)", url_busqueda("vb6", 1, "Switzerland", "recency", 10))):
        try:
            txt = pagina_pintada(c, url, 7000, 1, "diag")
            n = len(parsea_tarjetas(txt))
            tot = total_declarado(parsea_dom(txt).texto())
            out(f"  {'OK   ' if n else 'FALLA'} {nombre:<24} {n} oferta(s), total declarado {tot}")
            if not n:
                DIAG.guarda_respuesta(url, 200, "extension", txt, "sin-ofertas")
            fallos += 0 if n else 1
        except RuntimeError as e:
            err(f"  FALLA {nombre:<24} {str(e)[:160]}")
            fallos += 1
    DIAG.resultado = f"diagnostico: {fallos} prueba(s) fallida(s)" if fallos else "diagnostico: todo bien"
    out(f"Resultado: {DIAG.resultado}.")
    return 0 if not fallos else 1


def main(argv=None):
    p = argparse.ArgumentParser(prog="upw.py", description="upw.py - Buscar, leer y guardar en local ofertas de Upwork "
                                "desde Termux con la extension de Firefox. Solo biblioteca estandar.")
    comun = argparse.ArgumentParser(add_help=False)
    comun.add_argument("--puerto", type=int, default=PUERTO_DEFECTO)
    comun.add_argument("--informe", action="store_true", help="Escribir siempre el informe de diagnostico")
    comun.add_argument("--sin-informe", action="store_true", dest="sin_informe", help="No escribir informe aunque haya errores")
    comun.add_argument("-o", "--salida", help="Ruta del archivo .txt de salida")
    comun.add_argument("--pais", default="Switzerland", help="Pais del cliente (por defecto Switzerland)")
    comun.add_argument("--mundo", action="store_true", help="Sin filtro de pais")
    p.add_argument("--version", action="version", version=f"upw {VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("buscar", parents=[comun], help="Buscar ofertas")
    b.add_argument("terminos", nargs="*")
    b.add_argument("--paginas", type=int, default=2, help="Paginas de resultados (50 ofertas por pagina)")
    b.add_argument("--max", type=int, default=100)
    b.add_argument("--orden", choices=["recency", "relevance"], default="recency")
    b.add_argument("--leer", action="store_true", help="Abrir cada oferta y guardar su descripcion completa")
    b.add_argument("--mostrar", type=int, default=25)
    b.set_defaults(func=cmd_buscar)

    le = sub.add_parser("leer", parents=[comun], help="Leer ofertas concretas por enlace o identificador")
    le.add_argument("urls", nargs="+")
    le.set_defaults(func=cmd_leer)

    es = sub.add_parser("estadisticas", parents=[comun], help="Contar ofertas por palabra clave (vb6, altsystem...)")
    es.add_argument("--lista", help="Palabras separadas por comas (por defecto, una lista de tecnologias heredadas)")
    es.add_argument("--historia", action="store_true", help="Mostrar las cifras de ejecuciones anteriores")
    es.set_defaults(func=cmd_estadisticas)

    lo = sub.add_parser("local", parents=[comun], help="Buscar en lo ya guardado (sin red)")
    lo.add_argument("terminos", nargs="*")
    lo.add_argument("--desde")
    lo.add_argument("--mostrar", type=int, default=40)
    lo.add_argument("--exportar", action="store_true")
    lo.set_defaults(func=cmd_local)

    v = sub.add_parser("ver", parents=[comun], help="Mostrar una oferta guardada (parte del id o del enlace)")
    v.add_argument("id")
    v.set_defaults(func=cmd_ver)

    e = sub.add_parser("estado", parents=[comun], help="Resumen de lo guardado y estado del puente")
    e.set_defaults(func=cmd_estado)

    s = sub.add_parser("servidor", parents=[comun], help="Servidor del puente con la extension")
    s.set_defaults(func=cmd_servidor)

    d = sub.add_parser("diagnostico", parents=[comun], help="Prueba la busqueda de Upwork y escribe un informe")
    d.set_defaults(func=cmd_diagnostico)

    a = p.parse_args(argv)
    PUERTO_USADO[0] = getattr(a, "puerto", PUERTO_DEFECTO)
    DIAG.comando = "upw.py " + " ".join(argv if argv is not None else sys.argv[1:])
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
