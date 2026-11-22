#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
linkdApp.py - Lector de LinkedIn desde Termux
Trabaja junto con la extension de Firefox para Android "linkdApp".

Todas las ordenes se dan desde Termux. linkdApp.py abre la pagina de LinkedIn en
Firefox, la extension la lee con tu sesion, y el resultado vuelve a Termux,
se muestra en pantalla y se guarda como .txt en la carpeta desde la que
ejecutas la orden.

Uso rapido:   python linkdApp.py ayuda
"""

import sys
import os
import re
import json
import time
import uuid
import base64
import csv
import shutil
import textwrap
import threading
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.12"
VERSION_EXT_MINIMA = "1.10"
PUERTO = 47323
DIR = os.getcwd()
ARCH_CONF = os.path.join(DIR, "linkdApp_config.json")
ARCH_ESTADO = os.path.join(DIR, "linkdApp_estado.json")
LI = "https://www.linkedin.com"

CONF_DEFECTO = {
    "firefox_paquete": "org.mozilla.firefox",
    "escritorio": True,
    "cerrar_pestana": True,
    "volver_a_termux": True,
    "tiempo_espera": 150,
    "ancho": 0,
    "mostrar_en_pantalla": True,
    "carpeta_imagenes": "imagenes",
    "sin_promocionados": True,
    "enlaces_menu": False,
    "csv": True,
    "separador_csv": "tab",
    "csv_una_linea": False,
}

AYUDA = """
linkdApp.py {v} - Lector de LinkedIn desde Termux (con la extension linkdApp)

PUBLICACIONES
  feed [N]                 Lee las N primeras publicaciones de tu feed (15 por defecto)
  img N [M ...]            Descarga las imagenes de las publicaciones N, M...
  img todo                 Descarga las imagenes de todas las publicaciones leidas
  post N [V]               Lee la publicacion N con sus comentarios (V = cargar mas)
  abrir N                  Abre la publicacion N en Firefox (para verla o reaccionar)
  autor N                  Visita el perfil del autor de la publicacion N

PERSONAS Y EMPRESAS
  contactos [N]            Lista tus contactos
  siguiendo [N]            Lista, en dos apartados, las personas que sigues
                           (fuera de tu red) y las empresas que sigues
  buscar TEXTO [N] [-e]    Busqueda general, como el cuadro de busqueda de
                           LinkedIn: personas, empresas y publicaciones
                           TEXTO puede ser:  palabra
                                             palabra1+palabra2  (las dos)
                                             "un texto"         (exacto)
                           -e: solo publicaciones que contengan de verdad
                           las palabras o el texto exacto
  buscar-personas TEXTO    Solo personas
  buscar-empresa TEXTO     Solo empresas
  buscar-pub TEXTO [N]     Solo publicaciones (todas sus funciones: img N,
                           post N, abrir N)
  perfil X                 Visita un perfil. X = numero de la ultima lista,
                           nombre de usuario (juan-perez-123) o URL
  empresa X                Visita una empresa (numero, nombre-empresa o URL)
  actividad X [N]          Ultimas N publicaciones de una persona o empresa
                           (X = numero de la ultima lista: contactos,
                           siguiendo, buscar...)

NOTIFICACIONES
  notificaciones [N]       Tus N ultimas notificaciones (30 por defecto): quien,
                           que (visita al perfil, publicacion compartida...),
                           cuando, si es nueva, y enlace directo

MENSAJES
  mensajes [N]             Lista tus N ultimas conversaciones (20 por defecto)
  chat N [A]               Lee la conversacion N (A = cuantas veces cargar
                           mensajes anteriores, 2 por defecto)

OTRAS
  sql "CONSULTA" [CSV...]  Consulta SQL sobre los CSV (sin programas externos);
                           la primera tabla se llama tambien t. Sin
                           consulta: muestra tablas y columnas.
                           --completo: textos enteros   --csv: salida CSV
  pagina URL               Convierte en texto cualquier pagina de LinkedIn
  diag URL                 Guarda el HTML de una pagina (para diagnostico)
  prueba                   Comprueba que Termux y la extension se comunican
  config                   Muestra la configuracion
  config CLAVE VALOR       Cambia una opcion (ej: config escritorio no)
  ayuda                    Esta ayuda

Las listas y publicaciones se guardan tambien en .csv (separador tabulador).
Todo se guarda en la carpeta actual: .txt con cada lectura, imagenes/ con las
imagenes, linkdApp_estado.json con las ultimas listas (para usar los numeros).
""".format(v=VERSION)


# ----------------------------------------------------------------------------
# Configuracion y estado
# ----------------------------------------------------------------------------

ARCH_LOG = os.path.join(DIR, "linkdApp_log.txt")


def version_menor(v, minima):
    """True si la version v (texto '1.4') es menor que minima o no se conoce."""
    try:
        a = [int(x) for x in str(v).split(".")]
        b = [int(x) for x in str(minima).split(".")]
        return a < b
    except Exception:
        return True


def registrar(texto):
    """Añade una linea al registro linkdApp_log.txt (para diagnosticar fallos)."""
    try:
        if os.path.exists(ARCH_LOG) and os.path.getsize(ARCH_LOG) > 300000:
            os.replace(ARCH_LOG, ARCH_LOG + ".1")
        with open(ARCH_LOG, "a", encoding="utf-8") as f:
            f.write("{} {}\n".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), texto))
    except Exception:
        pass


def cargar_conf():
    conf = dict(CONF_DEFECTO)
    if os.path.exists(ARCH_CONF):
        try:
            with open(ARCH_CONF, encoding="utf-8") as f:
                conf.update(json.load(f))
        except Exception as e:
            print("Aviso: no pude leer linkdApp_config.json ({}); uso valores por defecto.".format(e))
    else:
        guardar_json(ARCH_CONF, conf)
    return conf


def guardar_json(ruta, obj):
    tmp = ruta + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, ruta)


def cargar_estado():
    if os.path.exists(ARCH_ESTADO):
        try:
            with open(ARCH_ESTADO, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def actualizar_estado(**claves):
    est = cargar_estado()
    est.update(claves)
    guardar_json(ARCH_ESTADO, est)


# ----------------------------------------------------------------------------
# Servidor local: la extension recoge aqui la orden y entrega el resultado
# ----------------------------------------------------------------------------

def sanear(x):
    """Quita caracteres sueltos no validos (medios emojis) que impedirian guardar en UTF-8."""
    if isinstance(x, str):
        return x.encode("utf-8", "replace").decode("utf-8")
    if isinstance(x, list):
        return [sanear(v) for v in x]
    if isinstance(x, dict):
        return {k: sanear(v) for k, v in x.items()}
    return x


class Canal:
    def __init__(self, orden, carpeta_img):
        self.orden = orden
        self.reclamada = False
        self.resultado = None
        self.evento = threading.Event()
        self.carpeta_img = carpeta_img
        self.archivos = []
        self.fallos = []
        self.parciales = []
        self.version_ext = ""
        self.ultimo_aviso = time.time()


class Manejador(BaseHTTPRequestHandler):
    canal = None

    def log_message(self, *args):
        pass

    def _responder(self, codigo, obj):
        cuerpo = json.dumps(obj).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def do_OPTIONS(self):
        self._responder(204, {})

    def do_GET(self):
        ruta = urllib.parse.urlparse(self.path).path
        c = self.canal
        if ruta == "/job":
            if c is None or c.reclamada:
                return self._responder(200, {})
            return self._responder(200, c.orden)
        if ruta == "/ping":
            return self._responder(200, {"ok": True, "version": VERSION})
        self._responder(404, {"error": "no existe"})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        largo = int(self.headers.get("Content-Length") or 0)
        datos = self.rfile.read(largo) if largo else b""
        try:
            obj = json.loads(datos.decode("utf-8")) if datos else {}
        except Exception:
            obj = {}
        c = self.canal
        if c is None:
            return self._responder(410, {"error": "sin orden"})
        oid = c.orden["id"]

        if u.path == "/claim":
            if qs.get("id", [""])[0] == oid and not c.reclamada:
                c.reclamada = True
                v = qs.get("v", [""])[0]
                c.version_ext = v
                print("   > La extension linkdApp {} ha recibido la orden. Leyendo...".format(v or "(antigua)"))
                registrar("   > extension version {}".format(v or "desconocida"))
                if version_menor(v, VERSION_EXT_MINIMA):
                    print("   ! AVISO: la extension instalada en Firefox es antigua ({}). linkdApp.py {}".format(v or "sin version", VERSION))
                    print("     necesita la extension {} o posterior. Instala la ultima linkdApp_extension".format(VERSION_EXT_MINIMA))
                    print("     firmada y desinstala 'LK Lector' y versiones viejas de linkdApp.")
                return self._responder(200, {"ok": True})
            return self._responder(409, {"error": "ya reclamada"})

        if obj.get("id") != oid:
            return self._responder(409, {"error": "orden distinta"})

        if u.path == "/progress":
            c.ultimo_aviso = time.time()
            m = str(obj.get("msg", ""))
            print("   . " + m)
            registrar("   . " + m)
            return self._responder(200, {"ok": True})
        if u.path == "/file":
            nombre = re.sub(r"[^\w\-.]", "_", str(obj.get("nombre", "archivo")))
            if obj.get("error"):
                c.fallos.append(nombre)
                print("   x {}: {}".format(nombre, obj.get("error")))
                return self._responder(200, {"ok": False})
            ext = extension_de(obj.get("tipo", ""))
            os.makedirs(c.carpeta_img, exist_ok=True)
            ruta = os.path.join(c.carpeta_img, nombre + ext)
            with open(ruta, "wb") as f:
                f.write(base64.b64decode(obj.get("b64", "")))
            c.archivos.append(ruta)
            print("   + " + os.path.relpath(ruta, DIR))
            return self._responder(200, {"ok": True})
        if u.path == "/partial":
            c.ultimo_aviso = time.time()
            if obj.get("pub"):
                c.parciales.append(sanear(obj["pub"]))
            return self._responder(200, {"ok": True})
        if u.path == "/result":
            c.resultado = sanear(obj.get("data") or {})
            c.evento.set()
            return self._responder(200, {"ok": True})
        self._responder(404, {"error": "no existe"})


class Servidor(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def marcar_url(url, oid, escritorio):
    p = urllib.parse.urlsplit(url)
    q = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
    q.append(("lajob", oid))
    if escritorio:
        q.append(("lad", "1"))
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, urllib.parse.urlencode(q), p.fragment))


def abrir_en_firefox(url, conf):
    paquete = conf.get("firefox_paquete") or "org.mozilla.firefox"
    try:
        r = subprocess.run(
            ["am", "start", "-a", "android.intent.action.VIEW", "-d", url, "-p", paquete],
            capture_output=True, text=True, timeout=25)
        salida = (r.stdout or "") + (r.stderr or "")
        if r.returncode == 0 and "Error" not in salida:
            return True
    except Exception:
        pass
    try:
        subprocess.run(["termux-open-url", url], timeout=25)
        return True
    except Exception:
        pass
    print("No pude abrir Firefox automaticamente. Abre esta direccion en Firefox:")
    print(url)
    return False


def volver_a_termux(conf):
    if not conf.get("volver_a_termux"):
        return
    try:
        subprocess.run(["am", "start", "-n", "com.termux/com.termux.app.TermuxActivity"],
                       capture_output=True, text=True, timeout=10)
    except Exception:
        pass


def ejecutar_orden(tipo, url, conf, n=0, extra=None, coincide=None, carpeta_img=None, abrir=True, tiempo=None,
                   volver=True):
    """Publica una orden, abre Firefox y espera el resultado de la extension."""
    oid = uuid.uuid4().hex[:12]
    espera = max(int(conf["tiempo_espera"]), int(tiempo or 0))
    if os.environ.get("LINKDAPP_ESPERA"):  # solo para pruebas
        espera = int(os.environ["LINKDAPP_ESPERA"])
    orden = {
        "id": oid, "tipo": tipo, "url": url, "n": n, "extra": extra or {},
        "creado": time.time(), "max_seg": max(30, espera - 15),
        "cerrar": bool(conf.get("cerrar_pestana")), "coincide": coincide or [],
    }
    canal = Canal(orden, carpeta_img or os.path.join(DIR, conf["carpeta_imagenes"]))
    Manejador.canal = canal
    try:
        srv = Servidor(("127.0.0.1", PUERTO), Manejador)
    except OSError:
        print("El puerto {} esta ocupado. Puede haber otra orden de linkdApp.py en marcha.".format(PUERTO))
        sys.exit(2)
    hilo = threading.Thread(target=srv.serve_forever, daemon=True)
    hilo.start()

    print("Abriendo en Firefox: " + url)
    if abrir:
        abrir_en_firefox(marcar_url(url, oid, conf.get("escritorio")), conf)
    print("Esperando a la extension (maximo {} s, Ctrl+C para cancelar)...".format(espera))

    fin = time.time() + espera
    avisado = False
    try:
        while not canal.evento.is_set() and time.time() < fin:
            canal.evento.wait(0.5)
            if canal.reclamada and not avisado and time.time() - canal.ultimo_aviso > 90:
                avisado = True
                print("   ! Hace 90 s que no llegan noticias de Firefox. Comprueba que la pantalla")
                print("     esta encendida y Firefox delante (Android lo congela si no).")
                registrar("   ! 90 s sin noticias de Firefox")
    except KeyboardInterrupt:
        print("\nCancelado.")
        srv.shutdown()
        srv.server_close()
        sys.exit(130)
    srv.shutdown()
    srv.server_close()
    if volver:
        volver_a_termux(conf)

    if not canal.evento.is_set() and canal.parciales:
        n = len(canal.parciales)
        aviso = ("Firefox no llego a terminar (pantalla apagada, Firefox en segundo plano o LinkedIn "
                 "muy lento). Se entregan las {} publicaciones que llegaron.".format(n))
        registrar("AVISO: " + aviso)
        return {"tipo": "publicaciones", "url": url, "publicaciones": canal.parciales, "aviso": aviso}, canal
    if not canal.evento.is_set():
        registrar("ERROR: no llego respuesta de la extension a tiempo (reclamada={})".format(canal.reclamada))
        print("\nNo llego respuesta de la extension a tiempo.")
        if not canal.reclamada:
            print("  - La extension no recogio la orden. Comprueba que linkdApp {} (o posterior)".format(VERSION_EXT_MINIMA))
            print("    esta instalada y activada en Firefox y que has iniciado sesion en LinkedIn.")
            print("    Las extensiones antiguas (LK Lector, linkdApp 1.4 o anteriores) ya no")
            print("    pueden recibir ordenes de esta version: instala la nueva.")
            print("  - Prueba:  python linkdApp.py prueba")
        else:
            print("  - La extension empezo pero no termino. No cambies a Termux mientras")
            print("    lee (Android frena Firefox en segundo plano), o sube el tiempo:")
            print("    python linkdApp.py config tiempo_espera 240")
        sys.exit(1)
    res = canal.resultado or {}
    if res.get("error"):
        registrar("ERROR: " + str(res["error"]))
        if canal.parciales:
            aviso = str(res["error"]) + " Se entregan las {} publicaciones que llegaron.".format(len(canal.parciales))
            return {"tipo": "publicaciones", "url": url, "publicaciones": canal.parciales, "aviso": aviso}, canal
        print("\nError: " + str(res["error"]))
        print("(Detalle guardado en linkdApp_log.txt)")
        sys.exit(1)
    registrar("OK: resultado recibido ({})".format(res.get("tipo", "")))
    return res, canal


# ----------------------------------------------------------------------------
# Formato de texto
# ----------------------------------------------------------------------------

def ancho_texto(conf):
    w = int(conf.get("ancho") or 0)
    if not w:
        w = shutil.get_terminal_size((72, 24)).columns
    return max(40, min(w, 110))


def envolver(texto, w, sangria=""):
    salida = []
    for linea in str(texto or "").split("\n"):
        if not linea.strip():
            salida.append("")
            continue
        salida.append(textwrap.fill(linea.strip(), width=w, initial_indent=sangria,
                                    subsequent_indent=sangria, break_long_words=True,
                                    break_on_hyphens=False))
    return "\n".join(salida)


def campo(etiqueta, valor, w, sangria="    ", ancho_etq=13):
    """Linea 'Etiqueta:   valor' con el valor alineado y envuelto."""
    pref = sangria + (etiqueta + ":").ljust(ancho_etq)
    resto = " " * len(pref)
    return textwrap.fill(str(valor), width=w, initial_indent=pref, subsequent_indent=resto,
                         break_long_words=True, break_on_hyphens=False)


SEPARADORES = {"tab": "\t", "tabulador": "\t", "\\t": "\t", ";": ";", "puntoycoma": ";",
               "|": "|", "barra": "|", ",": ",", "coma": ","}


def escribir_csv(ruta, columnas, filas, conf):
    """CSV con separador tabulador por defecto: el tabulador nunca aparece en los
    datos de LinkedIn (se normalizan los espacios), mientras que comas, puntos y
    coma y barras | son muy frecuentes en titulares y textos. Ademas, cualquier
    campo que contenga el separador, comillas o saltos de linea va entre comillas
    (formato estandar RFC 4180), asi que el archivo nunca se descuadra."""
    sep = SEPARADORES.get(str(conf.get("separador_csv", "tab")).lower(), "\t")
    with open(ruta, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=sep, quotechar='"', quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
        w.writerow(columnas)
        una_linea = bool(conf.get("csv_una_linea"))
        for fila in filas:
            valores = ["" if v is None else str(v).replace("\t", " ") for v in fila]
            if una_linea:
                # saltos de linea como \n literal (2 caracteres): cada registro ocupa una sola linea
                valores = [v.replace("\r\n", "\n").replace("\r", "\n").replace("\\", "\\\\").replace("\n", "\\n")
                           for v in valores]
            w.writerow(valores)


def entregar(texto, prefijo, conf, csv_datos=None):
    base = "{}_{}".format(prefijo, datetime.now().strftime("%Y%m%d_%H%M%S"))
    nombre = base + ".txt"
    ruta = os.path.join(DIR, nombre)
    with open(ruta, "w", encoding="utf-8") as f:
        f.write(texto.rstrip() + "\n")
    if conf.get("mostrar_en_pantalla"):
        print()
        print(texto.rstrip())
    print()
    print("Guardado en: " + nombre)
    if csv_datos and conf.get("csv", True):
        columnas, filas = csv_datos
        escribir_csv(os.path.join(DIR, base + ".csv"), columnas, filas, conf)
        print("CSV:         " + base + ".csv")
    return ruta


COLUMNAS_LISTA = ["n", "apartado", "tipo", "nombre", "titular", "seguidores", "contacto_desde", "url", "usuario"]
COLUMNAS_PUBS = ["n", "autor", "autor_url", "fecha", "contexto", "cabecera", "texto", "articulo",
                 "enlaces", "num_imagenes", "imagenes", "video", "documento", "reacciones",
                 "comentarios_visibles", "publicacion_url", "compartida_de"]
TIPOS = {"in": "persona", "company": "empresa", "school": "centro", "showcase": "pagina"}


def filas_lista(items):
    return [[i, it.get("apartado", ""), TIPOS.get(it.get("tipo"), it.get("tipo", "")), it.get("nombre", ""),
             it.get("titular", ""), it.get("seguidores", ""), it.get("desde", ""), it.get("url", ""),
             urllib.parse.unquote(it.get("slug", ""))] for i, it in enumerate(items, 1)]


def filas_pubs(pubs):
    filas = []
    for i, p in enumerate(pubs, 1):
        comp = p.get("compartida") or {}
        doc = p.get("documento")
        filas.append([i, p.get("autor", ""), p.get("autorUrl", ""), p.get("fecha", ""), p.get("contexto", ""),
                      " / ".join(p.get("cabecera") or []), p.get("texto", ""), p.get("articulo", ""),
                      " ".join(p.get("enlaces") or []), len(p.get("imagenes") or []),
                      " ".join(u for u in (p.get("imagenes") or []) if not u.startswith("data:")),
                      "si" if p.get("video") else "", (doc if isinstance(doc, str) else ("si" if doc else "")),
                      p.get("social", ""), len(p.get("comentarios") or []), p.get("url", ""), comp.get("autor", "")])
    return filas


def url_limpia(url):
    """Quita de la URL las marcas lajob/lkd que usa linkdApp.py."""
    if not url:
        return ""
    p = urllib.parse.urlsplit(url)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True) if k not in ("lajob", "lad")]
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, urllib.parse.urlencode(q), p.fragment))


def cabecera_doc(titulo, url, w):
    url = url_limpia(url)
    lin = "=" * w
    partes = [lin, envolver(titulo.upper(), w), "Fecha de lectura: " + datetime.now().strftime("%d/%m/%Y %H:%M")]
    if url:
        partes.append("Origen: " + url)
    partes.append(lin)
    return "\n".join(partes)


def formatear_publicaciones(pubs, w, titulo, url, primero=1):
    out = [cabecera_doc(titulo, url, w), ""]
    for i, p in enumerate(pubs, primero):
        out.append("=" * w)
        out.append(envolver("[{:>2}] {}".format(i, p.get("autor") or "(autor desconocido)"), w))
        if p.get("contexto"):
            out.append(envolver(p["contexto"], w, "     "))
        for l in p.get("cabecera", [])[:3]:
            out.append(envolver(l, w, "     "))
        if p.get("promocionado"):
            out.append("     (PUBLICIDAD / PROMOCIONADO)")
        out.append("-" * w)
        out.append(envolver(p.get("texto") or "(Sin texto: publicacion solo con imagen, video o documento)", w))
        if p.get("articulo"):
            out.append("")
            out.append(envolver("Articulo enlazado: " + p["articulo"], w))
        out.append("-" * w)
        if p.get("social"):
            out.append(campo("Reacciones", p["social"], w))
        nimg = len(p.get("imagenes") or [])
        if nimg:
            out.append(campo("Imagenes", "{}   (descargar: python linkdApp.py img {})".format(nimg, i), w))
        if p.get("video"):
            out.append(campo("Video", "si (abrir: python linkdApp.py abrir {})".format(i), w))
        if p.get("documento"):
            doc = p["documento"] if isinstance(p["documento"], str) else "si"
            out.append(campo("Documento", "{} (carrusel/PDF; las paginas cargadas van con img {})".format(doc, i), w))
        for e in p.get("enlaces") or []:
            out.append(campo("Enlace", e, w))
        if p.get("autorUrl"):
            out.append(campo("Autor", p["autorUrl"], w))
        if p.get("url"):
            out.append(campo("Publicacion", p["url"], w))
        com = p.get("comentarios") or []
        if com:
            out.append(campo("Comentarios", "{} visibles (todos: python linkdApp.py post {})".format(len(com), i), w))
            for c in com[:3]:
                cab = "> {}{}:".format(c.get("autor") or "?", " ({})".format(c["fecha"]) if c.get("fecha") else "")
                out.append(envolver(cab, w, "      "))
                out.append(envolver(c.get("texto", ""), w, "          "))
        out.append("")
    out.append("=" * w)
    out.append("Publicaciones: {}".format(len(pubs)))
    return "\n".join(out)


def formatear_bruto(lineas, w, titulo, url, aviso):
    out = [cabecera_doc(titulo, url, w), "", envolver(aviso, w), ""]
    out.extend(envolver(l, w) for l in lineas)
    return "\n".join(out)


def formatear_secciones(res, w, titulo_tipo):
    out = [cabecera_doc("{}: {}".format(titulo_tipo, res.get("nombre", "")), res.get("url", ""), w), ""]
    for s in res.get("secciones", []):
        if s.get("titulo"):
            t = " {} ".format(s["titulo"].upper())
            out.append(t.center(w, "-"))
        else:
            out.append("-" * w)
        for l in s.get("lineas", []):
            out.append(envolver(l, w, "  "))
        out.append("")
    det = res.get("detalles") or []
    if det:
        out.append("-" * w)
        out.append("Detalles completos (python linkdApp.py pagina URL):")
        for d in det[:15]:
            out.append(envolver(d, w, "  "))
    return "\n".join(out)


def formatear_lista(items, w, titulo, url):
    out = [cabecera_doc(titulo, url, w), ""]
    apartado_prev = None
    for i, it in enumerate(items, 1):
        ap = it.get("apartado")
        if ap and ap != apartado_prev:
            cuantos = sum(1 for x in items if x.get("apartado") == ap)
            out.append((" {} ({}) ".format(ap.upper(), cuantos)).center(w, "="))
            out.append("")
            apartado_prev = ap
        tipo = TIPOS.get(it.get("tipo"), "")
        nombre = it.get("nombre") or "(sin nombre)"
        out.append(textwrap.fill("{:>3}. {}   [{}]".format(i, nombre, tipo), width=w, subsequent_indent="      ",
                                 break_long_words=True, break_on_hyphens=False))
        info = [x for x in (it.get("titular"), it.get("seguidores"), it.get("desde")) if x] or \
            [x for x in it.get("info", []) if x != nombre][:2]
        for l in info:
            out.append(envolver(l, w, "      "))
        out.append(envolver(it.get("url", ""), w, "      "))
        out.append("")
    out.append("-" * w)
    out.append(envolver("Total: {}.  Publicaciones de uno: python linkdApp.py actividad N [cuantas]"
                        "   Perfil: perfil N / empresa N".format(len(items)), w))
    return "\n".join(out)


def formatear_conversaciones(convs, w, url):
    out = [cabecera_doc("Mensajes de LinkedIn", url, w), ""]
    nl = sum(1 for c in convs if c.get("noLeido"))
    out.append("Conversaciones: {}   No leidas: {}".format(len(convs), nl))
    out.append("")
    for i, c in enumerate(convs, 1):
        marca = " *NO LEIDO*" if c.get("noLeido") else ""
        cab = "{:>3}. {}{}".format(i, c.get("nombre") or "(sin nombre)", marca)
        if c.get("fecha"):
            cab = cab.ljust(max(len(cab) + 1, w - len(c["fecha"]) - 1)) + c["fecha"]
        out.append(envolver(cab, w) if len(cab) > w else cab)
        if c.get("resumen"):
            out.append(envolver(c["resumen"], w, "      "))
        out.append("")
    out.append("-" * w)
    out.append("Leer una:  python linkdApp.py chat N")
    return "\n".join(out)


def formatear_hilo(res, w):
    out = [cabecera_doc("Conversacion: " + (res.get("titulo") or ""), res.get("url", ""), w), ""]
    dia_prev = None
    for m in res.get("mensajes", []):
        if m.get("dia") and m["dia"] != dia_prev:
            out.append((" " + m["dia"] + " ").center(w, "-"))
            dia_prev = m["dia"]
        out.append("[{}] {}:".format(m.get("hora") or "--:--", m.get("autor") or "?"))
        out.append(envolver(m.get("texto", ""), w, "    "))
        for a in m.get("adjuntos") or []:
            out.append(envolver("Adjunto: " + a, w, "    "))
        out.append("")
    out.append("-" * w)
    out.append("Mensajes: {}".format(len(res.get("mensajes", []))))
    return "\n".join(out)


def formatear_post(res, w, n=1):
    out = []
    p = res.get("publicacion")
    if p:
        p = dict(p)
        p.pop("comentarios", None)
        out.append(formatear_publicaciones([p], w, "Publicacion", res.get("url", ""), primero=n))
    else:
        out.append(cabecera_doc("Publicacion", res.get("url", ""), w))
    com = res.get("comentarios") or []
    out.append("")
    out.append(" COMENTARIOS ({}) ".format(len(com)).center(w, "="))
    for c in com:
        sang = "        " if c.get("respuesta") else "    "
        cab = (c.get("autor") or "?") + (" ({})".format(c["fecha"]) if c.get("fecha") else "") + ":"
        out.append(("    > " if c.get("respuesta") else "") + cab)
        out.append(envolver(c.get("texto", ""), w, sang))
        out.append("")
    return "\n".join(out)


# ----------------------------------------------------------------------------
# Resolucion de destinos (numero de lista, nombre o URL)
# ----------------------------------------------------------------------------

def resolver(x, tipo_defecto):
    """Devuelve (tipo, slug, url_base) para perfil/empresa."""
    x = x.strip()
    est = cargar_estado()
    if x.isdigit():
        lista = est.get("lista") or []
        i = int(x)
        if not lista:
            print("No hay ninguna lista guardada. Usa antes: contactos, siguiendo o buscar.")
            sys.exit(1)
        if i < 1 or i > len(lista):
            print("Numero fuera de rango (la ultima lista tiene {} elementos).".format(len(lista)))
            sys.exit(1)
        it = lista[i - 1]
        return it["tipo"], it["slug"], it["url"]
    m = re.search(r"linkedin\.com/(in|company|school|showcase)/([^/?#]+)", x)
    if m:
        return m.group(1), m.group(2), "{}/{}/{}/".format(LI, m.group(1), m.group(2))
    slug = urllib.parse.quote(x.strip("/"))
    return tipo_defecto, slug, "{}/{}/{}/".format(LI, tipo_defecto, slug)


def publicacion_n(n_txt):
    est = cargar_estado()
    pubs = est.get("publicaciones") or []
    if not pubs:
        print("No hay publicaciones guardadas. Usa antes: python linkdApp.py feed")
        sys.exit(1)
    try:
        i = int(n_txt)
    except ValueError:
        print("Indica el numero de la publicacion.")
        sys.exit(1)
    if i < 1 or i > len(pubs):
        print("Numero fuera de rango (hay {} publicaciones guardadas).".format(len(pubs)))
        sys.exit(1)
    return i, pubs[i - 1]


def entero(args, pos, defecto):
    try:
        return int(args[pos])
    except (IndexError, ValueError):
        return defecto


# ----------------------------------------------------------------------------
# Imagenes
# ----------------------------------------------------------------------------

def extension_de(tipo):
    tipo = (tipo or "").lower()
    if "png" in tipo:
        return ".png"
    if "webp" in tipo:
        return ".webp"
    if "gif" in tipo:
        return ".gif"
    return ".jpg"


def bajar(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Android 14; Mobile; rv:141.0) Gecko/141.0 Firefox/141.0",
        "Referer": LI + "/",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read(), r.headers.get("Content-Type", "")


def cmd_img(args, conf):
    est = cargar_estado()
    pubs = est.get("publicaciones") or []
    if not pubs:
        print("No hay publicaciones guardadas. Usa antes: python linkdApp.py feed")
        return
    if not args:
        print("Indica los numeros de publicacion (ej: img 3 5) o 'img todo'.")
        return
    if args[0].lower() in ("todo", "todas", "all"):
        indices = list(range(1, len(pubs) + 1))
    else:
        indices = []
        for a in args:
            for parte in a.split(","):
                if "-" in parte:
                    ini, fin = parte.split("-", 1)
                    if ini.isdigit() and fin.isdigit():
                        indices.extend(range(int(ini), int(fin) + 1))
                elif parte.isdigit():
                    indices.append(int(parte))
    carpeta = os.path.join(DIR, conf["carpeta_imagenes"])
    os.makedirs(carpeta, exist_ok=True)
    pref = est.get("prefijo_publicaciones", "pub")
    pendientes, bajadas = [], 0
    for i in indices:
        if i < 1 or i > len(pubs):
            print("Publicacion {} no existe (hay {}).".format(i, len(pubs)))
            continue
        imgs = pubs[i - 1].get("imagenes") or []
        if not imgs:
            print("La publicacion {} no tiene imagenes.".format(i))
            continue
        for k, u in enumerate(imgs, 1):
            nombre = "{}_p{:02d}_{}".format(pref, i, k)
            try:
                datos, tipo = bajar(u)
                ruta = os.path.join(carpeta, nombre + extension_de(tipo))
                with open(ruta, "wb") as f:
                    f.write(datos)
                bajadas += 1
                print("  + " + os.path.relpath(ruta, DIR))
            except Exception:
                pendientes.append({"url": u, "nombre": nombre})
    if pendientes:
        print("{} imagen(es) necesitan la sesion de Firefox; se las pido a la extension...".format(len(pendientes)))
        res, canal = ejecutar_orden("descargar", LI + "/feed/", conf, extra={"items": pendientes},
                                    coincide=["/feed"], carpeta_img=carpeta)
        bajadas += len(canal.archivos)
        if canal.fallos:
            print("No se pudieron bajar {}. Si el feed es antiguo, los enlaces caducan:".format(len(canal.fallos)))
            print("vuelve a leer el feed y repite.")
    print("Imagenes descargadas: {}  (carpeta {}/)".format(bajadas, conf["carpeta_imagenes"]))


# ----------------------------------------------------------------------------
# Ordenes
# ----------------------------------------------------------------------------

def guardar_publicaciones(res, prefijo, titulo, conf):
    w = ancho_texto(conf)
    pubs = res.get("publicaciones") or []
    if not pubs:
        txt = formatear_bruto(res.get("bruto") or [], w, titulo, res.get("url", ""),
                              "No reconoci publicaciones en la pagina; este es su texto completo. "
                              "Comprueba primero con 'python linkdApp.py prueba' que la extension instalada es la ultima. "
                              "Si se repite, ejecuta 'python linkdApp.py diag {}' y revisa el HTML.".format(res.get("url", "")))
        entregar(txt, prefijo, conf)
        return
    actualizar_estado(publicaciones=pubs, prefijo_publicaciones="{}_{}".format(prefijo, datetime.now().strftime("%Y%m%d_%H%M")))
    txt = formatear_publicaciones(pubs, w, titulo, res.get("url", ""))
    if res.get("aviso"):
        txt += "\n\n" + envolver("AVISO: " + res["aviso"], w)
    entregar(txt, prefijo, conf, (COLUMNAS_PUBS, filas_pubs(pubs)))
    if res.get("aviso"):
        print("AVISO: " + res["aviso"])


def cmd_feed(args, conf):
    n = entero(args, 0, 15)
    enlaces = bool(conf.get("enlaces_menu"))
    res, _ = ejecutar_orden("feed", LI + "/feed/", conf, n=n,
                            extra={"sinPromo": bool(conf.get("sin_promocionados")), "enlaces": enlaces},
                            coincide=["/feed"], tiempo=120 + n * (18 if enlaces else 15))
    guardar_publicaciones(res, "feed", "Feed de LinkedIn", conf)


def cmd_actividad(args, conf):
    if not args:
        print("Uso: python linkdApp.py actividad X [N]")
        return
    tipo, slug, base = resolver(args[0], "in")
    n = entero(args, 1, 10)
    if tipo == "in":
        url, coinc = base + "recent-activity/all/", ["/in/"]
    else:
        url, coinc = base + "posts/", ["/company/", "/school/", "/showcase/"]
    nombre = urllib.parse.unquote(slug)
    est = cargar_estado()
    if args[0].isdigit():
        lista = est.get("lista") or []
        i = int(args[0])
        if 0 < i <= len(lista) and lista[i - 1].get("nombre"):
            nombre = lista[i - 1]["nombre"]
    res, _ = ejecutar_orden("actividad", url, conf, n=n, coincide=coinc, volver=False,
                            extra={"enlaces": bool(conf.get("enlaces_menu")), "sinPromo": True}, tiempo=120 + n * 15)
    if not res.get("publicaciones"):
        # Alternativa: la seccion "Actividad" de la pagina principal del perfil o empresa
        print("No hay publicaciones en la pagina de actividad; pruebo con la pagina principal...")
        res2, _ = ejecutar_orden("actividad", base, conf, n=n, coincide=coinc,
                                 extra={"esperaCorta": True, "sinPromo": True}, tiempo=90 + n * 5)
        if res2.get("publicaciones"):
            res = res2
    else:
        volver_a_termux(conf)
    guardar_publicaciones(res, "actividad_" + limpiar_nombre(slug), "Publicaciones de " + nombre, conf)


def limpiar_nombre(s):
    return re.sub(r"[^\w\-]", "_", urllib.parse.unquote(s))[:40]


def cmd_perfil(args, conf, tipo_defecto="in"):
    if not args:
        print("Uso: python linkdApp.py {} X".format("perfil" if tipo_defecto == "in" else "empresa"))
        return
    tipo, slug, base = resolver(" ".join(args), tipo_defecto)
    w = ancho_texto(conf)
    if tipo == "in":
        res, _ = ejecutar_orden("perfil", base, conf, coincide=["/in/"])
        entregar(formatear_secciones(res, w, "Perfil"), "perfil_" + limpiar_nombre(slug), conf)
    else:
        res, _ = ejecutar_orden("empresa", base + "about/", conf, coincide=["/company/", "/school/", "/showcase/"])
        entregar(formatear_secciones(res, w, "Empresa"), "empresa_" + limpiar_nombre(slug), conf)


def cmd_autor(args, conf):
    if not args:
        print("Uso: python linkdApp.py autor N")
        return
    i, p = publicacion_n(args[0])
    if not p.get("autorUrl"):
        print("No conozco el enlace del autor de la publicacion {}.".format(i))
        return
    cmd_perfil([p["autorUrl"]], conf, "in")


def leer_lista(conf, url, n, tipo, coincide, apartado="", volver=True):
    res, _ = ejecutar_orden("lista", url, conf, n=n, extra={"tipo": tipo}, coincide=coincide, volver=volver,
                            tiempo=90 + n * 2)
    items = res.get("items") or []
    for it in items:
        it["apartado"] = apartado
    return items, res.get("url", "")


def cmd_lista(conf, url, n, tipo, titulo, prefijo, coincide, apartado=""):
    items, origen = leer_lista(conf, url, n, tipo, coincide, apartado)
    actualizar_estado(lista=items)
    entregar(formatear_lista(items, ancho_texto(conf), titulo, origen), prefijo, conf,
             (COLUMNAS_LISTA, filas_lista(items)))


def cmd_siguiendo(args, conf):
    """Personas que sigo (fuera de mi red) y empresas que sigo, en dos apartados."""
    n = entero(args, 0, 100)
    print("1/2  Personas que sigues...")
    personas, _ = leer_lista(conf, LI + "/mynetwork/network-manager/people-follow/following/", n, "in",
                             ["/mynetwork"], "Personas que sigues", volver=False)
    print("2/2  Empresas que sigues...")
    empresas, _ = leer_lista(conf, LI + "/mynetwork/network-manager/company/", n, "company",
                             ["/mynetwork"], "Empresas que sigues")
    items = personas + empresas
    actualizar_estado(lista=items)
    entregar(formatear_lista(items, ancho_texto(conf), "Personas y empresas que sigo", ""), "siguiendo", conf,
             (COLUMNAS_LISTA, filas_lista(items)))


COLUMNAS_NOTIF = ["n", "leida", "hace", "tipo", "quien", "quien_url", "texto", "detalle", "enlace"]


def formatear_notificaciones(notifs, w, url):
    out = [cabecera_doc("Notificaciones de LinkedIn", url, w), ""]
    nl = sum(1 for x in notifs if not x.get("leida"))
    out.append("Notificaciones: {}   Sin leer: {}".format(len(notifs), nl))
    out.append("")
    for i, x in enumerate(notifs, 1):
        marca = "*NUEVA* " if not x.get("leida") else ""
        cab = "{:>3}. {}[{}]".format(i, marca, x.get("tipo", ""))
        hace = x.get("tiempo", "")
        if hace:
            cab = cab.ljust(max(len(cab) + 1, w - len(hace) - 1)) + hace
        out.append(cab if len(cab) <= w else envolver(cab, w))
        out.append(envolver(x.get("texto", ""), w, "      "))
        if x.get("detalle"):
            out.append(envolver("(" + x["detalle"] + ")", w, "      "))
        if x.get("enlace"):
            out.append(envolver(x["enlace"], w, "      "))
        out.append("")
    out.append("-" * w)
    out.append(envolver("Abrir una en Firefox: python linkdApp.py abrir URL", w))
    return "\n".join(out)


def cmd_notificaciones(args, conf):
    n = entero(args, 0, 30)
    res, _ = ejecutar_orden("notificaciones", LI + "/notifications/?filter=all", conf, n=n,
                            coincide=["/notifications"], tiempo=90 + n * 3)
    notifs = res.get("notificaciones") or []
    w = ancho_texto(conf)
    if not notifs:
        entregar(formatear_bruto(res.get("bruto") or [], w, "Notificaciones de LinkedIn", res.get("url", ""),
                                 "No reconoci las notificaciones; este es el texto de la pagina."), "notificaciones", conf)
        return
    filas = [[i, "si" if x.get("leida") else "no", x.get("tiempo", ""), x.get("tipo", ""), x.get("actor", ""),
              x.get("actorUrl", ""), x.get("texto", ""), x.get("detalle", ""), x.get("enlace", "")]
             for i, x in enumerate(notifs, 1)]
    entregar(formatear_notificaciones(notifs, w, res.get("url", "")), "notificaciones", conf, (COLUMNAS_NOTIF, filas))


def interpretar_busqueda(args):
    """Convierte lo escrito en Termux en la busqueda de LinkedIn.
        palabra                 -> palabra
        palabra1+palabra2       -> palabra1 AND palabra2   (las dos palabras)
        "un texto" (con comillas en Termux llega como un solo argumento con espacios)
                                -> "un texto"              (texto exacto)
    Devuelve (consulta_linkedin, descripcion, [palabras para comprobar], exacto) y N."""
    args = list(args)
    estricto = False
    if "-e" in args:
        args.remove("-e")
        estricto = True
    n = None
    if len(args) > 1 and re.fullmatch(r"\d+", args[-1]):
        n = int(args.pop())
    if not args:
        return None
    texto = " ".join(args).strip()
    if len(args) > 1:                      # varias palabras sin comillas: se toman como texto exacto
        texto = '"' + texto.strip('"') + '"'
    if texto.startswith('"') and texto.endswith('"') and len(texto) > 2:
        frase = texto.strip('"').strip()
        return ('"{}"'.format(frase), 'texto exacto "{}"'.format(frase), [frase], True, n, estricto)
    if "+" in texto:
        palabras = [p.strip() for p in texto.split("+") if p.strip()]
        return (" AND ".join(palabras), "las palabras " + " + ".join(palabras), palabras, False, n, estricto)
    if " " in texto:                       # un argumento con espacios = venia entre comillas
        return ('"{}"'.format(texto), 'texto exacto "{}"'.format(texto), [texto], True, n, estricto)
    return (texto, "la palabra " + texto, [texto], False, n, estricto)


def contiene(texto, palabras):
    t = (texto or "").lower()
    return all(p.lower() in t for p in palabras)


URLS_BUSQUEDA = {
    "todo": "/search/results/all/",
    "personas": "/search/results/people/",
    "empresas": "/search/results/companies/",
    "publicaciones": "/search/results/content/",
}


def cmd_buscar(args, conf, modo):
    b = interpretar_busqueda(args)
    if not b:
        print('Uso: python linkdApp.py buscar palabra | palabra1+palabra2 | "un texto"  [N] [-e]')
        return
    consulta, descripcion, palabras, exacto, n, estricto = b
    url = LI + URLS_BUSQUEDA[modo] + "?keywords=" + urllib.parse.quote(consulta) + "&origin=GLOBAL_SEARCH_HEADER"
    nombre = "buscar_{}_{}".format(modo, limpiar_nombre(" ".join(palabras).replace(" ", "_")))
    titulo = "Busqueda en LinkedIn ({}): {}".format(modo, descripcion)
    print("Buscando " + descripcion + "  ->  LinkedIn: " + consulta)
    w = ancho_texto(conf)
    if modo in ("personas", "empresas"):
        cmd_lista(conf, url, n or 20, "in" if modo == "personas" else "company", titulo, nombre, ["/search/"],
                  "Personas" if modo == "personas" else "Empresas")
        return
    if modo == "publicaciones":
        res, _ = ejecutar_orden("actividad", url, conf, n=n or 30, coincide=["/search/"],
                                extra={"sinPromo": True}, tiempo=120 + (n or 30) * 15)
        pubs = res.get("publicaciones") or []
        if estricto and pubs:
            antes = len(pubs)
            res["publicaciones"] = [p for p in pubs if contiene(p.get("texto", "") + " " + p.get("autor", ""), palabras)]
            print("Filtro estricto (-e): quedan {} de {} publicaciones.".format(len(res["publicaciones"]), antes))
            if not res["publicaciones"]:
                entregar(cabecera_doc(titulo, url_limpia(url), w) + "\n\n" + envolver(
                    "Ninguna de las {} publicaciones que devolvio LinkedIn contiene {} en su texto. "
                    "Repite sin -e para verlas todas.".format(antes, descripcion), w), nombre, conf)
                return
        guardar_publicaciones(res, nombre, titulo, conf)
        return
    # todo: personas, empresas y publicaciones de la pagina "Todo"
    res, _ = ejecutar_orden("busqueda", url, conf, n=n or 30, coincide=["/search/"], tiempo=150 + (n or 30) * 15)
    items = res.get("items") or []
    pubs = res.get("publicaciones") or []
    if estricto:
        pubs = [p for p in pubs if contiene(p.get("texto", "") + " " + p.get("autor", ""), palabras)]
    for it in items:
        it["apartado"] = "Personas" if it.get("tipo") == "in" else "Empresas y paginas"
    items.sort(key=lambda x: 0 if x.get("tipo") == "in" else 1)
    if not items and not pubs:
        entregar(formatear_bruto(res.get("bruto") or [], w, titulo, res.get("url", ""),
                                 "No reconoci resultados; este es el texto de la pagina. Prueba buscar-personas, "
                                 "buscar-empresa o buscar-pub."), nombre, conf)
        return
    actualizar_estado(lista=items)
    partes = [formatear_lista(items, w, titulo, res.get("url", ""))] if items else [cabecera_doc(titulo, res.get("url", ""), w)]
    if pubs:
        actualizar_estado(publicaciones=pubs, prefijo_publicaciones="{}_{}".format(nombre, datetime.now().strftime("%Y%m%d_%H%M")))
        partes.append("")
        partes.append((" PUBLICACIONES ({}) ".format(len(pubs))).center(w, "="))
        fp = formatear_publicaciones(pubs, w, "", "").split("\n")
        cortes = [i for i, l in enumerate(fp) if l == "=" * w]
        partes.append("\n".join(fp[cortes[1] + 2:] if len(cortes) > 1 else fp))
    base = entregar("\n".join(partes), nombre, conf, (COLUMNAS_LISTA, filas_lista(items)) if items else None)
    if pubs and conf.get("csv", True):
        rcsv = base[:-4] + "_publicaciones.csv"
        escribir_csv(rcsv, COLUMNAS_PUBS, filas_pubs(pubs), conf)
        print("CSV:         " + os.path.basename(rcsv))
    print("Siguiente paso: actividad N / perfil N (lista)   |   post N / img N / abrir N (publicaciones)")


def cmd_sql(args, conf):
    """Consulta SQL sobre los CSV de linkdApp sin programas externos (SQLite de Python)."""
    import sqlite3
    import glob
    args = list(args)
    salida_csv = "--csv" in args
    completo = "--completo" in args
    args = [a for a in args if a not in ("--csv", "--completo")]
    consulta = ""
    if args and not args[0].lower().endswith(".csv"):
        consulta = args.pop(0)
    archivos = []
    for a in args:
        archivos.extend(sorted(glob.glob(a)) or [a])
    if not archivos:
        recientes = sorted(glob.glob(os.path.join(DIR, "*.csv")), key=os.path.getmtime, reverse=True)
        if not recientes:
            print("No hay archivos .csv en esta carpeta.")
            return
        archivos = [recientes[0]]
    sep = SEPARADORES.get(str(conf.get("separador_csv", "tab")).lower(), "\t")
    db = sqlite3.connect(":memory:")
    tablas = []
    for i, ruta in enumerate(archivos):
        if not os.path.exists(ruta):
            print("No existe: " + ruta)
            return
        with open(ruta, encoding="utf-8-sig", newline="") as f:
            muestra = f.read(4096)
            f.seek(0)
            d = sep if sep in muestra.split("\n")[0] else ("\t" if "\t" in muestra.split("\n")[0] else ",")
            filas = list(csv.reader(f, delimiter=d))
        if not filas:
            continue
        cab = [c.strip() or "col{}".format(k + 1) for k, c in enumerate(filas[0])]
        base = re.sub(r"[^a-z0-9_]", "_", os.path.basename(ruta).split("_")[0].lower()) or "t"
        nombre = base
        k = 2
        while nombre in [t[0] for t in tablas]:
            nombre = "{}_{}".format(base, k)
            k += 1
        numericas = {"n", "num_imagenes", "comentarios_visibles"}
        cols = ", ".join('"{}" {}'.format(c.replace('"', ""), "INTEGER" if c in numericas else "TEXT") for c in cab)
        db.execute('CREATE TABLE "{}" ({})'.format(nombre, cols))
        datos = [(r + [""] * len(cab))[:len(cab)] for r in filas[1:]]
        db.executemany('INSERT INTO "{}" VALUES ({})'.format(nombre, ",".join("?" * len(cab))), datos)
        if i == 0:
            db.execute('CREATE VIEW t AS SELECT * FROM "{}"'.format(nombre))
        tablas.append((nombre, ruta, cab, len(datos)))
    if not consulta:
        print("Tablas cargadas (la primera tambien se llama t):")
        for nombre, ruta, cab, n in tablas:
            print("  {}  ({} filas)  <- {}".format(nombre, n, os.path.basename(ruta)))
            print(envolver("columnas: " + ", ".join(cab), ancho_texto(conf), "      "))
        print('\nEjemplo:  python linkdApp.py sql "select n, autor, fecha from t" ARCHIVO.csv')
        return
    try:
        cur = db.execute(consulta)
    except sqlite3.Error as e:
        print("Error en la consulta SQL: {}".format(e))
        print("Tablas disponibles: " + ", ".join(["t"] + [t[0] for t in tablas]))
        return
    cols = [c[0] for c in cur.description] if cur.description else []
    res = cur.fetchall()
    if salida_csv:
        w = csv.writer(sys.stdout, delimiter=sep, lineterminator="\n")
        w.writerow(cols)
        w.writerows(res)
        return
    w = ancho_texto(conf)
    for k, fila in enumerate(res, 1):
        print("-" * w)
        ancho_etq = min(22, max([len(c) for c in cols] + [4]) + 1)
        for c, v in zip(cols, fila):
            v = "" if v is None else str(v)
            if not completo:
                v = " ".join(v.split())
                if len(v) > 300:
                    v = v[:300] + "..."
            pref = (c + ":").ljust(ancho_etq) + " "
            lineas = v.split("\n") if completo else [v]
            primera = True
            for l in lineas:
                print(textwrap.fill(l or " ", width=w, initial_indent=pref if primera else " " * len(pref),
                                    subsequent_indent=" " * len(pref), break_long_words=True))
                primera = False
    print("-" * w)
    print("{} fila(s).  Tablas: {}".format(len(res), ", ".join(["t"] + [t[0] for t in tablas])))


def cmd_mensajes(args, conf):
    n = entero(args, 0, 20)
    res, _ = ejecutar_orden("mensajes", LI + "/messaging/", conf, n=n, coincide=["/messaging"])
    convs = res.get("conversaciones") or []
    w = ancho_texto(conf)
    if not convs:
        entregar(formatear_bruto(res.get("bruto") or [], w, "Mensajes de LinkedIn", res.get("url", ""),
                                 "No reconoci la lista de conversaciones; este es el texto de la pagina."),
                 "mensajes", conf)
        return
    actualizar_estado(conversaciones=convs)
    entregar(formatear_conversaciones(convs, w, res.get("url", "")), "mensajes", conf)


def cmd_chat(args, conf):
    if not args or not args[0].isdigit():
        print("Uso: python linkdApp.py chat N [A]")
        return
    est = cargar_estado()
    convs = est.get("conversaciones") or []
    i = int(args[0])
    if not convs:
        print("Primero lista las conversaciones: python linkdApp.py mensajes")
        return
    if i < 1 or i > len(convs):
        print("Numero fuera de rango (hay {} conversaciones).".format(len(convs)))
        return
    c = convs[i - 1]
    extra = {"anteriores": entero(args, 1, 2)}
    if c.get("url"):
        url = c["url"]
    else:
        url = LI + "/messaging/"
        extra["indice"] = c.get("indice", i - 1)
    res, _ = ejecutar_orden("hilo", url, conf, extra=extra, coincide=["/messaging"])
    w = ancho_texto(conf)
    nombre = "chat_" + limpiar_nombre(c.get("nombre") or str(i))
    if not res.get("mensajes"):
        entregar(formatear_bruto(res.get("bruto") or [], w, "Conversacion: " + (res.get("titulo") or ""),
                                 res.get("url", ""), "No reconoci los mensajes uno a uno; este es el texto de la conversacion."),
                 nombre, conf)
        return
    entregar(formatear_hilo(res, w), nombre, conf)


def cmd_post(args, conf):
    if not args:
        print("Uso: python linkdApp.py post N")
        return
    i, p = publicacion_n(args[0])
    firma = p.get("firma") or {"autor": p.get("autor", ""), "inicio": " ".join(str(p.get("texto", "")).split())[:80]}
    veces = entero(args, 1, 3)
    if p.get("url"):
        res, _ = ejecutar_orden("post", p["url"], conf, n=veces, extra={"firma": firma},
                                coincide=["/feed/update", "/posts/"], tiempo=90 + veces * 5)
    else:
        print("La publicacion {} no tiene enlace propio; la busco en el feed y abro sus comentarios.".format(i))
        res, _ = ejecutar_orden("post", LI + "/feed/", conf, n=veces, extra={"firma": firma},
                                coincide=["/feed"], tiempo=180 + veces * 5)
    entregar(formatear_post(res, ancho_texto(conf), i), "post_{:02d}".format(i), conf)
    pub = res.get("publicacion") or {}
    if pub.get("url") and not p.get("url"):
        est = cargar_estado()
        pubs = est.get("publicaciones") or []
        if 0 < i <= len(pubs):
            pubs[i - 1]["url"] = pub["url"]
            actualizar_estado(publicaciones=pubs)


def cmd_abrir(args, conf):
    if not args:
        print("Uso: python linkdApp.py abrir N  (o una URL)")
        return
    if args[0].startswith("http"):
        url = args[0]
    else:
        i, p = publicacion_n(args[0])
        url = p.get("url")
        if not url:
            print("No conozco el enlace de la publicacion {}. Prueba 'python linkdApp.py post {}'".format(i, i))
            print("(al abrir sus comentarios se obtiene el enlace) o abre el perfil: python linkdApp.py autor {}".format(i))
            return
    if url:
        abrir_en_firefox(url, conf)
        print("Abierto en Firefox: " + url)


def cmd_pagina(args, conf, diag=False):
    if not args:
        print("Uso: python linkdApp.py {} URL".format("diag" if diag else "pagina"))
        return
    url = args[0]
    if not url.startswith("http"):
        url = LI + "/" + url.lstrip("/")
    ruta = urllib.parse.urlsplit(url).path or "/"
    coinc = ["/" + ruta.strip("/").split("/")[0]] if ruta.strip("/") else ["/"]
    if diag:
        res, _ = ejecutar_orden("diag", url, conf, coincide=coinc)
        nombre = "diag_{}.html".format(datetime.now().strftime("%Y%m%d_%H%M%S"))
        with open(os.path.join(DIR, nombre), "w", encoding="utf-8") as f:
            f.write(res.get("html", ""))
        print("HTML guardado en: {}  ({} KB)".format(nombre, len(res.get("html", "")) // 1024))
        print("Navegador: " + res.get("ua", ""))
        return
    res, _ = ejecutar_orden("pagina", url, conf, coincide=coinc)
    entregar(formatear_secciones(res, ancho_texto(conf), "Pagina"), "pagina", conf)


def cmd_prueba(conf):
    print("Prueba de comunicacion Termux <-> Firefox (extension linkdApp)")
    res, canal = ejecutar_orden("ping", LI + "/feed/", conf, coincide=["/feed"])
    print()
    print("  Conexion con la extension : OK")
    print("  Version de la extension   : {}{}".format(canal.version_ext or "desconocida (antigua)",
          "  <- ANTIGUA, actualizala" if version_menor(canal.version_ext, VERSION_EXT_MINIMA) else ""))
    print("  Pagina                    : " + str(res.get("titulo", "")))
    print("  Sesion de LinkedIn        : " + ("parece iniciada" if res.get("sesion") else "NO detectada"))
    print("  Version escritorio        : " + ("si" if "Android" not in res.get("ua", "") and "Mobile" not in res.get("ua", "") else "no (movil)"))
    print("  Ancho de la pagina        : {} px".format(res.get("ancho", "?")))


def cmd_config(args, conf):
    if len(args) >= 2:
        clave, valor = args[0], " ".join(args[1:])
        if clave not in CONF_DEFECTO:
            print("Opcion desconocida. Opciones: " + ", ".join(CONF_DEFECTO))
            return
        base = CONF_DEFECTO[clave]
        if isinstance(base, bool):
            v = valor.lower() in ("si", "sí", "s", "true", "1", "yes", "on")
        elif isinstance(base, int):
            try:
                v = int(valor)
            except ValueError:
                print("Esa opcion necesita un numero.")
                return
        else:
            v = valor
        conf[clave] = v
        guardar_json(ARCH_CONF, conf)
        print("{} = {}".format(clave, v))
        return
    print("Configuracion (linkdApp_config.json en {}):".format(DIR))
    for k in CONF_DEFECTO:
        v = conf.get(k)
        if isinstance(v, bool):
            v = "si" if v else "no"
        print("  {:<20} {}".format(k, v))


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("ayuda", "help", "-h", "--help"):
        print(AYUDA)
        return
    conf = cargar_conf()
    orden, resto = args[0].lower(), args[1:]
    registrar("=== linkdApp {} :: {}".format(VERSION, " ".join(args)))

    if orden == "feed":
        cmd_feed(resto, conf)
    elif orden in ("img", "imagenes", "imágenes"):
        cmd_img(resto, conf)
    elif orden == "post":
        cmd_post(resto, conf)
    elif orden == "abrir":
        cmd_abrir(resto, conf)
    elif orden == "autor":
        cmd_autor(resto, conf)
    elif orden == "perfil":
        cmd_perfil(resto, conf, "in")
    elif orden == "empresa":
        cmd_perfil(resto, conf, "company")
    elif orden == "actividad":
        cmd_actividad(resto, conf)
    elif orden == "contactos":
        cmd_lista(conf, LI + "/mynetwork/invite-connect/connections/", entero(resto, 0, 100), "in",
                  "Mis contactos de LinkedIn", "contactos", ["/mynetwork"], "Contactos")
    elif orden == "siguiendo":
        cmd_siguiendo(resto, conf)
    elif orden in ("buscar", "buscar-todo"):
        cmd_buscar(resto, conf, "todo")
    elif orden in ("buscar-personas", "buscar-persona"):
        cmd_buscar(resto, conf, "personas")
    elif orden in ("buscar-empresa", "buscar-empresas", "buscar_empresa", "buscarempresa"):
        cmd_buscar(resto, conf, "empresas")
    elif orden in ("buscar-pub", "buscar-publicaciones", "buscar-posts"):
        cmd_buscar(resto, conf, "publicaciones")
    elif orden == "mensajes":
        cmd_mensajes(resto, conf)
    elif orden in ("notificaciones", "notif"):
        cmd_notificaciones(resto, conf)
    elif orden == "chat":
        cmd_chat(resto, conf)
    elif orden == "sql":
        cmd_sql(resto, conf)
    elif orden == "pagina":
        cmd_pagina(resto, conf)
    elif orden == "diag":
        cmd_pagina(resto, conf, diag=True)
    elif orden == "prueba":
        cmd_prueba(conf)
    elif orden == "config":
        cmd_config(resto, conf)
    elif orden in ("version", "--version"):
        print("linkdApp.py " + VERSION)
    else:
        print("Orden desconocida: {}\nEscribe: python linkdApp.py ayuda".format(orden))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("\nCancelado.")
    except Exception:
        import traceback
        detalle = traceback.format_exc()
        registrar("FALLO INTERNO:\n" + detalle)
        print("Fallo interno de linkdApp.py. Detalle guardado en linkdApp_log.txt:")
        print(detalle)
