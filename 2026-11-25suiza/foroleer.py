#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
foroleer.py 2.2 - Busca y lee Reddit, Forocoches y swissforum.co.uk desde Termux y guarda el
texto. Las peticiones salen por la extension de Firefox "foroleer" (con tu sesion y tu IP de
navegador), porque Reddit rechaza (403 "bloqueado por seguridad de red") las que salen de Termux.

Uso rapido:
  python3 foroleer.py reddit "freelance switzerland" --subreddit Switzerland   (un .txt por hilo)
  python3 foroleer.py quora "freelance switzerland"      (un .txt por pregunta)
  python3 foroleer.py quora-tema Switzerland --palabra swit   (temas de Quora; prueba primero sin Firefox)
  python3 foroleer.py reporte --desde 16:15 --hasta 16:45     (un .txt con los registros de ese rato, para enviarlo)
  python3 foroleer.py swissforum "self employed"
  python3 foroleer.py forocoches "trabajar en suiza"
  python3 foroleer.py diagnostico          (prueba todo y escribe un informe .txt)
  python3 foroleer.py abrir                (solo despierta la extension)
  python3 foroleer.py archivo pagina.html  (lee una pagina guardada con SingleFile)

Solo biblioteca estandar de Python. Manual: foroleerManualBono.txt
"""

import argparse
import html
import http.cookiejar
import json
import os
import platform
import random
import re
import shutil
import subprocess
import sys
import textwrap
import threading
import time
import unicodedata
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import request, parse, error

VERSION = "2.4.1"
PUERTO_DEFECTO = 8775
HOME = os.path.expanduser("~")
DIR = os.environ.get("FOROLEER_DIR") or os.path.join(HOME, "foroleer")
LOGDIR = os.path.join(DIR, "logs")
RESPDIR = os.path.join(DIR, "respuestas")
SELLO = time.strftime("%Y-%m-%d-%H-%M-%S")
ANCHO = 121
UA_NAVEGADOR = "Mozilla/5.0 (Android 14; Mobile; rv:130.0) Gecko/130.0 Firefox/130.0"
TIMEOUT = 30
AUTO_CIERRE_MIN = 45     # minutos sin clientes tras los que se apaga solo el servidor del puente


# ---------------------------------------------------------------------------
# Texto: normalizar y ajustar a 121 columnas
# ---------------------------------------------------------------------------

def normaliza(s):
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.lower()


def envuelve(texto, ancho=ANCHO, sangria="    "):
    """Devuelve lineas de como mucho `ancho` caracteres (corta las largas, con sangria de continuacion)."""
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


def texto_visible(contenido, limite=100000):
    t = re.sub(r"<(script|style|noscript)\b[\s\S]*?</\1>", " ", contenido[:400000], flags=re.I)
    t = t[:limite * 3]
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", html.unescape(t)).strip()


def titulo_html(contenido):
    m = re.search(r"<title[^>]*>([\s\S]*?)</title>", contenido[:200000], re.I)
    return re.sub(r"\s+", " ", html.unescape(m.group(1))).strip() if m else ""


# ---------------------------------------------------------------------------
# Salida y diagnostico
#   out()  -> salida normal (stdout) + registro
#   err()  -> errores (stderr) + registro
#   dbg()  -> solo registro
# ---------------------------------------------------------------------------

class Diag:
    def __init__(self):
        self.lineas = []
        self.peticiones = []
        self.ruta = None
        self.comando = ""
        self.resultado = "(sin terminar)"
        self.errores = []
        self.puente = {}
        self.ext_log_ruta = None
        self.ext_info = None
        self.n_resp = 0
        self.informe_ruta = None
        self.guardadas = []
        self.avisos = []

    def linea(self, nivel, msg):
        ms = int(time.time() * 1000) % 1000
        txt = f"[{time.strftime('%H:%M:%S')}.{ms:03d}] {nivel:5} {msg}"
        self.lineas.append(txt)
        if self.ruta is None:
            try:
                os.makedirs(LOGDIR, exist_ok=True)
                self.ruta = os.path.join(LOGDIR, SELLO + "logForoleer.log")
            except OSError:
                self.ruta = ""
        if self.ruta:
            try:
                with open(self.ruta, "a", encoding="utf-8") as f:
                    f.write("\n".join(envuelve(txt)) + "\n")
            except OSError:
                self.ruta = ""

    def guarda_respuesta(self, resp, etiqueta):
        """Guarda el cuerpo de una respuesta que no es la esperada, para poder leerlo despues."""
        self.n_resp += 1
        nombre = f"{SELLO}-resp{self.n_resp:02d}-{etiqueta}.txt"
        try:
            os.makedirs(RESPDIR, exist_ok=True)
            ruta = os.path.join(RESPDIR, nombre)
            cab = [f"URL: {resp.url}", f"Estado: {resp.status}", f"Via: {resp.via}",
                   f"Tipo: {resp.tipo}", f"Clase: {resp.clase}", f"Bytes: {len(resp.text)}", "-" * 60]
            with open(ruta, "w", encoding="utf-8") as f:
                f.write("\n".join(envuelve("\n".join(cab) + "\n" + resp.text[:400000])) + "\n")
            self.guardadas.append(ruta)
            return ruta
        except OSError:
            return ""


DIAG = Diag()


def out(msg=""):
    print(msg, flush=True)
    DIAG.linea("INFO", msg)


def err(msg):
    print(msg, file=sys.stderr, flush=True)
    DIAG.linea("ERROR", msg)
    DIAG.errores.append(msg)


def dbg(msg):
    DIAG.linea("DEBUG", msg)


# Compatibilidad con el codigo de analisis: log() es salida normal
def log(msg):
    out(msg)


class ErrorForoleer(Exception):
    pass


class ErrorPuente(ErrorForoleer):
    pass


class ErrorWeb(ErrorForoleer):
    def __init__(self, msg, resp=None):
        super().__init__(msg)
        self.resp = resp


def pausa(minimo, maximo, motivo=""):
    t = random.uniform(minimo, maximo)
    if motivo:
        out(f"  (esperando {t:.1f}s - {motivo})")
    time.sleep(t)


# ---------------------------------------------------------------------------
# Clasificar respuestas (para el diagnostico)
# ---------------------------------------------------------------------------

CLASES_MALAS = ("reddit-bloqueo-red", "reddit-bienvenida", "cloudflare", "limite-peticiones", "vacio",
                "sin-respuesta")


def clasifica(status, texto, tipo=""):
    if status is None:
        return "sin-respuesta"
    t = texto or ""
    if not t.strip():
        return "vacio" if status < 400 else f"http-{status}"
    bajo = t[:400000].lower().replace("%20", " ")
    tit = titulo_html(t).lower()
    if "blocked by network security" in bajo:
        return "reddit-bloqueo-red"
    contenido_reddit = ("search-sdui-post" in bajo or "<shreddit-post" in bajo or "<shreddit-comment" in bajo
                        or 'class="thing' in bajo or "class='thing" in bajo)
    if tit in ("te damos la bienvenida a reddit", "welcome to reddit") or ("<shreddit-app" in bajo[:300000]
                                                                           and not contenido_reddit):
        return "reddit-bienvenida"
    if "just a moment" in bajo[:6000] or "cf-chl" in bajo[:20000] or "attention required! | cloudflare" in bajo:
        return "cloudflare"
    if status == 429:
        return "limite-peticiones"
    if status >= 400:
        return f"http-{status}"
    if t.lstrip()[:1] in "{[":
        return "json"
    return "html"


EXPLICACION_CLASE = {
    "reddit-bloqueo-red": "Reddit respondio 'Your request has been blocked by network security. Please try to login with "
                          "your Reddit account.' Reddit bloquea la conexion (IP o falta de sesion). Si sale por la "
                          "extension: inicia sesion en reddit.com en Firefox y repite. Si sale directo desde Termux: "
                          "usa --via extension.",
    "reddit-bienvenida": "Reddit devolvio su pagina de bienvenida/registro en vez de datos: bloqueo o falta de sesion.",
    "cloudflare": "Hay una verificacion anti-bot (Cloudflare). Abre la web en Firefox, resuelvela y repite.",
    "limite-peticiones": "La web limita las peticiones (429). Espera unos minutos y baja --max-hilos.",
    "vacio": "La web respondio sin contenido.",
    "sin-respuesta": "No hubo respuesta.",
}


# ---------------------------------------------------------------------------
# Respuesta y transporte (directo desde Termux o a traves de la extension)
# ---------------------------------------------------------------------------

class Resp:
    def __init__(self, status, text, url, via, tipo=""):
        self.status = status
        self.text = text or ""
        self.url = url
        self.via = via
        self.tipo = tipo or ""
        self.clase = clasifica(status, self.text, self.tipo)

    def json(self):
        try:
            return json.loads(self.text)
        except ValueError:
            raise ErrorWeb(f"la respuesta no es JSON ({self.clase}; titulo: {titulo_html(self.text)[:60]!r})", self)


def decodifica(raw, tipo):
    cs = None
    m = re.search(r"charset=([\w-]+)", tipo or "", re.I)
    if m:
        cs = m.group(1)
    if not cs:
        m = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", raw[:3000], re.I)
        cs = m.group(1).decode("ascii", "ignore") if m else "utf-8"
    try:
        return raw.decode(cs, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Puente: servidor local que habla con la extension
# ---------------------------------------------------------------------------

PAGINA_ABRIR = ("<!doctype html><meta charset=utf-8><meta name=\"foroleer-despertar\" content=\"1\">"
                "<title>foroleer</title><p>foroleer: despertando la extension. Puedes cerrar esta pestana.</p>")


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
        self.logs_ext = {}
        self.arranque = time.strftime("%H%M%S")
        self.inicio = time.time()
        self.ultimo_cliente = time.time()
        self.en_curso = {}
        self.apagar = False
        self.servidos = 0


def crear_handler(estado):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _cors(self):
            origen = self.headers.get("Origin", "")
            if origen.startswith("moz-extension://"):
                self.send_header("Access-Control-Allow-Origin", origen)
                self.send_header("Access-Control-Allow-Headers", "X-Foroleer, Content-Type")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

        def _resp(self, codigo, obj=None, cuerpo_bytes=None, tipo="application/json"):
            if cuerpo_bytes is None:
                cuerpo_bytes = json.dumps(obj).encode("utf-8") if obj is not None else b""
            self.send_response(codigo)
            self._cors()
            if cuerpo_bytes:
                self.send_header("Content-Type", tipo)
            self.send_header("Content-Length", str(len(cuerpo_bytes)))
            self.end_headers()
            self.wfile.write(cuerpo_bytes)

        def _host_ok(self):
            return self.headers.get("Host", "").split(":")[0] in ("127.0.0.1", "localhost")

        def _autorizado(self):
            if self.headers.get("X-Foroleer", "") != "1" or not self._host_ok():
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
            ruta = self.path.split("?")[0]
            if ruta == "/abrir" and self._host_ok():
                with estado.lock:
                    estado.ultimo_abrir = time.time()
                return self._resp(200, cuerpo_bytes=PAGINA_ABRIR.encode("utf-8"), tipo="text/html; charset=utf-8")
            if not self._autorizado():
                return self._resp(403, {"error": "no autorizado"})
            if ruta == "/estado":
                with estado.lock:
                    ahora = time.time()
                    return self._resp(200, {
                        "foroleer": True, "version": VERSION,
                        "sondeo_hace": None if estado.ultimo_sondeo is None else ahora - estado.ultimo_sondeo,
                        "abrir_hace": None if estado.ultimo_abrir is None else ahora - estado.ultimo_abrir,
                        "ext_url": estado.ext_url, "ext_version": estado.ext_version,
                        "pendientes": len(estado.pendientes), "log_ext": estado.log_ext,
                        "en_curso": len(estado.en_curso), "servidos": estado.servidos, "pid": os.getpid(),
                        "activo_desde": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(estado.inicio)),
                        "cliente_hace": ahora - estado.ultimo_cliente})
            if ruta == "/latido":
                with estado.lock:
                    estado.ultimo_sondeo = time.time()
                return self._resp(204)
            if ruta == "/trabajo":
                with estado.lock:
                    estado.ultimo_sondeo = time.time()
                    if estado.pendientes:
                        trabajo = estado.pendientes.pop(0)
                        estado.en_curso[trabajo["id"]] = time.time()
                        return self._resp(200, trabajo)
                return self._resp(204)
            if ruta.startswith("/resultado/"):
                rid = ruta.rsplit("/", 1)[1]
                with estado.lock:
                    estado.ultimo_cliente = time.time()
                    if rid in estado.resultados:
                        res = estado.resultados.pop(rid)
                        res.pop("_t", None)
                        return self._resp(200, res)
                return self._resp(202, {"estado": "pendiente"})
            self._resp(404, {"error": "no existe"})

        def do_POST(self):
            ruta = self.path.split("?")[0]
            if not self._autorizado():
                return self._resp(403, {"error": "no autorizado"})
            try:
                datos = self._cuerpo()
            except ValueError:
                return self._resp(400, {"error": "json"})
            if ruta == "/trabajo":
                with estado.lock:
                    estado.n += 1
                    estado.ultimo_cliente = time.time()
                    datos["id"] = f"{estado.arranque}-{estado.n}"
                    estado.pendientes.append(datos)
                return self._resp(200, {"id": datos["id"]})
            if ruta == "/resultado":
                with estado.lock:
                    estado.resultados[str(datos.get("id"))] = datos
                    estado.en_curso.pop(str(datos.get("id")), None)
                    estado.servidos += 1
                    estado.ultimo_sondeo = time.time()
                    for viejo in [k for k, v in estado.resultados.items() if time.time() - v.get("_t", time.time()) > 900]:
                        estado.resultados.pop(viejo, None)
                    datos["_t"] = time.time()
                return self._resp(200, {"ok": True})
            if ruta == "/apagar":
                with estado.lock:
                    estado.apagar = True
                return self._resp(200, {"ok": True})
            if ruta == "/hola":
                with estado.lock:
                    estado.ext_url = str(datos.get("url", ""))[:300] or estado.ext_url
                    estado.ext_version = str(datos.get("version", ""))[:20] or estado.ext_version
                    estado.ultimo_sondeo = time.time()
                return self._resp(200, {"ok": True})
            if ruta == "/registro":
                nombre = str(datos.get("nombre", ""))
                if not re.fullmatch(r"[0-9]{4}(-[0-9]{2}){5}logForoleerExt\.log", nombre):
                    return self._resp(400, {"error": "nombre"})
                try:
                    # El archivo lleva en el nombre la fecha y hora de su ULTIMA escritura: se escribe con el
                    # nombre nuevo y se borra el anterior de esa misma sesion de la extension.
                    os.makedirs(LOGDIR, exist_ok=True)
                    sesion = nombre[:19]
                    ruta_log = os.path.join(LOGDIR, time.strftime("%Y-%m-%d-%H-%M-%S") + "logForoleerExt.log")
                    with open(ruta_log, "w", encoding="utf-8") as f:
                        f.write(f"(sesion de la extension iniciada el {sesion})\n")
                        f.write("\n".join(envuelve(str(datos.get("texto", "")))) + "\n")
                    with estado.lock:
                        anterior = estado.logs_ext.get(sesion)
                        estado.logs_ext[sesion] = ruta_log
                        estado.log_ext = ruta_log
                    if anterior and anterior != ruta_log:
                        try:
                            os.remove(anterior)
                        except OSError:
                            pass
                except OSError:
                    return self._resp(500, {"error": "no se pudo escribir"})
                return self._resp(200, {"ok": True})
            self._resp(404, {"error": "no existe"})

    return H


def _base(puerto):
    return f"http://127.0.0.1:{puerto}"


def _cab():
    return {"X-Foroleer": "1", "Content-Type": "application/json"}


def pide_estado(puerto, espera=4):
    req = request.Request(_base(puerto) + "/estado", headers=_cab())
    with request.urlopen(req, timeout=espera) as r:
        return json.loads(r.read().decode("utf-8"))


def quien_ocupa(puerto):
    """Si alguien responde por HTTP en el puerto, devuelve quien (empsui u otro servidor); si nadie, ''."""
    try:
        req = request.Request(_base(puerto) + "/estado", headers={"X-Empsui": "1"})
        with request.urlopen(req, timeout=2) as r:
            try:
                if json.loads(r.read().decode("utf-8")).get("empsui"):
                    return "empsui"
            except ValueError:
                pass
            return "otro servidor local (probablemente un empsui antiguo)"
    except error.HTTPError:
        return "otro servidor local (probablemente empsui)"
    except Exception:
        return ""


class Puente:
    """Arranca (o reutiliza) el servidor local y despierta la extension."""

    def __init__(self, puerto):
        self.puerto = puerto
        self.srv = None
        self.estado = None
        self.propio = False

    def arranca(self, en_proceso=False):
        """Reutiliza el servidor del puente si ya existe; si no, lo lanza como proceso APARTE y compartido (sigue vivo
        aunque termine esta busqueda, para que varias busquedas a la vez o seguidas no se queden sin puente)."""
        try:
            est = pide_estado(self.puerto, 2)
            if est.get("foroleer"):
                if est.get("version") == VERSION or en_proceso:
                    out(f"Puente: reutilizo el servidor foroleer que ya esta activo en el puerto {self.puerto}"
                        + (f" (desde {est['activo_desde']}, {est.get('servidos', 0)} trabajos servidos)."
                           if est.get("activo_desde") else "."))
                    DIAG.puente["servidor"] = "reutilizado" + (f" (pid {est['pid']})" if est.get("pid") else "")
                    return
                out(f"Puente: hay un servidor foroleer de otra version ({est.get('version')}); le pido que se cierre.")
                try:
                    req = request.Request(_base(self.puerto) + "/apagar", data=b"{}", headers=_cab(), method="POST")
                    request.urlopen(req, timeout=3).read()
                except Exception:
                    pass
                for _ in range(12):
                    time.sleep(0.5)
                    try:
                        pide_estado(self.puerto, 1)
                    except Exception:
                        break
                else:
                    out("  (no se cerro: es el servidor de una busqueda antigua en marcha; lo reutilizo)")
                    DIAG.puente["servidor"] = f"reutilizado (version {est.get('version')})"
                    return
        except Exception:
            pass
        otro = quien_ocupa(self.puerto)
        if otro:
            DIAG.puente["servidor"] = f"puerto ocupado por {otro}"
            raise ErrorPuente(f"El puerto {self.puerto} lo usa {otro}, no foroleer. Cierralo (Ctrl+C en su sesion de "
                              f"Termux, o 'pkill -f empsui.py') o usa --puerto OTRO (el mismo en la extension).")
        if not en_proceso and not os.environ.get("FOROLEER_SERVIDOR_EN_PROCESO") and self._lanza_aparte():
            return
        self.estado = EstadoPuente()
        try:
            self.srv = ThreadingHTTPServer(("127.0.0.1", self.puerto), crear_handler(self.estado))
        except OSError as e:
            DIAG.puente["servidor"] = f"fallo: {e}"
            raise ErrorPuente(f"No se pudo abrir el puerto {self.puerto} (otro programa lo usa?): {e}. "
                              f"Prueba con --puerto OTRO (el mismo numero hay que ponerlo en la extension).")
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.propio = True
        DIAG.puente["servidor"] = "propio (hilo en este proceso)"
        out(f"Puente: servidor local en 127.0.0.1:{self.puerto} (solo este telefono).")

    def _lanza_aparte(self):
        """Lanza 'foroleer.py servidor' en segundo plano, separado de este proceso. Devuelve True si responde."""
        try:
            os.makedirs(LOGDIR, exist_ok=True)
            cmd = [sys.executable, os.path.abspath(__file__), "servidor", "--puerto", str(self.puerto),
                   "--auto-cierre", str(AUTO_CIERRE_MIN)]
            with open(os.devnull, "rb") as nulo_in, open(os.devnull, "ab") as nulo_out:
                subprocess.Popen(cmd, stdin=nulo_in, stdout=nulo_out, stderr=nulo_out, start_new_session=True,
                                 cwd=DIR if os.path.isdir(DIR) else None)
        except Exception as e:
            dbg(f"no se pudo lanzar el servidor aparte ({e}); uso un servidor dentro de este proceso")
            return False
        for _ in range(20):
            time.sleep(0.3)
            try:
                est = pide_estado(self.puerto, 1)
                if est.get("foroleer"):
                    out(f"Puente: servidor compartido lanzado en 127.0.0.1:{self.puerto} (proceso {est.get('pid')}; "
                        f"se apaga solo tras {AUTO_CIERRE_MIN} min sin uso).")
                    DIAG.puente["servidor"] = f"compartido, lanzado ahora (pid {est.get('pid')})"
                    return True
            except Exception:
                pass
        dbg("el servidor aparte no respondio en 6 s; uso un servidor dentro de este proceso")
        return False

    def estado_actual(self):
        try:
            return pide_estado(self.puerto, 3)
        except Exception:
            return {}

    def para(self):
        if self.srv:
            try:
                self.srv.shutdown()
                self.srv.server_close()
            except Exception:
                pass


def espera_sondeo(puerto, segundos):
    fin = time.time() + segundos
    while True:
        try:
            est = pide_estado(puerto, 2)
            h = est.get("sondeo_hace")
            if h is not None and h < 10:
                return True
        except Exception:
            pass
        if time.time() >= fin:
            return False
        time.sleep(0.5)


PAQUETES_FIREFOX = ["org.mozilla.fenix", "org.mozilla.firefox", "org.mozilla.firefox_beta"]


def paquetes_firefox():
    forzado = os.environ.get("FOROLEER_FIREFOX")
    if forzado:
        return [p.strip() for p in forzado.split(",") if p.strip()]
    instalados = []
    if shutil.which("pm"):
        try:
            r = subprocess.run(["pm", "list", "packages"], capture_output=True, text=True, timeout=20)
            for linea in r.stdout.splitlines():
                p = linea.replace("package:", "").strip()
                if p in PAQUETES_FIREFOX:
                    instalados.append(p)
            dbg(f"pm list packages: {len(r.stdout.splitlines())} paquetes; Firefox: {instalados or 'ninguno visible'}"
                + (f"; stderr: {r.stderr.strip()[:150]}" if r.stderr.strip() else ""))
        except Exception as e:
            dbg(f"pm list packages fallo: {e}")
    orden = [p for p in PAQUETES_FIREFOX if p in instalados]
    return orden or PAQUETES_FIREFOX


def lanza_am(url, paquete=None):
    if not shutil.which("am"):
        dbg("el comando 'am' no existe (no es Termux?)")
        return False, "no hay comando am"
    cmd = ["am", "start", "-a", "android.intent.action.VIEW", "-d", url]
    if paquete:
        cmd += ["-p", paquete]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
        salida = (r.stdout + " " + r.stderr).strip().replace("\n", " | ")
        dbg(f"am start {paquete or '(navegador por defecto)'} -> codigo {r.returncode}: {salida[:300]}")
        malo = r.returncode != 0 or "Error" in salida or "unable to resolve" in salida
        return (not malo), salida
    except Exception as e:
        dbg(f"am start fallo: {e}")
        return False, str(e)


def despierta_extension(puerto):
    """Devuelve True si la extension esta sondeando el servidor."""
    if espera_sondeo(puerto, 8):
        out("Extension: conectada.")
        DIAG.puente["extension"] = "conectada sin necesidad de despertarla"
        return True
    url = f"{_base(puerto)}/abrir"
    out("Extension: sin senales; intento abrir Firefox para despertarla...")
    if os.environ.get("FOROLEER_NO_ABRIR"):
        out("  (FOROLEER_NO_ABRIR activo: no lanzo 'am start')")
    else:
        for pkg in paquetes_firefox():
            ok, salida = lanza_am(url, pkg)
            out(f"  am start en {pkg}: " + ("lanzado" if ok else f"fallo ({salida[:120]})"))
            if ok and espera_sondeo(puerto, 15):
                out("Extension: despertada y conectada.")
                DIAG.puente["extension"] = f"despertada con am start ({pkg})"
                return True
            try:
                est = pide_estado(puerto, 2)
                if est.get("abrir_hace") is not None and est["abrir_hace"] < 40:
                    DIAG.puente["abrir_visto"] = True
                    err("  Firefox abrio la pagina de despertar, pero la extension foroleer no respondio: "
                        "esta instalada y activa?")
                    break
            except Exception:
                pass
        else:
            if shutil.which("termux-open-url"):
                dbg("pruebo termux-open-url")
                try:
                    subprocess.run(["termux-open-url", url], timeout=20, capture_output=True)
                except Exception as e:
                    dbg(f"termux-open-url fallo: {e}")
                if espera_sondeo(puerto, 15):
                    out("Extension: despertada con termux-open-url.")
                    DIAG.puente["extension"] = "despertada con termux-open-url"
                    return True
    DIAG.puente["extension"] = "sin respuesta"
    return False


def pedir_trabajo(params, puerto, espera=180):
    """Deja un trabajo para la extension y devuelve su respuesta. Si el servidor del puente desaparece a mitad
    (lo cerro otra sesion), lo vuelve a levantar y repite el trabajo una vez."""
    for intento in (1, 2):
        try:
            return _pedir_trabajo(params, puerto, espera)
        except ErrorServidorCaido as e:
            if intento == 2:
                raise ErrorPuente(str(e))
            err(f"  el servidor del puente dejo de responder ({e}); lo levanto de nuevo y repito el trabajo")
            Puente(puerto).arranca()
            despierta_extension(puerto)


class ErrorServidorCaido(Exception):
    pass


def _pedir_trabajo(params, puerto, espera):
    try:
        req = request.Request(_base(puerto) + "/trabajo", data=json.dumps(params).encode("utf-8"),
                              headers=_cab(), method="POST")
        with request.urlopen(req, timeout=10) as r:
            rid = json.loads(r.read().decode("utf-8"))["id"]
    except (error.URLError, OSError) as e:
        raise ErrorServidorCaido(f"no hay servidor del puente en el puerto {puerto}: {e}")
    t0 = time.time()
    ultimo_aviso = 0
    fallos = 0
    while time.time() - t0 < espera:
        try:
            req = request.Request(f"{_base(puerto)}/resultado/{rid}", headers=_cab())
            with request.urlopen(req, timeout=10) as r:
                fallos = 0
                if r.status == 200:
                    return json.loads(r.read().decode("utf-8"))
        except (error.URLError, OSError) as e:
            fallos += 1
            if fallos >= 3:
                raise ErrorServidorCaido(f"se perdio la conexion con el servidor del puente: {e}")
        trans = time.time() - t0
        if trans - ultimo_aviso >= 15:
            ultimo_aviso = trans
            h = None
            try:
                h = pide_estado(puerto, 3).get("sondeo_hace")
            except Exception:
                pass
            out(f"  esperando a la extension ({int(trans)}s; ultima senal hace "
                + ("?" if h is None else f"{h:.0f}s") + ")...")
            if h is not None and h > 45 and trans > 45:
                dbg("la extension lleva mas de 45 s sin dar senales: intento despertarla")
                despierta_extension(puerto)
        time.sleep(1.0)
    raise ErrorPuente(f"La extension no respondio en {espera}s (trabajo {rid}). Si Firefox estaba en segundo plano, "
                      f"Android pudo congelarlo: abre Firefox, vuelve a Termux y repite; y ejecuta "
                      f"'python3 foroleer.py reporte' para enviar los registros.")


class Red:
    """Hace las peticiones: por la extension (Firefox) o directamente desde Termux."""

    def __init__(self, via, puerto):
        self.pedido = via
        self.puerto = puerto
        self.modo = "directo" if via == "directo" else "extension"
        self.puente = None
        jar = http.cookiejar.CookieJar()
        self.opener = request.build_opener(request.HTTPCookieProcessor(jar))
        self.n = 0

    def prepara(self):
        DIAG.puente["modo_pedido"] = self.pedido
        if self.pedido == "directo":
            out("Modo directo: las peticiones salen desde Termux (Reddit suele bloquearlas).")
            self.modo = "directo"
            return
        self.puente = Puente(self.puerto)
        self.puente.arranca()
        if despierta_extension(self.puerto):
            self.modo = "extension"
            self.info_extension()
            return
        if self.pedido == "extension":
            raise ErrorPuente("La extension no responde. Abre Firefox, comprueba que foroleer esta instalada y que "
                              "la casilla 'Conectar con Termux' esta marcada (ver foroleerManualBono.txt, apartado "
                              "'Si algo falla'), o ejecuta: python3 foroleer.py diagnostico")
        err("La extension no responde: sigo en modo directo desde Termux (Reddit probablemente lo bloqueara).")
        self.modo = "directo"
        DIAG.puente["modo_final"] = "directo (la extension no respondio)"

    def info_extension(self):
        try:
            r = pedir_trabajo({"op": "info"}, self.puerto, 60)
            if r.get("error"):
                dbg(f"info de la extension con error: {r['error']}")
                return
            info = json.loads(r.get("text") or "{}")
            DIAG.ext_info = info
            out(f"Extension: version {info.get('version', '?')}; pestanas de foros abiertas: "
                + (", ".join(f"{h} x{n}" for h, n in info.get("tabs", {}).items()) or "ninguna (abrira las que necesite)"))
        except Exception as e:
            dbg(f"no se pudo pedir la info de la extension: {e}")

    def get(self, url, cab=None, datos=None, metodo=None, etiqueta=""):
        metodo = metodo or ("POST" if datos is not None else "GET")
        self.n += 1
        modo = self.modo
        dbg(f"PETICION {self.n}: {metodo} {url} (modo {modo})")
        t0 = time.time()
        resp = None
        if modo == "extension":
            try:
                resp = self._ext(url, cab, datos, metodo)
            except ErrorPuente as e:
                if self.pedido == "extension":
                    raise
                err(f"  la extension fallo ({e}); pruebo directo desde Termux")
                resp = self._directo(url, cab, datos, metodo)
        else:
            resp = self._directo(url, cab, datos, metodo)
        self._registra(resp, metodo, url, time.time() - t0, etiqueta)
        return resp

    def _registra(self, resp, metodo, url, dt, etiqueta):
        titulo = titulo_html(resp.text)[:70]
        dbg(f"RESPUESTA {self.n}: estado {resp.status}, {len(resp.text)} car., clase {resp.clase}, via {resp.via}, "
            f"{dt:.1f}s" + (f", titulo {titulo!r}" if titulo else ""))
        muestra = texto_visible(resp.text, 20000)[:500] if resp.clase != "json" else resp.text[:300]
        ruta_resp = ""
        if resp.clase in CLASES_MALAS or (resp.status or 0) >= 400:
            ruta_resp = DIAG.guarda_respuesta(resp, resp.clase)
            dbg(f"  respuesta guardada en {ruta_resp}")
        DIAG.peticiones.append({"n": self.n, "metodo": metodo, "url": url, "via": resp.via, "status": resp.status,
                                "bytes": len(resp.text), "clase": resp.clase, "titulo": titulo, "muestra": muestra,
                                "seg": round(dt, 1), "archivo": ruta_resp, "etiqueta": etiqueta})

    def pagina(self, url, esperar=4000, etiqueta="", desplazar=0):
        """Abre la direccion en una pestana en segundo plano de Firefox, espera a que se pinte y devuelve su HTML."""
        if self.modo != "extension":
            raise ErrorWeb("Leer paginas pintadas necesita la extension (--via extension).")
        self.n += 1
        dbg(f"PETICION {self.n}: PAGINA {url} (espera {esperar} ms)")
        t0 = time.time()
        try:
            res = pedir_trabajo({"op": "pagina", "url": url, "esperar": esperar, "desplazar": desplazar,
                           "expande": bool(desplazar)}, self.puerto, 300)
        except ErrorPuente as e:
            raise ErrorWeb(str(e))
        if res.get("error"):
            raise ErrorWeb("la extension informo: " + str(res["error"]))
        resp = Resp(res.get("status"), res.get("text", ""), url, "extension/pagina", "text/html")
        self._registra(resp, "PAGINA", url, time.time() - t0, etiqueta)
        return resp

    def _ext(self, url, cab, datos, metodo):
        job = {"op": "fetch", "url": url, "metodo": metodo, "cabeceras": dict(cab or {}), "modo": "auto"}
        if datos is not None:
            job["cuerpo"] = datos.decode("utf-8") if isinstance(datos, bytes) else str(datos)
        res = pedir_trabajo(job, self.puerto)
        if res.get("error"):
            raise ErrorPuente("la extension informo: " + str(res["error"]))
        via = "extension/" + str(res.get("via", "?"))
        if res.get("aviso"):
            dbg(f"  aviso de la extension: {res['aviso']}")
        return Resp(res.get("status"), res.get("text", ""), res.get("url", url), via, res.get("tipo", ""))

    def _directo(self, url, cab, datos, metodo):
        h = {"User-Agent": UA_NAVEGADOR, "Accept": "*/*", "Accept-Language": "es-ES,es;q=0.9,en;q=0.8"}
        h.update(cab or {})
        req = request.Request(url, data=datos, headers=h, method=metodo)
        try:
            with self.opener.open(req, timeout=TIMEOUT) as r:
                raw = r.read()
                tipo = r.headers.get("Content-Type", "")
                return Resp(r.status, decodifica(raw, tipo), r.geturl(), "directo", tipo)
        except error.HTTPError as e:
            raw = e.read() or b""
            tipo = e.headers.get("Content-Type", "") if e.headers else ""
            return Resp(e.code, decodifica(raw, tipo), url, "directo", tipo)
        except error.URLError as e:
            raise ErrorWeb(f"No se pudo conectar con {url}: {e.reason}")
        except OSError as e:
            raise ErrorWeb(f"Error de red con {url}: {e}")

    def cierra(self):
        if self.puente:
            time.sleep(1.2)  # deja que la extension envie su registro
            est = self.puente.estado_actual()
            if est.get("log_ext"):
                DIAG.ext_log_ruta = est["log_ext"]
            self.puente.para()


FRASES_MURO = [
    "inicia sesion para seguir leyendo", "inicia sesion para ver",
    "debes iniciar sesion", "debes estar registrado",
    "regístrate para continuar", "registrate para continuar",
    "contenido solo para miembros", "contenido exclusivo para suscriptores",
    "esta respuesta es solo para", "necesitas una cuenta para",
    "sign up to continue reading", "sign up to see", "sign in to continue",
    "continue reading this answer", "log in to see", "log in to view",
    "you must be logged in", "you must be registered",
    "this content is for members only", "members only content",
    "subscribe to continue reading", "subscribe to read",
    "please register or login", "please log in or sign up",
    "answer is hidden", "to view this answer",
]


def detecta_muro(texto):
    t = normaliza(texto)
    for frase in FRASES_MURO:
        if normaliza(frase) in t:
            return frase
    return None


# ---------------------------------------------------------------------------
# Conversion generica de HTML a texto plano (misma logica que htmlToText en
# la extension de Firefox: mantiene parrafos, listas, citas y enlaces
# legibles en vez de aplanarlo todo a una sola linea).
# ---------------------------------------------------------------------------

BLOQUE = {
    "p", "div", "br", "li", "ul", "ol", "blockquote", "h1", "h2", "h3",
    "h4", "h5", "h6", "tr", "table", "section", "article", "pre",
}


class ConversorTexto(HTMLParser):
    """Convierte un fragmento de HTML (el 'cooked' de un post, por ejemplo)
    en texto plano legible, preservando salto de parrafo y marcando listas,
    citas e imagenes de forma similar a como lo haria un lector humano."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.partes = []
        self.en_a = False
        self.href_actual = None
        self.texto_a = []
        self.saltar = 0  # dentro de <script>/<style>

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("script", "style"):
            self.saltar += 1
            return
        if tag == "br":
            self.partes.append("\n")
        elif tag in BLOQUE:
            self.partes.append("\n")
        if tag == "li":
            self.partes.append("- ")
        if tag == "blockquote":
            self.partes.append("[cita] ")
        if tag == "img":
            alt = attrs.get("alt") or attrs.get("title") or ""
            self.partes.append(f"[imagen: {alt}] " if alt else "[imagen] ")
        if tag == "a":
            self.en_a = True
            self.href_actual = attrs.get("href")
            self.texto_a = []

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.saltar = max(0, self.saltar - 1)
            return
        if tag == "a" and self.en_a:
            texto = "".join(self.texto_a).strip()
            if self.href_actual and texto and self.href_actual not in texto:
                self.partes.append(f"{texto} ({self.href_actual})")
            else:
                self.partes.append(texto)
            self.en_a = False
            self.href_actual = None
            self.texto_a = []
        if tag in BLOQUE:
            self.partes.append("\n")

    def handle_data(self, data):
        if self.saltar:
            return
        if self.en_a:
            self.texto_a.append(data)
        else:
            self.partes.append(data)

    def texto(self):
        t = "".join(self.partes)
        t = html.unescape(t)
        t = re.sub(r"[ \t]+", " ", t)
        t = re.sub(r"[ \t]*\n[ \t]*", "\n", t)
        t = re.sub(r"\n{3,}", "\n\n", t)
        return t.strip()


def html_a_texto(fragmento_html):
    c = ConversorTexto()
    try:
        c.feed(fragmento_html or "")
    except Exception:
        pass
    return c.texto()


def quitar_etiquetas_simple(contenido_html):
    """Extraccion generica de texto para paginas completas (quita head,
    script y style, convierte el resto). Se usa cuando no se reconoce la
    estructura especifica del sitio."""
    c = re.sub(r"<head[\s\S]*?</head>", " ", contenido_html, flags=re.I)
    return html_a_texto(c)


def swissforum_archivo_a_texto(contenido_html):
    """Analiza una pagina de swissforum.co.uk guardada con SingleFile.
    Estructura real confirmada (8-oct-2026) sobre paginas de ejemplo:
      <article id=post_N aria-label="post #N by @usuario" ...>
        ... <span class=relative-date title="8 Sep 2010 08:54" ...>...
        ... <div class=cooked> CONTENIDO </div>
    """
    titulo_m = re.search(r"<title>([^<]*)</title>", contenido_html, re.I)
    titulo = html.unescape(titulo_m.group(1)).strip() if titulo_m else "(sin titulo)"
    posts = []
    for art_m in re.finditer(r'<article\b[^>]*\bid=["\']?post_\d+["\']?[^>]*>', contenido_html, re.I):
        inicio = art_m.start()
        # el bloque del post llega hasta el siguiente <article id=post_ o
        # hasta un cierre razonable; usamos un corte generoso y controlado.
        siguiente = contenido_html.find("<article", art_m.end())
        bloque = contenido_html[inicio: siguiente if siguiente != -1 else inicio + 20000]
        etiqueta = art_m.group(0)
        aria_m = re.search(r'aria-label=["\']post #(\d+) by @([^"\']+)["\']', etiqueta, re.I)
        numero = aria_m.group(1) if aria_m else "?"
        autor = aria_m.group(2) if aria_m else "(autor desconocido)"
        # el orden de los atributos (title antes o despues de class) varia,
        # asi que primero se aisla la etiqueta <span> completa y luego se
        # busca title dentro de ella, en vez de asumir un orden fijo.
        span_m = re.search(r'<span\b[^>]*\bclass="?relative-date"?[^>]*>', bloque, re.I)
        fecha = "(fecha desconocida)"
        if span_m:
            title_m = re.search(r'title="([^"]+)"', span_m.group(0))
            if title_m:
                fecha = html.unescape(title_m.group(1))
        cooked_m = re.search(r'<div class="?cooked"?>([\s\S]*?)</div>\s*(?:<div class="?cooked-selection-barrier)',
                              bloque, re.I)
        if not cooked_m:
            # variante sin el div cooked-selection-barrier al final
            cooked_m = re.search(r'<div class="?cooked"?>([\s\S]*?)</div>\s*</div>\s*<section',
                                  bloque, re.I)
        cuerpo = html_a_texto(cooked_m.group(1)) if cooked_m else "(no se pudo extraer el cuerpo del post)"
        posts.append({"numero": numero, "autor": autor, "fecha": fecha, "texto": cuerpo})
    muro = detecta_muro(quitar_etiquetas_simple(contenido_html)[:4000])
    return {"titulo": titulo, "posts": posts, "muro": muro}





# ---------------------------------------------------------------------------
# REDDIT (sin JSON: Firefox pinta la pagina y se lee el HTML; un .txt por hilo)
# ---------------------------------------------------------------------------

def parse_attrs(etiqueta):
    d = {}
    for m in re.finditer(r"""([\w:-]+)=(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", etiqueta):
        d[m.group(1).lower()] = html.unescape(m.group(2) if m.group(2) is not None else (m.group(3) or m.group(4) or ""))
    return d


def elemento_interior(texto, pos, tag):
    """HTML interior del elemento <tag> cuya etiqueta de apertura termina en `pos` (cuenta anidamiento)."""
    nivel = 1
    for m in re.finditer(r"<(/?)" + re.escape(tag) + r"(?=[\s>/])[^>]*>", texto[pos:]):
        if m.group(1):
            nivel -= 1
            if nivel == 0:
                return texto[pos:pos + m.start()]
        elif not m.group(0).endswith("/>"):
            nivel += 1
    return texto[pos:]


def fecha_corta(valor):
    """'2026-10-30T23:05:35.781000+0000' o epoch (s o ms) -> '2026-10-30 23:05 UTC'."""
    if valor in (None, ""):
        return ""
    s = str(valor).strip()
    m = re.match(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})", s)
    if m:
        return f"{m.group(1)} {m.group(2)} UTC"
    try:
        n = float(s)
        if n > 1e11:
            n /= 1000.0
        return time.strftime("%Y-%m-%d %H:%M", time.gmtime(n)) + " UTC"
    except ValueError:
        return s


def a_entero(valor):
    try:
        return int(float(str(valor).replace(",", "")))
    except (TypeError, ValueError):
        return None


def reddit_ruta_hilo(url):
    m = re.search(r"/r/([^/]+)/comments/(\w+)", url)
    return (m.group(1), m.group(2)) if m else (None, None)


# ---- listas de resultados --------------------------------------------------

def reddit_www_busqueda(texto):
    """Pagina de busqueda de www.reddit.com ya pintada. Cada resultado es un bloque data-testid=search-sdui-post;
    el titulo, autor y subreddit salen del JSON de seguimiento del bloque y las cifras de faceplate-number."""
    res, vistos = [], set()
    marcas = [m.start() for m in re.finditer(r"data-testid=[\"']?search-sdui-post[\"']?[\s>]", texto)]
    marcas.append(len(texto))
    for i in range(len(marcas) - 1):
        bloque = texto[marcas[i]:marcas[i + 1]]
        ma = re.search(r"<a\b[^>]*data-testid=[\"']?post-title[\"']?[^>]*>", bloque)
        if not ma:
            continue
        a = parse_attrs(ma.group(0))
        href = a.get("href", "")
        sub, id_ = reddit_ruta_hilo(href)
        if not id_ or id_ in vistos:
            continue
        vistos.add(id_)
        ctx = {}
        mc = re.search(r"data-faceplate-tracking-context=(?:'([^']*)'|\"([^\"]*)\")", bloque)
        if mc:
            try:
                ctx = json.loads(html.unescape(mc.group(1) or mc.group(2)))
            except ValueError:
                ctx = {}
        nums = re.findall(r"<faceplate-number\b[^>]*\bnumber=[\"']?([\d.,]+)", bloque)
        ts = re.search(r"<faceplate-timeago\b[^>]*\bts=[\"']?([^\s\"'>]+)", bloque)
        res.append({
            "id": id_, "subreddit": (ctx.get("subreddit") or {}).get("name") or sub,
            "url": href if href.startswith("http") else "https://www.reddit.com" + href,
            "titulo": (ctx.get("post") or {}).get("title") or a.get("aria-label") or id_,
            "autor": (ctx.get("profile") or {}).get("name"),
            "fecha": fecha_corta(ts.group(1)) if ts else "",
            "puntos": a_entero(nums[0]) if nums else None,
            "num_comentarios": a_entero(nums[1]) if len(nums) > 1 else None,
        })
    if not res:   # estructura distinta: al menos los enlaces a hilos
        for m in re.finditer(r"<a\b[^>]*href=[\"']?([^\s\"'>]*/r/[^/\s\"'>]+/comments/\w+[^\s\"'>]*)[^>]*>", texto):
            href = html.unescape(m.group(1))
            sub, id_ = reddit_ruta_hilo(href)
            if id_ and id_ not in vistos:
                vistos.add(id_)
                res.append({"id": id_, "subreddit": sub, "titulo": id_,
                            "url": href if href.startswith("http") else "https://www.reddit.com" + href})
    return res


def reddit_old_busqueda(texto):
    """Busqueda de old.reddit.com (HTML del servidor)."""
    res, vistos = [], set()
    for m in re.finditer(r"<a\b[^>]*class=[\"']?[^\"'>]*search-title[^>]*>", texto):
        a = parse_attrs(m.group(0))
        href = a.get("href", "")
        sub, id_ = reddit_ruta_hilo(href)
        if not id_ or id_ in vistos:
            continue
        vistos.add(id_)
        fin = texto.find("</a>", m.end())
        titulo = html_a_texto(texto[m.end():fin]) if fin > 0 else id_
        url = href if href.startswith("http") else "https://old.reddit.com" + href
        res.append({"id": id_, "subreddit": sub, "titulo": titulo or id_,
                    "url": url.replace("//old.reddit.com", "//www.reddit.com")})
    return res


# ---- lectura de un hilo ----------------------------------------------------

def reddit_www_hilo(texto):
    """Hilo de www.reddit.com pintado: <shreddit-post> y <shreddit-comment>. Las etiquetas se buscan con nombre exacto
    (shreddit-comment-tree, shreddit-comment-action-row... NO son comentarios) y el cuerpo es el div slot=comment
    (no slot=commentAvatar)."""
    info = {"autor": None, "titulo": None, "subreddit": None, "fecha": "", "puntos": None, "num_comentarios": None,
            "selftext": "", "comentarios": []}
    mp = re.search(r"<shreddit-post(?=[\s>])[^>]*>", texto)
    if mp:
        a = parse_attrs(mp.group(0))
        info.update(autor=a.get("author"), titulo=a.get("post-title"),
                    subreddit=(a.get("subreddit-name") or a.get("subreddit-prefixed-name", "")).replace("r/", "") or None,
                    fecha=fecha_corta(a.get("created-timestamp") or a.get("created")),
                    puntos=a_entero(a.get("score")), num_comentarios=a_entero(a.get("comment-count")))
        resto = texto[mp.end():]
        mt = re.search(r"<div\b[^>]*\bslot=(?:\"text-body\"|'text-body'|text-body(?=[\s>]))[^>]*>", resto)
        if mt:
            info["selftext"] = html_a_texto(elemento_interior(resto, mt.end(), "div"))
    marcas = list(re.finditer(r"<shreddit-comment(?=[\s>])[^>]*>", texto))
    vistos = set()
    for i, m in enumerate(marcas):
        a = parse_attrs(m.group(0))
        clave = a.get("thingid") or a.get("permalink") or str(m.start())
        if clave in vistos:
            continue
        vistos.add(clave)
        fin = marcas[i + 1].start() if i + 1 < len(marcas) else len(texto)
        trozo = texto[m.end():fin]
        mb = re.search(r"<div\b[^>]*\bslot=(?:\"comment\"|'comment'|comment(?=[\s>]))[^>]*>", trozo)
        cuerpo = html_a_texto(elemento_interior(trozo, mb.end(), "div")) if mb else ""
        autor = a.get("author") or ""
        if not cuerpo.strip() or cuerpo.strip() in ("[deleted]", "[removed]"):
            continue
        info["comentarios"].append({
            "autor": autor or "[eliminado]", "fecha": fecha_corta(a.get("created") or a.get("created-timestamp")),
            "profundidad": a_entero(a.get("depth")) or 0, "puntos": a_entero(a.get("score")), "texto": cuerpo})
    return info


class _ParserOld(HTMLParser):
    """old.reddit.com: div.thing.link (el hilo) y div.thing.comment anidados. Profundidad = comentarios que lo contienen."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.pila = []          # (tag, tipo) donde tipo: 'link', 'comment', 'md', ''
        self.hilo = {}
        self.comentarios = []
        self.actual = None      # comentario o hilo al que pertenece el siguiente div.md
        self.cap = None         # [tipo, buffer] mientras se captura un cuerpo
        self.cap_nivel = 0
        self.prof = 0
        self.en_tagline = None

    def _raw(self):
        return self.get_starttag_text()

    def handle_starttag(self, tag, attrs):
        at = dict(attrs)
        cls = (at.get("class") or "").split()
        tipo = ""
        if tag == "div" and "thing" in cls and ("comment" in cls or "link" in cls):
            tipo = "comment" if "comment" in cls else "link"
            d = {"autor": at.get("data-author") or "", "fecha": fecha_corta(at.get("data-timestamp")),
                 "puntos": a_entero(at.get("data-score")), "profundidad": self.prof, "texto": "",
                 "subreddit": at.get("data-subreddit"), "num_comentarios": a_entero(at.get("data-comments-count"))}
            if tipo == "comment":
                self.prof += 1
                self.comentarios.append(d)
            else:
                self.hilo = d
            self.actual = d
        elif tag == "div" and "md" in cls and self.actual is not None and self.cap is None and not self.actual["texto"]:
            tipo = "md"
            self.cap = []
            self.cap_nivel = 1
            self.pila.append((tag, tipo))
            return
        elif tag == "a" and "title" in cls and self.hilo is not None and not self.hilo.get("titulo"):
            self.en_tagline = "titulo"
            self.hilo["titulo"] = ""
        if self.cap is not None:
            self.cap.append(self._raw())
            if tag == "div":
                self.cap_nivel += 1
            return
        self.pila.append((tag, tipo))

    def handle_endtag(self, tag):
        if self.cap is not None:
            if tag == "div":
                self.cap_nivel -= 1
                if self.cap_nivel == 0:
                    self.actual["texto"] = html_a_texto("".join(self.cap))
                    self.cap = None
                    self.pila.pop()
                    return
            self.cap.append(f"</{tag}>")
            return
        if tag == "a" and self.en_tagline == "titulo":
            self.en_tagline = None
        for k in range(len(self.pila) - 1, -1, -1):
            if self.pila[k][0] == tag:
                for t in self.pila[k:]:
                    if t[1] == "comment":
                        self.prof = max(0, self.prof - 1)
                del self.pila[k:]
                break

    def handle_data(self, data):
        if self.cap is not None:
            self.cap.append(html.escape(data))
        elif self.en_tagline == "titulo":
            self.hilo["titulo"] = (self.hilo.get("titulo") or "") + data


def reddit_old_hilo(texto):
    p = _ParserOld()
    try:
        p.feed(texto)
    except Exception:
        pass
    h = p.hilo or {}
    coms = [c for c in p.comentarios if c["texto"].strip() and c["texto"].strip() not in ("[deleted]", "[removed]")]
    for c in coms:
        c["autor"] = c["autor"] or "[eliminado]"
    return {"autor": h.get("autor"), "titulo": (h.get("titulo") or "").strip() or None, "subreddit": h.get("subreddit"),
            "fecha": h.get("fecha", ""), "puntos": h.get("puntos"), "num_comentarios": h.get("num_comentarios"),
            "selftext": h.get("texto", ""), "comentarios": coms}


# ---- busqueda y lectura con Firefox ---------------------------------------

def reddit_url_busqueda(web, query, subreddit, sort, tiempo, exacta):
    q = f'"{query}"' if exacta and " " in query and '"' not in query else query
    host = "old.reddit.com" if web == "old" else "www.reddit.com"
    if web == "old":
        base = f"https://{host}/r/{parse.quote(subreddit)}/search" if subreddit else f"https://{host}/search"
        p = f"?q={parse.quote(q)}&sort={sort}&t={tiempo or 'all'}" + ("&restrict_sr=on" if subreddit else "")
        return base + p
    base = f"https://{host}/r/{parse.quote(subreddit)}/search/" if subreddit else f"https://{host}/search/"
    return base + f"?q={parse.quote(q)}&sort={sort}" + (f"&t={tiempo}" if tiempo else "") + ("&restrict_sr=1" if subreddit else "")


def reddit_lista(red, query, subreddit, sort, tiempo, exacta, web, maximo):
    """Devuelve la lista de hilos. Prueba la web elegida y, si no reconoce nada, la otra."""
    orden = ["old", "www"] if web == "old" else ["www", "old"]
    motivos = []
    for w in orden:
        url = reddit_url_busqueda(w, query, subreddit, sort, tiempo, exacta)
        try:
            r = red.pagina(url, 6000 if w == "www" else 3000, f"reddit-busqueda-{w}")
        except ErrorWeb as e:
            motivos.append(f"{w}: {e}")
            continue
        hilos = (reddit_www_busqueda(r.text) if w == "www" else reddit_old_busqueda(r.text))
        if subreddit:   # la busqueda dentro de un subreddit puede colar resultados de otros: se descartan
            hilos = [h for h in hilos if (h.get("subreddit") or "").lower() == subreddit.lower()] or hilos
        if hilos:
            out(f"[reddit] pagina de busqueda ({w}.reddit.com): {len(hilos)} hilo(s)")
            return hilos[:maximo]
        motivos.append(f"{w}: sin resultados reconocibles (clase {r.clase}, titulo {titulo_html(r.text)[:50]!r})")
        DIAG.guarda_respuesta(r, "reddit-busqueda-sin-resultados")
    raise ErrorWeb("Reddit no dio resultados: " + "; ".join(motivos))


def reddit_leer_hilo(red, h, max_comentarios, web, desplazar):
    """Lee un hilo. Devuelve el item listo para escribir. Prueba la web elegida y la otra si no hay texto."""
    sub, id_ = reddit_ruta_hilo(h["url"])
    # Los hilos se leen primero en old.reddit.com: www.reddit.com suele responder con el aviso de bloqueo y ademas
    # carga anuncios que abren el cuadro "Permitir que se abra este sitio?" de Firefox y dejan la pestana parada.
    orden = ["www", "old"] if web == "www" else ["old", "www"]
    mejor = None
    for w in orden:
        if w == "www":
            url = f"https://www.reddit.com/r/{sub}/comments/{id_}/"
        else:
            url = f"https://old.reddit.com/r/{sub}/comments/{id_}/?limit=500&sort=top"
        try:
            r = red.pagina(url, 5000 if w == "www" else 3000, f"reddit-hilo-{w}", desplazar if w == "www" else 0)
        except ErrorWeb as e:
            err(f"    ({w}.reddit.com no se pudo leer: {e})")
            continue
        d = reddit_www_hilo(r.text) if w == "www" else reddit_old_hilo(r.text)
        n = len(d["comentarios"])
        if mejor is None or n + (1 if d["selftext"] else 0) > len(mejor[0]["comentarios"]) + (1 if mejor[0]["selftext"] else 0):
            mejor = (d, w, r)
        esperado = h.get("num_comentarios") or d.get("num_comentarios") or 0
        if n and (not esperado or n >= min(esperado, max_comentarios) * 0.7):
            break
        if d["selftext"] or n:
            dbg(f"  {w}: {n} comentarios de {esperado} anunciados; pruebo la otra web")
    if mejor is None:
        return {**h, "comentarios": [], "aviso_calidad": "no se pudo leer el hilo (ni en www ni en old.reddit.com)"}
    d, w, r = mejor
    coms = d["comentarios"][:max_comentarios]
    if d["selftext"]:
        coms = [{"autor": d["autor"] or h.get("autor") or "[eliminado]", "fecha": d["fecha"] or h.get("fecha"),
                 "profundidad": 0, "puntos": d["puntos"], "texto": d["selftext"], "numero": "OP"}] + coms
    item = {**h}
    for k in ("autor", "titulo", "subreddit", "fecha", "puntos", "num_comentarios"):
        if d.get(k) not in (None, ""):
            item[k] = d[k]
    item["comentarios"] = coms
    item["via_web"] = w
    anunciados = h.get("num_comentarios") or d.get("num_comentarios")
    if not d["comentarios"] and not d["selftext"]:
        item["aviso_calidad"] = "no se pudo extraer el texto del hilo; la pagina se guardo en la carpeta respuestas"
        DIAG.guarda_respuesta(r, "reddit-hilo-sin-texto")
    elif anunciados and len(d["comentarios"]) < anunciados * 0.7 and len(d["comentarios"]) < max_comentarios:
        item["aviso_calidad"] = (f"solo se leyeron {len(d['comentarios'])} de {anunciados} comentarios que anuncia Reddit "
                                 f"(los demas cuelgan de 'ver mas respuestas')")
    return item


def reddit_buscar(red, query, subreddit=None, sort="relevance", limite_hilos=10, limite_comentarios=200, tiempo=None,
                  exacta=False, web="auto", desplazar=4):
    if red.modo != "extension":
        raise ErrorWeb("Reddit bloquea a Termux y no se usa su JSON: hace falta la extension de Firefox (--via extension). "
                       "Ejecuta: python3 foroleer.py abrir")
    ambito = f"r/{subreddit}" if subreddit else "todo Reddit"
    out(f"[reddit] buscando: {query!r} ({ambito}) leyendo la pagina pintada en Firefox")
    hilos = reddit_lista(red, query, subreddit, sort, tiempo, exacta, web, limite_hilos)
    resultados = []
    for i, h in enumerate(hilos, 1):
        out(f"  hilo {i}/{len(hilos)}: {h['titulo']!r} (r/{h.get('subreddit')}"
            + (f", {h['num_comentarios']} comentarios" if h.get("num_comentarios") is not None else "") + ")")
        try:
            item = reddit_leer_hilo(red, h, limite_comentarios, web, desplazar)
        except ErrorWeb as e:
            err(f"    (no se pudo leer el hilo {h['id']}: {e})")
            item = {**h, "comentarios": [], "aviso_calidad": "no se pudo leer el hilo"}
        item["consulta"] = query
        resultados.append(item)
        pausa(1.0, 2.0, "cortesia con Reddit")
    return resultados


def trozo_camel(texto, maximo=70):
    t = unicodedata.normalize("NFKD", str(texto)).encode("ascii", "ignore").decode()
    palabras = re.findall(r"[A-Za-z0-9]+", t)
    return "".join(p[:1].upper() + p[1:] for p in palabras)[:maximo] or "SinTitulo"


def nombre_hilo_reddit(subreddit, titulo, usados):
    sub = re.sub(r"[^A-Za-z0-9]+", "", unicodedata.normalize("NFKD", str(subreddit or "reddit")).encode("ascii", "ignore").decode()) or "reddit"
    base = f"{SELLO}reddit_{sub}_{trozo_camel(titulo)}"
    nombre, n = base, 2
    while nombre in usados:
        nombre = f"{base}_{n}"
        n += 1
    usados.add(nombre)
    return nombre + ".txt"


def escribe_hilos_reddit(items, consulta, via, carpeta):
    """Un .txt por hilo: AAAA-MM-DD-HH-MM-SSreddit_Subreddit_TituloDelHilo.txt"""
    rutas, usados = [], set()
    os.makedirs(carpeta, exist_ok=True)
    for it in items:
        ruta = os.path.join(carpeta, nombre_hilo_reddit(it.get("subreddit"), it.get("titulo"), usados))
        escribir_salida(ruta, "Reddit", consulta, [it], via)
        rutas.append(ruta)
    return rutas


def fmt_fecha_epoch(epoch):
    if not epoch:
        return "(fecha desconocida)"
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.gmtime(float(epoch))) + " UTC"
    except Exception:
        return str(epoch)


# ---------------------------------------------------------------------------
# SWISSFORUM.CO.UK (Discourse: API JSON publica)
# ---------------------------------------------------------------------------

def swissforum_buscar(red, query, limite_temas=10):
    out(f"[swissforum] buscando: {query!r}")
    r = red.get(f"https://swissforum.co.uk/search.json?q={parse.quote(query)}", cab={"Accept": "application/json"},
                etiqueta="swissforum-busqueda")
    if r.status != 200 or r.clase != "json":
        raise ErrorWeb(f"swissforum no dio datos: HTTP {r.status} ({r.clase})", r)
    data = r.json()
    temas = data.get("topics", [])[:limite_temas]
    out(f"[swissforum] {len(temas)} tema(s)")
    resultados = []
    for t in temas:
        tid = t["id"]
        out(f"  - tema: {t.get('title')!r} (id {tid})")
        try:
            resultados.append(swissforum_tema(red, tid, t.get("title")))
        except ErrorWeb as e:
            err(f"    (no se pudo leer el tema {tid}: {e})")
        pausa(1.0, 2.5, "cortesia con swissforum.co.uk")
    return resultados


def swissforum_tema(red, topic_id, titulo_conocido=None):
    r = red.get(f"https://swissforum.co.uk/t/{topic_id}.json", cab={"Accept": "application/json"},
                etiqueta="swissforum-tema")
    if r.status != 200 or r.clase != "json":
        raise ErrorWeb(f"HTTP {r.status} ({r.clase})", r)
    data = r.json()
    titulo = data.get("title") or titulo_conocido
    post_stream = data.get("post_stream", {})
    posts = list(post_stream.get("posts", []))
    ids_presentes = {p["id"] for p in posts}
    faltantes = [i for i in post_stream.get("stream", []) if i not in ids_presentes]
    while faltantes:
        lote, faltantes = faltantes[:20], faltantes[20:]
        ids_q = "&".join(f"post_ids[]={i}" for i in lote)
        try:
            r2 = red.get(f"https://swissforum.co.uk/t/{topic_id}/posts.json?{ids_q}", cab={"Accept": "application/json"},
                         etiqueta="swissforum-posts")
            if r2.status == 200 and r2.clase == "json":
                posts.extend(r2.json().get("post_stream", {}).get("posts", []))
            else:
                err(f"    (posts adicionales: HTTP {r2.status} ({r2.clase}))")
                break
        except ErrorWeb as e:
            err(f"    (no se pudieron completar todos los posts: {e})")
            break
    posts.sort(key=lambda p: p.get("post_number", 0))
    salida = []
    muro = None
    for p in posts:
        cuerpo = html_a_texto(p.get("cooked", ""))
        if muro is None:
            muro = detecta_muro(cuerpo)
        salida.append({"autor": p.get("username"), "fecha": p.get("created_at"),
                       "numero": p.get("post_number"), "texto": cuerpo})
    return {"id": topic_id, "titulo": titulo, "url": f"https://swissforum.co.uk/t/{topic_id}",
            "posts": salida, "muro": muro}


# ---------------------------------------------------------------------------
# FOROCOCHES (vBulletin: busqueda por formulario)
# ---------------------------------------------------------------------------

def forocoches_buscar(red, query, max_hilos=10, pausa_min=3.0, pausa_max=6.0, max_paginas=3):
    out(f"[forocoches] buscando: {query!r}")
    r0 = red.get("https://forocoches.com/foro/search.php", etiqueta="forocoches-formulario")
    if r0.status != 200:
        raise ErrorWeb(f"forocoches no dio el formulario de busqueda: HTTP {r0.status} ({r0.clase})", r0)
    if 'name="query"' not in r0.text:
        DIAG.guarda_respuesta(r0, "forocoches-sin-formulario")
        raise ErrorWeb("Forocoches solo deja buscar a usuarios registrados (la pagina de busqueda ensena el formulario "
                       "de acceso). Inicia sesion en forocoches.com en Firefox y repite con la extension (--via "
                       "extension). Sin cuenta puedes leer hilos concretos con --hilo NUMERO_O_ENLACE.", r0)
    token_m = re.search(r'name="securitytoken" value="([^"]+)"', r0.text)
    token = token_m.group(1) if token_m else "guest"
    dbg(f"securitytoken: {token[:12]}...")
    datos = parse.urlencode({
        "do": "process", "query": query, "titleonly": "0", "securitytoken": token, "starteronly": "0",
        "replyless": "0", "replylimit": "0", "searchdate": "0", "beforeafter": "after", "sortby": "",
        "order": "descending", "showposts": "0", "forumchoice[]": "0",
    }).encode()
    url = "https://forocoches.com/foro/search.php?do=process"
    raw = None
    for intento in range(1, 4):
        r = red.get(url, cab={"Content-Type": "application/x-www-form-urlencoded"}, datos=datos,
                    etiqueta="forocoches-busqueda")
        espera_m = re.search(r"esperar\s+(\d+)\s+segundos|wait\s+(\d+)\s+seconds", r.text, re.I)
        if espera_m:
            s = int(espera_m.group(1) or espera_m.group(2)) + 1
            out(f"  (control de flood de forocoches: esperando {s}s; intento {intento}/3)")
            time.sleep(s)
            continue
        if r.status >= 400:
            err(f"  HTTP {r.status} en el intento {intento}")
            time.sleep(5)
            continue
        raw = r.text
        break
    if raw is None:
        raise ErrorWeb("No se pudo completar la busqueda en forocoches tras 3 intentos.", r)

    hilos = []
    for m in re.finditer(r'<a[^>]+id=["\']?thread_title_(\d+)["\']?[^>]*>([^<]*)</a>', raw, re.I):
        hilos.append({"id": m.group(1), "titulo": html.unescape(m.group(2)).strip()})
    if not hilos:
        for m in re.finditer(r'<a[^>]+href="[^"]*showthread\.php\?t=(\d+)[^"]*"[^>]*>([^<]{3,})</a>', raw, re.I):
            tid, titulo = m.group(1), html.unescape(m.group(2)).strip()
            if not any(h["id"] == tid for h in hilos):
                hilos.append({"id": tid, "titulo": titulo})
    if not hilos:
        aviso = texto_visible(raw, 20000)[:200]
        dbg(f"sin hilos en la pagina de resultados; texto: {aviso!r}")
        DIAG.guarda_respuesta(Resp(r.status, raw, r.url, r.via, r.tipo), "forocoches-sin-hilos")
        if re.search(r"no se encontr|sin resultados|no matches|no results", raw, re.I):
            out("[forocoches] la busqueda no tiene coincidencias.")
        else:
            err("[forocoches] la pagina de resultados no tiene el formato esperado (ver el informe de diagnostico).")
    hilos = hilos[:max_hilos]
    out(f"[forocoches] {len(hilos)} hilo(s)")
    resultados = []
    for h in hilos:
        out(f"  - hilo: {h['titulo']!r} (t={h['id']})")
        pausa(pausa_min, pausa_max, "control de flood de forocoches")
        try:
            resultados.append(forocoches_hilo(red, h["id"], h["titulo"], max_paginas))
        except ErrorWeb as e:
            err(f"    (no se pudo leer el hilo {h['id']}: {e})")
    return resultados


def forocoches_hilo(red, thread_id, titulo, max_paginas=3):
    posts_totales = []
    muro = None
    for pagina in range(1, max_paginas + 1):
        url = f"https://forocoches.com/foro/showthread.php?t={thread_id}"
        if pagina > 1:
            url += f"&page={pagina}"
        r = red.get(url, etiqueta="forocoches-hilo")
        if r.status != 200:
            raise ErrorWeb(f"HTTP {r.status} ({r.clase})", r)
        datos_pagina = forocoches_extraer_posts(r.text)
        if not datos_pagina["posts"]:
            dbg(f"hilo {thread_id} pagina {pagina}: 0 posts extraidos")
        posts_totales.extend(datos_pagina["posts"])
        if muro is None:
            muro = datos_pagina["muro"]
        if forocoches_max_pagina(r.text, thread_id) <= pagina:
            break
        pausa(2.0, 4.0, f"pagina {pagina + 1} del hilo")
    return {"id": thread_id, "titulo": titulo,
            "url": f"https://forocoches.com/foro/showthread.php?t={thread_id}",
            "posts": posts_totales, "muro": muro}


def forocoches_hilos_directos(red, ids, max_paginas=3):
    """Lee hilos concretos (se pueden leer sin cuenta). `ids` = numeros o enlaces showthread."""
    resultados = []
    for x in ids:
        m = re.search(r"[?&]t=(\d+)", x) or re.fullmatch(r"\s*(\d+)\s*", x)
        if not m:
            err(f"  (no entiendo {x!r}: usa el numero del hilo o el enlace showthread.php?t=NUMERO)")
            continue
        tid = m.group(1)
        out(f"[forocoches] leyendo hilo t={tid}")
        try:
            h = forocoches_hilo(red, tid, f"hilo {tid}", max_paginas)
            r = red.get(f"https://forocoches.com/foro/showthread.php?t={tid}", etiqueta="forocoches-titulo")
            h["titulo"] = re.sub(r"\s*-\s*Forocoches\s*$", "", titulo_html(r.text)) or h["titulo"]
            resultados.append(h)
        except ErrorWeb as e:
            err(f"    (no se pudo leer el hilo {tid}: {e})")
        pausa(1.0, 2.0, "cortesia con forocoches")
    return resultados


def forocoches_max_pagina(contenido_html, thread_id):
    """Pagina siguiente = el numero de pagina mas alto enlazado para este
    mismo hilo. La plantilla moderna de forocoches NO usa 'Pagina X de Y'
    ni rel=next (se comprobo sobre 3 hilos reales guardados el 8-oct-2026);
    en vez de eso lista enlaces '...showthread.php?t=<id>&page=N' para cada
    numero de pagina, incluida una flecha 'siguiente' duplicada. Tomar el
    maximo N visto es robusto sin depender de iconos ni textos."""
    patron = rf"showthread\.php\?t={thread_id}&(?:amp;)?page=(\d+)"
    numeros = [int(n) for n in re.findall(patron, contenido_html)]
    return max(numeros) if numeros else 1


def forocoches_extraer_posts(contenido_html):
    posts = []
    for m in re.finditer(r'id=["\']?post_message_(\d+)["\']?', contenido_html, re.I):
        pid = m.group(1)
        cont_m = re.search(rf'id=["\']?post{pid}["\']?[^>]*class=["\']?postbit_wrapper["\']?',
                            contenido_html, re.I)
        inicio = cont_m.start() if cont_m else m.start()
        # el bloque de un post va hasta que empieza el contenedor del
        # siguiente post ("postbit_wrapper"); si es el ultimo post del
        # archivo no hay "siguiente", asi que se toma hasta el final del
        # archivo en vez de un limite fijo de caracteres - un avatar en
        # base64 incrustado antes del mensaje puede ocupar varios miles de
        # caracteres por si solo y un limite fijo corta el mensaje real.
        siguiente = contenido_html.find("postbit_wrapper", inicio + 50)
        fin = (siguiente + 200) if siguiente != -1 else len(contenido_html)
        bloque = contenido_html[inicio:fin]

        # estructura real confirmada (8-oct-2026, 3 hilos reales):
        # <div id=post_message_PID ...><table><tbody><tr><td> TEXTO </td>...
        # las citas anidadas dentro del mensaje usan <div>, no <td>, asi que
        # el primer </td> que aparece es siempre el cierre real del mensaje.
        cuerpo_m = re.search(
            rf'id=["\']?post_message_{pid}["\']?[^>]*>\s*<table[^>]*>\s*<tbody>\s*<tr[^>]*>\s*<td[^>]*>([\s\S]*?)</td>\s*</tr>\s*</table>',
            bloque, re.I)
        if not cuerpo_m:
            # variante sin tabla, por si una pagina distinta no la usa:
            # se corta donde empieza el area de la firma del post.
            cuerpo_m = re.search(
                rf'id=["\']?post_message_{pid}["\']?[^>]*>([\s\S]*?)<div style=display:flex;flex-direction:column',
                bloque, re.I)
        cuerpo_html = cuerpo_m.group(1) if cuerpo_m else ""
        cuerpo = html_a_texto(cuerpo_html) if cuerpo_html else "(no se pudo extraer el cuerpo del mensaje)"

        autor_m = (re.search(r'href="[^"]*member\.php\?u=\d+"[^>]*>\s*([^<]+?)\s*</a>', bloque)
                   or re.search(r'class=["\']?bigusername["\']?[^>]*>\s*([^<]+?)\s*</a>', bloque))
        autor = html.unescape(autor_m.group(1)).strip() if autor_m else "(autor desconocido)"

        fecha_m = re.search(r'class="?postdate[^"\']*"?[^>]*>\s*([^<]+?)\s*<', bloque)
        fecha = html.unescape(fecha_m.group(1)).strip() if fecha_m else "(fecha desconocida)"
        fecha = re.sub(r"\s*&nbsp;\s*", " ", fecha).strip()

        posts.append({"numero": pid, "autor": autor, "fecha": fecha, "texto": cuerpo})

    muro = detecta_muro(quitar_etiquetas_simple(contenido_html)[:6000])
    return {"posts": posts, "muro": muro}


# ---------------------------------------------------------------------------
# QUORA (solo archivo guardado - SIN MUESTRA REAL, logica sin probar)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# QUORA (pagina pintada por Firefox o guardada con SingleFile; un .txt por pregunta)
# ---------------------------------------------------------------------------

def quora_titulo_de_url(url):
    m = re.search(r"quora\.com/([^/?#]+)", url or "")
    if not m or m.group(1) in ("profile", "topic", "search", "unanswered"):
        return ""
    return urllib_unquote(m.group(1)).replace("-", " ").strip()


def urllib_unquote(s):
    return parse.unquote(s)


def quora_pagina(texto):
    """Lista de respuestas de una pagina de Quora (portada, busqueda, tema o pregunta). Cada respuesta es un bloque
    puppeteer_test_answer_content; la pregunta sale del enlace /answer/ de su cabecera (en una pregunta suelta, del
    titulo de la pagina). Devuelve [{pregunta, url, autor, hace, texto, votos}]."""
    res, vistos = [], set()
    ans = [m for m in re.finditer(r'<div class="[^"]*puppeteer_test_answer_content[^"]*"[^>]*>', texto)]
    titulos = [(m.start(), m.end()) for m in re.finditer(r'<div class="q-text puppeteer_test_question_title"[^>]*>', texto)]
    for k, m in enumerate(ans):
        a = m.start()
        previo = ans[k - 1].end() if k else max(0, a - 12000)
        ventana = texto[previo:a]
        mts = re.findall(r'<a [^>]*answer_timestamp[^>]*href=["\']?([^\s"\'>]+)[^>]*>([^<]*)</a>', ventana)
        url_resp, hace = (mts[-1] if mts else ("", ""))
        url_resp = html.unescape(url_resp)
        mq = re.match(r"(https://www\.quora\.com/[^/?]+)/answer/([^/?]+)", url_resp)
        url_preg = mq.group(1) if mq else ""
        autor = parse.unquote(mq.group(2)).replace("-", " ") if mq else ""
        titulo = ""
        for s, e in reversed([t for t in titulos if t[0] < a]):
            cand = html_a_texto(elemento_interior(texto, e, "div")).replace("\n", " ").strip()
            enlaces = re.findall(r'href=["\']?(https://www\.quora\.com/[^\s"\'>?]+)', texto[max(0, s - 1500):s])
            if enlaces and (not url_preg or enlaces[-1].rstrip("/") == url_preg.rstrip("/")):
                titulo = cand
            break
        if not titulo:
            titulo = quora_titulo_de_url(url_preg)
        cuerpo = html_a_texto(elemento_interior(texto, m.end(), "div"))
        cuerpo = re.sub(r"\s*\(more\)\s*$", "", cuerpo).strip()
        if not cuerpo:
            continue
        clave = (url_resp or autor) + cuerpo[:60]
        if clave in vistos:
            continue
        vistos.add(clave)
        post = texto[m.end() + len(cuerpo):m.end() + len(cuerpo) + 9000]
        up = re.search(r'aria-label=Upvote[\s\S]{0,1500}?<span[^>]*>(\d[\d.,]*[KkM]?)</span>', post)
        votos = up.group(1) if up else None
        if votos and re.fullmatch(r"9+", votos):   # cifra de relleno que Quora usa para reservar ancho
            votos = None
        res.append({"pregunta": titulo or "(pregunta sin identificar)", "url": url_preg, "autor": autor or "(desconocido)",
                    "hace": (hace or "").strip(), "texto": cuerpo, "votos": votos})
    return res


def quora_agrupa(respuestas):
    """Agrupa respuestas por pregunta -> items para construye_salida (uno por pregunta)."""
    grupos, orden = {}, []
    for r in respuestas:
        clave = r["url"] or r["pregunta"]
        if clave not in grupos:
            grupos[clave] = {"id": clave, "titulo": r["pregunta"], "url": r["url"], "comentarios": []}
            orden.append(clave)
        meta = " - ".join(x for x in (r["hace"], (r["votos"] + " votos") if r["votos"] else "") if x)
        grupos[clave]["comentarios"].append({"autor": r["autor"], "fecha": meta, "profundidad": 0, "texto": r["texto"]})
    return [grupos[c] for c in orden]


def quora_pregunta_titulo(texto):
    m = re.search(r"<title>([^<]*)</title>", texto)
    t = html.unescape(m.group(1)).strip() if m else ""
    return re.sub(r"^\(\d+\)\s*", "", t).replace(" - Quora", "").strip()


def quora_buscar(red, query, limite_preguntas=8, desplazar=6, leer_preguntas=True):
    if red.modo != "extension":
        raise ErrorWeb("Quora necesita la extension de Firefox con tu sesion: usa --via extension.")
    out(f"[quora] buscando: {query!r} leyendo la pagina pintada en Firefox")
    url = f"https://www.quora.com/search?q={parse.quote(query)}&type=question"
    r = red.pagina(url, 6000, "quora-busqueda", desplazar)
    resp = quora_pagina(r.text)
    items = quora_agrupa(resp)
    # Preguntas que salen en la pagina aunque no tengan respuesta visible
    for m in re.finditer(r'<a [^>]*href=["\']?(https://www\.quora\.com/[^\s"\'>?]+)[^>]*>\s*<div[^>]*>[\s\S]{0,300}?puppeteer_test_question_title', r.text):
        u = html.unescape(m.group(1))
        if "/answer/" not in u and not any(i["url"] == u for i in items):
            items.append({"id": u, "titulo": quora_titulo_de_url(u), "url": u, "comentarios": []})
    items = items[:limite_preguntas]
    out(f"[quora] pagina de busqueda: {len(items)} pregunta(s)")
    if not items:
        guardada = DIAG.guarda_respuesta(r, "quora-sin-resultados")
        visible = texto_visible(r.text, 80000)
        if re.search(r"(couldn.?t find any (more )?results|no results (for|found)|no (se )?(han )?encontr\w+ resultados|"
                     r"keine ergebnisse|aucun r[eé]sultat)", visible, re.I):
            out("[quora] Quora dice que no tiene resultados para esa busqueda. Prueba con menos palabras y en ingles "
                "(Quora casi no tiene contenido en aleman).")
            return []
        raise ErrorWeb(f"Quora no dio resultados reconocibles ({r.clase}; titulo {titulo_html(r.text)[:50]!r}). "
                       "Puede ser que no haya resultados, que falte iniciar sesion en quora.com en ese Firefox o que "
                       f"Quora haya cambiado la pagina. Pagina guardada en: {guardada or '(no se pudo guardar)'}", r)
    if leer_preguntas:
        for i, it in enumerate(items, 1):
            if not it["url"]:
                continue
            out(f"  pregunta {i}/{len(items)}: {it['titulo']!r}")
            try:
                rp = red.pagina(it["url"], 5000, "quora-pregunta", desplazar)
                otras = quora_pagina(rp.text)
                if len(otras) > len(it["comentarios"]):
                    tit = quora_pregunta_titulo(rp.text) or it["titulo"]
                    it["comentarios"] = quora_agrupa([{**o, "url": it["url"], "pregunta": tit} for o in otras])[0]["comentarios"]
                    it["titulo"] = tit
                if not otras:
                    DIAG.guarda_respuesta(rp, "quora-pregunta-sin-respuestas")
            except ErrorWeb as e:
                err(f"    (no se pudo leer la pregunta: {e})")
            if not it["comentarios"]:
                it["aviso_calidad"] = "no se pudo extraer ninguna respuesta (Quora pide sesion o cambio su marcado)"
            pausa(1.0, 2.0, "cortesia con Quora")
    for it in items:
        it["consulta"] = query
    return items


QUORA_SEMILLAS = ["Switzerland", "Zurich-city", "Geneva-Switzerland", "Living-in-Switzerland", "Working-in-Switzerland",
                  "Swiss-German", "Swiss-Culture", "Travel-to-Switzerland"]


def quora_slug(texto):
    """'Living in Switzerland' -> 'Living-in-Switzerland'; admite tambien una direccion completa de tema."""
    m = re.search(r"quora\.com/topic/([^/?#\s]+)", texto or "")
    if m:
        return parse.unquote(m.group(1))
    return re.sub(r"[^A-Za-z0-9-]+", "-", (texto or "").strip()).strip("-")


def quora_enlaces_tema(texto):
    """Nombres de tema (/topic/Nombre) que aparecen en una pagina de Quora (busqueda de temas o temas relacionados)."""
    vistos, res = set(), []
    for m in re.finditer(r'(?:https://www\.quora\.com)?/topic/([A-Za-z0-9%_.-]+)', texto or ""):
        n = parse.unquote(m.group(1))
        if n and n.lower() not in vistos and n.lower() not in ("create",):
            vistos.add(n.lower())
            res.append(n)
    return res


class QuoraAcceso:
    """Lee paginas de Quora sin tocar el navegador si se puede: primero una peticion directa desde Termux; si Quora la
    rechaza (403, verificacion, pagina sin contenido) usa la extension en segundo plano (con tu sesion)."""

    def __init__(self, red, args):
        self.red, self.args, self.preparada = red, args, False
        self.directo_ok = red.pedido in ("auto", "directo")
        self.ext_ok = red.pedido in ("auto", "extension")

    def _prepara_ext(self):
        if not self.preparada:
            self.preparada = True
            self.red.prepara()
        return self.red.modo == "extension"

    def html(self, url, desplazar, temas=False):
        if self.directo_ok:
            try:
                r = self.red._directo(url, None, None, "GET")
                self.red._registra(r, "GET", url, 0, "quora-directo")
                if r.status == 200 and temas and quora_enlaces_tema(r.text):
                    out(f"  (directo desde Termux: {len(quora_enlaces_tema(r.text))} tema(s))")
                    return r.text
                if r.status == 200 and not temas and ("puppeteer_test" in r.text or "q-box" in r.text) and quora_pagina(r.text):
                    out(f"  (directo desde Termux: {len(quora_pagina(r.text))} respuesta(s))")
                    return r.text
                out(f"  (Quora no dio contenido en modo directo: estado {r.status}; "
                    f"{'uso la extension' if self.ext_ok else 'sin extension no se puede seguir'})")
            except ErrorWeb as e:
                out(f"  (modo directo fallo: {e})")
            if self.args.via == "directo":
                raise ErrorWeb("Quora no entrega la pagina en modo directo. Usa --via auto o --via extension.")
        if not self.ext_ok or not self._prepara_ext():
            raise ErrorWeb("Quora necesita la extension de Firefox con tu sesion (modo directo rechazado).")
        return self.red.pagina(url, 6000, "quora-tema", desplazar).text


def quora_descubre_temas(acc, palabras, desplazar):
    """Busca temas cuyo nombre contenga cada palabra, con la busqueda de temas de Quora (marcado sin verificar)."""
    res = []
    for pal in palabras:
        out(f"[quora] buscando temas con '{pal}'")
        try:
            txt = acc.html(f"https://www.quora.com/search?q={parse.quote(pal)}&type=topic", desplazar, True)
        except ErrorWeb as e:
            err(f"  no se pudo buscar temas con '{pal}': {e}")
            continue
        nombres = quora_enlaces_tema(txt)
        out(f"  {len(nombres)} tema(s): " + (", ".join(nombres[:12]) or "(ninguno reconocido)"))
        if not nombres:
            DIAG.avisos.append(f"busqueda de temas '{pal}': sin temas reconocidos")
        res += nombres
    return res


def quora_tema(red, args):
    acc = QuoraAcceso(red, args)
    filtro = [x.strip().lower() for x in (args.palabra or "").split(",") if x.strip()]
    temas = [quora_slug(t) for t in (args.temas or [])]
    if args.buscar_temas:
        temas += quora_descubre_temas(acc, [x.strip() for x in args.buscar_temas.split(",") if x.strip()], args.desplazar)
    if not temas:
        temas = list(QUORA_SEMILLAS)
        out("[quora] sin temas indicados: uso la lista inicial " + ", ".join(temas))
    unicos = []
    for t in temas:
        if t and t.lower() not in [u.lower() for u in unicos]:
            unicos.append(t)
    if filtro:
        antes = len(unicos)
        unicos = [t for t in unicos if any(f in t.lower() for f in filtro)]
        out(f"[quora] filtro de nombre de tema {filtro}: {len(unicos)} de {antes} tema(s)")
    items, vistos = [], set()
    for t in unicos:
        url = f"https://www.quora.com/topic/{parse.quote(t)}"
        out(f"[quora] tema: {t}")
        try:
            txt = acc.html(url, args.desplazar)
        except ErrorWeb as e:
            err(f"  (no se pudo leer el tema {t}: {e})")
            continue
        nuevos = quora_agrupa(quora_pagina(txt))
        if not nuevos:
            DIAG.guarda_respuesta(Resp(200, txt, url, "quora", "text/html"), "quora-tema-vacio")
            err(f"  (el tema {t} no tiene preguntas reconocibles: nombre inexistente, sin sesion o marcado distinto)")
            continue
        n0 = len(items)
        for it in nuevos:
            if it["url"] in vistos:
                continue
            if args.titulo and not any(f in it["titulo"].lower() for f in [x.strip().lower() for x in args.titulo.split(",")]):
                continue
            vistos.add(it["url"])
            it["consulta"] = f"tema {t}"
            items.append(it)
        out(f"  {len(items) - n0} pregunta(s) nueva(s) en este tema")
        for extra in quora_enlaces_tema(txt):
            if extra.lower() not in [u.lower() for u in unicos] and (not filtro or any(f in extra.lower() for f in filtro)):
                out(f"  tema relacionado visto: {extra} (anadelo si lo quieres)")
        pausa(1.0, 2.0, "cortesia con Quora")
    items = items[:args.max_hilos or 200]
    if args.leer:
        for i, it in enumerate(items, 1):
            out(f"  pregunta {i}/{len(items)}: {it['titulo']!r}")
            try:
                rp = acc.html(it["url"], args.desplazar)
                otras = quora_pagina(rp)
                if len(otras) > len(it["comentarios"]):
                    tit = quora_pregunta_titulo(rp) or it["titulo"]
                    it["comentarios"] = quora_agrupa([{**o, "url": it["url"], "pregunta": tit} for o in otras])[0]["comentarios"]
                    it["titulo"] = tit
            except ErrorWeb as e:
                err(f"    (no se pudo leer la pregunta: {e})")
            pausa(1.0, 2.0, "cortesia con Quora")
    return items


def cmd_quora_tema(args):
    """Lee TEMAS de Quora (p. ej. /topic/Switzerland) y escribe UN .txt POR PREGUNTA."""
    DIAG.comando = "foroleer.py " + " ".join(sys.argv[1:])
    red = Red(args.via, args.puerto)
    try:
        items = quora_tema(red, args)
    except (ErrorWeb, ErrorPuente) as e:
        err(f"Error: {e}")
        return _termina(red, args, f"ERROR: {e}", 1)
    carpeta = args.output or "."
    if carpeta.lower().endswith(".txt"):
        carpeta = carpeta[:-4]
    os.makedirs(carpeta, exist_ok=True)
    usados = set()
    out(f"{len(items)} pregunta(s), {sum(len(i['comentarios']) for i in items)} respuesta(s).")
    for it in items:
        ruta = os.path.join(carpeta, nombre_hilo_quora(it["titulo"], usados))
        escribir_salida(ruta, "Quora", it.get("consulta", "tema"), [it], red.modo)
        out(f"  {os.path.abspath(ruta)}")
    if not items:
        out("Sin resultados.")
    return _termina(red, args, f"OK: {len(items)} pregunta(s)", 0 if items else 1)



def nombre_hilo_quora(titulo, usados):
    base = f"{SELLO}quora_{trozo_camel(titulo)}"
    nombre, n = base, 2
    while nombre in usados:
        nombre, n = f"{base}_{n}", n + 1
    usados.add(nombre)
    return nombre + ".txt"


# ---------------------------------------------------------------------------
# LINKEDIN PULSE (solo archivo guardado - SIN MUESTRA REAL, logica sin probar)
# ---------------------------------------------------------------------------

def linkedin_pulse_archivo_a_texto(contenido_html):
    """MEJOR ESFUERZO, NO PROBADO: tampoco se dispuso de ninguna pagina de
    LinkedIn Pulse real. Los articulos de Pulse suelen ir en un <article>
    o en un contenedor con 'article-content' / 'reader-article-content'.
    Igual que con Quora: si falla contra una pagina real, mandamela
    guardada con SingleFile y ajusto los patrones."""
    titulo_m = re.search(r"<title>([^<]*)</title>", contenido_html, re.I)
    titulo = html.unescape(titulo_m.group(1)).strip() if titulo_m else "(sin titulo)"

    autor = None
    autor_m = re.search(r'<a[^>]*class="[^"]*(?:article-author|author-name)[^"]*"[^>]*>([\s\S]*?)</a>',
                         contenido_html, re.I)
    if autor_m:
        autor = html_a_texto(autor_m.group(1))

    cuerpo_html = None
    for patron in (r'<article[^>]*>([\s\S]*?)</article>',
                   r'<div[^>]*class="[^"]*(?:article-content|reader-article-content)[^"]*"[^>]*>([\s\S]*?)</div>\s*</div>\s*</div>'):
        cm = re.search(patron, contenido_html, re.I)
        if cm:
            cuerpo_html = cm.group(1)
            break
    cuerpo = html_a_texto(cuerpo_html) if cuerpo_html else "(no se pudo localizar el cuerpo del articulo)"

    muro = detecta_muro(quitar_etiquetas_simple(contenido_html)[:6000])
    return {"titulo": titulo, "autor": autor, "texto": cuerpo, "muro": muro,
            "aviso_calidad": "Extraccion de LinkedIn Pulse SIN PROBAR contra una pagina real (no habia muestra disponible)."}


# ---------------------------------------------------------------------------
# Deteccion de sitio para --sitio auto (analisis de archivo guardado)
# ---------------------------------------------------------------------------

def detecta_sitio_archivo(contenido_html, nombre_archivo=""):
    cab = contenido_html[:4000]
    url_guardada_m = re.search(r"url:\s*(\S+)", cab)
    url_guardada = url_guardada_m.group(1) if url_guardada_m else ""
    pista = (url_guardada + " " + nombre_archivo).lower()
    cuerpo_muestra = contenido_html[:200000]

    if "reddit.com" in pista:
        return "reddit"
    if "quora.com" in pista:
        return "quora"
    if "linkedin.com" in pista and "pulse" in pista:
        return "linkedin"
    if "forocoches.com" in pista:
        return "forocoches"
    if "swissforum.co.uk" in pista:
        return "swissforum"
    # sin pista de URL (SingleFile no siempre la deja, o el archivo se
    # renombro): miramos marcas de generador/estructura tipicas.
    if re.search(r"vbulletin", cuerpo_muestra, re.I) or "post_message_" in cuerpo_muestra:
        return "forocoches"
    if re.search(r"discourse", cuerpo_muestra, re.I) or "data-topic-id" in cuerpo_muestra:
        return "swissforum"
    if "q-text" in cuerpo_muestra or "quora.com" in cuerpo_muestra:
        return "quora"
    if "linkedin.com" in cuerpo_muestra:
        return "linkedin"
    return None




# ---------------------------------------------------------------------------
# Salida en texto plano (121 columnas)
# ---------------------------------------------------------------------------

def _lineas_texto(texto, sangria=""):
    res = []
    for linea in str(texto).replace("\r", "").split("\n"):
        if not linea.strip():
            res.append("")
            continue
        pref = sangria
        for trozo in textwrap.wrap(linea, ANCHO - len(pref), subsequent_indent="", break_long_words=True,
                                   replace_whitespace=False, drop_whitespace=True) or [""]:
            res.append(pref + trozo)
    return res


def construye_salida(sitio, consulta, items, via):
    L = []
    L.append("=" * ANCHO)
    L.append(f"foroleer.py {VERSION} - {sitio}")
    L.append(f"Busqueda / archivo: {consulta}")
    L.append(f"Generado: {time.strftime('%Y-%m-%d %H:%M:%S')}   Via: {via}")
    L.append("=" * ANCHO)
    L.append("")
    if not items:
        L.append("(sin resultados)")
        return L
    for item in items:
        L.append("-" * ANCHO)
        L.extend(_lineas_texto("TEMA: " + str(item.get("titulo") or item.get("pregunta") or "(sin titulo)")))
        if item.get("url"):
            L.extend(_lineas_texto("URL: " + item["url"]))
        if item.get("subreddit"):
            meta = f"Subreddit: r/{item['subreddit']}"
            if item.get("autor"):
                meta += f"   Autor: {item['autor']}"
            if item.get("fecha") and not isinstance(item.get("fecha"), (int, float)):
                meta += f"   Fecha: {item['fecha']}"
            if item.get("puntos") is not None:
                meta += f"   Puntos: {item['puntos']}"
            if item.get("num_comentarios") is not None:
                meta += f"   Comentarios segun Reddit: {item['num_comentarios']}"
            L.extend(_lineas_texto(meta))
        if item.get("muro"):
            L.append("*** AVISO: posible muro de registro/suscripcion detectado")
            L.extend(_lineas_texto(f"***  (frase encontrada: \"{item['muro']}\")"))
            L.append("***  el contenido de abajo puede estar incompleto.")
        if item.get("aviso_calidad"):
            L.extend(_lineas_texto("*** AVISO DE CALIDAD: " + item["aviso_calidad"]))
        L.append("-" * ANCHO)
        L.append("")
        posts = item.get("posts") or item.get("comentarios") or item.get("respuestas")
        if posts:
            for p in posts:
                autor = p.get("autor") or "(desconocido)"
                fecha = p.get("fecha")
                if isinstance(fecha, (int, float)):
                    fecha = fmt_fecha_epoch(fecha)
                cab = f"[{p.get('numero')}] " if p.get("numero") else ""
                cab += f"{autor}" + (f" - {fecha}" if fecha else "")
                sangria = "  " * min(p.get("profundidad", 0) or 0, 8)
                L.extend(_lineas_texto(cab, sangria))
                L.extend(_lineas_texto((p.get("texto") or "").strip() or "(vacio)", sangria))
                L.append("")
        elif item.get("texto"):
            if item.get("autor"):
                L.extend(_lineas_texto(item["autor"]))
            L.extend(_lineas_texto(item["texto"]))
            L.append("")
        else:
            L.append("(no se encontro contenido para mostrar)")
            L.append("")
    return L


def nombre_salida(sitio, consulta):
    slug = re.sub(r"[^a-z0-9]+", "_", normaliza(consulta)).strip("_")[:40] or "busqueda"
    return f"foroleer_{sitio}_{slug}_{SELLO}.txt"


def escribir_salida(ruta, sitio, consulta, items, via="-"):
    lineas = construye_salida(sitio, consulta, items, via)
    with open(ruta, "w", encoding="utf-8") as f:
        f.write("\n".join(lineas) + "\n")
    return lineas


# ---------------------------------------------------------------------------
# Informe de diagnostico (un solo .txt de 121 columnas que se puede mandar entero)
# ---------------------------------------------------------------------------

def entorno():
    e = [f"foroleer.py {VERSION}", f"Python {platform.python_version()} en {platform.system()} {platform.machine()}",
         f"Termux: {'si' if os.environ.get('PREFIX', '').startswith('/data/data/com.termux') else 'no detectado'}",
         f"Carpeta de trabajo: {os.getcwd()}", f"Carpeta foroleer (registros): {DIR}",
         f"Comando 'am': {shutil.which('am') or 'no'}; 'pm': {shutil.which('pm') or 'no'}; "
         f"'termux-open-url': {shutil.which('termux-open-url') or 'no'}"]
    for v in ("FOROLEER_DIR", "FOROLEER_FIREFOX", "FOROLEER_NO_ABRIR"):
        if os.environ.get(v):
            e.append(f"{v}={os.environ[v]}")
    return e


def sugerencias():
    s = []
    clases = {p["clase"] for p in DIAG.peticiones}
    modos = {p["via"] for p in DIAG.peticiones}
    if clases & {"reddit-bloqueo-red", "reddit-bienvenida"}:
        if any(m.startswith("extension") for m in modos):
            s.append("Reddit bloquea tambien las peticiones que salen de Firefox: inicia sesion en reddit.com en el "
                     "Firefox donde esta la extension (si usas contenedores, en el mismo) y repite. Si ya tenias "
                     "sesion, cierra y vuelve a abrir esa pestana de Reddit y prueba otra vez.")
        else:
            s.append("Reddit bloquea las peticiones que salen directamente de Termux (esperado). Usa --via extension "
                     "(o deja el modo auto con la extension conectada).")
    if "cloudflare" in clases:
        s.append("Una web pide verificacion anti-bot: abre esa web en Firefox, resuelvela, y repite.")
    if "limite-peticiones" in clases:
        s.append("Limite de peticiones: espera unos minutos y baja --max-hilos.")
    pu = DIAG.puente
    if pu.get("extension") == "sin respuesta":
        if pu.get("abrir_visto"):
            s.append("Firefox llego a abrir la pagina de despertar pero la extension no contesto: comprueba que "
                     "foroleer.xpi esta instalada, habilitada y con la casilla 'Conectar con Termux' marcada.")
        else:
            s.append("La extension no responde y Firefox no llego a abrir la pagina de despertar. Abre Firefox a mano "
                     "(con foroleer instalada) y repite; si 'am start' fallo, da a Termux el permiso 'Mostrar sobre "
                     "otras aplicaciones' en los ajustes de Android.")
    if any("No se pudo abrir el puerto" in e for e in DIAG.errores):
        s.append("El puerto esta ocupado: usa --puerto con otro numero y pon el mismo en la extension.")
    if any("No se pudo conectar" in e or "Error de red" in e for e in DIAG.errores):
        s.append("Sin red desde Termux: comprueba la conexion a internet.")
    if not s and DIAG.errores:
        s.append("Mira las lineas ERROR del registro y las respuestas guardadas (carpeta 'respuestas').")
    if not s:
        s.append("No se detectaron problemas.")
    return s


def escribe_informe(destino_dir=None):
    destino_dir = destino_dir or os.getcwd()
    ruta = os.path.join(destino_dir, f"{SELLO}diagForoleer.txt")
    L = ["=" * ANCHO, f"INFORME DE DIAGNOSTICO DE FOROLEER {VERSION}", f"Generado: {time.strftime('%Y-%m-%d %H:%M:%S')}",
         "=" * ANCHO, ""]
    L.append("1. RESUMEN")
    L.append(f"Comando: {DIAG.comando}")
    L.append(f"Resultado: {DIAG.resultado}")
    L.append(f"Errores: {len(DIAG.errores)}")
    L.append("")
    L.append("2. QUE HACER (sugerencias automaticas)")
    L.extend(f"- {x}" for x in sugerencias())
    L.append("")
    L.append("3. ENTORNO")
    L.extend(entorno())
    L.append("")
    L.append("4. PUENTE CON LA EXTENSION")
    if DIAG.puente:
        L.extend(f"{k}: {v}" for k, v in DIAG.puente.items())
    else:
        L.append("(no se uso el puente)")
    if DIAG.ext_info:
        L.append("Informacion de la extension: " + json.dumps(DIAG.ext_info, ensure_ascii=True))
    L.append("")
    L.append("5. PETICIONES (n, metodo, estado, clase, via, bytes, segundos, url)")
    if DIAG.peticiones:
        for p in DIAG.peticiones:
            L.append(f"{p['n']:>3} {p['metodo']:4} {str(p['status']):>4} {p['clase']:<18} {p['via']:<22} {p['bytes']:>8} "
                     f"{p['seg']:>5}s {p['url']}")
    else:
        L.append("(ninguna)")
    L.append("")
    L.append("6. RESPUESTAS QUE NO ERAN LAS ESPERADAS (muestra del texto visible)")
    malas = [p for p in DIAG.peticiones if p["clase"] in CLASES_MALAS or (p["status"] or 0) >= 400]
    if not malas:
        L.append("(ninguna)")
    for p in malas:
        L.append(f"Peticion {p['n']}: {p['url']}")
        L.append(f"  estado {p['status']}, clase {p['clase']}, titulo {p['titulo']!r}")
        if p["clase"] in EXPLICACION_CLASE:
            L.extend("  " + x for x in textwrap.wrap(EXPLICACION_CLASE[p["clase"]], ANCHO - 2))
        if p["muestra"]:
            L.extend("  muestra: " + x if i == 0 else "    " + x
                     for i, x in enumerate(textwrap.wrap(p["muestra"], ANCHO - 12)[:6]))
        if p["archivo"]:
            L.append(f"  cuerpo completo guardado en: {p['archivo']}")
    if DIAG.guardadas:
        L.append("Paginas guardadas en esta ejecucion (enviarlas si se piden):")
        L.extend("  " + g for g in DIAG.guardadas)
    L.append("")
    L.append("7. REGISTRO DE ESTA EJECUCION (foroleer.py)")
    L.extend(DIAG.lineas[-400:])
    L.append("")
    L.append("8. REGISTRO DE LA EXTENSION (Firefox)")
    if not (DIAG.ext_log_ruta and os.path.exists(DIAG.ext_log_ruta)):
        DIAG.ext_log_ruta = (ultimos_archivos(LOGDIR, "logForoleerExt.log", 1) or [None])[0]
    if DIAG.ext_log_ruta and os.path.exists(DIAG.ext_log_ruta):
        try:
            with open(DIAG.ext_log_ruta, encoding="utf-8") as f:
                L.extend(f.read().splitlines()[-150:])
        except OSError as e:
            L.append(f"(no se pudo leer {DIAG.ext_log_ruta}: {e})")
    else:
        L.append("(la extension no envio registro en esta ejecucion)")
    L.append("")
    L.append(f"Registro completo de esta ejecucion: carpeta {LOGDIR}, archivo ...logForoleer.log con la fecha y hora "
             f"en que termino (hacia las {time.strftime('%H:%M:%S')}).")
    L.append("Para reunir todos los registros de un periodo en un solo archivo: python3 foroleer.py reporte")
    try:
        os.makedirs(destino_dir, exist_ok=True)
        with open(ruta, "w", encoding="utf-8") as f:
            f.write("\n".join(x for l in L for x in envuelve(l)) + "\n")
    except OSError as e:
        alt = os.path.join(DIR, os.path.basename(ruta))
        try:
            os.makedirs(DIR, exist_ok=True)
            with open(alt, "w", encoding="utf-8") as f:
                f.write("\n".join(x for l in L for x in envuelve(l)) + "\n")
            ruta = alt
        except OSError:
            err(f"No se pudo escribir el informe de diagnostico: {e}")
            return None
    DIAG.informe_ruta = ruta
    return ruta


# ---------------------------------------------------------------------------
# Comandos
# ---------------------------------------------------------------------------

def _termina(red, args, resultado, codigo, sitio=None, consulta=None, items=None):
    DIAG.resultado = resultado
    if red:
        red.cierra()
    if codigo != 0 or DIAG.errores:
        ruta = escribe_informe()
        if ruta:
            (err if codigo != 0 else out)(f"Informe de diagnostico: {ruta}")
    return codigo


def _ejecuta_busqueda(args, sitio, consulta, funcion):
    DIAG.comando = "foroleer.py " + " ".join(sys.argv[1:])
    red = Red(args.via, args.puerto)
    try:
        red.prepara()
        items = funcion(red)
    except ErrorForoleer as e:
        err(f"Error: {e}")
        return _termina(red, args, f"ERROR: {e}", 1)
    ruta = args.output or nombre_salida(sitio.lower().replace(".", "_"), consulta)
    try:
        lineas = escribir_salida(ruta, sitio, consulta, items, red.modo)
    except OSError as e:
        err(f"Error: no se pudo escribir {ruta}: {e}")
        return _termina(red, args, f"ERROR escribiendo {ruta}", 1)
    if args.texto:
        out("\n".join(lineas))
    else:
        n = sum(len(i.get("posts") or i.get("comentarios") or []) for i in items)
        out(f"{len(items)} hilo(s)/tema(s), {n} mensaje(s).")
        for i in items[:30]:
            out("  - " + str(i.get("titulo"))[:100])
    out(f"Archivo: {os.path.abspath(ruta)}")
    if not items:
        out("Sin resultados.")
    return _termina(red, args, f"OK: {len(items)} resultado(s) en {ruta}", 0)


def cmd_reddit(args):
    """Busca en Reddit leyendo las paginas pintadas por Firefox (sin JSON) y escribe UN .txt POR HILO:
    AAAA-MM-DD-HH-MM-SSreddit_Subreddit_TituloDelHilo.txt"""
    DIAG.comando = "foroleer.py " + " ".join(sys.argv[1:])
    subs = []
    for s in (args.subreddit or []):
        subs += [re.sub(r"^/?r/", "", x.strip()).strip("/") for x in s.split(",") if x.strip()]
    red = Red("extension" if args.via == "auto" else args.via, args.puerto)
    try:
        red.prepara()
        items = []
        for sub in (subs or [None]):
            items += reddit_buscar(red, args.query, subreddit=sub, sort=args.sort, limite_hilos=args.max_hilos or 10,
                                   limite_comentarios=args.max_comentarios, tiempo=args.tiempo,
                                   exacta=args.exacta, web=args.web, desplazar=args.desplazar)
    except ErrorForoleer as e:
        err(f"Error: {e}")
        return _termina(red, args, f"ERROR: {e}", 1)
    vistos, unicos = set(), []
    for it in items:
        if it.get("id") in vistos:
            continue
        vistos.add(it.get("id"))
        unicos.append(it)
    carpeta = args.output or "."
    if carpeta.lower().endswith(".txt"):
        err("Aviso: en 'reddit' --output es una CARPETA (se escribe un .txt por hilo); uso la carpeta de ese nombre sin .txt.")
        carpeta = carpeta[:-4]
    try:
        rutas = escribe_hilos_reddit(unicos, args.query, red.modo, carpeta)
    except OSError as e:
        err(f"Error: no se pudo escribir en {carpeta}: {e}")
        return _termina(red, args, "ERROR escribiendo", 1)
    n = sum(len(i.get("comentarios") or []) for i in unicos)
    out(f"{len(unicos)} hilo(s), {n} mensaje(s).")
    for it, ruta in zip(unicos, rutas):
        aviso = f"  [{it['aviso_calidad']}]" if it.get("aviso_calidad") else ""
        out(f"  {os.path.abspath(ruta)}{aviso}")
        if args.texto:
            out("\n".join(construye_salida("Reddit", args.query, [it], red.modo)))
    if not unicos:
        out("Sin resultados.")
    return _termina(red, args, f"OK: {len(unicos)} hilo(s) en {os.path.abspath(carpeta)}", 0)


def cmd_quora(args):
    """Busca en Quora con tu sesion de Firefox y escribe UN .txt POR PREGUNTA:
    AAAA-MM-DD-HH-MM-SSquora_TituloDeLaPregunta.txt"""
    DIAG.comando = "foroleer.py " + " ".join(sys.argv[1:])
    red = Red("extension" if args.via == "auto" else args.via, args.puerto)
    try:
        red.prepara()
        items = quora_buscar(red, args.query, limite_preguntas=args.max_hilos or 8, desplazar=args.desplazar,
                             leer_preguntas=not args.solo_busqueda)
    except ErrorForoleer as e:
        err(f"Error: {e}")
        return _termina(red, args, f"ERROR: {e}", 1)
    carpeta = args.output or "."
    if carpeta.lower().endswith(".txt"):
        err("Aviso: en 'quora' --output es una CARPETA (un .txt por pregunta); uso el nombre sin .txt.")
        carpeta = carpeta[:-4]
    os.makedirs(carpeta, exist_ok=True)
    usados = set()
    out(f"{len(items)} pregunta(s), {sum(len(i['comentarios']) for i in items)} respuesta(s).")
    if not items:
        out("Sin resultados.")
        return _termina(red, args, "OK: sin resultados en Quora", 0)
    for it in items:
        ruta = os.path.join(carpeta, nombre_hilo_quora(it["titulo"], usados))
        escribir_salida(ruta, "Quora", args.query, [it], red.modo)
        aviso = f"  [{it['aviso_calidad']}]" if it.get("aviso_calidad") else ""
        out(f"  {os.path.abspath(ruta)}{aviso}")
    return _termina(red, args, f"OK: {len(items)} pregunta(s) en {os.path.abspath(carpeta)}", 0)


def cmd_swissforum(args):
    return _ejecuta_busqueda(args, "swissforum.co.uk", args.query,
                             lambda red: swissforum_buscar(red, args.query, limite_temas=args.max_hilos or 10))


def cmd_forocoches(args):
    if args.hilo:
        return _ejecuta_busqueda(args, "Forocoches", "hilos " + ", ".join(args.hilo),
                                 lambda red: forocoches_hilos_directos(red, args.hilo, args.max_paginas))
    if not args.query:
        err("Error: indica una busqueda, o --hilo NUMERO_O_ENLACE para leer hilos concretos.")
        return 2
    return _ejecuta_busqueda(args, "Forocoches", args.query,
                             lambda red: forocoches_buscar(red, args.query, max_hilos=args.max_hilos or 5,
                                                           pausa_min=args.pausa_min, pausa_max=args.pausa_max,
                                                           max_paginas=args.max_paginas))


def cmd_archivo(args):
    DIAG.comando = "foroleer.py " + " ".join(sys.argv[1:])
    try:
        with open(args.archivo, encoding="utf-8", errors="replace") as fh:
            contenido = fh.read()
    except OSError as e:
        err(f"Error: no se pudo leer {args.archivo}: {e}")
        return 1
    sitio = args.sitio or detecta_sitio_archivo(contenido, args.archivo)
    if not sitio:
        err("No se pudo determinar el sitio de origen de este archivo. Indicalo con "
            "--sitio {reddit,quora,swissforum,linkedin,forocoches}.")
        return 1
    out(f"[archivo] sitio detectado: {sitio}")
    if sitio == "swissforum":
        items = [swissforum_archivo_a_texto(contenido)]
    elif sitio == "forocoches":
        datos = forocoches_extraer_posts(contenido)
        items = [{"titulo": titulo_html(contenido) or args.archivo, "posts": datos["posts"], "muro": datos["muro"]}]
    elif sitio == "reddit":
        d = reddit_www_hilo(contenido)
        if not d["comentarios"] and not d["selftext"]:
            d = reddit_old_hilo(contenido)
        coms = list(d["comentarios"])
        if d["selftext"]:
            coms.insert(0, {"autor": d["autor"] or "[eliminado]", "fecha": d["fecha"], "profundidad": 0,
                            "puntos": d["puntos"], "texto": d["selftext"], "numero": "OP"})
        it = {"titulo": d["titulo"] or titulo_html(contenido) or args.archivo, "subreddit": d["subreddit"],
              "comentarios": coms}
        if not coms:
            it["aviso_calidad"] = "no se reconocio ningun mensaje en este archivo de Reddit"
        items = [it]
        if not args.output:
            ruta = nombre_hilo_reddit(d["subreddit"] or "reddit", it["titulo"], set())
            escribir_salida(ruta, "Reddit", args.archivo, items, "archivo")
            out(f"Archivo: {os.path.abspath(ruta)}")
            return 0
    elif sitio == "quora":
        resp = quora_pagina(contenido)
        items = quora_agrupa(resp)
        if not items:
            items = [{"titulo": quora_pregunta_titulo(contenido) or args.archivo, "comentarios": [],
                      "aviso_calidad": "no se reconocio ninguna respuesta de Quora en este archivo"}]
        if not args.output:
            carpeta, usados = ".", set()
            for it in items:
                ruta = nombre_hilo_quora(it["titulo"], usados)
                escribir_salida(ruta, "Quora", args.archivo, [it], "archivo")
                out(f"Archivo: {os.path.abspath(ruta)}")
            return 0
    elif sitio == "linkedin":
        items = [linkedin_pulse_archivo_a_texto(contenido)]
    else:
        items = [{"titulo": args.archivo, "texto": quitar_etiquetas_simple(contenido)}]
    ruta = args.output or (os.path.splitext(os.path.basename(args.archivo))[0] + "_leido.txt")
    escribir_salida(ruta, sitio, args.archivo, items, "archivo")
    out(f"Archivo: {os.path.abspath(ruta)}")
    return 0


def cmd_servidor(args):
    DIAG.comando = "foroleer.py servidor"
    p = Puente(args.puerto)
    try:
        p.arranca(en_proceso=True)
    except ErrorForoleer as e:
        err(f"Error: {e}")
        return 1
    if not p.propio:
        out("Ya hay un servidor activo; no hace falta arrancar otro.")
        return 0
    limite = max(0, getattr(args, "auto_cierre", 0) or 0) * 60
    out("Servidor activo. Las busquedas de foroleer.py lo reutilizan. "
        + (f"Se apaga solo tras {limite // 60} min sin uso." if limite else "Ctrl+C para parar."))
    motivo = "Parado."
    try:
        while True:
            time.sleep(1)
            with p.estado.lock:
                ocioso = time.time() - p.estado.ultimo_cliente
                libre = not p.estado.pendientes and not p.estado.en_curso
                if p.estado.apagar:
                    motivo = "Parado a peticion de otra sesion de foroleer."
                    break
                if limite and libre and ocioso > limite:
                    motivo = f"Parado solo: {limite // 60} min sin busquedas."
                    break
                # un trabajo entregado a la extension que nunca volvio no debe impedir el cierre
                for k in [k for k, t in p.estado.en_curso.items() if time.time() - t > 600]:
                    p.estado.en_curso.pop(k, None)
    except KeyboardInterrupt:
        pass
    out(motivo)
    p.para()
    return 0


def ultimos_archivos(carpeta, sufijo, n=None):
    """Rutas de los archivos de la carpeta que acaban en sufijo, del mas reciente al mas antiguo."""
    try:
        r = [os.path.join(carpeta, f) for f in os.listdir(carpeta) if f.endswith(sufijo)]
    except OSError:
        return []
    r.sort(key=lambda x: os.path.getmtime(x), reverse=True)
    return r[:n] if n else r


def _momento(txt):
    """'2026-11-11-16-15', '2026-11-11 16:15' o '16:15' (de hoy) -> segundos desde 1970."""
    nums = [int(x) for x in re.findall(r"\d+", txt or "")]
    if len(nums) in (2, 3) and nums[0] < 24:
        hoy = time.localtime()
        nums = [hoy.tm_year, hoy.tm_mon, hoy.tm_mday] + nums
    if len(nums) < 5:
        raise ErrorForoleer(f"No entiendo la fecha {txt!r}. Usa AAAA-MM-DD-HH-MM (por ejemplo 2026-11-11-16-15) o HH:MM.")
    nums = (nums + [0])[:6]
    return time.mktime((nums[0], nums[1], nums[2], nums[3], nums[4], nums[5], 0, 0, -1))


def cmd_reporte(args):
    """Reune en UN archivo .txt los registros de foroleer.py y de la extension de un periodo, para enviarlo."""
    DIAG.comando = "foroleer.py " + " ".join(sys.argv[1:])
    try:
        hasta = _momento(args.hasta) if args.hasta else time.time()
        desde = _momento(args.desde) if args.desde else hasta - args.horas * 3600
    except ErrorForoleer as e:
        err(f"Error: {e}")
        return 1
    margen = 120
    fmt = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))
    L = ["=" * ANCHO, f"REPORTE DE REGISTROS DE FOROLEER {VERSION}", f"Generado: {fmt(time.time())}",
         f"Periodo pedido: de {fmt(desde)} a {fmt(hasta)}", "=" * ANCHO, "", "1. ENTORNO"]
    L.extend(entorno())
    L += ["", "2. SERVIDOR DEL PUENTE AHORA"]
    try:
        est = pide_estado(args.puerto, 3)
        L.append(json.dumps({k: (round(v, 1) if isinstance(v, float) else v) for k, v in est.items()}, ensure_ascii=True))
        L.append("Extension: " + ("dando senales (hace %.0f s)" % est["sondeo_hace"] if est.get("sondeo_hace") is not None
                                  and est["sondeo_hace"] < 15 else "SIN senales recientes"))
    except Exception as e:
        L.append(f"(no hay servidor activo en el puerto {args.puerto}: {e})")
    L += ["", "3. ARCHIVOS DE REGISTRO (fecha de ultima escritura, tamano, nombre); * = incluido abajo"]
    elegidos = []
    for carpeta, sufijos in ((LOGDIR, ("logForoleer.log", "logForoleerExt.log")), (RESPDIR, (".txt",))):
        for suf in sufijos:
            for ruta in ultimos_archivos(carpeta, suf, 400):
                m = os.path.getmtime(ruta)
                nombre = os.path.basename(ruta)
                try:
                    ini = _momento(nombre[:19])
                except ErrorForoleer:
                    ini = m
                dentro = (m >= desde - margen and ini <= hasta + margen)
                if dentro and carpeta == LOGDIR:
                    elegidos.append(ruta)
                if dentro or m > time.time() - 86400:
                    L.append(f"{'*' if dentro and carpeta == LOGDIR else ' '} {fmt(m)} {os.path.getsize(ruta):>9} "
                             f"{os.path.join(os.path.basename(carpeta), nombre)}")
    if not elegidos:
        L.append("(ningun registro escrito en ese periodo; prueba con --horas 24)")
    L += ["", "4. CONTENIDO DE LOS REGISTROS DEL PERIODO (del mas antiguo al mas reciente)"]
    total = 0
    for ruta in sorted(elegidos, key=os.path.getmtime):
        try:
            with open(ruta, encoding="utf-8", errors="replace") as f:
                lineas = f.read().splitlines()
        except OSError as e:
            lineas = [f"(no se pudo leer: {e})"]
        tope = 1500 if ruta.endswith("Ext.log") else 500
        L += ["", "-" * ANCHO, f"ARCHIVO: {os.path.basename(ruta)}  (ultima escritura {fmt(os.path.getmtime(ruta))}; "
              f"{len(lineas)} lineas" + (f", se incluyen las ultimas {tope}" if len(lineas) > tope else "") + ")",
              "-" * ANCHO]
        L.extend(lineas[-tope:])
        total += min(len(lineas), tope)
        if total > 12000:
            L += ["", "(reporte cortado: demasiadas lineas; acota el periodo con --desde y --hasta)"]
            break
    L += ["", "5. PAGINAS GUARDADAS", f"Las paginas que foroleer no supo leer estan en {RESPDIR} (lista en el punto 3). "
          "Si se pide alguna, hay que enviarla aparte."]
    ruta = os.path.join(args.output or os.getcwd(), f"{time.strftime('%Y-%m-%d-%H-%M-%S')}reporteForoleer.txt")
    try:
        with open(ruta, "w", encoding="utf-8") as f:
            for linea in L:
                f.write("\n".join(envuelve(linea)) + "\n")
    except OSError as e:
        err(f"Error: no se pudo escribir el reporte: {e}")
        return 1
    out(f"Reporte: {ruta}")
    out(f"  {len(elegidos)} registro(s) del periodo {fmt(desde)} - {fmt(hasta)}."
        + (" Envia ese archivo." if elegidos else " No hay registros de ese rato: repite con --horas 24 y envialo."))
    return 0


def cierra_registro():
    """El registro de esta ejecucion pasa a llamarse con la fecha y hora de su ultima escritura."""
    if DIAG.ruta and os.path.exists(DIAG.ruta):
        nueva = os.path.join(LOGDIR, time.strftime("%Y-%m-%d-%H-%M-%S") + "logForoleer.log")
        try:
            if nueva != DIAG.ruta and not os.path.exists(nueva):
                os.replace(DIAG.ruta, nueva)
                DIAG.ruta = nueva
        except OSError:
            pass


def cmd_abrir(args):
    DIAG.comando = "foroleer.py abrir"
    red = Red("extension", args.puerto)
    try:
        red.prepara()
    except ErrorForoleer as e:
        err(f"Error: {e}")
        return _termina(red, args, f"ERROR: {e}", 1)
    out("La extension esta conectada y lista.")
    return _termina(red, args, "OK: extension conectada", 0)


def cmd_diagnostico(args):
    DIAG.comando = "foroleer.py diagnostico"
    out(f"Diagnostico de foroleer {VERSION}")
    for linea in entorno():
        out("  " + linea)
    red = Red("auto", args.puerto)
    fallos = 0
    try:
        red.prepara()
    except ErrorForoleer as e:
        err(f"Error: {e}")
        return _termina(red, args, f"ERROR: {e}", 1)
    pruebas = [
        ("swissforum", "https://swissforum.co.uk/search.json?q=zurich", "json"),
        ("forocoches", "https://forocoches.com/foro/search.php", "html"),
    ]
    modos = [red.modo] + (["directo"] if red.modo == "extension" else [])
    for modo in modos:
        out(f"Pruebas en modo {modo}:")
        red.modo = modo
        for nombre, url, esperado in pruebas:
            try:
                r = red.get(url, etiqueta="diag-" + nombre.replace(" ", "-"))
                bien = r.status == 200 and r.clase == esperado
                out(f"  {'OK   ' if bien else 'FALLA'} {nombre:<11} estado {r.status}, clase {r.clase}, {len(r.text)} car.")
                fallos += 0 if bien else 1
            except ErrorForoleer as e:
                err(f"  FALLA {nombre:<11} {e}")
                fallos += 1
    if red.modo == "extension" or "extension" in (red.modo or ""):
        red.modo = "extension"
        out("Pruebas de Reddit (paginas pintadas por Firefox, sin JSON):")
        for nombre, w in (("reddit www", "www"), ("reddit old", "old")):
            try:
                url = reddit_url_busqueda(w, "switzerland", None, "relevance", None, False)
                r = red.pagina(url, 6000 if w == "www" else 3000, "diag-" + nombre.replace(" ", "-"))
                hilos = reddit_www_busqueda(r.text) if w == "www" else reddit_old_busqueda(r.text)
                bien = bool(hilos)
                out(f"  {'OK   ' if bien else 'FALLA'} {nombre:<11} {len(hilos)} hilo(s) reconocidos, clase {r.clase}, "
                    f"{len(r.text)} car.")
                fallos += 0 if bien else 1
            except ErrorForoleer as e:
                err(f"  FALLA {nombre:<11} {e}")
                fallos += 1
    ruta_res = f"{fallos} prueba(s) fallida(s)" if fallos else "todas las pruebas bien"
    DIAG.resultado = f"diagnostico: {ruta_res}"
    red.cierra()
    ruta = escribe_informe()
    out(f"Resultado: {ruta_res}.")
    if ruta:
        out(f"Informe de diagnostico: {ruta}")
    return 0 if not fallos else 1


def main(argv=None):
    comun = argparse.ArgumentParser(add_help=False)
    comun.add_argument("--via", choices=["auto", "extension", "directo"], default="auto",
                       help="auto (por defecto): la extension si responde, si no directo; extension; directo")
    comun.add_argument("--puerto", type=int, default=PUERTO_DEFECTO, help="puerto del puente (8775)")
    busq = argparse.ArgumentParser(add_help=False, parents=[comun])
    busq.add_argument("--max-hilos", type=int, default=None, dest="max_hilos", help="hilos/temas a leer (10; forocoches 5)")
    busq.add_argument("--output", "-o", help="archivo .txt de salida")
    busq.add_argument("--texto", action="store_true", help="mostrar tambien todo el texto por pantalla")

    ap = argparse.ArgumentParser(prog="foroleer.py", description="Lee Reddit, Forocoches y swissforum.co.uk desde "
                                 "Termux a traves de la extension de Firefox y guarda el texto.")
    ap.add_argument("--version", action="version", version=f"foroleer {VERSION}")
    sub = ap.add_subparsers(dest="comando", required=True)

    p = sub.add_parser("reddit", parents=[busq], help="Busca en Reddit (todo o en subreddits)")
    p.add_argument("query")
    p.add_argument("--subreddit", "-r", action="append", help="subreddit (repetible o separados por comas)")
    p.add_argument("--sort", default="relevance", choices=["relevance", "top", "new", "comments"])
    p.add_argument("--tiempo", choices=["hour", "day", "week", "month", "year", "all"], help="filtro de fecha")
    p.add_argument("--max-comentarios", type=int, default=200, dest="max_comentarios")
    p.add_argument("--web", choices=["auto", "www", "old"], default="auto",
                   help="web de Reddit que se lee primero (la otra es el plan B). Por defecto auto: la busqueda en www y "
                        "los hilos en old.reddit.com, que no carga anuncios")
    p.add_argument("--desplazar", type=int, default=4, metavar="N",
                   help="veces que Firefox baja la pagina del hilo y pulsa 'ver mas respuestas' (0 = no; por defecto 4)")
    p.add_argument("--exacta", action="store_true", help="buscar la frase exacta (entre comillas)")
    p.set_defaults(func=cmd_reddit)

    p = sub.add_parser("quora", parents=[busq], help="Busca en Quora (un .txt por pregunta)")
    p.add_argument("query")
    p.add_argument("--desplazar", type=int, default=6, metavar="N", help="vueltas de scroll/'more' por pagina (6)")
    p.add_argument("--solo-busqueda", action="store_true", dest="solo_busqueda",
                   help="no abrir cada pregunta: solo las respuestas que muestra la pagina de busqueda")
    p.set_defaults(func=cmd_quora)

    p = sub.add_parser("quora-tema", parents=[busq], help="Lee temas de Quora (p. ej. Switzerland), un .txt por pregunta")
    p.add_argument("temas", nargs="*", help="nombres o direcciones de tema (Switzerland, Zurich-city...)")
    p.add_argument("--buscar-temas", dest="buscar_temas", metavar="PALABRAS",
                   help="descubre temas con la busqueda de temas de Quora (palabras separadas por comas)")
    p.add_argument("--palabra", metavar="TROZOS", help="solo temas cuyo nombre contenga alguno de estos trozos (swit,suisse,zurich)")
    p.add_argument("--titulo", metavar="TROZOS", help="solo preguntas cuyo titulo contenga alguno de estos trozos")
    p.add_argument("--leer", action="store_true", help="abrir cada pregunta para leer todas sus respuestas")
    p.add_argument("--desplazar", type=int, default=6, metavar="N", help="vueltas de scroll/'more' por pagina (6)")
    p.set_defaults(func=cmd_quora_tema)

    p = sub.add_parser("swissforum", parents=[busq], help="Busca en swissforum.co.uk")
    p.add_argument("query")
    p.set_defaults(func=cmd_swissforum)

    p = sub.add_parser("forocoches", parents=[busq], help="Busca en Forocoches (hace falta sesion) o lee hilos")
    p.add_argument("query", nargs="?", help="busqueda (necesita tu sesion de Forocoches en Firefox)")
    p.add_argument("--hilo", action="append", help="leer un hilo concreto (numero o enlace); repetible")
    p.add_argument("--max-paginas", type=int, default=3, dest="max_paginas", help="paginas por hilo (3)")
    p.add_argument("--pausa-min", type=float, default=3.0, dest="pausa_min")
    p.add_argument("--pausa-max", type=float, default=6.0, dest="pausa_max")
    p.set_defaults(func=cmd_forocoches)

    p = sub.add_parser("archivo", help="Lee una pagina .html guardada (SingleFile)")
    p.add_argument("archivo")
    p.add_argument("--sitio", choices=["reddit", "quora", "swissforum", "linkedin", "forocoches"])
    p.add_argument("--output", "-o")
    p.set_defaults(func=cmd_archivo)

    p = sub.add_parser("servidor", parents=[comun], help="Deja el puente activo en primer plano")
    p.add_argument("--auto-cierre", type=int, default=0, dest="auto_cierre", metavar="MIN",
                   help="apagarse tras MIN minutos sin busquedas (0 = no apagarse; por defecto 0)")
    p.set_defaults(func=cmd_servidor)
    p = sub.add_parser("reporte", parents=[comun], help="Reune los registros de un periodo en un .txt para enviarlo")
    p.add_argument("--desde", help="inicio: AAAA-MM-DD-HH-MM o HH:MM de hoy")
    p.add_argument("--hasta", help="fin: AAAA-MM-DD-HH-MM o HH:MM de hoy (por defecto, ahora)")
    p.add_argument("--horas", type=float, default=3, help="si no hay --desde: horas hacia atras (3)")
    p.add_argument("--output", "-o", help="carpeta donde dejar el reporte (por defecto, la actual)")
    p.set_defaults(func=cmd_reporte)
    p = sub.add_parser("abrir", parents=[comun], help="Despierta la extension y comprueba la conexion")
    p.set_defaults(func=cmd_abrir)
    p = sub.add_parser("diagnostico", parents=[comun], help="Prueba todo y escribe un informe .txt")
    p.set_defaults(func=cmd_diagnostico)

    args = ap.parse_args(argv)
    try:
        return _corre(args)
    finally:
        cierra_registro()


def _corre(args):
    try:
        return args.func(args)
    except KeyboardInterrupt:
        err("Interrumpido.")
        DIAG.resultado = "interrumpido"
        return 130
    except Exception as e:  # error inesperado: se deja todo en el informe
        import traceback
        err(f"Error inesperado: {e}")
        DIAG.linea("TRACE", traceback.format_exc())
        DIAG.resultado = f"ERROR inesperado: {e}"
        ruta = escribe_informe()
        if ruta:
            err(f"Informe de diagnostico: {ruta}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
