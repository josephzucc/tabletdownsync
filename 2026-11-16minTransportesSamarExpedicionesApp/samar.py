#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
samar.py - Control por linea de comandos (Termux) de la web SAMAR "Bono Virtual"
(https://samar-bono.autobusing.es) a traves de la extension de Firefox
"SAMAR Bono Bridge".

Solo usa la libreria estandar de Python 3.

Comandos:
  config                         guardar email/contrasena (chmod 600)
  estado                         ver si el puente y la extension estan conectados
  abrir                          abrir Firefox Nightly (para que arranque la extension)
  bonos                          listar bonos
  horarios ORIG DEST FECHA       horarios con plazas libres (y los LLENOS conocidos)
  comprobar ORIG DEST FECHA HORA ver si un viaje concreto tiene plazas
  viajes                         listar viajes pendientes/pagados
  anular ...                     anular un viaje pagado
  serve                          arrancar el puente en primer plano
  desvincular                    olvidar la extension emparejada
  salida ORIG DEST FECHA         primera/ultima salida con llegada (-primero / -ultimo)
  rutas [anadir|quitar|restaurar] ver o cambiar las paradas que cubre cada bono
  diagnostico [--probar O D F] [--copiar]  generar archivo de diagnostico
  alias [ver|anadir|quitar|restaurar]  otras formas de escribir una parada
  aprendidos                     ver horarios aprendidos

Este programa NUNCA compra viajes.
"""
import argparse
import datetime as dt
import getpass
import json
import os
import queue
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.10.5"
HOST = "127.0.0.1"
PORT = int(os.environ.get("SAMAR_PORT", "8770"))
HOME = os.environ.get("SAMAR_HOME") or os.path.join(os.path.expanduser("~"), ".samar_bono")
CONFIG = os.path.join(HOME, "config.json")
CLI_TOKEN = os.path.join(HOME, "cli_token")
PAIR = os.path.join(HOME, "pair.json")
APRENDIDOS = os.path.join(HOME, "horarios.json")
LOGFILE = os.path.join(HOME, "serve.log")
CARPETA_PY = os.path.dirname(os.path.realpath(os.path.abspath(__file__)))
# Los diagnosticos se guardan directamente en la carpeta de samar.py (da igual desde donde lo ejecutes),
# con nombre AAAA-MM-DD-HH-MM-SS.txt; si esa carpeta no es escribible se usa ~/.samar_bono/diag.
DIAGDIR = CARPETA_PY
REGISTRO_DEFECTO = os.path.join(CARPETA_PY, "registro_comprobar.txt")
REGISTRO_CANJES = os.path.join(CARPETA_PY, "registro_canjes.txt")
BASE_URL = "https://samar-bono.autobusing.es/"
BRIDGE_URL = "http://%s:%d" % (HOST, PORT)

DIAS = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]

POLL_WAIT = 20          # segundos que el long-poll espera trabajo
EXT_ALIVE = 40          # la extension se considera conectada si hizo poll hace menos de esto
JOB_TIMEOUT = 170


# ----------------------------------------------------------------------------
# utilidades
# ----------------------------------------------------------------------------
def info(*args):
    """Texto normal (resultados, avisos, progreso, preguntas): stdout."""
    print(*args, flush=True)


def err(*args):
    """Errores del programa: stderr."""
    print(*args, file=sys.stderr, flush=True)


def preguntar(msg):
    """Como input(), con la pregunta por stdout."""
    sys.stdout.write(msg)
    sys.stdout.flush()
    linea = sys.stdin.readline()
    if linea == "":
        raise SystemExit("No hay respuesta (entrada cerrada).")
    return linea.rstrip("\n")


def ensure_home():
    os.makedirs(HOME, exist_ok=True)
    try:
        os.chmod(HOME, 0o700)
    except OSError:
        pass


def write_private(path, data):
    ensure_home()
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(data)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def norm(s):
    s = unicodedata.normalize("NFD", str(s or ""))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return " ".join(s.replace("_", " ").split()).upper()


def parse_fecha(s):
    s = s.strip()
    w = norm(s).lower()
    hoy = dt.date.today()
    if w == "hoy":
        return hoy
    if w in ("manana", "mañana"):
        return hoy + dt.timedelta(days=1)
    if w == "pasado manana":
        return hoy + dt.timedelta(days=2)
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise ValueError("fecha no valida: %r (usa DD/MM/AAAA)" % s)


def parse_hora(s):
    s = s.strip().lower().replace("h", ":").replace(".", ":")
    if s.endswith(":"):
        s += "00"
    if ":" not in s:
        s += ":00"
    h, m = s.split(":", 1)
    try:
        hh, mm = int(h), int(m or 0)
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError
    except ValueError:
        raise ValueError("hora no valida: %r (usa HH:MM, por ejemplo 16:00)" % s)
    return "%02d:%02d" % (hh, mm)


DIAS3 = ["lun", "mar", "mie", "jue", "vie", "sab", "dom"]


def corto(origen, destino, fecha, hora=None):
    """Formato corto de los mensajes: 'sal 17:30 AVILA-LA ADRADA jue 22/10' (sin hora: 'AVILA-LA ADRADA jue 22/10')."""
    return "%s%s-%s %s %s" % ("sal %s " % hora if hora else "", origen, destino,
                              DIAS3[fecha.weekday()], fecha.strftime("%d/%m"))


def fmt_fecha(d):
    return "%s %s" % (DIAS[d.weekday()], d.strftime("%d/%m/%Y"))


def load_config():
    cfg = read_json(CONFIG, {})
    if os.environ.get("SAMAR_EMAIL"):
        cfg["email"] = os.environ["SAMAR_EMAIL"]
    if os.environ.get("SAMAR_PASS"):
        cfg["password"] = os.environ["SAMAR_PASS"]
    return cfg


def cli_token():
    ensure_home()
    if not os.path.exists(CLI_TOKEN):
        write_private(CLI_TOKEN, secrets.token_hex(24))
    with open(CLI_TOKEN, encoding="utf-8") as f:
        return f.read().strip()


# ----------------------------------------------------------------------------
# puente HTTP (servidor)
# ----------------------------------------------------------------------------
class Bridge:
    def __init__(self):
        self.q = queue.Queue()
        self.pending = {}            # id -> {"ev": Event, "res": dict|None, "cancel": bool}
        self.lock = threading.Lock()
        self.last_poll = 0.0
        self.cli_token = cli_token()

    def paired_token(self):
        return read_json(PAIR, {}).get("ext_token")

    def ext_alive(self):
        return (time.time() - self.last_poll) < EXT_ALIVE


BR = None


class Handler(BaseHTTPRequestHandler):
    server_version = "samar-bridge/1.0"

    def log_message(self, fmt, *args):  # silencio
        pass

    def _send(self, code, obj=None):
        try:
            body = b"" if obj is None else json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "X-Samar-Token, X-Cli-Token, Content-Type")
            self.end_headers()
            if body:
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass                                   # el cliente ya colgo: no es un error
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass                                       # el otro lado ya colgo: no es un error

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def _ext_ok(self):
        tok = self.headers.get("X-Samar-Token", "")
        if not tok:
            return False
        paired = BR.paired_token()
        if paired is None:
            write_private(PAIR, json.dumps({"ext_token": tok}))
            return True
        return secrets.compare_digest(paired, tok)

    def _cli_ok(self):
        return secrets.compare_digest(self.headers.get("X-Cli-Token", ""), BR.cli_token)

    def do_OPTIONS(self):
        self._send(204)

    def do_GET(self):
        if self.path.startswith("/hello"):
            return self._send(200, {"ok": True})
        if self.path.startswith("/poll"):
            if not self._ext_ok():
                return self._send(403, {"error": "extension no emparejada"})
            BR.last_poll = time.time()
            deadline = time.time() + POLL_WAIT
            while time.time() < deadline:
                try:
                    job = BR.q.get(timeout=1)
                except queue.Empty:
                    BR.last_poll = time.time()
                    continue
                with BR.lock:
                    st = BR.pending.get(job["id"])
                if st is None or st["cancel"]:
                    continue
                job["cred"] = {k: v for k, v in load_config().items() if k in ("email", "password")}
                return self._send(200, job)
            return self._send(204)
        if self.path.startswith("/estado"):
            if not self._cli_ok():
                return self._send(403, {"error": "token"})
            return self._send(200, {
                "conectada": BR.ext_alive(),
                "ultimo_poll_s": round(time.time() - BR.last_poll, 1) if BR.last_poll else None,
                "emparejada": BR.paired_token() is not None,
            })
        self._send(404, {"error": "no existe"})

    def do_POST(self):
        if self.path.startswith("/result"):
            if not self._ext_ok():
                return self._send(403, {"error": "extension no emparejada"})
            res = self._body()
            with BR.lock:
                st = BR.pending.get(res.get("id"))
            if st:
                st["res"] = res
                st["ev"].set()
            return self._send(200, {"ok": True})
        if self.path.startswith("/job"):
            if not self._cli_ok():
                return self._send(403, {"error": "token"})
            req = self._body()
            if not BR.ext_alive():
                return self._send(200, {"ok": False, "error": "ext_no_conectada"})
            jid = uuid.uuid4().hex
            st = {"ev": threading.Event(), "res": None, "cancel": False}
            with BR.lock:
                BR.pending[jid] = st
            BR.q.put({"id": jid, "op": req.get("op"), "args": req.get("args") or {}})
            if not st["ev"].wait(min(JOB_TIMEOUT, int(req.get("timeout") or JOB_TIMEOUT))):
                st["cancel"] = True
                with BR.lock:
                    BR.pending.pop(jid, None)
                return self._send(200, {"ok": False, "error": "tiempo agotado esperando a la extension"})
            with BR.lock:
                BR.pending.pop(jid, None)
            return self._send(200, st["res"])
        self._send(404, {"error": "no existe"})


def serve():
    global BR
    ensure_home()
    BR = Bridge()
    try:
        srv = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as e:
        print("No puedo abrir %s:%d (%s). Quiza ya esta en marcha." % (HOST, PORT, e), file=sys.stderr)
        return 1
    srv.daemon_threads = True
    print("Puente SAMAR escuchando en %s (Ctrl+C para parar)" % BRIDGE_URL, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


# ----------------------------------------------------------------------------
# cliente
# ----------------------------------------------------------------------------
def http(method, path, body=None, timeout=10):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(BRIDGE_URL + path, data=data, method=method)
    req.add_header("X-Cli-Token", cli_token())
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw.decode("utf-8")) if raw else None


def server_up():
    try:
        urllib.request.urlopen(BRIDGE_URL + "/hello", timeout=2).read()
        return True
    except Exception:
        return False


def start_server_detached():
    ensure_home()
    log = open(LOGFILE, "ab")
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "serve"],
                     stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
    for _ in range(30):
        if server_up():
            return True
        time.sleep(0.2)
    return False


def abrir_firefox(url=BASE_URL, marcar=False):
    """Abre Firefox Nightly (Android) desde Termux."""
    if marcar:
        url = url.rstrip("/") + "/#samar_bridge"      # la extension cierra las pestanas con esta marca
    pkg = os.environ.get("SAMAR_FIREFOX_PKG", "org.mozilla.fenix")
    if shutil.which("am"):
        r = subprocess.run(
            ["am", "start", "-a", "android.intent.action.VIEW", "-d", url, pkg],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if r.returncode == 0 and "Error" not in r.stdout:
            return True
    if shutil.which("termux-open-url"):
        return subprocess.run(["termux-open-url", url]).returncode == 0
    return False


def ensure_ready(verbose=True):
    if not server_up():
        if verbose:
            print("[puente] arrancando...", flush=True)
        if not start_server_detached():
            raise SystemExit("No se pudo arrancar el puente (mira %s)" % LOGFILE)
    st = http("GET", "/estado")
    if st["conectada"]:
        return
    if verbose:
        print("[puente] la extension no esta conectada; abriendo Firefox...", flush=True)
    abrir_firefox(marcar=True)
    for _ in range(60):
        time.sleep(1)
        if http("GET", "/estado")["conectada"]:
            return
    ruta = guardar_diag("La extension no se conecta al puente", snapshot=False)
    raise SystemExit(
        "La extension no se conecta. Abre Firefox Nightly (con la extension 'SAMAR Bono Bridge' instalada) "
        "y vuelve a probar. Comprueba con: python samar.py estado\n" + mensaje_diag(ruta))


def run_job(op, args=None, verbose=True):
    ensure_ready(verbose)
    res = http("POST", "/job", {"op": op, "args": args or {}}, timeout=JOB_TIMEOUT + 10)
    if not res.get("ok"):
        if res.get("error") == "ext_no_conectada":
            raise SystemExit("La extension no esta conectada.")
        ruta = guardar_diag("Fallo de la orden '%s': %s" % (op, res.get("error")),
                            job={"op": op, "args": args}, res=res)
        raise SystemExit("%s\n%s" % (res.get("error"), mensaje_diag(ruta)))
    return res["data"]


# ----------------------------------------------------------------------------
# diagnostico
# ----------------------------------------------------------------------------
def mensaje_diag(ruta):
    if not ruta:
        return "(No se pudo guardar el diagnostico.)"
    return ("\n*** SE HA GENERADO UN DIAGNOSTICO ***\n"
            "  Archivo: %s\n"
            "  Enviame ese archivo (y el *-pagina.html que lo acompana) para saber que ha pasado.\n"
            "  Aviso: puede contener tu nombre y datos visibles en la web; no incluye tu contrasena." % ruta)


def _recorta(txt, n):
    txt = txt or ""
    return txt if len(txt) <= n else txt[:n] + "\n[...recortado...]"


def guardar_diag(motivo, job=None, res=None, snapshot=True, extra=None):
    """Escribe AAAA-MM-DD-HH-MM-SS.txt junto a samar.py (+ -pagina.html). Devuelve la ruta o None."""
    try:
        import platform
        ensure_home()
        diagdir = DIAGDIR
        try:
            os.makedirs(diagdir, exist_ok=True)
            if not os.access(diagdir, os.W_OK):
                raise OSError("sin permiso")
        except OSError:
            diagdir = os.path.join(HOME, "diag")
            os.makedirs(diagdir, exist_ok=True)
        base = os.path.join(diagdir, time.strftime("%Y-%m-%d-%H-%M-%S"))
        n_ = 1
        while os.path.exists(base + ".txt"):          # dos diagnosticos en el mismo segundo
            n_ += 1
            base = os.path.join(diagdir, time.strftime("%Y-%m-%d-%H-%M-%S") + "-%d" % n_)
        diag = (res or {}).get("diag")
        if diag is None and snapshot:
            try:
                if server_up() and http("GET", "/estado").get("conectada"):
                    r = http("POST", "/job", {"op": "snapshot", "args": {}, "timeout": 25}, timeout=40)
                    if r.get("ok"):
                        diag = r["data"]
            except Exception as e:                      # noqa
                diag = {"error_snapshot": str(e)}
        pagina = ((diag or {}).get("pagina") or {})
        html = pagina.pop("html", None) if isinstance(pagina, dict) else None
        cfg = read_json(CONFIG, {})
        L = []
        add = L.append
        add("DIAGNOSTICO SAMAR  (programa %s)" % VERSION)
        add("=" * 60)
        add("Fecha ............ %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
        add("Motivo ........... %s" % motivo)
        add("Comando .......... %s" % " ".join(sys.argv))
        add("Python/sistema ... %s / %s" % (platform.python_version(), platform.platform()))
        add("Extension ........ %s" % ((diag or {}).get("version", "desconocida")))
        add("Credenciales ..... email=%s, contrasena %s" % (cfg.get("email", "(no)"), "guardada" if cfg.get("password") else "NO guardada"))
        try:
            add("Puente ........... %s" % json.dumps(http("GET", "/estado"), ensure_ascii=False))
        except Exception as e:                           # noqa
            add("Puente ........... no responde (%s)" % e)
        if job:
            add("")
            add("ORDEN ENVIADA A LA EXTENSION")
            add(json.dumps(job, ensure_ascii=False, indent=1))
        if res:
            add("")
            add("RESPUESTA DE LA EXTENSION")
            add(json.dumps({k: v for k, v in res.items() if k != "diag"}, ensure_ascii=False, indent=1)[:3000])
        if extra:
            add("")
            add(str(extra)[:6000])
        if diag:
            add("")
            add("PESTANA DE LA EXTENSION")
            add(json.dumps(diag.get("pestana", diag.get("error_snapshot", "?")), ensure_ascii=False, indent=1))
            if isinstance(pagina, dict) and pagina:
                add("")
                add("PAGINA")
                for k in ("url", "titulo", "readyState", "oculta", "mensajes", "avisos_visibles", "tiene", "formularios"):
                    add("  %-16s %s" % (k, json.dumps(pagina.get(k), ensure_ascii=False)))
                add("  selects:")
                for sl in pagina.get("selects", []):
                    add("    #%s: %s" % (sl.get("id"), json.dumps(sl.get("opciones"), ensure_ascii=False)))
                add("")
                add("TEXTO VISIBLE DE LA PAGINA")
                add(pagina.get("texto", ""))
            elif diag.get("pagina_error"):
                add("PAGINA: no se pudo leer (%s)" % diag["pagina_error"])
            add("")
            add("REGISTRO DE LA EXTENSION (ultimos eventos)")
            L.extend(diag.get("log", []))
        add("")
        add("RUTAS (rutas.json)")
        try:
            add(open(RUTAS_FILE, encoding="utf-8").read()[:3000])
        except OSError:
            add("(no existe)")
        add("")
        add("REGISTRO DEL PUENTE (serve.log, final)")
        try:
            add(_recorta("".join(open(LOGFILE, encoding="utf-8", errors="replace").readlines()[-25:]), 4000))
        except OSError:
            add("(vacio)")
        with open(base + ".txt", "w", encoding="utf-8") as f:
            f.write("\n".join(L) + "\n")
        os.chmod(base + ".txt", 0o600)
        if html:
            with open(base + "-pagina.html", "w", encoding="utf-8") as f:
                f.write(html)
            os.chmod(base + "-pagina.html", 0o600)
        return base + ".txt"
    except Exception as e:                               # noqa
        print("(No pude guardar el diagnostico: %s)" % e, file=sys.stderr)
        return None


def copiar_diag(ruta):
    destinos = [os.path.expanduser("~/storage/downloads"), "/sdcard/Download", "/storage/emulated/0/Download"]
    pref = os.path.splitext(ruta)[0]
    for d in destinos:
        if os.path.isdir(d):
            copiados = []
            for suf in (".txt", "-pagina.html"):
                src = pref + suf
                if os.path.exists(src):
                    shutil.copyfile(src, os.path.join(d, os.path.basename(src)))
                    copiados.append(os.path.join(d, os.path.basename(src)))
            return copiados
    return []


def cmd_diagnostico(a):
    """Genera un diagnostico del estado actual (sin tocar nada). Con --probar ejecuta antes una consulta."""
    extra = None
    if a.probar:
        if len(a.probar) != 3:
            raise SystemExit("Uso: diagnostico --probar ORIGEN DESTINO FECHA")
        try:
            fecha = parse_fecha(a.probar[2])
            r = consultar_horarios(a.probar[0], a.probar[1], fecha)
            extra = "PRUEBA OK: %d horarios con plazas: %s" % (
                len(r["horarios"]), ", ".join(h["hora"] for h in r["horarios"]))
        except SystemExit as e:
            extra = "PRUEBA FALLIDA: %s" % e
        print(extra, flush=True)
    ruta = guardar_diag("Diagnostico pedido por el usuario", extra=extra)
    info(mensaje_diag(ruta))
    if ruta:
        print(ruta)
    if ruta and a.copiar:
        c = copiar_diag(ruta)
        if c:
            info("Copiado a: %s" % ", ".join(c))
        else:
            info("No encuentro la carpeta Descargas; ejecuta antes: termux-setup-storage")
    return 0 if ruta else 1


# ----------------------------------------------------------------------------
# logica: bonos y horarios
# ----------------------------------------------------------------------------
def match_name(inp, name):
    a, b = norm(inp), norm(name)
    canon = mapa_alias().get(compacto(inp)) or mapa_alias().get(sin_articulo(inp))
    if canon:
        a = canon
    if a == b:
        return 2
    if a and (a in b or b in a):
        return 1
    return 0


def parse_vig(s):
    try:
        return dt.datetime.strptime(s.strip(), "%d/%m/%y").date()
    except ValueError:
        try:
            return dt.datetime.strptime(s.strip(), "%d/%m/%Y").date()
        except ValueError:
            return None


RUTAS_FILE = os.path.join(HOME, "rutas.json")

# Paradas que cubre cada bono (en orden desde La Adrada). Se crea el fichero
# ~/.samar_bono/rutas.json con esto la primera vez; puedes editarlo para
# anadir paradas o bonos nuevos.
RUTAS_DEFECTO = {
    "368173": {"nombre": "La Adrada - Avila",
               "paradas": ["LA ADRADA", "SOTILLO DE LA ADRADA", "SANTA MARIA DEL TIETAR", "EL TIEMBLO", "AVILA"]},
    "382744": {"nombre": "La Adrada - Madrid Estacion Sur",
               "paradas": ["LA ADRADA", "SOTILLO DE LA ADRADA", "SANTA MARIA DEL TIETAR", "MADRID ESTACION SUR"]},
    "368146": {"nombre": "La Adrada - Sotillo de la Adrada",
               "paradas": ["LA ADRADA", "SOTILLO DE LA ADRADA", "SANTA MARIA DEL TIETAR"]},
}
ARTICULOS = ("EL", "LA", "LOS", "LAS", "DE", "DEL")


RUTAS_AVISOS = []
ALIAS_PENDIENTES = []          # (parada_canonica, alias) deducidos de un bono repetido


def _unir_duplicados(pairs):
    """Si un bono aparece dos veces en rutas.json se fusiona. Si las dos listas de paradas tienen el
    mismo numero de elementos, la segunda se interpreta como nombres alternativos (alias) de la primera."""
    d = {}
    for k, v in pairs:
        if k in d and isinstance(d[k], dict) and isinstance(v, dict) and "paradas" in v and "paradas" in d[k]:
            RUTAS_AVISOS.append(k)
            a, b = list(d[k]["paradas"]), list(v["paradas"])
            if len(a) == len(b):
                for x, y in zip(a, b):
                    if compacto(x) != compacto(y):
                        ALIAS_PENDIENTES.append((norm(x), str(y)))
            else:
                d[k]["paradas"] = a + [y for y in b if compacto(y) not in [compacto(x) for x in a]]
        else:
            d[k] = v
    return d


def cargar_rutas(avisar=True):
    if not os.path.exists(RUTAS_FILE):
        guardar_rutas(RUTAS_DEFECTO)
        return json.loads(json.dumps(RUTAS_DEFECTO))
    del RUTAS_AVISOS[:]
    del ALIAS_PENDIENTES[:]
    try:
        with open(RUTAS_FILE, encoding="utf-8") as f:
            r = json.load(f, object_pairs_hook=_unir_duplicados)
    except ValueError as e:
        raise SystemExit(
            "El fichero %s esta mal escrito (%s).\n"
            "Arreglalo, o vuelve a los valores de fabrica con:  python samar.py rutas restaurar" % (RUTAS_FILE, e))
    if not isinstance(r, dict) or not r:
        raise SystemExit("%s esta vacio. Usa:  python samar.py rutas restaurar" % RUTAS_FILE)
    for k, v in r.items():
        ps = v.get("paradas") if isinstance(v, dict) else None
        if not isinstance(ps, list):
            raise SystemExit("En %s el bono %s no tiene la lista \"paradas\"." % (RUTAS_FILE, k))
        vistos, claves = [], []
        for x in ps:
            n = norm(x)
            if n and compacto(n) not in claves:      # "LAADRADA" y "LA ADRADA" son la misma parada
                vistos.append(n)
                claves.append(compacto(n))
        v["paradas"] = vistos
    if RUTAS_AVISOS:
        # Limpieza automatica: se reescribe el fichero sin repetidos (copia en .bak) y, si procede,
        # los nombres de la segunda copia pasan a ser alias. El aviso sale una sola vez, por stderr.
        try:
            shutil.copyfile(RUTAS_FILE, RUTAS_FILE + ".bak")
            guardar_rutas(r)
            if ALIAS_PENDIENTES:
                al = cargar_alias()
                for canon, alias in ALIAS_PENDIENTES:
                    anadir_alias(al, canon, alias)
                guardar_alias(al)
            if avisar:
                err("AVISO (una sola vez): rutas.json tenia repetido(s) el/los bono(s) %s. Lo he limpiado "
                     "(copia anterior: rutas.json.bak)%s." % (
                         ", ".join(sorted(set(RUTAS_AVISOS))),
                         "; los nombres de la copia repetida se han guardado como alias" if ALIAS_PENDIENTES else ""))
        except OSError as e:
            if avisar:
                err("AVISO: rutas.json tiene bonos repetidos y no pude limpiarlo (%s)." % e)
    return r


def guardar_rutas(r):
    ensure_home()
    write_private(RUTAS_FILE, json.dumps(r, ensure_ascii=False, indent=2))


ALIAS_FILE = os.path.join(HOME, "alias.json")
ALIAS_DEFECTO = {
    "LA ADRADA": ["adrada", "la-adrada", "la adrada"],
    "SOTILLO DE LA ADRADA": ["sotillo"],
    "SANTA MARIA DEL TIETAR": ["santa maria", "sta maria", "tietar"],
    "EL TIEMBLO": ["tiemblo"],
    "AVILA": ["avila capital"],
    "MADRID ESTACION SUR": ["madrid", "madrid sur", "estacion sur"],
}


def guardar_alias(al):
    ensure_home()
    write_private(ALIAS_FILE, json.dumps(al, ensure_ascii=False, indent=2))


def cargar_alias():
    """{PARADA CANONICA: [alias, ...]}. Se crea con valores por defecto la primera vez."""
    if not os.path.exists(ALIAS_FILE):
        guardar_alias(ALIAS_DEFECTO)
        return json.loads(json.dumps(ALIAS_DEFECTO))
    try:
        with open(ALIAS_FILE, encoding="utf-8") as f:
            al = json.load(f)
        if not isinstance(al, dict) or not all(isinstance(v, list) for v in al.values()):
            raise ValueError("debe ser {\"PARADA\": [\"alias\", ...]}")
    except ValueError as e:
        raise SystemExit("El fichero %s esta mal escrito (%s).\n"
                         "Arreglalo o borralo (se recreara con los alias por defecto)." % (ALIAS_FILE, e))
    return al


def mapa_alias():
    """{forma compacta o sin articulo: PARADA CANONICA}"""
    m = {}
    for canon, lst in cargar_alias().items():
        c = norm(canon)
        for a in lst:
            m.setdefault(compacto(a), c)
            m.setdefault(sin_articulo(a), c)
    return m


def anadir_alias(al, canon, alias):
    """Anade un alias; error si ya apunta a otra parada. Devuelve True si es nuevo."""
    canon = norm(canon)
    k = compacto(alias)
    if not k:
        raise SystemExit("Alias vacio.")
    for otro, lst in al.items():
        if norm(otro) != canon and any(compacto(x) == k for x in lst):
            raise SystemExit("El alias '%s' ya pertenece a la parada %s." % (alias, norm(otro)))
    lst = al.setdefault(canon, [])
    if any(compacto(x) == k for x in lst) or k == compacto(canon):
        return False
    lst.append(str(alias).strip())
    return True


def compacto(s):
    return "".join(c for c in norm(s) if c.isalnum())


def sin_articulo(s):
    p = norm(s).split()
    while len(p) > 1 and p[0] in ARTICULOS:
        p = p[1:]
    return "".join("".join(c for c in w if c.isalnum()) for w in p)


def paradas_de(bono, rutas):
    r = rutas.get(str(bono["bono"]))
    if r:
        return [norm(x) for x in r["paradas"]]
    return [norm(x) for x in (bono.get("estaciones") or [])]      # bono desconocido: solo sus extremos


def resolver_parada(texto, catalogo):
    """Nombre canonico de la parada. Devuelve: str (acierto), list (ambiguo), None (desconocida)."""
    c = compacto(texto)
    sa = sin_articulo(texto)
    if not c:
        return None
    for p in catalogo:
        if compacto(p) == c:
            return p
    for p in catalogo:
        if sin_articulo(p) == sa:
            return p
    # alias propios (alias.json): "adrada", "tiemblo", "madrid"...
    m = mapa_alias()
    canon = m.get(c) or m.get(sa)
    if canon and canon in catalogo:
        return canon
    # mismas letras en otro orden: "AdradaLa" = "La Adrada"
    letras = "".join(sorted(c))
    ana = [p for p in catalogo if "".join(sorted(compacto(p))) == letras or "".join(sorted(sin_articulo(p))) == "".join(sorted(sa))]
    if len(ana) == 1:
        return ana[0]
    cands = [p for p in catalogo if sa and (sa in sin_articulo(p) or sin_articulo(p) in sa)]
    if len(cands) == 1:
        return cands[0]
    if cands:
        return cands
    # parecido por letras (errores de escritura): "tiemlo" -> EL TIEMBLO
    import difflib
    mapa = {sin_articulo(p): p for p in catalogo}
    cerca = difflib.get_close_matches(sa, list(mapa), n=3, cutoff=0.7)
    if cerca:
        sug = [mapa[x] for x in cerca]
        if sys.stdin.isatty():
            r = preguntar("No encuentro '%s'. Quisiste decir %s? [S/n] " % (texto, sug[0])).strip().lower()
            if r in ("", "s", "si", "y"):
                return sug[0]
            return None
        raise SystemExit("No encuentro la parada '%s'. Quisiste decir: %s ?" % (texto, ", ".join(sug)))
    return None


def elegir_bono(bonos, origen, destino, fecha, forzado=None):
    """Devuelve (bono, origen_canonico, destino_canonico). Pregunta si sirven varios bonos."""
    rutas = cargar_rutas()
    if not bonos:
        raise SystemExit("La web no ha devuelto ningun bono (sesion caducada o pagina distinta). "
                         "Prueba:  python samar.py bonos")
    catalogo = []
    for b in bonos:
        for p in paradas_de(b, rutas):
            if p not in catalogo:
                catalogo.append(p)
    res = {}
    for nombre, txt in (("origen", origen), ("destino", destino)):
        r = resolver_parada(txt, catalogo)
        if isinstance(r, list):
            raise SystemExit("'%s' es ambiguo: %s. Escribe el nombre completo." % (txt, ", ".join(r)))
        res[nombre] = r
    if forzado:
        for b in bonos:
            if str(b["bono"]) == str(forzado):
                return b, res["origen"] or norm(origen), res["destino"] or norm(destino)
        raise SystemExit("No existe el bono %s. Bonos: %s" % (forzado, ", ".join(x["bono"] for x in bonos)))
    for nombre, txt in (("origen", origen), ("destino", destino)):
        if not res[nombre]:
            raise SystemExit(
                "No conozco la parada %s '%s'.\nBonos leidos de la web: %s. Paradas conocidas:\n  %s\n"
                "Si es otra forma de escribir una parada conocida:  python samar.py alias anadir \"PARADA\" \"%s\"\n"
                "Si es una parada nueva:  python samar.py rutas anadir NUMERO_DE_BONO \"%s\"\n"
                "(o usa --bono NUMERO para saltarte esta comprobacion)." % (
                    nombre, txt, ", ".join(str(b["bono"]) for b in bonos),
                    "\n  ".join(catalogo) or "(ninguna)", txt, txt))
    o, d = res["origen"], res["destino"]
    if o == d:
        raise SystemExit("El origen y el destino son la misma parada.")
    sirven, motivos = [], []
    for b in bonos:
        ps = paradas_de(b, rutas)
        if o in ps and d in ps:
            vig = parse_vig(b.get("vigencia", ""))
            if vig is not None and vig < fecha:
                motivos.append("bono %s caducado (%s)" % (b["bono"], b.get("vigencia")))
            elif (b.get("saldo") or 0) <= 0:
                motivos.append("bono %s sin saldo" % b["bono"])
            else:
                sirven.append(b)
    if not sirven:
        raise SystemExit("Ningun bono tuyo sirve para %s -> %s%s.\nBonos:\n%s" % (
            o, d, (" (" + "; ".join(motivos) + ")") if motivos else "",
            "\n".join("  %s %s: %s" % (b["bono"], b.get("saldo_texto", ""), " > ".join(paradas_de(b, rutas)))
                       for b in bonos)))
    if len(sirven) == 1:
        return sirven[0], o, d
    info("Para %s -> %s sirven varios bonos:" % (o, d))
    for i, b in enumerate(sirven, 1):
        info("  %d) bono %s  %s  saldo %s  vigencia %s" % (
            i, b["bono"], " - ".join(b.get("estaciones", [])), b.get("saldo"), b.get("vigencia")))
    if not sys.stdin.isatty():
        raise SystemExit("Indica cual con --bono NUMERO.")
    while True:
        r = preguntar("Elige 1-%d (o escribe el numero de bono): " % len(sirven)).strip()
        if r.isdigit() and 1 <= int(r) <= len(sirven):
            return sirven[int(r) - 1], o, d
        for b in sirven:
            if r == str(b["bono"]):
                return b, o, d
        info("Opcion no valida.")


HORA_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def hora_valida(h):
    """True solo para HH:MM reales (00:00-23:59). Rechaza '00000', '', None, '25:00'..."""
    return isinstance(h, str) and bool(HORA_RE.match(h))


def limpiar_aprendidos(ap):
    """Devuelve (datos_limpios, n_eliminados): quita horas invalidas y claves vacias."""
    n = 0
    out = {}
    if not isinstance(ap, dict):
        return {}, 0
    for k, ent in ap.items():
        if not isinstance(ent, dict):
            n += 1
            continue
        nuevo = {}
        for h, v in ent.items():
            ok = hora_valida(h)
            if ok and k.endswith("#llegada"):
                ok = hora_valida(v)
            if ok:
                nuevo[h] = v
            else:
                n += 1
        if nuevo:
            out[k] = nuevo
    return out, n


def aprendidos():
    """Horarios aprendidos, siempre limpios. Si el archivo guardado trae horas invalidas
    (p.ej. '00000' de versiones antiguas) se corrige una vez, con copia .bak."""
    raw = read_json(APRENDIDOS, {})
    ap, n = limpiar_aprendidos(raw)
    if n:
        try:
            ensure_home()
            with open(APRENDIDOS, "rb") as f_:
                write_private(APRENDIDOS + ".bak", f_.read().decode("utf-8", "replace"))
            write_private(APRENDIDOS, json.dumps(ap, ensure_ascii=False, indent=1, sort_keys=True))
            info("Limpieza: eliminadas %d entradas invalidas (horas como 00000) de horarios aprendidos "
                 "(copia en %s.bak)." % (n, APRENDIDOS))
        except OSError:
            pass
    return ap


# Servicios conocidos de antemano (laborables lunes-viernes). Se consideran
# existentes aunque estén llenos. Festivos: no se tienen en cuenta.
SERVICIOS_FIJOS = {
    ("AVILA", "LA ADRADA"): {"dias": (0, 1, 2, 3, 4), "horas": ["12:45", "17:30"]},
}


def fijos(o, d, fecha):
    f = SERVICIOS_FIJOS.get((norm(o), norm(d)))
    return [h for h in f["horas"] if hora_valida(h)] if f and fecha.weekday() in f["dias"] else []


def clave_ap(o, d, fecha):
    return "%s|%s|%d" % (norm(o), norm(d), fecha.weekday())


def guardar_aprendidos(o, d, fecha, horarios, manual=()):
    if not horarios and not manual:
        return
    ap = aprendidos()
    k = clave_ap(o, d, fecha)
    ent = ap.get(k, {})
    horarios = [h for h in horarios if hora_valida(h.get("hora"))]
    manual = [h for h in manual if hora_valida(h)]
    if not horarios and not manual:
        return
    for h in horarios:
        ent[h["hora"]] = fecha.isoformat()
    for h in manual:
        ent.setdefault(h, "manual")
    ap[k] = ent
    ll = ap.get(k + "#llegada", {})
    for h in horarios:
        if hora_valida(h.get("llegada")):
            ll[h["hora"]] = h["llegada"]
    if ll:
        ap[k + "#llegada"] = ll
    ensure_home()
    write_private(APRENDIDOS, json.dumps(ap, ensure_ascii=False, indent=1, sort_keys=True))


FESTIVOS_FILE = os.path.join(HOME, "festivos.json")
# Festivos en los que la empresa no circula (editables con el comando "festivos"). Valores iniciales:
# nacionales fijos y de Castilla y Leon de 2026. Solo sirven para poner la etiqueta SIN SERVICIO
# cuando la web no ofrece ninguna salida ese dia.
FESTIVOS_DEFECTO = ["01/01/2026", "06/01/2026", "02/04/2026", "03/04/2026", "23/04/2026", "01/05/2026",
                    "15/08/2026", "12/10/2026", "01/11/2026", "06/12/2026", "08/12/2026", "25/12/2026"]
# Paradas del lado de Avila: la empresa no circula en esa linea sabados ni domingos.
PARADAS_LINEA_AVILA = ("AVILA", "EL TIEMBLO")
ANTELACION_DEFECTO = 8          # dias maximos de antelacion (se lee tambien del aviso de la web)


def cargar_festivos():
    if not os.path.exists(FESTIVOS_FILE):
        write_private(FESTIVOS_FILE, json.dumps(FESTIVOS_DEFECTO, indent=1))
        return list(FESTIVOS_DEFECTO)
    try:
        v = json.load(open(FESTIVOS_FILE, encoding="utf-8"))
        if not isinstance(v, list):
            raise ValueError("debe ser una lista de fechas DD/MM/AAAA")
        return [str(x) for x in v]
    except ValueError as e:
        raise SystemExit("El fichero %s esta mal escrito (%s). Borralo para recrearlo." % (FESTIVOS_FILE, e))


def es_festivo(fecha):
    for x in cargar_festivos():
        f = parse_fecha(x) if re.match(r"^\s*\d{1,2}/\d{1,2}/\d{4}\s*$", x) else None
        if f == fecha:
            return True
    return False


def analizar_dia(res, fecha):
    """Si la web no ofrece ninguna salida, intenta explicar por que. Devuelve None o un dict:
       {"palabra": "FUERA DE PLAZO"|"SIN SERVICIO", "codigo": 5|6, "motivo": texto}."""
    if res["horarios"]:
        return None
    hoy = dt.date.today()
    dias = (fecha - hoy).days
    m = re.search(r"con\s+(\d+)\b[^|]{0,40}antelaci", res.get("mensaje", ""), re.I) or \
        re.search(r"(\d+)\s*(?:dias?\s*)?de\s+antelaci", res.get("mensaje", ""), re.I)
    if m or re.search(r"antelaci", res.get("mensaje", ""), re.I):
        n = int(m.group(1)) if m else ANTELACION_DEFECTO
        desde = fecha - dt.timedelta(days=n)
        return {"palabra": "FUERA DE PLAZO", "codigo": 5, "motivo": (
            "solo se reserva con %d dias de antelacion como maximo y faltan %d (hoy es %s). "
            "Reservable desde el %s" % (n, dias, fmt_fecha(hoy), fmt_fecha(desde)))}
    o, d = norm(res["origen"]), norm(res["destino"])
    if es_festivo(fecha):
        return {"palabra": "SIN SERVICIO", "codigo": 6,
                "motivo": "festivo: la empresa no circula (lista: python samar.py festivos)"}
    if fecha.weekday() >= 5 and (o in PARADAS_LINEA_AVILA or d in PARADAS_LINEA_AVILA):
        return {"palabra": "SIN SERVICIO", "codigo": 6,
                "motivo": "la empresa no circula sabados ni domingos en la linea La Adrada - Avila"}
    return None


def texto_especial(res, fecha, hora=None):
    e = res.get("especial")
    pref = corto(res["origen"], res["destino"], fecha, hora)
    return "%s: %s - %s" % (e["palabra"], pref, e["motivo"])


def cmd_festivos(a):
    acc = a.accion or "ver"
    fs = cargar_festivos()
    if acc == "ver":
        for x in sorted(fs, key=lambda y: (parse_fecha(y) or dt.date.max)):
            f = parse_fecha(x)
            print("%s%s" % (x, " (%s)" % DIAS[f.weekday()] if f else ""))
        return 0
    if acc == "restaurar":
        write_private(FESTIVOS_FILE, json.dumps(FESTIVOS_DEFECTO, indent=1))
        info("Festivos restaurados.")
        return 0
    if not a.fechas:
        raise SystemExit("Uso:  python samar.py festivos anadir|quitar DD/MM/AAAA [DD/MM/AAAA ...]")
    for x in a.fechas:
        f = parse_fecha(x)
        txt = f.strftime("%d/%m/%Y")
        if acc == "anadir":
            if txt in fs:
                info("%s ya estaba." % txt)
            else:
                fs.append(txt)
                info("Anadido %s (%s)." % (txt, DIAS[f.weekday()]))
        else:
            if txt in fs:
                fs.remove(txt)
                info("Quitado %s." % txt)
            else:
                info("%s no estaba." % txt)
    write_private(FESTIVOS_FILE, json.dumps(fs, indent=1))
    for x in sorted(fs, key=lambda y: (parse_fecha(y) or dt.date.max)):
        print(x)
    return 0


def consultar_horarios(origen, destino, fecha, bono_forzado=None, verbose=True, manual=()):
    bd = run_job("bonos", verbose=verbose)
    if not bd.get("bonos"):
        ruta = guardar_diag("La web no devolvio ningun bono", job={"op": "bonos"}, extra="Respuesta: %s" % json.dumps(bd, ensure_ascii=False)[:2000])
        raise SystemExit("La web no ha devuelto ningun bono (la extension estaba en: %s). Abre Firefox, entra en "
                         "samar-bono.autobusing.es y comprueba que ves tus bonos; luego repite.\n%s" % (bd.get("url"), mensaje_diag(ruta)))
    bono, o, d = elegir_bono(bd["bonos"], origen, destino, fecha, bono_forzado)
    data = run_job("horarios", {
        "bono": bono["bono"], "origen": o, "destino": d, "fecha": fecha.isoformat()}, verbose=verbose)
    horarios = [h for h in (data.get("horarios") or []) if hora_valida(str(h.get("hora", "")))]
    ap = aprendidos()
    k = clave_ap(o, d, fecha)
    conocidas = dict(ap.get(k, {}))   # hora -> ultima fecha vista
    for h in fijos(o, d, fecha):
        conocidas.setdefault(h, "fijo")
    llegadas = {}
    pref = "%s|%s|" % (norm(o), norm(d))
    for kk in sorted(ap):                      # primero otros dias, luego el propio (prioridad)
        if kk.startswith(pref) and kk.endswith("#llegada") and kk != k + "#llegada":
            llegadas.update(ap[kk])
    llegadas.update(ap.get(k + "#llegada", {}))
    for h in horarios:
        if hora_valida(h.get("llegada")):
            llegadas[h["hora"]] = h["llegada"]
    guardar_aprendidos(o, d, fecha, horarios, manual)
    res = {"bono": bono, "origen": o, "destino": d, "fecha": fecha, "horarios": horarios,
           "mensaje": data.get("mensaje", ""), "conocidas": conocidas, "llegadas": llegadas,
           "consulta": data.get("consulta") or {}}
    res["especial"] = analizar_dia(res, fecha)
    c = res["consulta"]
    if not horarios and c.get("vigilancia_red") and not any(
            "horarios" in str(p_.get("url", "")) for p_ in c.get("peticiones", [])):
        res["dudoso"] = True
        info("AVISO: no se observo la respuesta de la web a la peticion de horarios; una lista vacia puede no ser fiable. "
             "Repite la consulta (usa --detalle para ver las peticiones).")
    return res


def mostrar_consulta(res):
    """--detalle: prueba de que la lista viene de una peticion nueva a la web (stdout)."""
    c = res.get("consulta") or {}
    if not c:
        info("Consulta a la web: la extension no ha enviado datos de la consulta (extension antigua? instala la 1.3.0).")
        return

    def hora(iso):
        try:
            u = dt.datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc)
            return u.astimezone().strftime("%H:%M:%S")
        except (ValueError, TypeError):
            return "?"
    info("Consulta a la web: de %s a %s (hora del movil), intento %s" % (hora(c.get("inicio")), hora(c.get("fin")), c.get("intento", "?")))
    for p_ in c.get("peticiones", []):
        info("  %s %s -> %s (%s)" % (p_.get("metodo"), p_.get("url"), p_.get("estado"), "DESDE CACHE" if p_.get("cache") else "red"))
    if not c.get("vigilancia_red"):
        info("  Vigilancia de red no disponible en este Firefox: no se puede demostrar que la respuesta sea nueva.")
    elif c.get("verificada"):
        info("  VERIFICADA: la lista llego de una peticion nueva a la web (sin cache).")
    else:
        info("  NO VERIFICADA: no se observo ninguna peticion de datos a la web; el resultado puede no ser fiable.")


def clasificar(res, esperadas=()):
    """Lista de (hora, estado, info) con estado LIBRE / LLENA."""
    libres = {h["hora"]: h for h in res["horarios"] if hora_valida(h.get("hora"))}
    horas = {h for h in (set(libres) | set(res["conocidas"]) | set(esperadas)) if hora_valida(h)}
    out = []
    for h in sorted(horas):
        if h in libres:
            out.append((h, "LIBRE", libres[h]))
        else:
            out.append((h, "LLENA", None))
    return out


def imprimir_horarios(res, esperadas=()):
    print("%s   (bono %s: %s, saldo %s)" % (
        corto(res["origen"], res["destino"], res["fecha"]),
        res["bono"]["bono"], " - ".join(res["bono"].get("estaciones", [])), res["bono"].get("saldo")))
    filas = clasificar(res, esperadas)
    if res.get("especial") and not res["horarios"]:
        print(texto_especial(res, res["fecha"]))
        return
    if not filas:
        print("  Sin horarios con plazas libres.")
        if res["mensaje"]:
            info("Aviso de la web: %s" % res["mensaje"])
        info("(No conozco horarios previos para esta ruta/dia, asi que no puedo decir si hay salidas llenas;"
             " usa --esperadas 12:45,16:00)")
        return
    for hora, est, h in filas:
        if est == "LIBRE":
            ll = " llegada %s" % h["llegada"] if h["llegada"] else ""
            lb = "%s libres" % h["libres"] if h["libres"] is not None else "plazas libres"
            print("  sal %s  LIBRE  (%s%s)" % (hora, lb, ll))
        else:
            print("  sal %s  LLENA" % hora)
    if res["mensaje"]:
        info("Aviso de la web: %s" % res["mensaje"])


# ----------------------------------------------------------------------------
# comandos
# ----------------------------------------------------------------------------
def cmd_config(a):
    ensure_home()
    cfg = read_json(CONFIG, {})
    email = preguntar("Email [%s]: " % cfg.get("email", "")).strip() or cfg.get("email", "")
    pw = getpass.getpass("Contrasena (vacio = mantener): ") or cfg.get("password", "")
    if not email or not pw:
        raise SystemExit("Faltan datos.")
    write_private(CONFIG, json.dumps({"email": email, "password": pw}))
    info("Guardado en %s (solo legible por tu usuario)." % CONFIG)


def cmd_estado(a):
    if not server_up():
        print("Puente: parado (se arranca solo al usar cualquier comando).")
        return 1
    st = http("GET", "/estado")
    print("Puente: en marcha en %s" % BRIDGE_URL)
    print("Extension emparejada: %s" % ("si" if st["emparejada"] else "no (se empareja sola al conectarse)"))
    print("Extension conectada:  %s" % ("si" if st["conectada"] else "no"))
    cfg = load_config()
    print("Credenciales:         %s" % (cfg.get("email") if cfg.get("password") else "NO configuradas (python samar.py config)"))
    return 0 if st["conectada"] else 1


def cmd_abrir(a):
    if abrir_firefox():
        info("Abierto.")
        return 0
    err("No pude abrir Firefox (abrelo a mano).")
    return 1


def cmd_desvincular(a):
    if os.path.exists(PAIR):
        os.remove(PAIR)
    info("Emparejamiento borrado; la proxima extension que se conecte se emparejara.")


def cmd_bonos(a):
    d = run_job("bonos")
    if a.json:
        print(json.dumps(d["bonos"], ensure_ascii=False, indent=1))
        return 0
    for b in d["bonos"]:
        print("Bono %s  %s" % (b["bono"], b["titulo"]))
        print("   %s   vigencia %s   saldo %s" % (" <-> ".join(b["estaciones"]), b["vigencia"], b["saldo_texto"]))
    return 0


def cmd_horarios(a):
    fecha = parse_fecha(a.fecha)
    esp = [parse_hora(x) for x in a.esperadas.split(",") if x.strip()] if a.esperadas else []
    res = consultar_horarios(a.origen, a.destino, fecha, a.bono, manual=esp)
    if a.json:
        print(json.dumps({
            "origen": res["origen"], "destino": res["destino"], "fecha": fecha.isoformat(),
            "bono": res["bono"]["bono"],
            "horarios": [{"hora": h, "estado": e, "libres": (i or {}).get("libres")} for h, e, i in clasificar(res, esp)],
            "estado_dia": (res.get("especial") or {}).get("palabra"),
            "mensaje": res["mensaje"]}, ensure_ascii=False, indent=1))
        return 0
    imprimir_horarios(res, esp)
    if a.detalle:
        mostrar_consulta(res)
    return res["especial"]["codigo"] if res.get("especial") and not res["horarios"] else 0


def anotar_registro(a, codigo, mensaje):
    """Anade una linea al archivo del registro: fecha hora | comando | codigo | mensaje."""
    if getattr(a, "registro", None) is None:
        return
    ruta = os.path.abspath(os.path.expanduser(a.registro)) if a.registro else REGISTRO_DEFECTO
    escribir_registro(ruta, codigo, mensaje)


def escribir_registro(ruta, codigo, mensaje):
    cmd = " ".join(["python", os.path.basename(sys.argv[0])] + [shlex.quote(x) for x in sys.argv[1:]])
    try:
        carpeta = os.path.dirname(ruta)
        if carpeta:
            os.makedirs(carpeta, exist_ok=True)
        with open(ruta, "a", encoding="utf-8") as f:
            f.write("%s | %s | codigo=%s | %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), cmd, codigo,
                                                    " ".join(str(mensaje).split())))
    except OSError as e:
        err("ERROR: no pude escribir el registro %s (%s)" % (ruta, e))


def cmd_comprobar(a):
    """Envuelve _comprobar para anotar en el registro (--registro) el resultado o el error."""
    try:
        code = _comprobar(a)
    except SystemExit as e:
        if isinstance(e.code, str):
            anotar_registro(a, 10, "ERROR: " + e.code.splitlines()[0])
        raise
    except Exception as e:
        anotar_registro(a, 10, "ERROR INESPERADO: %s" % e)
        raise
    anotar_registro(a, code, _ULTIMA[0])
    return code


_ULTIMA = [""]


def _lineas_dia(res, fecha):
    """(lineas, codigo) de un dia sin hora: "Con plazas ese dia: HH:MM (N libres), ..." y, si hay, las llenas conocidas."""
    filas = clasificar(res)
    libres = [f for f in filas if f[1] == "LIBRE"]
    llenas = [f[0] for f in filas if f[1] == "LLENA"]
    if res.get("especial") and not libres:
        return [texto_especial(res, fecha)], res["especial"]["codigo"]
    lineas = []
    if libres:
        lineas.append("Con plazas ese dia: " + ", ".join(
            "%s (%s libres)" % (h, i["libres"]) if i["libres"] is not None else "%s (con plazas)" % h
            for h, _, i in libres))
    else:
        lineas.append("Ese dia no hay ningun horario con plazas.")
    if llenas:
        lineas.append("Llenas (existen pero sin plazas): " + ", ".join(llenas))
    return lineas, (0 if libres else (1 if llenas else 2))


def _comprobar_dia(a):
    """Sin HORA: "Con plazas ese dia: ..." (como cuando la hora pedida no existia).
    Con --vuelta [FECHA]: despues, lo mismo para el trayecto inverso (por defecto el mismo dia).
    Codigos: 0 si hay plazas en alguna salida (de cada trayecto pedido), 1 si alguno tiene todo lleno, 2 si en alguno no consta nada."""
    if a.existe:
        raise SystemExit("--existe necesita una HORA concreta.")
    fecha = parse_fecha(a.fecha)
    fecha_v = None
    if a.vuelta is not None:
        fecha_v = parse_fecha(a.vuelta) if a.vuelta else fecha
    res = consultar_horarios(a.origen, a.destino, fecha, a.bono)
    lineas, code = _lineas_dia(res, fecha)
    todo = []
    if fecha_v is not None:
        todo.append("IDA: %s" % corto(res["origen"], res["destino"], fecha))
    todo += lineas
    mensajes_web = [res["mensaje"]] if res["mensaje"] and not res.get("especial") else []
    consultas = [res]
    if fecha_v is not None:
        # la vuelta usa el mismo bono que la ida (no se vuelve a preguntar)
        res2 = consultar_horarios(res["destino"], res["origen"], fecha_v, str(res["bono"]["bono"]))
        l2, c2 = _lineas_dia(res2, fecha_v)
        todo.append("VUELTA: %s" % corto(res2["origen"], res2["destino"], fecha_v))
        todo += l2
        code = max(code, c2)
        if res2["mensaje"] and not res2.get("especial"):
            mensajes_web.append(res2["mensaje"])
        consultas.append(res2)
    for l in todo:
        print(l)
    _ULTIMA[0] = " ; ".join(todo)
    if not a.callado:
        for m in mensajes_web:
            info("Aviso de la web: %s" % m)
    if a.detalle:
        for r_ in consultas:
            mostrar_consulta(r_)
    return code


def _comprobar(a):
    """Una unica linea por stdout que empieza por LIBRE, LLENA o NO CONSTA.
    Codigos de salida: 0 LIBRE, 1 LLENA, 2 NO CONSTA (errores: 10)."""
    if a.hora is None:
        return _comprobar_dia(a)
    if a.vuelta is not None:
        raise SystemExit("--vuelta solo se puede usar sin HORA (comprobar ORIGEN DESTINO FECHA --vuelta [FECHA]).")
    fecha = parse_fecha(a.fecha)
    hora = parse_hora(a.hora)
    res = consultar_horarios(a.origen, a.destino, fecha, a.bono, manual=[hora] if a.existe else ())
    if a.existe:
        res["conocidas"].setdefault(hora, "manual")
    libres = {h["hora"]: h for h in res["horarios"]}
    cab = corto(res["origen"], res["destino"], fecha, hora)
    if res.get("especial") and hora not in libres:
        linea = texto_especial(res, fecha, hora)
        print(linea)
        _ULTIMA[0] = linea
        if a.detalle:
            mostrar_consulta(res)
        return res["especial"]["codigo"]
    if hora in libres:
        h = libres[hora]
        n = h["libres"]
        det = ("quedan %s plazas libres" % n) if n is not None else "hay plazas libres"
        linea = "LIBRE: %s - %s" % (cab, det)
        print(linea)
        code = 0
    elif hora in res["conocidas"]:
        linea = "LLENA: %s - existe pero no quedan plazas" % cab
        print(linea)
        code = 1
    else:
        linea = "NO CONSTA: %s - la web no la ofrece y no consta que exista (puede estar llena, no circular o ser festivo)" % cab
        print(linea)
        code = 2
    if not a.callado:
        if code == 2:      # la hora no existe: ademas, las que si tienen plazas ese dia
            if res["horarios"]:
                info("Con plazas ese dia: " + ", ".join(
                    "%s (%s libres)" % (h["hora"], h["libres"]) for h in res["horarios"]))
            else:
                info("Ese dia no hay ningun horario con plazas.")
        if res["mensaje"]:
            info("Aviso de la web: %s" % res["mensaje"])
    _ULTIMA[0] = linea
    if a.detalle:
        mostrar_consulta(res)
    return code


def cmd_canjear(a):
    """Canjea 1 viaje del bono por un viaje concreto (tiene efecto real, gasta 1 viaje del bono).
    Codigos: 0 canjeado/simulado, 1 cancelado por ti, 2 sin plazas/hora no disponible,
    3 no confirmado, 4 ya existe ese viaje (errores: 10)."""
    fecha = parse_fecha(a.fecha)
    if fecha < dt.date.today():
        raise SystemExit("La fecha %s ya ha pasado." % fmt_fecha(fecha))
    hora = parse_hora(a.hora) if a.hora else None
    res = consultar_horarios(a.origen, a.destino, fecha, a.bono)
    libres = {h["hora"]: h for h in res["horarios"]}
    ruta_txt = corto(res["origen"], res["destino"], fecha)

    def salir(code, lineas, canje=False):
        for l in lineas:
            print(l)
        texto = " ; ".join(lineas)
        if canje:
            escribir_registro(REGISTRO_CANJES, code, texto)
        if getattr(a, "registro", None) is not None:
            anotar_registro(a, code, texto)
        return code

    if not libres and res.get("especial"):
        return salir(res["especial"]["codigo"], [texto_especial(res, fecha)])
    if not libres:
        return salir(2, ["SIN PLAZAS: ninguna salida %s tiene plazas libres." % ruta_txt])
    if hora is None:
        if not sys.stdin.isatty():
            raise SystemExit("Sin HORA hace falta teclado para elegir. Indica la hora: canjear ORIGEN DESTINO FECHA HH:MM")
        orden = sorted(libres)
        print("Salidas con plazas %s:" % ruta_txt)
        for i, h in enumerate(orden, 1):
            x = libres[h]
            extra = []
            if x["libres"] is not None:
                extra.append("%s libres" % x["libres"])
            if x["llegada"]:
                extra.append("llegada %s" % x["llegada"])
            print("  %d) %s%s" % (i, h, "  (" + ", ".join(extra) + ")" if extra else ""))
        while True:
            r = preguntar("Elige 1-%d (0 o vacio = cancelar): " % len(orden)).strip()
            if r in ("", "0"):
                return salir(1, ["CANCELADO: no has elegido ninguna salida; no se ha canjeado nada."])
            if r.isdigit() and 1 <= int(r) <= len(orden):
                hora = orden[int(r) - 1]
                break
            if r in libres:
                hora = r
                break
            print("Opcion no valida.")
    if hora not in libres:
        conocida = hora in res["conocidas"]
        return salir(2, [("LLENA: sal %s %s - existe pero no quedan plazas; no se canjea nada." if conocida else
                          "NO CONSTA: sal %s %s - la web no la ofrece (llena o inexistente); no se canjea nada.") % (hora, ruta_txt),
                         "Con plazas ese dia: " + ", ".join("%s (%s libres)" % (h["hora"], h["libres"]) for h in res["horarios"])])
    x = libres[hora]
    bono = res["bono"]
    resumen = "sal %s %s" % (hora, ruta_txt)
    if not a.simular and not a.si:
        saldo = bono.get("saldo")
        print("VAS A CANJEAR 1 VIAJE DEL BONO %s%s:" % (
            bono["bono"], " (saldo %s -> %s)" % (saldo, saldo - 1) if isinstance(saldo, int) else ""))
        print("  %s%s" % (resumen, " - quedan %s plazas" % x["libres"] if x["libres"] is not None else ""))
        if x["llegada"]:
            print("  llegada estimada: %s" % x["llegada"])
        print("  Si lo anulas despues, la web puede aplicar penalizacion.")
        if not sys.stdin.isatty():
            raise SystemExit("Para canjear sin teclado anade --si (o usa --simular para probar).")
        if preguntar("Escribe SI para canjear: ").strip() != "SI":
            return salir(1, ["CANCELADO: no se ha canjeado nada."])
    try:
        v = run_job("canjear", {"bono": bono["bono"], "origen": res["origen"], "destino": res["destino"],
                                "fecha": fecha.isoformat(), "hora": hora, "simular": bool(a.simular),
                                "duplicado": bool(a.duplicado), "plaza": getattr(a, "plaza", None) or ""})
    except SystemExit as e:
        if not a.simular and isinstance(e.code, str):
            escribir_registro(REGISTRO_CANJES, 10, "ERROR durante el canje de %s: %s" % (resumen, e.code.splitlines()[0]))
            raise SystemExit(e.code + "\nATENCION: si el error ocurrio despues de pulsar Aceptar, el viaje PODRIA haberse canjeado.\n"
                             "Antes de repetir, comprueba:  python samar.py viajes")
        raise
    est = v.get("estado")
    if est == "SIMULADO":
        lin = ["SIMULADO: %s - seleccionada en la web; NO se ha pulsado Aceptar ni se ha gastado ningun viaje." % resumen]
        pz = v.get("plazas") or {}
        if pz.get("total_libres"):
            lin.append("Esta salida pide elegir plaza (%s libres: %s%s). Sin --plaza se elige la primera libre; con --plaza N, la N." % (
                pz["total_libres"], ", ".join(str(x) for x in (pz.get("libres") or [])[:25]), " ..." if pz["total_libres"] > 25 else ""))
        else:
            lin.append("Esta salida no pide elegir plaza (la asigna la web).")
        return salir(0, lin)
    if est == "DUPLICADO":
        w = v.get("viaje") or {}
        return salir(4, ["YA EXISTE: ya tienes un viaje igual (%s %s, localizador %s); no se canjea. "
                         "Usa --duplicado si de verdad quieres otro." % (w.get("fecha"), w.get("hora"), w.get("localizador"))])
    if est == "CANJEADO":
        w = v.get("viaje") or {}
        lineas = ["CANJEADO: %s - localizador %s, bono %s" % (resumen, w.get("localizador"), w.get("bono") or bono["bono"])]
        if w.get("plaza") or v.get("plaza"):
            lineas[0] += ", plaza %s" % (w.get("plaza") or v.get("plaza"))
        try:
            for b in run_job("bonos", verbose=False)["bonos"]:
                if str(b["bono"]) == str(bono["bono"]):
                    lineas.append("Saldo del bono %s: %s -> %s" % (b["bono"], bono.get("saldo"), b.get("saldo")))
        except SystemExit:
            pass
        return salir(0, lineas, canje=True)
    diag_pag = v.pop("diag", None)
    ruta_d = guardar_diag("Canje NO confirmado: " + resumen, job={"op": "canjear", "hora": hora},
                          res={"ok": True, "diag": diag_pag} if diag_pag else {"ok": True}, extra=json.dumps(v, ensure_ascii=False)[:3000])
    lineas = ["NO CONFIRMADO: %s - se pulso Aceptar pero el viaje no aparece en tu lista; no puedo asegurar que se haya canjeado." % resumen,
              "Mira 'python samar.py viajes' ANTES de repetir, para no canjear dos veces."]
    if v.get("mensajes"):
        lineas.append("Mensaje de la web: %s" % v["mensajes"])
    ep = v.get("errores_pagina") or {}
    for t in (ep.get("avisos") or []) + (ep.get("campos_invalidos") or []):
        lineas.append("La web indica: %s" % t)
    if v.get("plaza_elegida"):
        lineas.append("Plaza elegida: %s" % v["plaza_elegida"])
    code = salir(3, lineas, canje=True)
    print(mensaje_diag(ruta_d))
    return code


def cmd_viajes(a):
    d = run_job("viajes")
    rows = d["viajes"]
    if a.json:
        print(json.dumps(d, ensure_ascii=False, indent=1))
        return 0
    if not rows:
        info("No hay viajes.")
        return 0
    cols = [("fecha", "Fecha"), ("hora", "Hora"), ("origen", "Origen"), ("destino", "Destino"),
            ("localizador", "Localiz."), ("bono", "Bono"), ("precio", "Precio"), ("plaza", "Plaza")]
    w = {k: max(len(t), *(len(r.get(k, "")) for r in rows)) for k, t in cols}
    print("  ".join(t.ljust(w[k]) for k, t in cols))
    for r in rows:
        print("  ".join(r.get(k, "").ljust(w[k]) for k, _ in cols))
    if d.get("penaliza") == "True":
        info("AVISO: la web indica que anular ahora PENALIZA.")
    return 0


def seleccionar_viajes(rows, a):
    sel = rows
    if a.loc:
        sel = [r for r in sel if r.get("localizador") == str(a.loc)]
    if a.fecha:
        f = parse_fecha(a.fecha).strftime("%d/%m/%y")
        sel = [r for r in sel if r.get("fecha") == f]
    if a.hora:
        hh = parse_hora(a.hora)
        sel = [r for r in sel if r.get("hora", "").rjust(5, "0") == hh]
    if a.origen:
        sel = [r for r in sel if match_name(a.origen, r.get("origen", ""))]
    if a.destino:
        sel = [r for r in sel if match_name(a.destino, r.get("destino", ""))]
    return sel


def cmd_anular(a):
    if not (a.loc or a.fecha):
        raise SystemExit("Indica --loc LOCALIZADOR o --fecha DD/MM/AAAA [--hora HH:MM --origen X --destino Y]")
    d = run_job("viajes")
    sel = seleccionar_viajes(d["viajes"], a)
    if not sel:
        raise SystemExit("Ningun viaje coincide. Mira 'python samar.py viajes'.")
    if len(sel) > 1 and not a.todos:
        info("Varios viajes coinciden; concreta con --loc o usa --todos:")
        for r in sel:
            info("  %s %s %s -> %s  loc %s" % (r["fecha"], r["hora"], r["origen"], r["destino"], r["localizador"]))
        return 2
    if d.get("penaliza") == "True":
        info("AVISO: la web indica que anular ahora PENALIZA.")
    rc = 0
    for r in sel:
        desc = "%s %s  %s -> %s  (localizador %s, bono %s)" % (
            r["fecha"], r["hora"], r["origen"], r["destino"], r["localizador"], r["bono"])
        if a.simular:
            v = run_job("anular", {"localizador": r["localizador"], "ejecutar": False})
            print("SIMULACION: %s -> ficha abierta, anulable=%s. No se ha anulado nada." % (desc, v["anulable"]))
            continue
        if not a.si:
            resp = preguntar("Anular %s ?  Escribe SI para confirmar: " % desc).strip()
            if resp != "SI":
                print("Cancelado.")
                rc = 1
                continue
        v = run_job("anular", {"localizador": r["localizador"], "ejecutar": True})
        if v["anulado"]:
            print("ANULADO: %s" % desc)
        else:
            print("NO se pudo confirmar la anulacion de %s (el viaje sigue en la lista). Mensaje: %s" % (
                desc, v.get("mensajes") or "-"))
            rc = 3
        if v.get("mensajes"):
            info("Web: %s" % v["mensajes"])
    return rc


def cmd_salida(a):
    """Primera o ultima salida del dia. Codigos: 0 libre, 1 llena, 2 sin servicios conocidos."""
    fecha = parse_fecha(a.fecha)
    res = consultar_horarios(a.origen, a.destino, fecha, a.bono)
    filas = clasificar(res)
    if res.get("especial") and not res["horarios"]:
        print(texto_especial(res, fecha))
        return res["especial"]["codigo"]
    if not filas:
        info("SIN SALIDAS CONOCIDAS: no conozco ninguna salida para %s y no hay plazas libres. "
             "Usa 'horarios ... --esperadas H1,H2' para indicarlas." % corto(res["origen"], res["destino"], fecha))
        return 2
    quiere = "ultimo" if a.ultimo and not a.primero else "primero" if a.primero and not a.ultimo else None
    elegidas = []
    if quiere in (None, "primero"):
        elegidas.append(("PRIMERA", filas[0]))
    if quiere in (None, "ultimo") and (quiere == "ultimo" or len(filas) > 1):
        elegidas.append(("ULTIMA", filas[-1]))
    print(corto(res["origen"], res["destino"], fecha))
    code = 0
    for nombre, (hora, est, h) in elegidas:
        if est == "LIBRE":
            lb = " (%s libres)" % h["libres"] if h["libres"] is not None else ""
            ll = h["llegada"] or "no indicada"
            print("  %s sal %s: LIBRE%s - llegada estimada a %s: %s (hora de la web)" % (
                nombre, hora, lb, res["destino"], ll))
        else:
            ll = res["llegadas"].get(hora)
            print("  %s sal %s: LLENA - llegada estimada a %s: %s" % (
                nombre, hora, res["destino"],
                "%s (vista en la web otro dia)" % ll if ll else "desconocida (la web no muestra salidas llenas)"))
            code = 1
    if res["mensaje"]:
        info("Aviso de la web: %s" % res["mensaje"])
    if a.detalle:
        mostrar_consulta(res)
    return code


def mostrar_rutas(r):
    print("Paradas que cubre cada bono (fichero %s):" % RUTAS_FILE)
    for k, v in r.items():
        print("  Bono %s (%s):" % (k, v.get("nombre", "")))
        print("      " + " > ".join(v["paradas"]))


def cmd_rutas(a):
    acc = a.accion or "ver"
    if acc == "ver":
        mostrar_rutas(cargar_rutas())
        return 0
    if acc == "restaurar":
        if os.path.exists(RUTAS_FILE):
            shutil.copyfile(RUTAS_FILE, RUTAS_FILE + ".bak")
            info("Copia de seguridad en %s.bak" % RUTAS_FILE)
        guardar_rutas(RUTAS_DEFECTO)
        mostrar_rutas(RUTAS_DEFECTO)
        return 0
    if acc in ("anadir", "quitar"):
        if not a.bono or not a.parada:
            raise SystemExit("Uso:  python samar.py rutas %s NUMERO_DE_BONO \"PARADA\"" % acc)
        r = cargar_rutas(avisar=False)
        guardar_rutas(r)                      # deja el fichero limpio (sin duplicados)
        bono = str(a.bono)
        parada = norm(a.parada)
        if bono not in r:
            if acc == "quitar":
                raise SystemExit("El bono %s no esta en rutas.json. Bonos: %s" % (bono, ", ".join(r)))
            r[bono] = {"nombre": "Bono %s" % bono, "paradas": []}
        ps = r[bono]["paradas"]
        if acc == "anadir":
            if parada in ps:
                info("La parada %s ya estaba en el bono %s." % (parada, bono))
            elif a.despues:
                ref = resolver_parada(a.despues, ps)
                if not isinstance(ref, str):
                    raise SystemExit("No encuentro '%s' en el bono %s: %s" % (a.despues, bono, " > ".join(ps)))
                ps.insert(ps.index(ref) + 1, parada)
            else:
                ps.append(parada)
        else:
            ref = resolver_parada(a.parada, ps)
            if not isinstance(ref, str):
                raise SystemExit("No encuentro '%s' en el bono %s: %s" % (a.parada, bono, " > ".join(ps)))
            ps.remove(ref)
        guardar_rutas(r)
        mostrar_rutas(r)
        return 0
    raise SystemExit("Accion desconocida: %s" % acc)


def todas_las_paradas():
    out = []
    for v in cargar_rutas().values():
        for p in v["paradas"]:
            if p not in out:
                out.append(p)
    return out


def mostrar_alias(al):
    for canon in sorted(al):
        print("%s: %s" % (norm(canon), ", ".join(al[canon]) if al[canon] else "(sin alias)"))


def cmd_alias(a):
    acc = a.accion or "ver"
    nombres = a.nombres or []
    if acc == "ver":
        mostrar_alias(cargar_alias())
        return 0
    if acc == "restaurar":
        if os.path.exists(ALIAS_FILE):
            shutil.copyfile(ALIAS_FILE, ALIAS_FILE + ".bak")
            info("Copia de seguridad en %s.bak" % ALIAS_FILE)
        guardar_alias(ALIAS_DEFECTO)
        mostrar_alias(ALIAS_DEFECTO)
        return 0
    al = cargar_alias()
    if acc == "anadir":
        if len(nombres) < 2:
            raise SystemExit("Uso:  python samar.py alias anadir \"PARADA\" \"ALIAS\" [\"OTRO ALIAS\" ...]")
        paradas = todas_las_paradas()
        canon = resolver_parada(nombres[0], paradas)
        if isinstance(canon, list):
            raise SystemExit("'%s' es ambiguo: %s. Escribe el nombre completo." % (nombres[0], ", ".join(canon)))
        if not canon:
            raise SystemExit("No conozco la parada '%s'. Paradas conocidas:\n  %s\n(Una parada nueva se anade con: "
                             "python samar.py rutas anadir BONO \"PARADA\")" % (nombres[0], "\n  ".join(paradas)))
        for x in nombres[1:]:
            if anadir_alias(al, canon, x):
                info("Alias '%s' -> %s" % (x, canon))
            else:
                info("El alias '%s' ya existia para %s." % (x, canon))
        guardar_alias(al)
        mostrar_alias(al)
        return 0
    if acc == "quitar":
        if not nombres:
            raise SystemExit("Uso:  python samar.py alias quitar \"ALIAS\" [\"ALIAS\" ...]")
        for x in nombres:
            k = compacto(x)
            hallado = False
            for canon in al:
                nuevo = [y for y in al[canon] if compacto(y) != k]
                if len(nuevo) != len(al[canon]):
                    al[canon] = nuevo
                    hallado = True
                    info("Quitado el alias '%s' de %s." % (x, norm(canon)))
            if not hallado:
                info("No existe el alias '%s'." % x)
        guardar_alias(al)
        mostrar_alias(al)
        return 0
    raise SystemExit("Accion desconocida: %s" % acc)


def cmd_aprendidos(a):
    ap = aprendidos()
    if not ap:
        info("Aun no hay horarios aprendidos.")
        return 0
    for k in sorted(ap):
        if "#" in k:
            continue
        o, d, wd = k.split("|")
        print("%s -> %s (%s): %s" % (o, d, DIAS[int(wd)], ", ".join(sorted(ap[k]))))
    return 0


def main(argv=None):
    class Parser(argparse.ArgumentParser):
        def error(self, mensaje):                       # errores de uso: codigo 64 (el 2 es de NO CONSTA)
            self.print_usage(sys.stderr)
            self.exit(64, "%s: error: %s\n" % (self.prog, mensaje))

    p = Parser(prog="samar.py", description="Control de SAMAR Bono Virtual desde Termux.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="arrancar el puente en primer plano")
    sub.add_parser("config", help="guardar email y contrasena")
    sub.add_parser("estado", help="estado del puente/extension")
    sub.add_parser("abrir", help="abrir Firefox Nightly")
    sub.add_parser("desvincular", help="olvidar la extension emparejada")
    sub.add_parser("aprendidos", help="horarios aprendidos")
    s = sub.add_parser("diagnostico", help="generar un archivo de diagnostico")
    s.add_argument("--probar", nargs=3, metavar=("ORIGEN", "DESTINO", "FECHA"), help="ejecuta antes esta consulta y anota el resultado")
    s.add_argument("--copiar", action="store_true", help="ademas, copiarlo a la carpeta Descargas (opcional)")
    s = sub.add_parser("festivos", help="festivos en los que la empresa no circula")
    s.add_argument("accion", nargs="?", choices=["ver", "anadir", "quitar", "restaurar"])
    s.add_argument("fechas", nargs="*")
    s = sub.add_parser("alias", help="formas alternativas de escribir una parada")
    s.add_argument("accion", nargs="?", choices=["ver", "anadir", "quitar", "restaurar"])
    s.add_argument("nombres", nargs="*")
    s = sub.add_parser("rutas", help="ver o cambiar las paradas que cubre cada bono")
    s.add_argument("accion", nargs="?", choices=["ver", "anadir", "quitar", "restaurar"])
    s.add_argument("bono", nargs="?"); s.add_argument("parada", nargs="?")
    s.add_argument("--despues", help="al anadir: colocar la parada detras de esta otra")
    s = sub.add_parser("salida", help="primera/ultima salida del dia con hora de llegada")
    s.add_argument("origen"); s.add_argument("destino"); s.add_argument("fecha")
    s.add_argument("-primero", "--primero", action="store_true", help="solo la primera salida del dia")
    s.add_argument("-ultimo", "--ultimo", action="store_true", help="solo la ultima salida del dia")
    s.add_argument("--bono")
    s.add_argument("--detalle", action="store_true", help="muestra la prueba de que la consulta fue nueva")
    s = sub.add_parser("bonos", help="listar bonos"); s.add_argument("--json", action="store_true")
    s = sub.add_parser("viajes", help="listar viajes"); s.add_argument("--json", action="store_true")
    s = sub.add_parser("horarios", help="horarios con plazas y salidas llenas")
    s.add_argument("origen"); s.add_argument("destino"); s.add_argument("fecha")
    s.add_argument("--bono"); s.add_argument("--esperadas", help="horas que sabes que existen (se recuerdan): 12:45,16:00")
    s.add_argument("--json", action="store_true")
    s.add_argument("--detalle", action="store_true", help="muestra la prueba de que la consulta fue nueva")
    s = sub.add_parser("comprobar", help="comprobar un viaje concreto")
    s.add_argument("origen"); s.add_argument("destino"); s.add_argument("fecha")
    s.add_argument("hora", nargs="?", help="opcional: sin hora muestra todas las salidas del dia")
    s.add_argument("--bono"); s.add_argument("--callado", action="store_true")
    s.add_argument("--detalle", action="store_true", help="muestra la prueba de que la consulta fue nueva")
    s.add_argument("--vuelta", nargs="?", const="", default=None, metavar="FECHA",
                   help="solo sin HORA: muestra tambien el trayecto inverso (sin FECHA, el mismo dia)")
    s.add_argument("--registro", nargs="?", const="", default=None, metavar="ARCHIVO",
                   help="ademas de stdout, anade una linea (fecha, comando, codigo, mensaje) a ARCHIVO "
                        "(sin nombre: registro_comprobar.txt junto a samar.py)")
    s.add_argument("--existe", action="store_true",
                   help="ese horario existe: si no tiene plazas se informa LLENA (y se recuerda)")
    s = sub.add_parser("canjear", aliases=["comprar", "reservar"],
                       help="canjear 1 viaje del bono por una salida concreta (gasta 1 viaje)")
    s.add_argument("origen"); s.add_argument("destino"); s.add_argument("fecha")
    s.add_argument("hora", nargs="?", help="opcional: sin hora muestra las salidas con plazas para que elijas")
    s.add_argument("--bono")
    s.add_argument("--si", action="store_true", help="no pedir confirmacion (cuidado)")
    s.add_argument("--simular", action="store_true", help="prepara todo en la web pero NO pulsa Aceptar")
    s.add_argument("--duplicado", action="store_true", help="permite canjear aunque ya tengas ese mismo viaje")
    s.add_argument("--plaza", help="si la salida pide elegir plaza: la que quieres (por defecto, la primera libre)")
    s.add_argument("--registro", nargs="?", const="", default=None, metavar="ARCHIVO",
                   help="anade tambien una linea a ARCHIVO (sin nombre: registro_comprobar.txt)")
    s = sub.add_parser("anular", help="anular un viaje pagado")
    s.add_argument("--loc"); s.add_argument("--fecha"); s.add_argument("--hora")
    s.add_argument("--origen"); s.add_argument("--destino")
    s.add_argument("--si", action="store_true", help="no pedir confirmacion")
    s.add_argument("--simular", action="store_true", help="abre la ficha pero no anula")
    s.add_argument("--todos", action="store_true", help="anular todos los que coincidan")
    a = p.parse_args(argv)
    if a.cmd == "serve":
        return serve()
    f = {"config": cmd_config, "estado": cmd_estado, "abrir": cmd_abrir, "desvincular": cmd_desvincular,
         "aprendidos": cmd_aprendidos, "diagnostico": cmd_diagnostico, "rutas": cmd_rutas, "alias": cmd_alias, "festivos": cmd_festivos, "salida": cmd_salida, "bonos": cmd_bonos, "viajes": cmd_viajes, "horarios": cmd_horarios,
         "comprobar": cmd_comprobar, "canjear": cmd_canjear, "comprar": cmd_canjear, "reservar": cmd_canjear, "anular": cmd_anular}[a.cmd]
    try:
        return f(a) or 0
    except SystemExit as e:
        if isinstance(e.code, str):                       # error: texto por stderr, codigo 10
            msg = e.code
            err("ERROR: " + msg)
            return 10
        raise
    except ValueError as e:
        err("ERROR: %s" % e)
        return 10
    except (KeyboardInterrupt, BrokenPipeError):
        raise
    except urllib.error.URLError as e:
        err("ERROR: No hay conexion con el puente: %s" % e)
        return 10
    except Exception:                                    # error inesperado del programa
        import traceback
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        ruta = guardar_diag("ERROR INESPERADO DEL PROGRAMA", extra=tb)
        err("ERROR: " + mensaje_diag(ruta))
        return 10


if __name__ == "__main__":
    sys.exit(main())
