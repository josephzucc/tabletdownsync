#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pesthist.py - Pestañas e historial de Firefox (Android) desde Termux.   v2.0

  python pesthist.py pestanas [opciones] [búsqueda]   lista las pestañas abiertas
  python pesthist.py ir N                             va a la pestaña N
  python pesthist.py cerrar N [M 5-9 ...] | todas     cierra pestañas
  python pesthist.py historial [opciones] búsqueda    busca en el historial
  python pesthist.py abrir N                          abre en Firefox el resultado N
  python pesthist.py actualizar [DÍAS]                descarga el historial (Sync)
  python pesthist.py estado                           estado del puente y archivos
  python pesthist.py parar                            detiene el servidor puente
  python pesthist.py ayuda                            ayuda completa

La lista completa de pestañas se lee de Firefox Sync y el cierre usa la orden
oficial "cerrar pestañas en otro dispositivo" de Mozilla. La extensión
PestHist (>= 1.3) solo se usa para seleccionar pestañas cargadas ("ir"). El historial se busca en los archivos JSON que genera
pesthist_sync.sh (Firefox Sync).
"""

import base64
import csv
import glob
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "2.0"
PUERTO = int(os.environ.get("PESTHIST_PUERTO", "8765"))
BASE = "http://127.0.0.1:%d" % PUERTO
HOME = os.path.expanduser("~")
CONF = os.path.join(HOME, ".config", "pesthist")
CACHE = os.path.join(HOME, ".cache", "pesthist")
TOKEN_F = os.path.join(CONF, "token")
LOG_F = os.path.join(CACHE, "servidor.log")
ULT_TABS = os.path.join(CACHE, "ultima_lista_pestanas.json")
ULT_HIST = os.path.join(CACHE, "ultima_busqueda_historial.json")
FIREFOX = os.environ.get("PESTHIST_FIREFOX", "org.mozilla.firefox")
DESCARGAS = os.path.join(HOME, "storage", "downloads")
DIR_PROG = os.path.dirname(os.path.abspath(__file__))
DAY = 86400000

AYUDA = """
PESTHIST - pestañas e historial de Firefox desde Termux          v%s
==========================================================================

PESTAÑAS ABIERTAS (lista completa vía Firefox Sync)
  pestanas [búsqueda] [opciones]
      -d N          solo las no usadas desde hace al menos N días
      --en CAMPO    ambos | titulo | url   (dónde buscar; por defecto ambos)
      -o ORDEN      antiguas | recientes | titulo  (def. antiguas)
      -c            usar la última lista descargada (sin conectar a Sync)
      --dispositivo NOMBRE   elegir el dispositivo (se recuerda)
      --txt         además guarda el resultado en un .txt en esta carpeta
  ir N              abre Firefox en la pestaña N (si no está cargada en
                    memoria, abre su URL en una pestaña nueva)
  cerrar N [M ...]  cierra pestañas por número; admite rangos: 3-7
  cerrar todas      cierra todas las de la última lista (pide confirmación)
      -s            no pedir confirmación
  dispositivo       muestra/elige el dispositivo (móvil) cuyas pestañas usar

HISTORIAL (archivos JSON/CSV de Firefox Sync)
  historial [búsqueda] [opciones]
      -d N          solo visitas de los últimos N días
      --en CAMPO    ambos | titulo | url
      -o ORDEN      reciente | antigua | visitas | titulo  (def. reciente)
      -a ARCHIVO    archivo(s) a usar (se puede repetir, admite * )
                    por defecto: el historial_firefox_*.json más reciente
                    de esta carpeta, de Descargas o de la del programa
      --txt         además guarda el resultado en un .txt
  abrir N           abre en Firefox el resultado N de la última búsqueda
  actualizar [N]    descarga el historial con pesthist_sync.sh (N días)

OTROS
  estado            servidor, conexión con la extensión y archivos
  servidor          arranca el servidor puente en primer plano (depuración)
  parar             detiene el servidor puente
  ayuda             esta ayuda
  Opción global --sin-color: salida sin colores.

SINTAXIS DE BÚSQUEDA (igual que en la extensión)
  palabra                 contiene la palabra
  palabra1+palabra2       contiene las dos (en título o URL)
  palabra1 palabra2       igual que con +
  "un texto"              texto exacto (las comillas agrupan en bash)
  -palabra                excluir     -"un texto"  excluir texto
  Sin distinguir mayúsculas ni tildes.

EJEMPLOS
  python pesthist.py pestanas -d 30
  python pesthist.py pestanas infojobs+madrid
  python pesthist.py cerrar 2 5 8-12
  python pesthist.py historial "protección de datos" -wiki
  python pesthist.py historial programador+net -d 90 --txt
""" % VERSION


# ==========================================================================
#  Utilidades de salida
# ==========================================================================

COLOR = sys.stdout.isatty() and "--sin-color" not in sys.argv
if "--sin-color" in sys.argv:
    sys.argv.remove("--sin-color")


def c(txt, cod):
    return "\033[%sm%s\033[0m" % (cod, txt) if COLOR else txt


def info(msg):
    print(c(msg, "36"))


def aviso(msg):
    print(c(msg, "33"))


def morir(msg, codigo=1):
    print(c("ERROR: " + msg, "31"), file=sys.stderr)
    sys.exit(codigo)


# ==========================================================================
#  Búsqueda (misma lógica que query.js de la extensión)
# ==========================================================================

def norm(s):
    s = unicodedata.normalize("NFD", (s or "").lower())
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def safe_decode(s):
    try:
        return urllib.parse.unquote((s or "").replace("+", " "))
    except Exception:
        return s or ""


RE_TERM = re.compile(r'(-?)"([^"]*)"|([^\s+"]+)')
RE_SEP = re.compile(r"[-_+./=&?#:%~,;!()\[\]]+")


def parse_query(q):
    terms = []
    for m in RE_TERM.finditer(q or ""):
        if m.group(2) is not None:
            t = re.sub(r"\s+", " ", norm(m.group(2))).strip()
            if t:
                terms.append({"t": t, "neg": m.group(1) == "-", "phrase": True})
        else:
            w = m.group(3)
            neg = False
            if w.startswith("-"):
                if len(w) == 1:
                    continue
                neg, w = True, w[1:]
            t = norm(w)
            if t:
                terms.append({"t": t, "neg": neg, "phrase": False})
    return terms


def args_a_query(tokens):
    """Convierte los argumentos de bash en texto de búsqueda.
    Un argumento con espacios (venía entre comillas) se trata como texto exacto."""
    partes = []
    for t in tokens:
        if re.search(r"\s", t) and '"' not in t:
            neg = t.startswith("-")
            partes.append(('-"' if neg else '"') + (t[1:] if neg else t) + '"')
        else:
            partes.append(t)
    return " ".join(partes)


def prep(item):
    item["_t"] = re.sub(r"\s+", " ", norm(item.get("title")))
    item["_u"] = re.sub(r"\s+", " ", norm(safe_decode(item.get("url"))))
    item["_u2"] = re.sub(r"\s+", " ", RE_SEP.sub(" ", item["_u"]))
    return item


def coincide(item, terms, campo):
    if not terms:
        return True
    en_t = campo != "url"
    en_u = campo != "titulo"
    for x in terms:
        hay = (en_t and x["t"] in item["_t"]) or \
              (en_u and (x["t"] in item["_u"] or x["t"] in item["_u2"]))
        if (x["neg"] and hay) or (not x["neg"] and not hay):
            return False
    return True


def resaltar(texto, terms):
    """Devuelve el texto con las coincidencias resaltadas (ANSI)."""
    texto = texto or ""
    pos = [x["t"] for x in terms if not x["neg"] and x["t"]]
    if not COLOR or not pos or not texto:
        return texto
    n, mapa = "", []
    for i, ch in enumerate(texto):
        cn = norm(ch)
        n += cn
        mapa.extend([i] * len(cn))
    marca = [False] * len(texto)
    for t in pos:
        i = n.find(t)
        while i != -1:
            for j in range(mapa[i], mapa[i + len(t) - 1] + 1):
                marca[j] = True
            i = n.find(t, i + 1)
    out, dentro = [], False
    for i, ch in enumerate(texto):
        if marca[i] and not dentro:
            out.append("\033[30;43m")
            dentro = True
        elif not marca[i] and dentro:
            out.append("\033[0m")
            dentro = False
        out.append(ch)
    if dentro:
        out.append("\033[0m")
    return "".join(out)


# ==========================================================================
#  Fechas y formato
# ==========================================================================

def fmt_fecha(ms):
    if not ms:
        return "(desconocida)"
    return datetime.fromtimestamp(ms / 1000).strftime("%d/%m/%Y %H:%M")


def fmt_hace(ms, ahora=None):
    if not ms:
        return "?"
    dif = max(0, (ahora or time.time() * 1000) - ms)
    mins = int(dif // 60000)
    if mins < 60:
        return "%d min" % mins
    h = mins // 60
    if h < 24:
        return "%d h" % h
    return "%d d" % (h // 24)


def estado_tab(t):
    s = []
    if t.get("active"):
        s.append("Activa")
    if t.get("pinned"):
        s.append("Fijada")
    if t.get("discarded"):
        s.append("Descargada")
    if t.get("incognito"):
        s.append("Privada")
    return ",".join(s) or "-"


def plural(n, uno, varios):
    return "%d %s" % (n, uno if n == 1 else varios)


def sello():
    return datetime.now().strftime("%Y%m%d_%H%M")


def cabecera_txt(titulo, lineas):
    sep = "=" * 78
    return "\n".join([sep, titulo, "Generado: " + fmt_fecha(time.time() * 1000)] +
                     lineas + [sep, ""])


def tabs_txt(lista, filtro):
    ahora = time.time() * 1000
    out = [cabecera_txt("PESTAÑAS ABIERTAS - PestHist (Termux)", [filtro, "Total: %d" % len(lista)])]
    out.append("  Nº  Último uso         Inactiva  Misma URL")
    out.append("----  ----------------   --------  ---------")
    for t in lista:
        out.append("%4d  %-16s   %8s  %9s" % (t["n"], fmt_fecha(t["last"]), fmt_hace(t["last"], ahora),
                                            ("x%d" % t["repes"]) if t.get("repes", 1) > 1 else ""))
        out.append("      Título: " + (t["title"] or "(sin título)"))
        out.append("      URL:    " + t["url"])
        out.append("")
    return "\n".join(out)


def hist_txt(lista, filtro):
    out = [cabecera_txt("BÚSQUEDA EN EL HISTORIAL - PestHist (Termux)",
                        [filtro, "Resultados: %d" % len(lista)])]
    out.append("   Nº  Última visita      Visitas")
    out.append("-----  ----------------   -------")
    for h in lista:
        out.append("%5d  %-16s   %7d" % (h["n"], fmt_fecha(h["last"]), h["visits"]))
        out.append("       Título: " + (h["title"] or "(sin título)"))
        out.append("       URL:    " + h["url"])
        out.append("")
    return "\n".join(out)


def guardar_txt(prefijo, texto):
    ruta = os.path.join(os.getcwd(), "%s_%s.txt" % (prefijo, sello()))
    with open(ruta, "w", encoding="utf-8") as f:
        f.write(texto)
    info("Guardado: " + ruta)


def guardar_json(ruta, datos):
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False)


def leer_json(ruta):
    try:
        with open(ruta, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# ==========================================================================
#  Opciones de línea de órdenes
# ==========================================================================

def parse_opts(args, spec, listas=()):
    """spec: {'-d': ('dias', True), '--txt': ('txt', False), ...}
    Lo que no es una opción conocida queda en 'resto' (texto de búsqueda),
    así que '-palabra' funciona como exclusión."""
    opts, resto, i = {}, [], 0
    while i < len(args):
        a = args[i]
        if a == "--":
            resto += args[i + 1:]
            break
        if a in spec:
            nombre, con_valor = spec[a]
            if con_valor:
                if i + 1 >= len(args):
                    morir("falta el valor de " + a)
                if nombre in listas:
                    opts.setdefault(nombre, []).append(args[i + 1])
                else:
                    opts[nombre] = args[i + 1]
                i += 2
                continue
            opts[nombre] = True
        else:
            resto.append(a)
        i += 1
    return opts, resto


def entero(v, nombre):
    try:
        n = int(v)
        if n < 0:
            raise ValueError
        return n
    except (TypeError, ValueError):
        morir("%s debe ser un número entero positivo (recibido: %s)" % (nombre, v))


def elegir(v, validos, nombre):
    if v not in validos:
        morir("%s debe ser uno de: %s" % (nombre, ", ".join(validos)))
    return v


# ==========================================================================
#  Servidor puente (Termux <-> extensión)
# ==========================================================================

def token():
    try:
        with open(TOKEN_F, encoding="utf-8") as f:
            t = f.read().strip()
            if t:
                return t
    except OSError:
        pass
    os.makedirs(CONF, exist_ok=True)
    t = secrets.token_hex(16)
    with open(TOKEN_F, "w", encoding="utf-8") as f:
        f.write(t)
    os.chmod(TOKEN_F, 0o600)
    return t


class Estado:
    def __init__(self):
        self.cv = threading.Condition()
        self.pend = None          # orden pendiente {id, accion, datos}
        self.res = {}             # resultados por id
        self.seq = 0
        self.ultimo = 0.0         # último contacto de la extensión
        self.esperando = 0        # peticiones de la extensión en espera
        self.version_ext = None
        self.token = token()


ESTADO = None
SERVIDOR = None


class Manejador(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _responder(self, codigo, datos=None):
        cuerpo = b"" if datos is None else json.dumps(datos, ensure_ascii=False).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if cuerpo:
            self.wfile.write(cuerpo)

    def _cuerpo(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            return {}

    def _es_ext(self):
        return self.headers.get("X-PestHist") == "1"

    def _es_cli(self):
        return secrets.compare_digest(self.headers.get("X-Token", ""), ESTADO.token)

    def do_GET(self):
        ruta = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(ruta.query)
        e = ESTADO

        if ruta.path == "/ext/esperar":
            if not self._es_ext():
                return self._responder(403)
            limite = time.time() + 25
            with e.cv:
                e.ultimo = time.time()
                e.esperando += 1
                try:
                    while True:
                        if e.pend is not None and e.pend["id"] not in e.res:
                            orden = e.pend
                            break
                        resta = limite - time.time()
                        if resta <= 0:
                            orden = None
                            break
                        e.cv.wait(resta)
                finally:
                    e.esperando -= 1
                    e.ultimo = time.time()
            return self._responder(200, orden) if orden else self._responder(204)

        if not self._es_cli():
            return self._responder(403)

        if ruta.path == "/cli/estado":
            with e.cv:
                conectada = e.esperando > 0 or (time.time() - e.ultimo) < 8
                hace = (time.time() - e.ultimo) if e.ultimo else None
            return self._responder(200, {"version": VERSION, "extension_conectada": conectada,
                                         "ultimo_contacto_seg": hace, "pid": os.getpid()})

        if ruta.path == "/cli/resultado":
            oid = int((q.get("id") or ["0"])[0])
            espera = min(float((q.get("espera") or ["5"])[0]), 20)
            limite = time.time() + espera
            with e.cv:
                while oid not in e.res:
                    resta = limite - time.time()
                    if resta <= 0:
                        break
                    e.cv.wait(resta)
                r = e.res.pop(oid, None)
            return self._responder(200, r) if r else self._responder(204)

        return self._responder(404)

    def do_POST(self):
        ruta = urllib.parse.urlparse(self.path).path
        e = ESTADO
        datos = self._cuerpo()

        if ruta == "/ext/resultado":
            if not self._es_ext():
                return self._responder(403)
            with e.cv:
                e.ultimo = time.time()
                if e.pend is not None and e.pend["id"] == datos.get("id"):
                    e.res[datos["id"]] = datos
                    e.pend = None
                    e.cv.notify_all()
            return self._responder(204)

        if not self._es_cli():
            return self._responder(403)

        if ruta == "/cli/orden":
            with e.cv:
                e.seq += 1
                e.pend = {"id": e.seq, "accion": datos.get("accion"), "datos": datos.get("datos") or {}}
                e.res.clear()
                e.cv.notify_all()
                oid = e.seq
            return self._responder(200, {"id": oid})

        if ruta == "/cli/cancelar":
            with e.cv:
                if e.pend is not None and e.pend["id"] == datos.get("id"):
                    e.pend = None
            return self._responder(204)

        if ruta == "/cli/parar":
            self._responder(204)
            threading.Thread(target=SERVIDOR.shutdown, daemon=True).start()
            return None

        return self._responder(404)


def servidor():
    global ESTADO, SERVIDOR
    ESTADO = Estado()
    try:
        SERVIDOR = ThreadingHTTPServer(("127.0.0.1", PUERTO), Manejador)
    except OSError as ex:
        morir("no se pudo abrir el puerto %d: %s" % (PUERTO, ex))
    SERVIDOR.daemon_threads = True
    print("PestHist: servidor puente en %s (pid %d). Ctrl+C para parar." % (BASE, os.getpid()), flush=True)
    try:
        SERVIDOR.serve_forever()
    except KeyboardInterrupt:
        pass
    print("PestHist: servidor detenido.", flush=True)


# ==========================================================================
#  Cliente: órdenes a la extensión
# ==========================================================================

def api(metodo, ruta, cuerpo=None, timeout=30):
    req = urllib.request.Request(
        BASE + ruta, method=metodo,
        data=json.dumps(cuerpo).encode("utf-8") if cuerpo is not None else None,
        headers={"X-Token": token(), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            txt = r.read()
            return r.status, (json.loads(txt) if txt else None)
    except urllib.error.HTTPError as ex:
        return ex.code, None


def servidor_activo():
    try:
        st, d = api("GET", "/cli/estado", timeout=3)
        return d if st == 200 else None
    except (urllib.error.URLError, OSError):
        return None


def asegurar_servidor():
    if servidor_activo():
        return
    os.makedirs(CACHE, exist_ok=True)
    log = open(LOG_F, "a", encoding="utf-8")
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "servidor"],
                     stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                     start_new_session=True, cwd=HOME)
    for _ in range(50):
        time.sleep(0.1)
        if servidor_activo():
            return
    morir("no arrancó el servidor puente; mira %s" % LOG_F)


def am(*args):
    if not shutil.which("am"):
        return False
    try:
        return subprocess.call(["am"] + list(args), stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=15) == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def abrir_firefox():
    return am("start", "-a", "android.intent.action.MAIN",
              "-c", "android.intent.category.LAUNCHER", "-p", FIREFOX)


def volver_termux():
    am("start", "-n", "com.termux/.app.TermuxActivity")


def esperar_resultado(oid, segundos):
    limite = time.time() + segundos
    while True:
        resta = limite - time.time()
        if resta <= 0:
            return None
        st, d = api("GET", "/cli/resultado?id=%d&espera=%.1f" % (oid, min(resta, 5)),
                    timeout=min(resta, 5) + 10)
        if st == 200 and d:
            return d


def orden(accion, datos=None, volver=True):
    asegurar_servidor()
    st, d = api("POST", "/cli/orden", {"accion": accion, "datos": datos or {}})
    if st != 200 or not d:
        morir("el servidor puente no aceptó la orden (código %s)" % st)
    oid = d["id"]
    res = esperar_resultado(oid, 3)
    if res is None:
        info("Firefox no responde (Android lo pausa en segundo plano). "
             "Lo abro un momento para despertar la extensión...")
        if abrir_firefox():
            res = esperar_resultado(oid, 20)
            if volver:
                volver_termux()
        else:
            res = esperar_resultado(oid, 12)
    if res is None:
        api("POST", "/cli/cancelar", {"id": oid})
        morir("la extensión PestHist no responde.\n"
              "  - Comprueba que está instalada y activada en Firefox (versión 1.2 o superior).\n"
              "  - Abre Firefox, espera unos segundos, vuelve a Termux y repite.\n"
              "  - 'python pesthist.py estado' muestra si la extensión está conectada.")
    if not res.get("ok"):
        morir("Firefox respondió: " + str(res.get("error")))
    return res.get("datos")


# ==========================================================================
#  FIREFOX SYNC / MOZILLA ACCOUNTS (lista completa y cierre remoto)
# ==========================================================================
#  Firefox para Android solo deja a las extensiones ver las pestañas
#  CARGADAS en memoria (bug 1583281 de Mozilla). La lista completa se lee
#  de Firefox Sync (el móvil sube ahí todas sus pestañas) y el cierre se
#  hace con la orden oficial "cerrar pestañas en otro dispositivo"
#  (https://identity.mozilla.com/cmd/close-uri/v1), igual que hace Firefox
#  de escritorio. Usa la sesión que creó "pesthist_sync.sh login".

SESION_FFS = os.path.join(HOME, ".config", "firefox-sync-client.secret")
CONFIG_F = os.path.join(CONF, "config.json")
TABS_SYNC_F = os.path.join(CACHE, "pestanas_sync.json")
COOKIE_F = os.path.join(CACHE, "fastly_cookie.json")
FXA_API = os.environ.get("PESTHIST_FXA_API", "https://api.accounts.firefox.com/v1")
FASTLY_PAGE = "https://accounts.firefox.com/"
FASTLY_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36")
CMD_CERRAR = "https://identity.mozilla.com/cmd/close-uri/v1"
CMD_TTL = 2 * 24 * 3600
MAX_CHUNK = 3500   # bytes de JSON por orden de cierre (un solo registro ECE)


class ErrorFxA(Exception):
    pass


def leer_config():
    return leer_json(CONFIG_F) or {}


def guardar_config(cfg):
    guardar_json(CONFIG_F, cfg)


def b64u_dec(s):
    s = s.strip()
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def b64u_enc(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def hkdf(ikm, info, n, salt=b""):
    """HKDF-SHA256 (RFC 5869)."""
    prk = hmac.new(salt or b"\x00" * 32, ikm, hashlib.sha256).digest()
    out, t, i = b"", b"", 1
    while len(out) < n:
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        out += t
        i += 1
    return out[:n]


def sesion_ffs():
    d = leer_json(SESION_FFS)
    if not d or not d.get("sessionToken") or not d.get("keyB"):
        raise ErrorFxA("no hay sesión de Firefox Sync. Ejecuta: bash pesthist_sync.sh login")
    return bytes.fromhex(d["sessionToken"]), bytes.fromhex(d["keyB"])


def hawk_cabecera(metodo, url, cuerpo, clave, ident):
    """Cabecera Hawk (misma construcción que ffsclient/hawk.go)."""
    u = urllib.parse.urlsplit(url)
    puerto = str(u.port or (443 if u.scheme == "https" else 80))
    ruta = u.path + ("?" + u.query if u.query else "")
    h = ""
    if metodo != "GET" and cuerpo:
        h = base64.b64encode(hashlib.sha256(
            ("hawk.1.payload\napplication/json\n" + cuerpo + "\n").encode("utf-8")).digest()).decode()
    ts = str(int(time.time()))
    nonce = base64.b64encode(os.urandom(5)).decode()
    normal = "\n".join(["hawk.1.header", ts, nonce, metodo, ruta, u.hostname.lower(),
                        puerto, h, "", ""])
    mac = base64.b64encode(hmac.new(clave, normal.encode("utf-8"), hashlib.sha256).digest()).decode()
    cab = 'Hawk id="%s", mac="%s", ts="%s", nonce="%s"' % (ident, mac, ts, nonce)
    if h:
        cab += ', hash="%s"' % h
    return cab


# ---- Protección anti-bots de Fastly (portado de ffsclient/fastly.go) ----

def _fastly_pow(base, objetivo):
    cs = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    objetivo = objetivo.lower()
    import itertools
    for n in range(1, 4):
        for tup in itertools.product(cs, repeat=n):
            suf = "".join(tup)
            if hashlib.sha256((base + suf).encode()).hexdigest() == objetivo:
                return suf
    return None


def resolver_fastly():
    import http.cookiejar
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    dominio = "https://accounts.firefox.com"

    def pedir(url, datos=None, cab=None, metodo=None):
        h = {"User-Agent": FASTLY_UA}
        h.update(cab or {})
        req = urllib.request.Request(url, data=datos, headers=h, method=metodo)
        try:
            with op.open(req, timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as ex:
            return ex.read().decode("utf-8", "replace")

    html = pedir(FASTLY_PAGE, cab={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
    m = re.search(r"_fs-ch-([^/'\"?\s]+)", html)
    if not m:
        raise ErrorFxA("reto anti-bot de Mozilla: no se encontró el script")
    sid = m.group(1)
    js = pedir("%s/_fs-ch-%s/script.js?reload=true" % (dominio, sid),
               cab={"Referer": FASTLY_PAGE, "Accept": "*/*"})
    m = re.search(r'init\(\[[^\]]*\],\s*"([^"]+)"', js)
    if not m:
        raise ErrorFxA("reto anti-bot de Mozilla: no se encontró el token")
    tok = m.group(1)
    pedir("%s/_fs-ch-%s/pat?token=%s" % (dominio, sid, tok), datos=b"", metodo="POST",
          cab={"Referer": FASTLY_PAGE, "Accept": "text/plain", "Content-Type": "application/json"})

    def post_back(token, data):
        cuerpo = '{"token":%s,"data":%s}' % (json.dumps(token), json.dumps(data, separators=(",", ":")))
        txt = pedir("%s/_fs-ch-%s/fst-post-back" % (dominio, sid), datos=cuerpo.encode(), metodo="POST",
                    cab={"Content-Type": "application/json", "Accept": "application/json",
                         "Referer": FASTLY_PAGE})
        return json.loads(txt)

    ini = post_back(tok, [{"ty": "pat", "auth": ""}])
    soluciones = []
    for ch in ini.get("ch", []):
        if ch.get("ty") == "pow":
            d = ch.get("data", {})
            resp = _fastly_pow(d.get("base", ""), d.get("hash", ""))
            if resp is None:
                raise ErrorFxA("reto anti-bot de Mozilla: prueba de trabajo sin solución")
            soluciones.append({"ty": "pow", "base": d.get("base"), "answer": resp,
                               "hmac": d.get("hmac"), "expires": d.get("expires")})
        elif ch.get("ty") == "clientmetrics":
            soluciones.append({"ty": "clientmetrics", "webdriver": False,
                               "bot_detection_result": {"bot_detected": False, "bot_kind": None},
                               "browser_metrics": {"client_data": "{}", "error_trace": '""'}})
    fin = post_back(ini.get("tok", tok), soluciones)
    if fin.get("status") != "success":
        raise ErrorFxA("reto anti-bot de Mozilla no superado (%s)" % fin.get("status"))
    cookies = [c for c in jar if c.name.startswith("_fs_ch_cp_")] or list(jar)
    if not cookies:
        raise ErrorFxA("reto anti-bot de Mozilla: no se recibió la cookie")
    valor = "%s=%s" % (cookies[0].name, cookies[0].value)
    guardar_json(COOKIE_F, {"cookie": valor, "caduca": time.time() + 50 * 60})
    return valor


def fxa(metodo, ruta, cuerpo=None):
    """Petición autenticada a la API de cuentas Mozilla con el sessionToken."""
    st, _ = sesion_ffs()
    km = hkdf(st, b"identity.mozilla.com/picl/v1/sessionToken", 96)
    ident, clave = km[:32].hex(), km[32:64]
    url = FXA_API + ruta
    datos = json.dumps(cuerpo, separators=(",", ":")) if cuerpo is not None else ""
    for intento in (1, 2):
        cab = {"Content-Type": "application/json", "Accept": "application/json",
               "User-Agent": "firefox-sync-client/1.10.0 pesthist/" + VERSION,
               "Authorization": hawk_cabecera(metodo, url, datos, clave, ident)}
        ck = leer_json(COOKIE_F)
        if ck and ck.get("caduca", 0) > time.time():
            cab["Cookie"] = ck["cookie"]
        req = urllib.request.Request(url, data=datos.encode("utf-8") if datos else None,
                                     headers=cab, method=metodo)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                txt = r.read()
                return json.loads(txt) if txt.strip() else None
        except urllib.error.HTTPError as ex:
            txt = ex.read().decode("utf-8", "replace")
            if ex.code == 406 and intento == 1:
                resolver_fastly()
                continue
            if ex.code == 401:
                raise ErrorFxA("Mozilla rechaza la sesión (401). Repite: bash pesthist_sync.sh login"
                               "\n  Detalle: " + txt[:300])
            raise ErrorFxA("Mozilla respondió %d: %s" % (ex.code, txt[:300]))
        except urllib.error.URLError as ex:
            raise ErrorFxA("sin conexión con Mozilla: %s" % ex.reason)
    raise ErrorFxA("no se pudo completar la petición a Mozilla")


def descifrar_claves_orden(paquete_json, kb):
    """Descifra las claves públicas de una orden (formato Sync 1.5, clave oldsync)."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    p = json.loads(paquete_json)
    ksync = hkdf(kb, b"identity.mozilla.com/picl/v1/oldsync", 64)
    k_enc, k_mac = ksync[:32], ksync[32:]
    kxcs = hashlib.sha256(kb).digest()[:16]
    if p.get("kid") and bytes.fromhex(p["kid"]) != kxcs:
        raise ErrorFxA("las claves de Sync no coinciden (¿cambiaste la contraseña? repite el login)")
    calc = hmac.new(k_mac, p["ciphertext"].encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, p["hmac"]):
        raise ErrorFxA("firma incorrecta en las claves del dispositivo")
    d = Cipher(algorithms.AES(k_enc), modes.CBC(base64.b64decode(p["IV"]))).decryptor()
    claro = d.update(base64.b64decode(p["ciphertext"])) + d.finalize()
    claro = claro[:-claro[-1]]
    return json.loads(claro.decode("utf-8"))


def ece_cifrar(datos, clave_pub_b64, secreto_b64, sal=None, privada=None):
    """Cifrado Web Push aes128gcm (RFC 8188 + RFC 8291), un solo registro."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    ua_pub = b64u_dec(clave_pub_b64)
    auth = b64u_dec(secreto_b64)
    priv = privada or ec.generate_private_key(ec.SECP256R1())
    as_pub = priv.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    remoto = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub)
    compartido = priv.exchange(ec.ECDH(), remoto)
    ikm = hkdf(compartido, b"WebPush: info\x00" + ua_pub + as_pub, 32, salt=auth)
    sal = sal or os.urandom(16)
    cek = hkdf(ikm, b"Content-Encoding: aes128gcm\x00", 16, salt=sal)
    nonce = hkdf(ikm, b"Content-Encoding: nonce\x00", 12, salt=sal)
    rs = max(4096, len(datos) + 17)
    cifrado = AESGCM(cek).encrypt(nonce, datos + b"\x02", None)
    return sal + rs.to_bytes(4, "big") + bytes([len(as_pub)]) + as_pub + cifrado


def dispositivos_fxa():
    lista = fxa("GET", "/account/devices")
    return lista if isinstance(lista, list) else []


def dispositivo_destino(nombre):
    devs = [d for d in dispositivos_fxa() if d.get("name") == nombre]
    if not devs:
        raise ErrorFxA("no encuentro el dispositivo '%s' en tu cuenta Mozilla. "
                       "Usa: python pesthist.py dispositivo" % nombre)
    con = [d for d in devs if CMD_CERRAR in (d.get("availableCommands") or {})]
    if not con:
        raise ErrorFxA("el Firefox de '%s' no anuncia la orden de cerrar pestañas remotas.\n"
                       "  Actualiza Firefox en el móvil y comprueba que la sincronización está activa." % nombre)
    return max(con, key=lambda d: d.get("lastAccessTime") or 0)


def trocear_urls(urls):
    trozos, actual = [], []
    for u in urls:
        prueba = actual + [u]
        if actual and len(json.dumps({"urls": prueba, "flowID": "x" * 12, "streamID": "x" * 12})) > MAX_CHUNK:
            trozos.append(actual)
            actual = [u]
        else:
            actual = prueba
    if actual:
        trozos.append(actual)
    return trozos


def cerrar_remoto(nombre_disp, urls):
    """Envía la orden close-uri al móvil. Devuelve nº de órdenes enviadas."""
    _, kb = sesion_ffs()
    dev = dispositivo_destino(nombre_disp)
    claves = descifrar_claves_orden(dev["availableCommands"][CMD_CERRAR], kb)
    enviadas = 0
    for trozo in trocear_urls(urls):
        carga = {"urls": trozo, "flowID": secrets.token_urlsafe(9)[:12],
                 "streamID": secrets.token_urlsafe(9)[:12]}
        cifrado = ece_cifrar(json.dumps(carga, separators=(",", ":")).encode("utf-8"),
                             claves["publicKey"], claves["authSecret"])
        fxa("POST", "/account/devices/invoke_command",
            {"command": CMD_CERRAR, "target": dev["id"],
             "payload": {"encrypted": b64u_enc(cifrado)}, "ttl": CMD_TTL})
        enviadas += 1
    return enviadas


# ---- Lista de pestañas desde Sync ----

def script_sync():
    for carpeta in dict.fromkeys([DIR_PROG, os.getcwd()]):
        p = os.path.join(carpeta, "pesthist_sync.sh")
        if os.path.isfile(p):
            return p
    morir("no encuentro pesthist_sync.sh junto a pesthist.py ni en esta carpeta")


def descargar_pestanas_sync():
    os.makedirs(CACHE, exist_ok=True)
    tmp = TABS_SYNC_F + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    info("Descargando la lista de pestañas de Firefox Sync...")
    r = subprocess.run(["bash", script_sync(), "pestanas-json", tmp],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if r.returncode != 0 or not os.path.isfile(tmp):
        morir("falló la descarga de pestañas desde Sync:\n" + r.stdout[-800:])
    os.replace(tmp, TABS_SYNC_F)


def leer_pestanas_sync():
    """Devuelve {nombre_dispositivo: {"modified": seg, "tabs": [...]}}"""
    regs = leer_json(TABS_SYNC_F)
    if not isinstance(regs, list):
        morir("el archivo de pestañas de Sync no es válido; repite sin --cache")
    clientes = {}
    for r in regs:
        data = r.get("data")
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except ValueError:
                continue
        if not isinstance(data, dict) or data.get("deleted"):
            continue
        nombre = data.get("clientName") or r.get("id") or "?"
        tabs = []
        for t in data.get("tabs") or []:
            hist = t.get("urlHistory") or []
            if not hist:
                continue
            lu = t.get("lastUsed") or 0
            try:
                lu = float(lu)
            except (TypeError, ValueError):
                lu = 0
            last = int(lu) if lu > 1e11 else int(lu * 1000)
            tabs.append({"title": t.get("title") or "", "url": hist[0], "last": last})
        mod = r.get("modified_unix") or 0
        previo = clientes.get(nombre)
        if not previo or mod > previo["modified"]:
            clientes[nombre] = {"modified": float(mod), "tabs": tabs}
    return clientes


def elegir_dispositivo(clientes, forzado=None):
    cfg = leer_config()
    if forzado:
        if forzado not in clientes:
            morir("'%s' no está entre los dispositivos con pestañas en Sync: %s"
                  % (forzado, ", ".join(clientes)))
        cfg["dispositivo"] = forzado
        guardar_config(cfg)
        return forzado
    if cfg.get("dispositivo") in clientes:
        return cfg["dispositivo"]
    if not clientes:
        morir("Firefox Sync no tiene pestañas de ningún dispositivo.\n"
              "  En el móvil: Firefox -> Ajustes -> Sincronizar -> activa 'Pestañas abiertas'\n"
              "  y pulsa 'Sincronizar ahora'.")
    nombres = sorted(clientes, key=lambda n: -len(clientes[n]["tabs"]))
    if len(nombres) == 1:
        elegido = nombres[0]
    else:
        print("Dispositivos con pestañas en Firefox Sync:")
        for i, n in enumerate(nombres, 1):
            print("  %d) %s  (%d pestañas)" % (i, n, len(clientes[n]["tabs"])))
        try:
            r = input("¿Cuál es el móvil? (número): ").strip()
            elegido = nombres[int(r) - 1]
        except (ValueError, IndexError, EOFError):
            morir("elección no válida")
    cfg["dispositivo"] = elegido
    guardar_config(cfg)
    info("Dispositivo elegido: %s  (cámbialo con: python pesthist.py dispositivo)" % elegido)
    return elegido


# ==========================================================================
#  PESTAÑAS
# ==========================================================================

def cmd_pestanas(args):
    opts, resto = parse_opts(args, {
        "-d": ("dias", True), "--dias": ("dias", True),
        "-b": ("buscar", True), "--buscar": ("buscar", True),
        "--en": ("en", True), "-o": ("orden", True), "--orden": ("orden", True),
        "--dispositivo": ("disp", True),
        "-c": ("cache", False), "--cache": ("cache", False),
        "--txt": ("txt", False)})
    dias = entero(opts.get("dias", 0), "-d")
    campo = elegir(opts.get("en", "ambos"), ["ambos", "titulo", "url"], "--en")
    orden_ = elegir(opts.get("orden", "antiguas"), ["antiguas", "recientes", "titulo"], "-o")
    q = opts.get("buscar") or args_a_query(resto)
    terms = parse_query(q)

    if not (opts.get("cache") and os.path.isfile(TABS_SYNC_F)):
        descargar_pestanas_sync()
    clientes = leer_pestanas_sync()
    disp = elegir_dispositivo(clientes, opts.get("disp"))
    cli_ = clientes[disp]

    ahora = time.time() * 1000
    repes = {}
    for t in cli_["tabs"]:
        repes[t["url"]] = repes.get(t["url"], 0) + 1
    lista = [prep(dict(t)) for t in cli_["tabs"]]
    total = len(lista)
    lista = [t for t in lista
             if (dias == 0 or (t["last"] and ahora - t["last"] >= dias * DAY))
             and coincide(t, terms, campo)]
    if orden_ == "antiguas":
        lista.sort(key=lambda t: t["last"])
    elif orden_ == "recientes":
        lista.sort(key=lambda t: -t["last"])
    else:
        lista.sort(key=lambda t: norm(t["title"]))
    for i, t in enumerate(lista, 1):
        t["n"] = i
        t["win"] = "-"
        t["repes"] = repes[t["url"]]

    for t in lista:
        viejo = t["last"] and ahora - t["last"] >= 30 * DAY
        extra = c("  x%d misma URL" % t["repes"], "35") if t["repes"] > 1 else ""
        print("%s  %s  %s%s" % (c("%3d" % t["n"], "1;36"),
                                c("%6s" % fmt_hace(t["last"], ahora), "33" if viejo else "0"),
                                fmt_fecha(t["last"]), extra))
        print("     " + c(resaltar(t["title"] or "(sin título)", [] if campo == "url" else terms), "1"))
        print("     " + c(resaltar(safe_decode(t["url"]), [] if campo == "titulo" else terms), "34"))

    subida = cli_["modified"]
    hace_subida = fmt_hace(subida * 1000, ahora) if subida else "?"
    filtro = ("Dispositivo: %s  |  Lista subida a Sync hace %s\n" % (disp, hace_subida) +
              "Filtro: inactivas desde hace >= %d días" % dias +
              ("; búsqueda: %s (en %s)" % (q, campo) if q else ""))
    print()
    info("%d de %d pestañas de '%s'.  Lista subida por Firefox hace %s." %
         (len(lista), total, disp, hace_subida))
    if subida and time.time() - subida > 3600:
        aviso("La lista tiene más de 1 hora: para refrescarla, en el móvil abre Firefox ->\n"
              "  Ajustes -> Sincronizar -> 'Sincronizar ahora', y repite.")
    if lista:
        info("Usa: ir N  |  cerrar N [M 3-7 ...]  |  cerrar todas")

    guardar_json(ULT_TABS, {"fecha": time.time(), "dispositivo": disp,
                            "pestanas": [{k: t[k] for k in ("n", "url", "title", "repes")}
                                         for t in lista]})
    if opts.get("txt"):
        guardar_txt("pestanas", tabs_txt(lista, filtro))


def cargar_ultima_lista():
    d = leer_json(ULT_TABS)
    if not d or not d.get("pestanas") or "dispositivo" not in d:
        morir("no hay ninguna lista de pestañas; ejecuta primero: python pesthist.py pestanas")
    minutos = (time.time() - d.get("fecha", 0)) / 60
    if minutos > 60:
        aviso("Aviso: la última lista es de hace %d minutos." % minutos)
    return d


def numeros(tokens, maximo):
    ns = []
    for tok in tokens:
        for parte in tok.split(","):
            parte = parte.strip()
            if not parte:
                continue
            m = re.fullmatch(r"(\d+)-(\d+)", parte)
            if m:
                a, b = int(m.group(1)), int(m.group(2))
                ns.extend(range(min(a, b), max(a, b) + 1))
            elif parte.isdigit():
                ns.append(int(parte))
            else:
                morir("número no válido: " + parte)
    malos = [n for n in ns if n < 1 or n > maximo]
    if malos:
        morir("números fuera de la lista (1-%d): %s" % (maximo, ", ".join(map(str, malos))))
    return list(dict.fromkeys(ns))


def orden_directa(accion, datos, segundos):
    """Orden a la extensión sin despertar Firefox; None si no responde."""
    try:
        asegurar_servidor()
        st, d = api("POST", "/cli/orden", {"accion": accion, "datos": datos or {}})
        if st != 200 or not d:
            return None
        res = esperar_resultado(d["id"], segundos)
        if res is None:
            api("POST", "/cli/cancelar", {"id": d["id"]})
        return res
    except (urllib.error.URLError, OSError, SystemExit):
        return None


def abrir_url_firefox(url):
    return am("start", "-a", "android.intent.action.VIEW", "-d", url, "-p", FIREFOX)


def cmd_ir(args):
    d = cargar_ultima_lista()
    tabs = d["pestanas"]
    if len(args) != 1:
        morir("indica un número: python pesthist.py ir N")
    n = numeros(args, len(tabs))[0]
    t = tabs[n - 1]
    if t.get("cerrada"):
        morir("la pestaña %d ya se cerró" % n)
    info("Abriendo Firefox en: %s" % (t["title"] or t["url"]))
    abrir_firefox()
    # Si la pestaña está cargada en memoria, la extensión la selecciona.
    res = orden_directa("ir", {"url": t["url"]}, 12)
    if res and res.get("ok") and (res.get("datos") or {}).get("encontrada"):
        info("Pestaña %d seleccionada." % n)
        return
    # No cargada (o extensión sin respuesta): Firefox no permite seleccionarla
    # desde fuera, así que se abre su URL.
    if abrir_url_firefox(t["url"]):
        aviso("Esa pestaña no estaba cargada en memoria y Firefox no permite seleccionarla\n"
              "  desde fuera: se ha abierto su URL en una pestaña nueva.\n"
              "  La original sigue abierta; si quieres quitar la copia antigua sin cerrar la nueva,\n"
              "  ciérrala desde Firefox (cerrar N cerraría ambas: van por URL).")
    else:
        morir("no se pudo abrir Firefox (orden 'am' no disponible)")


def cmd_cerrar(args):
    opts, resto = parse_opts(args, {"-s": ("si", False), "--si": ("si", False)})
    d = cargar_ultima_lista()
    tabs = d["pestanas"]
    if not resto:
        morir("indica qué cerrar: cerrar N [M 3-7 ...]  o  cerrar todas")
    if resto == ["todas"]:
        elegidas = [t for t in tabs if not t.get("cerrada")]
    else:
        elegidas = [tabs[n - 1] for n in numeros(resto, len(tabs))]
        for t in [t for t in elegidas if t.get("cerrada")]:
            aviso("La %d ya estaba cerrada." % t["n"])
        elegidas = [t for t in elegidas if not t.get("cerrada")]
    if not elegidas:
        morir("no hay pestañas que cerrar")
    for t in elegidas:
        print("  %3d  %s" % (t["n"], (t["title"] or t["url"])[:70]))
    urls = list(dict.fromkeys(t["url"] for t in elegidas))
    extra = sum(t.get("repes", 1) for t in {t["url"]: t for t in elegidas}.values()) - len(urls)
    if extra > 0:
        aviso("Ojo: Firefox cierra por URL; se cerrarán también %d pestaña(s) más con la misma URL." % extra)
    if (len(elegidas) > 1 or resto == ["todas"]) and not opts.get("si"):
        try:
            r = input("¿Cerrar estas %d pestañas en '%s'? (s/N): "
                      % (len(elegidas), d["dispositivo"])).strip().lower()
        except EOFError:
            r = ""
        if r not in ("s", "si", "sí", "y"):
            info("Cancelado.")
            return
    try:
        n_ordenes = cerrar_remoto(d["dispositivo"], urls)
    except ImportError:
        morir("falta el módulo de cifrado. Instálalo con:  pkg install python-cryptography")
    except ErrorFxA as ex:
        morir(str(ex))
    cerr = set(urls)
    for t in tabs:
        if t["url"] in cerr:
            t["cerrada"] = True
    guardar_json(ULT_TABS, d)
    info("Orden de cierre enviada a '%s' (%d URL, %d envío/s)." % (d["dispositivo"], len(urls), n_ordenes))
    info("Firefox la aplica al recibirla (al momento si está conectado; si no, al abrirlo).")
    info("La lista de Sync se actualizará cuando Firefox vuelva a sincronizar.")


def cmd_dispositivo(args):
    if not os.path.isfile(TABS_SYNC_F):
        descargar_pestanas_sync()
    clientes = leer_pestanas_sync()
    cfg = leer_config()
    if args:
        elegir_dispositivo(clientes, " ".join(args))
        info("Dispositivo guardado: %s" % " ".join(args))
        return
    print("Dispositivos con pestañas en Firefox Sync:")
    for n in sorted(clientes):
        marca = "  <- elegido" if n == cfg.get("dispositivo") else ""
        print("  %s  (%d pestañas)%s" % (n, len(clientes[n]["tabs"]), marca))
    try:
        devs = dispositivos_fxa()
        print("\nDispositivos de tu cuenta Mozilla (¿admite cerrar pestañas remotas?):")
        for dv in devs:
            ok = CMD_CERRAR in (dv.get("availableCommands") or {})
            print("  %-30s %-8s %s" % (dv.get("name"), dv.get("type"), "sí" if ok else "no"))
    except ErrorFxA as ex:
        aviso("No se pudo consultar la cuenta Mozilla: %s" % ex)
    print("\nPara elegir otro:  python pesthist.py dispositivo NOMBRE EXACTO")


# ==========================================================================
#  HISTORIAL
# ==========================================================================

def to_ms(v):
    if v is None or v == "":
        return 0
    if isinstance(v, str) and re.fullmatch(r"\s*\d+(\.\d+)?\s*", v):
        v = float(v)
    if isinstance(v, (int, float)):
        if v <= 0:
            return 0
        if v > 1e14:
            return int(v / 1000)
        if v > 1e11:
            return int(v)
        return int(v * 1000)
    s = str(v).strip()
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?", s)
    try:
        if m:
            return int(datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)),
                                int(m.group(4) or 0), int(m.group(5) or 0),
                                int(m.group(6) or 0)).timestamp() * 1000)
        s = s.replace("Z", "+00:00")
        if re.match(r"\d{4}-\d{2}-\d{2} ", s):
            s = s.replace(" ", "T", 1)
        return int(datetime.fromisoformat(s).timestamp() * 1000)
    except ValueError:
        return 0


def _pick(r, claves):
    low = {str(k).lower(): v for k, v in r.items()}
    for k in claves:
        v = low.get(k)
        if v not in (None, ""):
            return v
    return None


def registro_a_item(r):
    if not isinstance(r, dict):
        return None
    dele = _pick(r, ["deleted"])
    if dele is True or str(dele).lower() == "true":
        return None
    url = _pick(r, ["uri", "histuri", "url", "href", "link"])
    if not isinstance(url, str) or not url:
        return None
    title = _pick(r, ["title", "titulo", "título", "name"]) or ""
    last = to_ms(_pick(r, ["lastvisittime", "lastvisit", "last_visit", "last visit",
                           "lastvisitdate", "last_visit_time", "ultima visita", "última visita"]))
    visitas = _pick(r, ["visitcount", "visit_count", "visitas"])
    vv = _pick(r, ["visits"])
    if isinstance(vv, list):
        for v in vv:
            if isinstance(v, dict):
                x = v.get("date_unix", v.get("date", v.get("visitTime")))
            else:
                x = v
            last = max(last, to_ms(x))
        if visitas is None:
            visitas = len(vv)
    elif visitas is None and vv is not None:
        visitas = vv
    try:
        visitas = int(visitas)
    except (TypeError, ValueError):
        visitas = 0
    return {"title": str(title), "url": url, "last": last, "visits": visitas}


def leer_archivo_historial(ruta):
    with open(ruta, encoding="utf-8-sig") as f:
        texto = f.read().strip()
    if texto.startswith("[") or texto.startswith("{"):
        j = json.loads(texto)
        if isinstance(j, list):
            regs = j
        else:
            regs = next((j[k] for k in ("history", "historial", "items", "records", "entries", "data")
                         if isinstance(j.get(k), list)), [j])
    else:
        primera = texto.split("\n", 1)[0]
        sep = "\t" if "\t" in primera else (";" if primera.count(";") > primera.count(",") else ",")
        regs = list(csv.DictReader(io.StringIO(texto), delimiter=sep))
    return [x for x in (registro_a_item(r) for r in regs) if x]


def unir(listas):
    m = {}
    for lista in listas:
        for h in lista:
            p = m.get(h["url"])
            if not p:
                m[h["url"]] = dict(h)
                continue
            if h["last"] > p["last"]:
                p["last"] = h["last"]
                if h["title"]:
                    p["title"] = h["title"]
            if not p["title"] and h["title"]:
                p["title"] = h["title"]
            p["visits"] = max(p["visits"], h["visits"])
    return list(m.values())


def buscar_archivos_por_defecto():
    cands = []
    for carpeta in dict.fromkeys([os.getcwd(), DESCARGAS, DIR_PROG]):
        cands += glob.glob(os.path.join(carpeta, "historial_firefox_*.json"))
    cands = [p for p in cands if os.path.isfile(p)]
    if not cands:
        return []
    return [max(cands, key=os.path.getmtime)]


def cmd_historial(args):
    opts, resto = parse_opts(args, {
        "-d": ("dias", True), "--dias": ("dias", True),
        "-b": ("buscar", True), "--buscar": ("buscar", True),
        "--en": ("en", True), "-o": ("orden", True), "--orden": ("orden", True),
        "-a": ("archivos", True), "--archivo": ("archivos", True),
        "--txt": ("txt", False)}, listas=("archivos",))
    dias = entero(opts.get("dias", 0), "-d")
    campo = elegir(opts.get("en", "ambos"), ["ambos", "titulo", "url"], "--en")
    orden_ = elegir(opts.get("orden", "reciente"), ["reciente", "antigua", "visitas", "titulo"], "-o")
    q = opts.get("buscar") or args_a_query(resto)
    terms = parse_query(q)

    if opts.get("archivos"):
        rutas = []
        for patron in opts["archivos"]:
            encontrados = glob.glob(os.path.expanduser(patron))
            if not encontrados:
                morir("no existe: " + patron)
            rutas += encontrados
    else:
        rutas = buscar_archivos_por_defecto()
        if not rutas:
            morir("no encuentro ningún historial_firefox_*.json (aquí, en Descargas ni junto al programa).\n"
                  "  Descárgalo con:  python pesthist.py actualizar")
    listas = []
    for r in rutas:
        try:
            listas.append(leer_archivo_historial(r))
        except (OSError, ValueError) as ex:
            aviso("No se pudo leer %s: %s" % (r, ex))
    items = [prep(h) for h in unir(listas)]
    total = len(items)
    if dias:
        desde = time.time() * 1000 - dias * DAY
        items = [h for h in items if h["last"] >= desde]
    lista = [h for h in items if coincide(h, terms, campo)]
    if orden_ == "reciente":
        lista.sort(key=lambda h: -h["last"])
    elif orden_ == "antigua":
        lista.sort(key=lambda h: h["last"])
    elif orden_ == "visitas":
        lista.sort(key=lambda h: (-h["visits"], -h["last"]))
    else:
        lista.sort(key=lambda h: norm(h["title"]))
    for i, h in enumerate(lista, 1):
        h["n"] = i

    for h in lista:
        print("%s  %s  %s" % (c("%4d" % h["n"], "1;36"), fmt_fecha(h["last"]),
                              c(plural(h["visits"], "visita", "visitas"), "2")))
        print("      " + c(resaltar(h["title"] or "(sin título)", [] if campo == "url" else terms), "1"))
        print("      " + c(resaltar(safe_decode(h["url"]), [] if campo == "titulo" else terms), "34"))
    nombres = ", ".join(os.path.basename(r) for r in rutas)
    filtro = "Búsqueda: %s  |  En: %s  |  Periodo: %s\nArchivo(s): %s" % (
        q or "(todo)", campo, ("últimos %d días" % dias) if dias else "todo", nombres)
    print()
    info("%s de %d páginas.  Archivo(s): %s" % (plural(len(lista), "resultado", "resultados"), total, nombres))
    if lista:
        info("Usa: abrir N  (abre el resultado en Firefox)")
    guardar_json(ULT_HIST, {"fecha": time.time(),
                            "resultados": [{"n": h["n"], "url": h["url"], "title": h["title"]}
                                           for h in lista]})
    if opts.get("txt"):
        guardar_txt("historial", hist_txt(lista, filtro))


def cmd_abrir(args):
    d = leer_json(ULT_HIST)
    if not d or not d.get("resultados"):
        morir("no hay búsqueda previa; ejecuta primero: python pesthist.py historial ...")
    res = d["resultados"]
    if len(args) != 1:
        morir("indica un número: python pesthist.py abrir N")
    n = numeros(args, len(res))[0]
    url = res[n - 1]["url"]
    # Se abre directamente con Android en Firefox (no necesita la extensión)
    if am("start", "-a", "android.intent.action.VIEW", "-d", url, "-p", FIREFOX):
        info("Abierto en Firefox: " + url)
    elif shutil.which("termux-open-url") and subprocess.call(["termux-open-url", url]) == 0:
        info("Abierto: " + url)
    else:
        orden("abrir", {"url": url}, volver=False)
        abrir_firefox()
        info("Abierto en Firefox: " + url)


def cmd_actualizar(args):
    script = None
    for carpeta in dict.fromkeys([DIR_PROG, os.getcwd()]):
        p = os.path.join(carpeta, "pesthist_sync.sh")
        if os.path.isfile(p):
            script = p
            break
    if not script:
        morir("no encuentro pesthist_sync.sh junto a pesthist.py ni en esta carpeta")
    if args:
        entero(args[0], "días")
    sys.exit(subprocess.call(["bash", script, "historial"] + args[:1]))


# ==========================================================================
#  ESTADO / PARAR
# ==========================================================================

def cmd_estado(args):
    print("pesthist.py v%s   puerto %d   Firefox: %s" % (VERSION, PUERTO, FIREFOX))
    e = servidor_activo()
    if not e:
        aviso("Servidor puente: parado (se arranca solo al usar pestanas/ir/cerrar).")
    else:
        print("Servidor puente: en marcha (pid %s)" % e.get("pid"))
        if e.get("extension_conectada"):
            info("Extensión PestHist: conectada.")
        elif e.get("ultimo_contacto_seg") is None:
            aviso("Extensión PestHist: todavía no ha contactado (¿instalada la 1.2? ¿Firefox abierto?).")
        else:
            aviso("Extensión PestHist: sin contacto desde hace %d s (Android puede tener "
                  "Firefox pausado; se despierta solo al dar una orden)." % e["ultimo_contacto_seg"])
    arch = buscar_archivos_por_defecto()
    if arch:
        p = arch[0]
        print("Historial más reciente: %s (%s)" % (p, fmt_fecha(os.path.getmtime(p) * 1000)))
    else:
        aviso("Historial: no hay archivos historial_firefox_*.json (usa: actualizar).")
    if not shutil.which("am"):
        aviso("No se encuentra la orden 'am' (no se podrá abrir Firefox desde Termux).")


def cmd_parar(args):
    if not servidor_activo():
        info("El servidor puente no está en marcha.")
        return
    api("POST", "/cli/parar", {})
    info("Servidor puente detenido.")


# ==========================================================================

COMANDOS = {
    "pestanas": cmd_pestanas, "pestañas": cmd_pestanas, "p": cmd_pestanas,
    "ir": cmd_ir,
    "cerrar": cmd_cerrar,
    "historial": cmd_historial, "h": cmd_historial,
    "abrir": cmd_abrir,
    "actualizar": cmd_actualizar,
    "estado": cmd_estado,
    "parar": cmd_parar,
    "dispositivo": cmd_dispositivo,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("ayuda", "-h", "--help", "help"):
        print(AYUDA)
        return
    cmd = sys.argv[1]
    if cmd == "servidor":
        servidor()
        return
    f = COMANDOS.get(cmd)
    if not f:
        morir("orden desconocida: %s  (python pesthist.py ayuda)" % cmd)
    try:
        f(sys.argv[2:])
    except KeyboardInterrupt:
        print()
        sys.exit(130)
    except (urllib.error.URLError, ConnectionError) as ex:
        morir("problema de conexión con el servidor puente: %s" % ex)


if __name__ == "__main__":
    main()
