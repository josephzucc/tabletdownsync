#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ree.py  v1.0  -  Red Electrica: acceso y conexion de DEMANDA (Termux).

Red Electrica protege su web con Incapsula y solo deja los listados durante un
mes.  Este programa NO salta ninguna proteccion: abre en Termux un servidor
local (solo 127.0.0.1) y la extension de Firefox "REE Puente" (ree.xpi), que
navega como tu, descarga los ficheros publicados y se los entrega.  Aqui se
archivan por mes, se analizan y se comparan con el mes anterior.

Ordenes:
  serve      Servidor local para la extension (dejarlo abierto)
  despertar  Pide a Firefox/extension que descargue ahora
  analizar   Analiza el ultimo listado de solicitudes (y lo compara con el anterior)
  listar     Lista lo archivado
  pagina     Ver / anadir / quitar paginas de REE que vigila la extension
  estado     Resumen
"""
import argparse
import csv
import datetime as dt
import hashlib
import http.server
import io
import json
import logging
import os
import re
import shutil
import socketserver
import sqlite3
import subprocess
import sys
import time
import textwrap
import unicodedata
import urllib.parse
import atexit
import signal

VERSION = "1.4"
BASE = os.path.abspath(os.path.expanduser(os.environ.get("REE_DIR") or "~/ree"))
ANCHO = 121
PUERTO_DEFECTO = 8785
PUERTO_ANTIGUO = 8765   # hasta ree.py 1.3; chocaba con foroleer.py (ver puertos.txt)
MAX_BYTES = 80 * 1024 * 1024
EXT_OK = {"xlsx", "xlsm", "xls", "csv", "pdf", "zip", "kml", "kmz", "json", "geojson"}
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
         "septiembre", "octubre", "noviembre", "diciembre"]
PAGINAS_DEFECTO = [
    "https://www.ree.es/es/clientes/consumidor/acceso-conexion/conoce-el-estado-de-las-solicitudes",
    "https://www.ree.es/es/clientes/consumidor/acceso-conexion/conoce-la-capacidad-de-acceso",
    "https://www.ree.es/es/clientes/consumidor/acceso-conexion/subestaciones-no-ampliables-para-nuevas-conexiones",
    "https://www.ree.es/es/clientes/consumidor/acceso-conexion/corrientes-de-cortocircuito",
    "https://www.ree.es/es/clientes/generador/acceso-conexion/conoce-la-capacidad-de-acceso",
]
log = logging.getLogger("ree")


# ---- Nombres de fichero con fecha/hora: AAAA-MM-DD-HH-MM-SS (mes 09, minuto 03) ----
LOGS_ACTIVOS = []


def ts(t=None):
    return time.strftime("%Y-%m-%d-%H-%M-%S", time.localtime(t))


def renombrar_por_fecha(ruta):
    """Renombra un fichero a '<fecha-hora de su ultima escritura>_<nombre>' (en la misma carpeta)."""
    try:
        if not os.path.exists(ruta):
            return None
        if os.path.getsize(ruta) == 0:
            os.remove(ruta)
            return None
        d, n = os.path.split(ruta)
        nuevo = os.path.join(d, "%s_%s" % (ts(os.path.getmtime(ruta)), n))
        k = 1
        while os.path.exists(nuevo):
            k += 1
            nuevo = os.path.join(d, "%s_%d_%s" % (ts(os.path.getmtime(ruta)), k, n))
        os.rename(ruta, nuevo)
        return nuevo
    except OSError:
        return None


def registrar_log(ruta):
    """Un log vigente: el anterior con ese nombre se renombra ya, y este se renombrara al terminar."""
    renombrar_por_fecha(ruta)
    if ruta not in LOGS_ACTIVOS:
        LOGS_ACTIVOS.append(ruta)


def cerrar_logs(*_a):
    logging.shutdown()
    for r in list(LOGS_ACTIVOS):
        renombrar_por_fecha(r)
    LOGS_ACTIVOS.clear()


def ultimo_con_sufijo(carpeta, sufijo):
    """El fichero mas reciente de la carpeta cuyo nombre termina en sufijo (con o sin prefijo de fecha)."""
    try:
        c = [os.path.join(carpeta, f) for f in os.listdir(carpeta) if f.endswith(sufijo)]
    except OSError:
        return None
    return max(c, key=os.path.getmtime) if c else None


def dirs():
    return {k: os.path.join(BASE, k) for k in ("descargas", "diagnostico", "informes")}


def preparar():
    os.makedirs(BASE, exist_ok=True)
    for d in dirs().values():
        os.makedirs(d, exist_ok=True)


def fallar(msg, codigo=2):
    print("ERROR: " + msg)
    logging.getLogger("ree").error(msg)
    raise SystemExit(codigo)


def norm(s):
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s).lower().strip()


def conf_path():
    return os.path.join(BASE, "config.json")


def cargar_conf():
    try:
        with open(conf_path(), encoding="utf-8") as f:
            c = json.load(f)
    except Exception:
        c = {}
    c.setdefault("paginas", list(PAGINAS_DEFECTO))
    c.setdefault("puerto", PUERTO_DEFECTO)
    if c.get("puerto") == PUERTO_ANTIGUO and not c.get("puerto_migrado"):
        # Migracion unica (1.4): el 8765 lo usa foroleer.py. Si lo habias puesto a mano a otro numero, se respeta.
        c["puerto"] = PUERTO_DEFECTO
        c["puerto_migrado"] = True
        try:
            guardar_conf(c)
            print("AVISO: el puerto de REE pasa de %d a %d (el 8765 es de foroleer.py). Pon %d en las opciones de "
                  "la extension REE Puente (la 1.0.2 lo cambia sola)." % (PUERTO_ANTIGUO, PUERTO_DEFECTO, PUERTO_DEFECTO),
                  file=sys.stderr)
        except Exception:
            pass
    return c


def guardar_conf(c):
    with open(conf_path(), "w", encoding="utf-8") as f:
        json.dump(c, f, ensure_ascii=False, indent=2)


def db():
    preparar()
    c = sqlite3.connect(os.path.join(BASE, "ree.db"))
    c.executescript("""
    CREATE TABLE IF NOT EXISTS ficheros(id INTEGER PRIMARY KEY, sha TEXT UNIQUE, nombre TEXT,
        url TEXT, pagina TEXT, mes TEXT, bytes INTEGER, ruta TEXT, recibido TEXT);
    CREATE TABLE IF NOT EXISTS eventos(id INTEGER PRIMARY KEY, cuando TEXT, tipo TEXT, detalle TEXT);
    """)
    return c


def evento(c, tipo, detalle):
    c.execute("INSERT INTO eventos(cuando,tipo,detalle) VALUES(?,?,?)",
              (dt.datetime.now().isoformat(timespec="seconds"), tipo, detalle[:2000]))
    c.commit()


# ---------------------------------------------------------------- meses y nombres
def mes_de_nombre(nombre):
    n = norm(nombre)
    for i, m in enumerate(MESES, 1):
        mm = re.search(m + r"[-_ ]*(\d{4})", n)
        if mm:
            return "%s-%02d" % (mm.group(1), i)
    mm = re.search(r"(20\d{2})[-_]?(\d{2})", n)
    if mm and 1 <= int(mm.group(2)) <= 12:
        return "%s-%s" % (mm.group(1), mm.group(2))
    return None


def limpiar_nombre(n):
    n = os.path.basename(urllib.parse.unquote(n or "")).strip()
    n = re.sub(r"[^A-Za-z0-9._-]", "_", n)[:150]
    return n.lstrip(".") or "sin_nombre"


# ---------------------------------------------------------------- servidor local
class Manejador(http.server.BaseHTTPRequestHandler):
    server_version = "ree/" + VERSION

    def log_message(self, fmt, *a):
        txt = fmt % a
        if any(k in txt for k in ("/ping", "/tarea", "/ext/")) and " 200 " in txt or "/ext/" in txt:
            return   # latidos y sondeos de otras extensiones: no se registran
        logging.getLogger("ree").info("http %s", txt)

    def _json(self, obj, codigo=200):
        cuerpo = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(cuerpo)

    def _origen_ok(self):
        """Solo la extension (moz-extension://) puede entregar datos."""
        o = self.headers.get("Origin", "")
        return o.startswith("moz-extension://")

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/ping":
            return self._json({"ok": True, "version": VERSION})
        if u.path == "/tarea":
            c = cargar_conf()
            try:
                with open(os.path.join(BASE, "ultimo_latido.txt"), "w") as f:
                    f.write(str(time.time()))
                if not os.path.exists(os.path.join(BASE, "extension_vista.flag")):
                    open(os.path.join(BASE, "extension_vista.flag"), "w").close()
                    log_diag("Primer contacto de la extension con el servidor.")
            except OSError:
                pass
            pedido = os.path.join(BASE, "pedido.flag")
            hay = os.path.exists(pedido)
            if hay:
                os.remove(pedido)
            return self._json({"paginas": c["paginas"], "extensiones": sorted(EXT_OK),
                               "pedido": hay, "version": VERSION})
        if u.path == "/despertar":
            cuerpo = ("<!doctype html><meta charset=utf-8><title>REE despertar</title>"
                      "<h3>REE: avisando a la extension...</h3><p id=m>Si en unos segundos no cambia, "
                      "la extension REE Puente no esta activa.</p>").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(cuerpo)))
            self.end_headers()
            self.wfile.write(cuerpo)
            return
        self._json({"error": "no existe"}, 404)

    def do_POST(self):
        if not self._origen_ok():
            return self._json({"error": "origen no permitido"}, 403)
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BYTES:
            return self._json({"error": "demasiado grande"}, 413)
        datos = self.rfile.read(n)
        conn = db()
        if u.path == "/fichero":
            nombre = limpiar_nombre((q.get("nombre") or [""])[0])
            ext = nombre.rsplit(".", 1)[-1].lower() if "." in nombre else ""
            if ext not in EXT_OK:
                return self._json({"error": "extension no permitida"}, 400)
            url = (q.get("url") or [""])[0][:500]
            pagina = (q.get("pagina") or [""])[0][:500]
            sha = hashlib.sha256(datos).hexdigest()
            fila = conn.execute("SELECT ruta FROM ficheros WHERE sha=?", (sha,)).fetchone()
            if fila:
                return self._json({"nuevo": False, "ruta": fila[0]})
            mes = mes_de_nombre(nombre) or dt.date.today().strftime("%Y-%m")
            carpeta = os.path.join(dirs()["descargas"], mes)
            os.makedirs(carpeta, exist_ok=True)
            ruta = os.path.join(carpeta, nombre)
            if os.path.exists(ruta):   # mismo nombre, contenido distinto: se conserva la version anterior
                b, e = os.path.splitext(nombre)
                ruta = os.path.join(carpeta, "%s_%s%s" % (b, dt.datetime.now().strftime("%Y%m%d%H%M%S"), e))
            with open(ruta, "wb") as f:
                f.write(datos)
            conn.execute("INSERT INTO ficheros(sha,nombre,url,pagina,mes,bytes,ruta,recibido) VALUES(?,?,?,?,?,?,?,?)",
                         (sha, nombre, url, pagina, mes, len(datos), ruta,
                          dt.datetime.now().isoformat(timespec="seconds")))
            evento(conn, "fichero", "%s (%d bytes) desde %s" % (nombre, len(datos), pagina))
            conn.commit()
            logging.getLogger("ree").info("Guardado %s", ruta)
            if ext == "pdf":
                pdf_a_txt(ruta)
            return self._json({"nuevo": True, "ruta": ruta})
        if u.path == "/pagina":
            nombre = limpiar_nombre((q.get("nombre") or ["pagina"])[0]) + ".html"
            ruta = os.path.join(dirs()["diagnostico"], ts() + "_" + nombre)
            with open(ruta, "wb") as f:
                f.write(datos)
            return self._json({"guardado": ruta})
        if u.path == "/informe":
            ruta = os.path.join(dirs()["diagnostico"], "extension.log")
            if ruta not in LOGS_ACTIVOS:
                registrar_log(ruta)
            with open(ruta, "a", encoding="utf-8") as f:
                f.write("%s %s\n" % (dt.datetime.now().isoformat(timespec="seconds"),
                                     datos.decode("utf-8", "replace")))
            evento(conn, "informe", datos.decode("utf-8", "replace"))
            return self._json({"ok": True})
        self._json({"error": "no existe"}, 404)

    def do_OPTIONS(self):          # preflight de la extension
        self.send_response(204)
        o = self.headers.get("Origin", "")
        if o.startswith("moz-extension://"):
            self.send_header("Access-Control-Allow-Origin", o)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def end_headers(self):
        o = self.headers.get("Origin", "")
        if o.startswith("moz-extension://"):
            self.send_header("Access-Control-Allow-Origin", o)
        super().end_headers()


class Servidor(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def pdf_a_txt(ruta):
    """Convierte el PDF a .txt con lo que haya instalado (pdftotext o pdf2txt.py)."""
    destino = os.path.splitext(ruta)[0] + ".txt"
    if shutil.which("pdftotext"):
        r = subprocess.run(["pdftotext", "-layout", ruta, destino], capture_output=True, timeout=120)
        if r.returncode == 0:
            return destino
    p2 = shutil.which("pdf2txt.py") or shutil.which("pdf2txt")
    if p2:
        r = subprocess.run([sys.executable, p2, "-o", destino, ruta] if p2.endswith(".py")
                           else [p2, "-o", destino, ruta], capture_output=True, timeout=300)
        if r.returncode == 0:
            return destino
    return None


def log_diag(texto):
    """Escribe en diagnostico/servidor.log (lo puede leer Claude)."""
    try:
        ruta = os.path.join(dirs()["diagnostico"], "servidor.log")
        if ruta not in LOGS_ACTIVOS:
            registrar_log(ruta)
        with open(ruta, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), texto))
    except OSError:
        pass


def servidor_activo(puerto):
    import socket
    try:
        with socket.create_connection(("127.0.0.1", puerto), timeout=2):
            return True
    except OSError:
        return False


def cmd_serve(args):
    preparar()
    c = cargar_conf()
    puerto = args.puerto or c["puerto"]
    if getattr(args, "fondo", False):
        if servidor_activo(puerto):
            print("El servidor ya esta en marcha en 127.0.0.1:%d." % puerto)
            return 0
        orden = [sys.executable, os.path.abspath(__file__), "--dir", BASE, "serve", "--puerto", str(puerto)]
        ruta_sal = os.path.join(dirs()["diagnostico"], "servidor_salida.log")
        renombrar_por_fecha(ruta_sal)
        sal = open(ruta_sal, "a")
        subprocess.Popen(orden, stdout=sal, stderr=sal, stdin=subprocess.DEVNULL, start_new_session=True)
        for _ in range(20):
            time.sleep(0.25)
            if servidor_activo(puerto):
                print("Servidor arrancado en segundo plano en 127.0.0.1:%d." % puerto)
                print("Para pararlo:  pkill -f 'ree.py.*serve'")
                return 0
        fallar("El servidor no arranco; mira %s" % ruta_sal)
    try:
        srv = Servidor(("127.0.0.1", puerto), Manejador)
    except OSError as e:
        fallar("No puedo abrir el puerto %d (%s). Quiza ya hay otro 'serve' abierto." % (puerto, e))
    log_diag("Servidor arrancado en el puerto %d (version %s)." % (puerto, VERSION))
    print("REE servidor escuchando en http://127.0.0.1:%d  (solo este movil).  Ctrl+C para parar." % puerto)
    print("Ficheros en: %s" % dirs()["descargas"])
    try:
        if args.una_vez:
            srv.handle_request()
        else:
            srv.serve_forever()
    except KeyboardInterrupt:
        print("\nParado.")
    return 0


def cmd_despertar(args):
    c = cargar_conf()
    puerto = args.puerto or c["puerto"]
    preparar()
    if not servidor_activo(puerto):
        print("ERROR: el servidor NO esta en marcha en 127.0.0.1:%d, asi que la extension no tiene a quien" % puerto)
        print("preguntar y Firefox diria 'No se puede conectar'.  Arrancalo primero:")
        print("    python3 ree.py serve --fondo        (en segundo plano)   o   python3 ree.py serve   (en otra sesion)")
        log_diag("despertar sin servidor en marcha.")
        return 1
    open(os.path.join(BASE, "pedido.flag"), "w").close()
    url = "http://127.0.0.1:%d/despertar" % puerto
    for orden in (["termux-open-url", url],
                  ["am", "start", "-a", "android.intent.action.VIEW", "-d", url]):
        if shutil.which(orden[0]):
            r = subprocess.run(orden, capture_output=True, timeout=30)
            if r.returncode == 0:
                print("Pedido registrado y Firefox avisado (%s).  La extension descarga en menos de 1 minuto; mira 'estado'." % orden[0])
                return 0
    print("Pedido registrado. No pude abrir Firefox desde Termux (falta termux-open-url / am), pero la")
    print("extension comprueba cada minuto si hay pedido, asi que descargara sola con Firefox abierto.")
    print("Para acelerarlo abre a mano en Firefox:  %s" % url)
    return 0


def cmd_pagina(args):
    c = cargar_conf()
    if args.anadir:
        if not args.anadir.startswith("https://www.ree.es/"):
            fallar("Solo se admiten paginas de https://www.ree.es/")
        if args.anadir not in c["paginas"]:
            c["paginas"].append(args.anadir)
            guardar_conf(c)
    if args.quitar:
        c["paginas"] = [p for p in c["paginas"] if p != args.quitar]
        guardar_conf(c)
    print("Paginas vigiladas por la extension (%d):" % len(c["paginas"]))
    for p in c["paginas"]:
        print("  " + p)
    return 0


# ---------------------------------------------------------------- lectura de tablas
def leer_filas(ruta):
    ext = os.path.splitext(ruta)[1].lower()
    if ext == ".csv":
        raw = open(ruta, "rb").read()
        txt = None
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                txt = raw.decode(enc)
                break
            except UnicodeDecodeError:
                pass
        try:
            dial = csv.Sniffer().sniff(txt[:4000], delimiters=";,\t")
        except csv.Error:
            dial = csv.excel
        return list(csv.reader(io.StringIO(txt), dial))
    if ext in (".xlsx", ".xlsm"):
        try:
            import openpyxl
        except ImportError:
            fallar("Falta openpyxl:  pip install openpyxl")
        wb = openpyxl.load_workbook(ruta, read_only=True, data_only=True)
        filas = []
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                filas.append(["" if v is None else str(v) for v in row])
        return filas
    fallar("Formato %s no soportado para analizar (usa xlsx o csv)." % ext)


COLS = {"id": ["id solicitud", "identificador"], "tipo": ["tipo de solicitud", "tipo"],
        "nudo": ["nudo"], "pos": ["posicion"], "ccaa": ["comunidad"],
        "mw": ["capacidad solicitada", "mw"], "fecha": ["fecha"], "concurso": ["concurso"]}


def a_num(s):
    s = (s or "").strip().replace(" ", "")
    if not s:
        return 0.0
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def cargar_solicitudes(ruta):
    filas = leer_filas(ruta)
    idx = None
    for i, f in enumerate(filas[:40]):
        cel = [norm(x) for x in f]
        if any("comunidad" in x for x in cel) and any("nudo" in x for x in cel):
            idx, cab = i, cel
            break
    if idx is None:
        d = os.path.join(dirs()["diagnostico"], "sin_cabecera.txt")
        with open(d, "w", encoding="utf-8") as fh:
            fh.write("\n".join(";".join(f) for f in filas[:60]))
        fallar("No encuentro la cabecera (Comunidad / Nudo) en %s. Primeras filas en %s" % (ruta, d))
    pos = {}
    for k, frs in COLS.items():
        for j, h in enumerate(cab):
            if any(fr in h for fr in frs):
                pos.setdefault(k, j)
                break
    for k in ("nudo", "ccaa", "mw"):
        if k not in pos:
            fallar("Falta la columna %r. Cabecera: %s" % (k, " | ".join(cab)))
    out = []
    for f in filas[idx + 1:]:
        if len(f) <= max(pos.values()):
            continue
        nudo, ccaa = f[pos["nudo"]].strip(), f[pos["ccaa"]].strip()
        if not nudo and not ccaa:
            continue
        g = lambda k: f[pos[k]].strip() if k in pos else ""
        out.append({"id": g("id"), "tipo": g("tipo"), "nudo": nudo, "ccaa": ccaa, "mw": a_num(g("mw")),
                    "concurso": norm(g("concurso")) in ("si", "s", "x", "1", "true"), "pos": g("pos")})
    return out, pos


def archivos_listado():
    """Listados de solicitudes archivados, ordenados por mes."""
    res = []
    base = dirs()["descargas"]
    if not os.path.isdir(base):
        return res
    for mes in sorted(os.listdir(base)):
        d = os.path.join(base, mes)
        for f in sorted(os.listdir(d)):
            if re.search(r"solicitudes.*demanda.*\.(xlsx|xlsm|csv)$", f, re.I):
                res.append((mes, os.path.join(d, f)))
    return res


def fmt(x):
    return "{:,.0f}".format(x).replace(",", ".")


def cmd_analizar(args):
    preparar()
    if args.archivo:
        ruta = os.path.abspath(os.path.expanduser(args.archivo))
        mes = mes_de_nombre(os.path.basename(ruta)) or dt.date.today().strftime("%Y-%m")
        previo = None
        if not os.path.exists(ruta):
            fallar("No existe %r. Usa 'listar' para ver lo archivado." % ruta)
    else:
        lst = archivos_listado()
        if not lst:
            fallar("Aun no hay listados archivados. Ejecuta 'serve' y 'despertar' (ver manual) o usa --archivo.")
        mes, ruta = lst[-1]
        previo = lst[-2] if len(lst) > 1 and lst[-2][0] != mes else None
    sol, pos = cargar_solicitudes(ruta)
    lineas = []
    P = lineas.append
    P("REE - SOLICITUDES DE ACCESO DE DEMANDA (nudos >= 220 kV)  |  mes %s  |  %s" % (mes, os.path.basename(ruta)))
    P("Solicitudes: %d | MW totales: %s | en concurso: %d" % (len(sol), fmt(sum(s["mw"] for s in sol)),
                                                          sum(1 for s in sol if s["concurso"])))
    P("")
    P("MW POR COMUNIDAD AUTONOMA")
    por = {}
    for s in sol:
        por.setdefault(s["ccaa"] or "(sin dato)", [0.0, 0])
        por[s["ccaa"] or "(sin dato)"][0] += s["mw"]
        por[s["ccaa"] or "(sin dato)"][1] += 1
    P("  %-34s %12s %8s" % ("COMUNIDAD", "MW", "SOLIC."))
    for k, (mw, n) in sorted(por.items(), key=lambda kv: -kv[1][0]):
        P("  %-34s %12s %8d" % (k[:34], fmt(mw), n))
    cf = [norm(x) for x in (args.ccaa or "").split(",") if x.strip()]
    sel = [s for s in sol if (not cf or any(x in norm(s["ccaa"]) for x in cf)) and s["mw"] >= args.min_mw]
    P("")
    P("NUDOS%s%s" % ((" EN " + args.ccaa.upper()) if cf else "", (" (>= %s MW)" % args.min_mw) if args.min_mw else ""))
    nudos = {}
    for s in sel:
        k = (s["nudo"], s["ccaa"])
        nudos.setdefault(k, [0.0, 0, 0])
        nudos[k][0] += s["mw"]
        nudos[k][1] += 1
        nudos[k][2] += 1 if s["concurso"] else 0
    P("  %-52s %-16s %10s %6s %9s" % ("NUDO", "CCAA", "MW", "SOLIC.", "CONCURSO"))
    for (nudo, ccaa), (mw, n, nc) in sorted(nudos.items(), key=lambda kv: -kv[1][0])[:args.top]:
        P("  %-52s %-16s %10s %6d %9d" % (nudo[:52], ccaa[:16], fmt(mw), n, nc))
    if not nudos:
        P("  (ninguno con ese filtro)")
    if previo:
        pm, pr = previo
        try:
            ant, _ = cargar_solicitudes(pr)
        except SystemExit:
            ant = []
        P("")
        P("CAMBIOS RESPECTO A %s" % pm)
        usa_id = all(s["id"] for s in sol) and all(s["id"] for s in ant) and ant
        if usa_id:
            a = {s["id"]: s for s in ant}
            b = {s["id"]: s for s in sol}
            nuevas = [b[i] for i in b if i not in a and (not cf or any(x in norm(b[i]["ccaa"]) for x in cf))]
            desap = [a[i] for i in a if i not in b and (not cf or any(x in norm(a[i]["ccaa"]) for x in cf))]
            camb = [(a[i], b[i]) for i in b if i in a and abs(a[i]["mw"] - b[i]["mw"]) > 0.5]
            P("  Nuevas: %d (%s MW) | desaparecidas: %d (%s MW) | con MW distintos: %d" % (
                len(nuevas), fmt(sum(s["mw"] for s in nuevas)), len(desap), fmt(sum(s["mw"] for s in desap)), len(camb)))
            for tit, lst in (("NUEVAS", nuevas), ("DESAPARECIDAS (resueltas, retiradas o caducadas)", desap)):
                if lst:
                    P("  " + tit)
                    for s in sorted(lst, key=lambda x: -x["mw"])[:args.top]:
                        P("    %-10s %-44s %-14s %8s MW%s" % (s["id"][:10], s["nudo"][:44], s["ccaa"][:14], fmt(s["mw"]),
                                                          "  [concurso]" if s["concurso"] else ""))
            for x, y in camb[:args.top]:
                P("    CAMBIO %-8s %s: %s -> %s MW" % (y["id"][:8], y["nudo"][:40], fmt(x["mw"]), fmt(y["mw"])))
        else:
            P("  (las solicitudes no tienen identificador comun; se comparan MW por nudo)")
            def agr(l):
                d = {}
                for s in l:
                    d[(s["nudo"], s["ccaa"])] = d.get((s["nudo"], s["ccaa"]), 0) + s["mw"]
                return d
            da, db_ = agr(ant), agr(sol)
            difs = sorted(((db_.get(k, 0) - da.get(k, 0), k) for k in set(da) | set(db_)), key=lambda t: -abs(t[0]))
            for dm, (nudo, ccaa) in difs[:args.top]:
                if abs(dm) > 0.5 and (not cf or any(x in norm(ccaa) for x in cf)):
                    P("    %-52s %-14s %+10s MW" % (nudo[:52], ccaa[:14], fmt(dm)))
    else:
        P("")
        P("(sin mes anterior archivado para comparar; la comparacion aparecera cuando haya dos meses)")
    P("")
    P("Fuente: Red Electrica, listado publicado segun art. 20 quater RD 1183/2020. Solo nudos >= 220 kV.")
    salida = []
    for ln in lineas:
        salida += textwrap.wrap(ln, ANCHO, subsequent_indent="      ", replace_whitespace=False) or [""]
    txt = "\n".join(salida)
    print(txt)
    informe = os.path.join(dirs()["informes"], "%s_ree_analisis_%s.txt" % (ts(), mes))
    with open(informe, "w", encoding="utf-8") as f:
        f.write(txt + "\n")
    print("\nInforme guardado en: %s" % informe)
    return 0


def archivo_capacidad(tipo):
    """Ultimo fichero GRT_demanda / GRT_generacion archivado (csv preferido, luego xlsx)."""
    base = dirs()["descargas"]
    hallados = []
    if os.path.isdir(base):
        for mes in sorted(os.listdir(base)):
            d = os.path.join(base, mes)
            for f in os.listdir(d):
                if re.search(r"GRT_%s\.(csv|xlsx)$" % tipo, f, re.I):
                    hallados.append((f[:10], 0 if f.lower().endswith(".csv") else 1, os.path.join(d, f)))
    if not hallados:
        return None
    hallados.sort(key=lambda x: (x[0], -x[1]))
    return hallados[-1][2]


def cmd_capacidad(args):
    """Capacidad de acceso DISPONIBLE por nudo (fichero GRT_demanda / GRT_generacion de REE)."""
    ruta = args.archivo or archivo_capacidad(args.tipo)
    if not ruta:
        fallar("No hay GRT_%s archivado. Ejecuta 'despertar' o usa --archivo." % args.tipo)
    filas = leer_filas(ruta)
    if len(filas) < 6:
        fallar("El fichero tiene muy pocas filas: %s" % ruta)
    n = max(len(f) for f in filas[:4])
    h = [list(f) + [""] * (n - len(f)) for f in filas[:4]]
    grupo, cur = [], ""
    for c in h[0]:
        if c.strip():
            cur = c.strip()
        grupo.append(cur)
    cols = [i for i, g in enumerate(grupo) if "DISPONIBLE PARA SOLICITUDES CRITERIO GENERAL RDT" in g.upper()]
    if not cols:
        fallar("No encuentro el bloque 'CAPACIDAD DE ACCESO DISPONIBLE ... RDT' en la cabecera de %s." % ruta)
    uso, sub, etiq = "", "", {}
    for i in cols:
        if h[1][i].strip():
            uso, sub = h[1][i].strip(), ""
        if h[2][i].strip():
            sub = h[2][i].strip()
        etiq[i] = (uso, ((sub + " " + h[3][i].strip()).strip() or "?"))
    # el primer bloque (DEMANDA para demanda, GENERACION/ALMACENAMIENTO para generacion)
    primero = etiq[cols[0]][0]
    elegidas = [i for i in cols if etiq[i][0] == primero] if not args.todos_usos else cols
    # columnas de nudo y concurso
    ic = next((i for i, v in enumerate(h[0]) if v.strip().lower().startswith("nudo de concurso")), None)
    datos = []
    for f in filas[4:]:
        if f and f[0].strip():
            f = list(f) + [""] * (n - len(f))
            datos.append(f)
    def num(x):
        v = a_num(x)
        return v if v is not None else 0.0
    ccaa_f = norm(args.ccaa) if args.ccaa else ""
    tot = {}
    for f in datos:
        t = tot.setdefault(f[2], [0.0, 0])
        t[0] += num(f[elegidas[0]])
        t[1] += 1
    sal = ["REE - CAPACIDAD DE ACCESO DISPONIBLE POR NUDO (RdT)  |  %s" % os.path.basename(ruta),
           "Bloque: %s  |  columnas: %s" % (primero, "; ".join(etiq[i][1] for i in elegidas)),
           "Nudos en el fichero: %d" % len(datos), "",
           "SUMA BRUTA POR COMUNIDAD (MW, primera columna del bloque; sobreestima donde varios nudos comparten zona)",
           "  %-34s %10s %7s" % ("COMUNIDAD", "MW", "NUDOS")]
    for k, (mw, nn) in sorted(tot.items(), key=lambda x: -x[1][0])[:args.top]:
        sal.append("  %-34s %10s %7d" % (k[:34], fmt(mw), nn))
    if ccaa_f:
        sel = [f for f in datos if ccaa_f in norm(f[2])]
        sal += ["", "NUDOS DE %s (%d)" % (args.ccaa.upper(), len(sel)),
                "  %-30s %s %9s" % ("NUDO", " ".join("%9s" % etiq[i][1][:9] for i in elegidas), "CONCURSO")]
        sel.sort(key=lambda f: -num(f[elegidas[0]]))
        for f in sel:
            vac = all(f[i].strip() == "" for i in elegidas)
            conc = f[ic].strip() if ic is not None else ""
            vals = " ".join("%9s" % (fmt(num(f[i])) if f[i].strip() else "-") for i in elegidas)
            sal.append("  %-30s %s %9s" % (f[0][:30], vals, conc or ("(sin dato)" if vac else "")))
        con = [f[0] for f in sel if ic is not None and f[ic].strip().upper() == "SI"]
        sal += ["", "Nudos de concurso en %s: %s" % (args.ccaa, ", ".join(con) if con else "ninguno")]
    sal += ["", "Un nudo de concurso se adjudica por concurso de capacidad, no por orden de solicitud: su margen puede",
            "aparecer vacio. Fuente: Red Electrica, ficha de capacidades. Solo la red de transporte (RdT)."]
    texto = "\n".join(sal)
    print(texto)
    out = os.path.join(dirs()["informes"], "%s_ree_capacidad_%s.txt" % (ts(), args.tipo))
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(texto + "\n")
    print("\nInforme guardado en %s" % out)
    return 0



def cmd_listar(args):
    c = db()
    filas = c.execute("SELECT mes,nombre,bytes,recibido,pagina FROM ficheros ORDER BY mes,nombre").fetchall()
    if not filas:
        print("Nada archivado todavia.")
        return 0
    for mes, nombre, n, rec, pag in filas:
        print("%s  %-86s %9s  %s" % (mes, nombre[:86], fmt(n), rec[:16]))
    return 0


def cmd_estado(args):
    c = db()
    cf = cargar_conf()
    print("Carpeta de trabajo: %s" % BASE)
    print("Puerto: %d | paginas vigiladas: %d" % (cf["puerto"], len(cf["paginas"])))
    print("Servidor: %s" % ("EN MARCHA" if servidor_activo(cf["puerto"]) else
                            "NO esta en marcha  ->  python3 ree.py serve --fondo"))
    try:
        t = float(open(os.path.join(BASE, "ultimo_latido.txt")).read())
        print("Ultimo latido de la extension: hace %d s%s" % (time.time() - t,
              "" if time.time() - t < 150 else "  (la extension no esta llamando: ver manual, seccion 7)"))
    except (OSError, ValueError):
        print("Ultimo latido de la extension: NUNCA (la extension no ha contactado con el servidor).")
    n = c.execute("SELECT COUNT(*), COALESCE(SUM(bytes),0) FROM ficheros").fetchone()
    print("Ficheros archivados: %d (%s bytes)" % (n[0], fmt(n[1])))
    print("Ultimos eventos:")
    for cuando, tipo, det in c.execute("SELECT cuando,tipo,detalle FROM eventos ORDER BY id DESC LIMIT 8"):
        print("  %s %-8s %s" % (cuando, tipo, det.replace("\n", " | ")[:90]))
    ext_log = ultimo_con_sufijo(dirs()["diagnostico"], "extension.log")
    if ext_log:
        print("Informes de la extension: %s" % ext_log)
    return 0


def parser():
    p = argparse.ArgumentParser(prog="ree.py", description=__doc__.split("\n\n")[0])
    p.add_argument("--version", action="version", version="ree " + VERSION)
    p.add_argument("--dir", help="carpeta de trabajo (defecto ~/ree o variable REE_DIR)")
    sp = p.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("serve", aliases=["server"], help="servidor local para la extension")
    s.add_argument("--fondo", action="store_true", help="arrancarlo en segundo plano y volver al prompt")
    s.add_argument("--puerto", type=int)
    s.add_argument("--una-vez", action="store_true", help=argparse.SUPPRESS)
    d = sp.add_parser("despertar", help="pide a la extension que descargue ya")
    d.add_argument("--puerto", type=int)
    a = sp.add_parser("analizar", help="analiza y compara el ultimo listado")
    a.add_argument("--archivo"); a.add_argument("--ccaa", default="Aragon")
    a.add_argument("--min-mw", type=float, default=0.0); a.add_argument("--top", type=int, default=25)
    c = sp.add_parser("capacidad", help="capacidad de acceso disponible por nudo (GRT_demanda / GRT_generacion)")
    c.add_argument("--tipo", choices=["demanda", "generacion"], default="demanda")
    c.add_argument("--archivo"); c.add_argument("--ccaa", default="Aragon")
    c.add_argument("--top", type=int, default=20)
    c.add_argument("--todos-usos", action="store_true", help="incluir tambien las columnas de almacenamiento")
    sp.add_parser("listar", help="lista lo archivado")
    g = sp.add_parser("pagina", help="paginas de REE que vigila la extension")
    g.add_argument("--anadir"); g.add_argument("--quitar")
    sp.add_parser("estado", help="resumen")
    return p


def main(argv=None):
    global BASE
    args = parser().parse_args(argv)
    if args.dir:
        BASE = os.path.abspath(os.path.expanduser(args.dir))
    preparar()
    ruta_log = os.path.join(BASE, "ree.log")
    registrar_log(ruta_log)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(ruta_log, encoding="utf-8")])
    atexit.register(cerrar_logs)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    print("[ree %s | carpeta de trabajo: %s]" % (VERSION, BASE))
    if args.cmd == "server":
        args.cmd = "serve"
    return {"serve": cmd_serve, "despertar": cmd_despertar, "analizar": cmd_analizar, "capacidad": cmd_capacidad, "listar": cmd_listar,
            "pagina": cmd_pagina, "estado": cmd_estado}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
