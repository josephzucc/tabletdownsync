#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fotocasa.py - Busqueda de HABITACIONES COMPARTIDAS en Fotocasa desde Termux.

Funciona junto con la extension de Firefox "Fotocasa Bridge" (fotocasa.xpi):
  - Este programa levanta un pequeno servidor local en 127.0.0.1:8766.
  - La extension (dentro de Firefox, con una pestana de fotocasa.es) pregunta
    a ese servidor que hacer, lo hace en la pagina real de Fotocasa (escribir en el
    buscador, elegir zonas, ordenar, leer anuncios) y devuelve el resultado.
  - Asi se usa tu propia sesion de navegador y no hay que saltarse nada.

Solo libreria estandar de Python 3. Todo el texto normal sale por stdout;
solo los errores del programa salen por stderr.

Uso rapido:   python fotocasa.py ayuda
"""
import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from collections import deque

VERSION = "1.0.0"
PUERTO = 8766  # tiene que coincidir con el de la extension
HOST = "127.0.0.1"
DIR = os.path.join(os.path.expanduser("~"), ".fotocasa")
F_VISTOS = os.path.join(DIR, "vistos.json")
F_LOG = os.path.join(DIR, "servidor.log")
F_PID = os.path.join(DIR, "servidor.pid")

ORDENES = {
    "relevancia": "scoring,true",
    "recientes": "publicationDate,true",
    "baratos": "price,false",
    "caros": "price,true",
    "grandes": "surface,true",
    "pequenos": "surface,false",
    "eurom2": "pricem2,false",
}
ALIAS_ORDEN = {
    "relevante": "relevancia", "scoring": "relevancia",
    "reciente": "recientes", "nuevos": "recientes", "fecha": "recientes", "publicacion": "recientes",
    "barato": "baratos", "precio": "baratos", "economicos": "baratos", "baratas": "baratos",
    "caro": "caros", "caras": "caros",
    "grande": "grandes", "superficie": "grandes",
    "pequeno": "pequenos", "pequeño": "pequenos", "pequeños": "pequenos", "chicos": "pequenos",
    "m2": "eurom2", "euro-m2": "eurom2", "eurosm2": "eurom2",
}


def norm(s):
    s = unicodedata.normalize("NFD", str(s or "").lower())
    return re.sub(r"\s+", " ", "".join(c for c in s if unicodedata.category(c) != "Mn")).strip()


def err(msg):
    print("Error: " + str(msg), file=sys.stderr)


# ======================================================================= SERVIDOR
class Estado:
    def __init__(self):
        self.cond = threading.Condition()
        self.cola = deque()
        self.trabajos = {}   # id -> {"job":..., "res":None, "ev": Event, "t": time}
        self.ult_poll = 0.0
        self.siguiente_id = 1


EST = Estado()


class Manejador(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # silencio
        pass

    def _origen_ok(self):
        # Una pagina web cualquiera no debe poder dar ordenes: solo aceptamos
        # peticiones sin Origin (Termux) o con el de nuestra extension.
        o = self.headers.get("Origin")
        return (not o) or o.startswith("moz-extension://")

    def _json(self, codigo, obj):
        cuerpo = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def _vacio(self, codigo):
        self.send_response(codigo)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _cuerpo(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}

    def do_OPTIONS(self):
        self._vacio(403)

    def do_GET(self):
        if not self._origen_ok():
            return self._vacio(403)
        ruta, _, q = self.path.partition("?")
        if ruta == "/poll":
            EST.ult_poll = time.time()
            fin = time.time() + 20
            with EST.cond:
                while not EST.cola and time.time() < fin:
                    EST.cond.wait(timeout=1.0)
                    EST.ult_poll = time.time()
                if EST.cola:
                    jid = EST.cola.popleft()
                    return self._json(200, EST.trabajos[jid]["job"])
            return self._vacio(204)
        if ruta == "/estado":
            return self._json(200, {
                "version": VERSION,
                "extension_conectada": (time.time() - EST.ult_poll) < 30,
                "segundos_desde_ultimo_contacto": round(time.time() - EST.ult_poll, 1) if EST.ult_poll else None,
                "pendientes": len(EST.cola),
            })
        m = re.match(r"^/job/(\d+)$", ruta)
        if m:
            jid = int(m.group(1))
            t = EST.trabajos.get(jid)
            if not t:
                return self._json(404, {"error": "trabajo desconocido"})
            espera = 20
            mm = re.search(r"espera=(\d+)", q)
            if mm:
                espera = min(30, int(mm.group(1)))
            t["ev"].wait(espera)
            if t["res"] is None:
                return self._json(200, {"pendiente": True})
            res = t["res"]
            EST.trabajos.pop(jid, None)
            return self._json(200, res)
        self._vacio(404)

    def do_POST(self):
        if not self._origen_ok():
            return self._vacio(403)
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            return self._vacio(415)
        try:
            datos = self._cuerpo()
        except Exception:
            return self._vacio(400)
        if self.path == "/job":
            with EST.cond:
                jid = EST.siguiente_id
                EST.siguiente_id += 1
                job = {"id": jid, "accion": datos.get("accion"), "args": datos.get("args") or {}}
                EST.trabajos[jid] = {"job": job, "res": None, "ev": threading.Event(), "t": time.time()}
                EST.cola.append(jid)
                EST.cond.notify_all()
            return self._json(200, {"id": jid})
        if self.path == "/result":
            t = EST.trabajos.get(datos.get("id"))
            if t:
                t["res"] = datos
                t["ev"].set()
            return self._json(200, {"ok": True})
        if self.path == "/parar":
            self._json(200, {"ok": True})
            threading.Thread(target=lambda: (time.sleep(0.3), os._exit(0)), daemon=True).start()
            return
        self._vacio(404)


def servidor_foreground():
    os.makedirs(DIR, exist_ok=True)
    try:
        srv = ThreadingHTTPServer((HOST, PUERTO), Manejador)
    except OSError as e:
        err("No puedo abrir %s:%d (%s). Quizas ya esta en marcha: prueba 'python fotocasa.py estado'." % (HOST, PUERTO, e))
        return 1
    srv.daemon_threads = True
    with open(F_PID, "w") as f:
        f.write(str(os.getpid()))
    print("Servidor de Fotocasa escuchando en http://%s:%d  (Ctrl+C para parar)" % (HOST, PUERTO))
    sys.stdout.flush()
    # Limpieza de trabajos antiguos
    def limpiar():
        while True:
            time.sleep(60)
            ahora = time.time()
            for k in [k for k, v in list(EST.trabajos.items()) if ahora - v["t"] > 600]:
                EST.trabajos.pop(k, None)
    threading.Thread(target=limpiar, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


# ========================================================================= CLIENTE
def http(metodo, ruta, cuerpo=None, timeout=35):
    url = "http://%s:%d%s" % (HOST, PUERTO, ruta)
    datos = json.dumps(cuerpo).encode("utf-8") if cuerpo is not None else None
    req = urllib.request.Request(url, data=datos, method=metodo)
    if datos is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw.decode("utf-8")) if raw else {}


def servidor_vivo():
    try:
        http("GET", "/estado", timeout=2)
        return True
    except Exception:
        return False


def arrancar_servidor():
    if servidor_vivo():
        return True
    os.makedirs(DIR, exist_ok=True)
    log = open(F_LOG, "ab")
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "servidor", "--silencioso"],
                     stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
    for _ in range(30):
        time.sleep(0.2)
        if servidor_vivo():
            return True
    return False


class ErrorExt(Exception):
    pass


def pedir(accion, args=None, espera=60):
    """Manda una orden a la extension y devuelve sus datos."""
    if not arrancar_servidor():
        raise ErrorExt("No he podido arrancar el servidor local (mira %s)." % F_LOG)
    st = http("GET", "/estado", timeout=3)
    if not st.get("extension_conectada"):
        # Dar unos segundos por si Firefox acaba de abrirse
        for _ in range(12):
            time.sleep(1)
            st = http("GET", "/estado", timeout=3)
            if st.get("extension_conectada"):
                break
        else:
            raise ErrorExt(
                "La extension no esta conectada. Abre Firefox (Nightly) con fotocasa.xpi instalada "
                "y deja abierta la pestana de fotocasa.es; despues repite el comando.")
    jid = http("POST", "/job", {"accion": accion, "args": args or {}})["id"]
    fin = time.time() + espera
    while time.time() < fin:
        r = http("GET", "/job/%d?espera=20" % jid, timeout=30)
        if r.get("pendiente"):
            continue
        if r.get("ok"):
            return r.get("datos")
        raise ErrorExt(r.get("error") or "error desconocido de la extension")
    raise ErrorExt("La extension no ha respondido a tiempo a la orden '%s'." % accion)


# ======================================================================== LOGICA
URL_INICIO = "https://www.fotocasa.es/es/compartir/viviendas/oviedo/todas-las-zonas/l"


def asegurar_pagina_habitaciones():
    r = pedir("estado")
    ps = r.get("pestanas") or []
    if not any("/compartir/" in (p.get("url") or "") for p in ps):
        pedir("ir", {"url": URL_INICIO}, espera=70)


def elegir_sugerencia(texto, n=None, solo_zonas=False):
    """Escribe el texto en el buscador y elige una sugerencia. Devuelve (elegida, zonas)."""
    asegurar_pagina_habitaciones()
    r = pedir("sugerir", {"texto": texto})
    sug = r.get("sugerencias") or []
    if not sug:
        raise ErrorExt("Fotocasa no ha ofrecido sugerencias para '%s'. Prueba con otro nombre (sin tildes, sin abreviar)." % texto)
    if n:
        if n < 1 or n > len(sug):
            raise ErrorExt("Solo hay %d sugerencias; -n %d no existe." % (len(sug), n))
        cand = sug[n - 1]
    else:
        cand = None
        # 1) municipio con zonas cuyo nombre coincide; 2) cualquier municipio con zonas; 3) la primera
        for s in sug:
            if s.get("zonas") and norm(texto) in norm(s.get("nombre")):
                cand = s
                break
        if not cand:
            cand = next((s for s in sug if s.get("zonas")), None)
        if not cand and not solo_zonas:
            cand = sug[0]
        if not cand:
            raise ErrorExt(
                "Fotocasa no ofrece 'zonas dentro de' para '%s' (a veces no lo ofrece). "
                "Sugerencias recibidas:\n%s" % (texto, formato_sugerencias(sug)))
    e = pedir("elegir", {"indice": cand["indice"]}, espera=40)
    return e.get("elegida") or cand, e.get("zonas") or []


def formato_sugerencias(sug):
    filas = [("N", "Nombre", "Tipo", "Anuncios", "Zonas")]
    for s in sug:
        filas.append((str(s["indice"]), s["nombre"], s["tipo"], str(s["anuncios"]), "si" if s["zonas"] else "no"))
    return tabla(filas, [3, 44, 18, 9, 5], dcha={3})


def tabla(filas, anchos, dcha=()):
    out = []
    for i, f in enumerate(filas):
        celdas = []
        for j, c in enumerate(f):
            c = c if len(c) <= anchos[j] else c[: anchos[j] - 1] + "…"
            celdas.append(c.rjust(anchos[j]) if j in dcha else c.ljust(anchos[j]))
        out.append("  ".join(celdas).rstrip())
        if i == 0:
            out.append("  ".join("-" * a for a in anchos))
    return "\n".join(out)


def cargar_vistos():
    try:
        with open(F_VISTOS, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def guardar_vistos(v):
    os.makedirs(DIR, exist_ok=True)
    with open(F_VISTOS, "w", encoding="utf-8") as f:
        json.dump(v, f, ensure_ascii=False, indent=1)


def leer_paginas(max_paginas, max_anuncios=None, cambio_pagina=True):
    """Lee la pagina actual de la pestana y las siguientes. Devuelve (info, anuncios)."""
    todos, ids = [], set()
    info = None
    for p in range(max_paginas):
        r = pedir("leer", espera=70)
        if info is None:
            info = {k: r.get(k) for k in ("titulo", "total", "url")}
        nuevos = 0
        for a in r.get("anuncios") or []:
            if a["id"] in ids:
                continue
            ids.add(a["id"])
            todos.append(a)
            nuevos += 1
        if max_anuncios and len(todos) >= max_anuncios:
            break
        if p + 1 >= max_paginas or nuevos == 0:
            break
        sig = r.get("siguiente")
        if not sig:
            m = re.match(r"^(https://[^?#]*?)(?:/(\d+))?/?(\?[^#]*)?(#.*)?$", r.get("url") or "")
            base = m.group(1) if m else None
            if not base:
                break
            if not base.endswith("/l"):
                base = re.sub(r"/l/\d+$", "/l", base)
            actual = int(m.group(2)) if m and m.group(2) and re.search(r"/l/\d+", r.get("url") or "") else 1
            sig = "%s/%d%s" % (base, actual + 1, (m.group(3) or ""))
        pedir("ir", {"url": sig}, espera=70)
    return info or {}, todos


def marcar_cambios(anuncios, vistos):
    ahora = time.strftime("%Y-%m-%d %H:%M")
    for a in anuncios:
        v = vistos.get(a["id"])
        a["_nuevo"] = v is None
        a["_cambio"] = None
        if v and a.get("precio") is not None and v.get("precio") is not None and a["precio"] != v["precio"]:
            a["_cambio"] = v["precio"]
    return ahora


def actualizar_vistos(anuncios, vistos, ahora):
    for a in anuncios:
        v = vistos.get(a["id"]) or {"primera": ahora}
        v.update({"precio": a.get("precio"), "ultima": ahora, "zona": a.get("zona"), "url": a.get("url")})
        vistos[a["id"]] = v


def euros(n):
    return "?" if n is None else "%s €" % format(n, ",").replace(",", ".")


def imprimir_anuncios(info, anuncios, largo=False, compacto=False):
    print("%s · Fotocasa dice %s resultados · leidos %d (%d de zonas cercanas)" % (
        info.get("titulo") or "Habitaciones", info.get("total") if info.get("total") is not None else "?",
        len(anuncios), sum(1 for a in anuncios if a.get("cercano"))))
    print("Fecha de la consulta: " + time.strftime("%d/%m/%Y %H:%M"))
    print()
    if not anuncios:
        print("(sin anuncios)")
        return
    if compacto:
        filas = [("N", "Precio", "Hab", "Bañ", "m2", "Zona", "Anunciante", "Marca", "ID")]
        for i, a in enumerate(anuncios, 1):
            filas.append((str(i), euros(a.get("precio")), str(a.get("habs") or "-"), str(a.get("banos") or "-"),
                          str(a.get("m2") or "-"), a.get("zona") or "", a.get("anunciante") or "",
                          marca(a), a["id"]))
        print(tabla(filas, [3, 8, 3, 3, 6, 34, 20, 14, 10], dcha={0, 1, 2, 3, 4}))
        return
    for i, a in enumerate(anuncios, 1):
        m2 = "%s m²" % a["m2"] if a.get("m2") else "m² ?"
        if a.get("m2") and a["m2"] > 2000:
            m2 += " (dato dudoso)"
        det = " · ".join(x for x in [
            "%s hab" % a["habs"] if a.get("habs") else "", "%s baño" % a["banos"] if a.get("banos") else "", m2,
            a.get("otros") or ""] if x)
        cambio = ""
        if a.get("variacion"):
            cambio = " (%s %s)" % ("bajó" if a["variacion"] < 0 else "subió", euros(abs(a["variacion"])))
        elif a.get("_cambio") is not None:
            cambio = " (antes %s)" % euros(a["_cambio"])
        print("%3d) %s%s   %s   %s" % (i, euros(a.get("precio")), cambio, det, marca(a, corchetes=True)))
        print("     Zona: %s%s" % (a.get("zona") or "-", "   [CERCANO, fuera de lo buscado]" if a.get("cercano") else ""))
        anun = a.get("anunciante") or "-"
        tel = (" · Tel. " + a["telefono"]) if a.get("telefono") else ""
        dur = (" · " + a["duracion"]) if a.get("duracion") else ""
        print("     %s%s%s" % (anun, tel, dur))
        print("     " + a["url"])
        if largo and a.get("descripcion"):
            for linea in a["descripcion"].split("\n"):
                if linea.strip():
                    print("       | " + linea.strip())
        print()


def marca(a, corchetes=False):
    m = []
    if a.get("_nuevo"):
        m.append("NUEVO")
    if a.get("_cambio") is not None:
        m.append("PRECIO")
    if "Visto" in (a.get("etiquetas") or []):
        m.append("visto-web")
    s = ",".join(m)
    return ("[" + s + "]") if (corchetes and s) else s


# ======================================================================= COMANDOS
def cmd_servidor(a):
    return servidor_foreground()


def cmd_parar(a):
    try:
        http("POST", "/parar", {}, timeout=3)
        print("Servidor parado.")
    except Exception:
        print("El servidor no estaba en marcha.")
    return 0


def cmd_estado(a):
    if not servidor_vivo():
        print("Servidor local: parado (se arranca solo al lanzar cualquier otro comando).")
        return 0
    st = http("GET", "/estado", timeout=3)
    print("Servidor local: en marcha (puerto %d, version %s)" % (PUERTO, st.get("version")))
    print("Extension de Firefox: %s" % ("CONECTADA" if st.get("extension_conectada") else "NO conectada"))
    if st.get("extension_conectada"):
        try:
            r = pedir("estado", espera=15)
            print("Version de la extension: %s" % r.get("version"))
            ps = r.get("pestanas") or []
            print("Pestañas de fotocasa.es abiertas: %d" % len(ps))
            for p in ps:
                print("  - %s%s" % (p.get("url"), "  (activa)" if p.get("activa") else ""))
        except ErrorExt as e:
            err(e)
            return 1
    return 0


def cmd_sugerir(a):
    texto = " ".join(a.texto)
    asegurar_pagina_habitaciones()
    r = pedir("sugerir", {"texto": texto})
    sug = r.get("sugerencias") or []
    if not sug:
        print("Fotocasa no ha ofrecido ninguna sugerencia para '%s'." % texto)
        return 0
    print("Sugerencias de Fotocasa para '%s':" % texto)
    print()
    print(formato_sugerencias(sug))
    print()
    print("Columna 'Zonas' = si, Fotocasa permite elegir zonas dentro de ese municipio (comando 'zonas').")
    return 0


def cmd_zonas(a):
    texto = " ".join(a.texto)
    elegida, zonas = elegir_sugerencia(texto, a.n, solo_zonas=True)
    if a.sub and zonas:
        zonas = pedir("zonas", {"expandir": True}).get("zonas") or zonas
    print("Zonas de %s según Fotocasa:" % elegida.get("nombre"))
    print()
    if not zonas:
        print("Fotocasa no ha mostrado zonas para esa opción.")
        return 0
    filas = [("Zona", "Anuncios", "Subzonas")]
    for z in zonas:
        nombre = ("    " if z.get("nivel") else "") + z["nombre"]
        filas.append((nombre, str(z["anuncios"]), "sí (-sub)" if z.get("expandible") and not a.sub and not z.get("nivel") else ""))
    print(tabla(filas, [46, 8, 10], dcha={1}))
    print()
    print("Para buscar solo en algunas zonas:  fotocasa.py buscar \"%s\" -z \"Centro\" -z \"Sur\"" % texto)
    return 0


def cmd_buscar(a):
    if a.orden:
        o = ALIAS_ORDEN.get(norm(a.orden), norm(a.orden))
        if o not in ORDENES:
            err("Orden desconocido '%s'. Validos: %s" % (a.orden, ", ".join(ORDENES)))
            return 2
    else:
        o = None
    texto = " ".join(a.texto)
    elegida, zonas = elegir_sugerencia(texto, a.n)
    if a.z:
        if not elegida.get("zonas"):
            raise ErrorExt("Fotocasa no ofrece zonas para '%s'; quita -z o elige otra sugerencia con -n." % elegida.get("nombre"))
        lista = [z.strip() for zz in a.z for z in zz.split(",") if z.strip()]
        r = pedir("marcar", {"zonas": lista}, espera=60)
        if r.get("noEncontradas"):
            nombres = ", ".join(z["nombre"] for z in r.get("zonas") or [])
            raise ErrorExt("Zona(s) no encontrada(s): %s.\nZonas disponibles: %s\n(Con -sub se pueden usar también subzonas: 'fotocasa.py zonas \"%s\" -sub')"
                           % (", ".join(r["noEncontradas"]), nombres, texto))
    b = pedir("buscar", {}, espera=60)
    if not b.get("clic"):
        print("Aviso: " + (b.get("motivo") or "no se ha pulsado Buscar") + " - leo la página tal como está.", file=sys.stdout)
    if o:
        try:
            pedir("ordenar", {"valor": ORDENES[o]}, espera=40)
        except ErrorExt as e:
            err("No he podido ordenar (%s). Sigo sin ordenar." % e)
    return mostrar_resultados(a)


def cmd_leer(a):
    return mostrar_resultados(a)


def mostrar_resultados(a):
    info, anuncios = leer_paginas(max(1, a.paginas), a.max)
    if a.sin_cercanos:
        anuncios = [x for x in anuncios if not x.get("cercano")]
    if a.pmin is not None:
        anuncios = [x for x in anuncios if x.get("precio") is not None and x["precio"] >= a.pmin]
    if a.pmax is not None:
        anuncios = [x for x in anuncios if x.get("precio") is not None and x["precio"] <= a.pmax]
    if a.max:
        anuncios = anuncios[: a.max]
    vistos = cargar_vistos()
    ahora = marcar_cambios(anuncios, vistos)
    mostrar = [x for x in anuncios if x["_nuevo"] or x["_cambio"] is not None] if a.nuevos else anuncios
    if a.json:
        print(json.dumps({"info": info, "anuncios": mostrar}, ensure_ascii=False, indent=1))
    else:
        imprimir_anuncios(info, mostrar, largo=a.largo, compacto=a.tabla)
        if a.nuevos:
            print("(solo nuevos o con cambio de precio: %d de %d)" % (len(mostrar), len(anuncios)))
    if not a.nosave:
        actualizar_vistos(anuncios, vistos, ahora)
        guardar_vistos(vistos)
    return 0


def cmd_diag(a):
    r = pedir("diag", espera=30)
    print(json.dumps(r, ensure_ascii=False, indent=1))
    return 0


def cmd_vistos(a):
    if a.borrar:
        try:
            os.remove(F_VISTOS)
        except FileNotFoundError:
            pass
        print("Historial de anuncios vistos borrado.")
        return 0
    v = cargar_vistos()
    print("Anuncios recordados: %d (fichero %s)" % (len(v), F_VISTOS))
    return 0


AYUDA = """\
fotocasa.py %(v)s - habitaciones compartidas en Fotocasa desde Termux

COMANDOS
  sugerir TEXTO        Lo que ofrece el buscador de Fotocasa al escribir TEXTO
                       (municipios, comarcas, barrios, calles...), con nº de anuncios.
  zonas MUNICIPIO      Zonas del municipio que Fotocasa deja elegir (con -sub, subzonas).
  buscar MUNICIPIO     Busca habitaciones y las muestra.
  leer                 Lee la búsqueda que ya está abierta en la pestaña de Firefox.
  estado               Comprueba servidor y extensión.
  servidor / parar     Arranca (a mano) o para el servidor local. Se arranca solo si hace falta.
  vistos [-borrar]     Memoria de anuncios ya vistos (para marcar NUEVO).
  diag                 Información técnica de la página (por si algo falla).
  ayuda                Este texto.

OPCIONES DE buscar / leer
  -z ZONA              Solo esa zona (repetible o con comas): -z Centro -z "Somió - Cabueñes"
  -n N                 Elige la sugerencia número N (ver 'sugerir') en vez de la automática.
  -orden X             recientes | baratos | caros | grandes | pequenos | eurom2 | relevancia
  -paginas N           Lee hasta N páginas de resultados (por defecto 1).
  -max N               Máximo de anuncios.
  -pmin E / -pmax E    Filtra por precio mensual en euros.
  -sin-cercanos        Quita los anuncios "de zonas cercanas" que añade Fotocasa.
  -nuevos              Solo anuncios nuevos o con cambio de precio desde la última vez.
  -largo               Incluye la descripción completa.
  -tabla               Formato compacto de una línea por anuncio.
  -json                Salida en JSON.
  -nosave              No recordar estos anuncios como vistos.

EJEMPLOS
  python fotocasa.py sugerir gijon
  python fotocasa.py zonas Gijón
  python fotocasa.py buscar Oviedo -orden recientes -pmax 300
  python fotocasa.py buscar Gijón -z Centro -z Oeste -paginas 3 -tabla
  python fotocasa.py buscar Oviedo -nuevos
""" % {"v": VERSION}


def cmd_ayuda(a):
    print(AYUDA)
    return 0


def construir_parser():
    p = argparse.ArgumentParser(prog="fotocasa.py", add_help=False)
    sp = p.add_subparsers(dest="cmd")

    def res(s):
        s.add_argument("-n", type=int)
        s.add_argument("-z", action="append")
        s.add_argument("-orden")
        s.add_argument("-paginas", type=int, default=1)
        s.add_argument("-max", type=int)
        s.add_argument("-pmin", type=int)
        s.add_argument("-pmax", type=int)
        s.add_argument("-sin-cercanos", dest="sin_cercanos", action="store_true")
        s.add_argument("-nuevos", action="store_true")
        s.add_argument("-largo", action="store_true")
        s.add_argument("-tabla", action="store_true")
        s.add_argument("-json", action="store_true")
        s.add_argument("-nosave", action="store_true")

    s = sp.add_parser("servidor"); s.add_argument("--silencioso", action="store_true"); s.set_defaults(f=cmd_servidor)
    s = sp.add_parser("parar"); s.set_defaults(f=cmd_parar)
    s = sp.add_parser("estado"); s.set_defaults(f=cmd_estado)
    s = sp.add_parser("sugerir", aliases=["sugerencias", "sug"]); s.add_argument("texto", nargs="+"); s.set_defaults(f=cmd_sugerir)
    s = sp.add_parser("zonas", aliases=["zona", "areas"]); s.add_argument("texto", nargs="+")
    s.add_argument("-n", type=int); s.add_argument("-sub", action="store_true"); s.set_defaults(f=cmd_zonas)
    s = sp.add_parser("buscar", aliases=["b", "busca"]); s.add_argument("texto", nargs="+"); res(s); s.set_defaults(f=cmd_buscar)
    s = sp.add_parser("leer", aliases=["ver", "resultados"]); res(s); s.set_defaults(f=cmd_leer)
    s = sp.add_parser("diag", aliases=["diagnostico"]); s.set_defaults(f=cmd_diag)
    s = sp.add_parser("vistos"); s.add_argument("-borrar", action="store_true"); s.set_defaults(f=cmd_vistos)
    s = sp.add_parser("ayuda", aliases=["help", "-h", "--help"]); s.set_defaults(f=cmd_ayuda)
    return p


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help", "-help", "help", "ayuda"):
        print(AYUDA)
        return 0
    p = construir_parser()
    try:
        a = p.parse_args(argv)
    except SystemExit as e:
        if e.code not in (0, None):
            err("Argumentos no válidos. Escribe: python fotocasa.py ayuda")
        return 2 if e.code else 0
    if not getattr(a, "f", None):
        print(AYUDA)
        return 0
    try:
        return a.f(a) or 0
    except ErrorExt as e:
        err(e)
        return 1
    except urllib.error.URLError as e:
        err("No puedo hablar con el servidor local (%s)." % e)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
