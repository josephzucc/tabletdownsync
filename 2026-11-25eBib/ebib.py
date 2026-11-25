#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ebib.py v2 - Consulta de cuentas eBiblio desde Termux.
Plataforma actual de eBiblio: De Marque / Cantook (web con usuario y contrasena).

Ejemplos:
  python3 ebib.py -acc madrid -sondeo            (diagnostico con sesion iniciada)
  python3 ebib.py -acc madrid -verhistorial
  python3 ebib.py -acc todas -verhistorial -csv
  python3 ebib.py -acc todas -verreservas
  python3 ebib.py -acc madrid -verprestamos
  python3 ebib.py -acc todas -resumen
  python3 ebib.py -acc todas -vigilar
  python3 ebib.py -acc madrid -verhistorial -debug

Configuracion: ~/.config/ebib/cuentas.json (se crea la plantilla la primera vez).
Requisitos: pip install requests beautifulsoup4
"""

import argparse
import csv
import getpass
import json
import os
import re
import stat
import subprocess
import sys
import time
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse, quote_plus, quote

try:
    import requests
except ImportError:
    sys.exit("Falta la libreria 'requests'. Instala con:  pip install requests")
try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Falta la libreria 'beautifulsoup4'. Instala con:  pip install beautifulsoup4")

VERSION = "2.36"
CONFIG_DIR = os.path.expanduser("~/.config/ebib")
CONFIG_FILE = os.path.join(CONFIG_DIR, "cuentas.json")
ESTADO_FILE = os.path.join(CONFIG_DIR, "estado_reservas.json")

PLANTILLA = {
    "madrid": {"host": "madrid.ebiblio.es",
               "login_url": "https://madrid.ebiblio.es/ebiblio_auth_sign_in",
               "usuario": "", "password": ""},
    "castillaleon": {"host": "castillayleon.ebiblio.es", "login_url": "",
                     "usuario": "", "password": ""},
}

UA = ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36")

DEBUG = False
FILTRO_PRENSA = ""  # -p: solo periodicos cuyo nombre contenga este texto
LIMITE = None   # -n: numero maximo de libros del historial (None = todos)

# Palabras clave para encontrar cada seccion en los enlaces de la cuenta,
# y rutas a probar si no aparece ningun enlace.
SECCIONES = {
    "prestamos": {
        "claves": r"my_profile/activity|loan|prest|checkout|bookshelf|estanter",
        "excluir": r"histor|past|confirm|return|devol",
        "rutas": ["/my_profile/activity", "/dashboard/loans", "/loans", "/dashboard"],
    },
    "reservas": {
        "claves": r"my_profile/activity|hold|reserv|booking|waiting|espera",
        "excluir": r"histor|past",
        "rutas": ["/my_profile/activity", "/dashboard/holds", "/holds", "/dashboard/bookings", "/bookings",
                  "/dashboard"],
    },
    "historial": {
        "claves": r"histor|past_loans|returned|anteriores|lectura",
        "excluir": r"__nada__",
        "rutas": ["/my_profile/history", "/my_profile/loans_history", "/my_profile/reading_history",
                  "/dashboard/history", "/history", "/loans/history", "/dashboard/loans/history",
                  "/dashboard/loans_history", "/loan_history", "/reading_history",
                  "/dashboard/reading_history", "/dashboard/past_loans", "/past_loans"],
    },
    "favoritos": {
        "claves": r"favor|wish|deseo|lists?\b",
        "excluir": r"histor",
        "rutas": ["/dashboard/favorites", "/favorites", "/dashboard/wishlist", "/wishlist",
                  "/dashboard/lists", "/lists"],
    },
}

# Prensa a vigilar por defecto (se puede cambiar con "prensa" en cuentas.json)
PRENSA_DEFECTO = {
    "madrid.ebiblio.es": {
        "El Mundo": "https://madrid.ebiblio.es/resources?nature=newspaper&serial_name=El+Mundo+%28Ed.+Nacional%29",
        "El Pais": "https://madrid.ebiblio.es/resources?nature=newspaper&serial_name=El+Pa%C3%ADs%2C+edici%C3%B3n+Madrid",
        "Cinco Dias": "https://madrid.ebiblio.es/resources?nature=newspaper&serial_name=Cinco+D%C3%ADas",
    },
    "castillayleon.ebiblio.es": {
        "Diario de Avila": "https://castillayleon.ebiblio.es/resources?nature=newspaper&serial_name=Diario+de+%C3%81vila",
        "Cinco Dias": "https://castillayleon.ebiblio.es/resources?nature=newspaper&serial_name=Cinco+D%C3%ADas",
    },
}

MESES = {"ene": 1, "enero": 1, "feb": 2, "febrero": 2, "mar": 3, "marzo": 3, "abr": 4, "abril": 4,
         "may": 5, "mayo": 5, "jun": 6, "junio": 6, "jul": 7, "julio": 7, "ago": 8, "agosto": 8,
         "sep": 9, "sept": 9, "septiembre": 9, "set": 9, "oct": 10, "octubre": 10,
         "nov": 11, "noviembre": 11, "dic": 12, "diciembre": 12,
         "jan": 1, "apr": 4, "aug": 8, "dec": 12}

RE_FECHA = re.compile(
    r"(\d{1,2})\s+(?:de\s+)?([a-záéíóú]{3,10})\.?\s+(?:de\s+)?(\d{4})"      # 1 dic 2026 / 1 de diciembre de 2026
    r"|(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})"                                 # 01/12/2026
    r"|(\d{4})-(\d{2})-(\d{2})", re.I)                                      # 2026-12-01
RE_HORA = re.compile(r"(\d{1,2}):(\d{2})")
RE_POSICION = re.compile(
    r"(?:posici[oó]n|puesto|lugar|n[uú]mero|orden)\D{0,45}?(\d+)"
    r"|(\d+)\s*(?:º|ª|o\b|a\b)?\s*(?:en la (?:lista|cola)|de la (?:lista|cola))", re.I)
# Textos de la web cuando TU reserva ya esta disponible (tienes N minutos para pedirla)
RE_TU_TURNO = re.compile(r"para recuperar tu pr[eé]stamo|haga clic sobre .{0,3}prestar|"
                         r"disponibles? para (?:el |su )?pr[eé]stamo|reservas? disponibles? para su pr[eé]stamo", re.I)
RE_DISPONIBLE_YA = re.compile(
    r"ya (?:est[aá] )?disponible|disponible para (?:ti|tomar|pr[eé]stamo)|puedes (?:tomarlo|tomar|pedir)"
    r"|lista para (?:tomar|pr[eé]stamo)|reserva disponible|est[aá] disponible hasta", re.I)


# ----------------------------------------------------------------- utilidades

def log_debug(titulo, texto=""):
    if DEBUG:
        print("\n----- DEBUG: %s -----" % titulo)
        if texto:
            print(str(texto)[:4000])
        print("----- fin DEBUG -----")


def limpiar(t):
    return re.sub(r"\s+", " ", t or "").strip()


def recortar(texto, n):
    texto = str(texto if texto is not None else "")
    return texto if len(texto) <= n else texto[: n - 1] + "…"


def parse_fechas(texto):
    """Devuelve lista de (datetime, texto_original) encontrados en el texto."""
    res = []
    for m in RE_FECHA.finditer(texto):
        try:
            if m.group(1):
                mes = MESES.get(m.group(2).lower().rstrip("."))
                if not mes:
                    continue
                d = datetime(int(m.group(3)), mes, int(m.group(1)))
            elif m.group(4):
                d = datetime(int(m.group(6)), int(m.group(5)), int(m.group(4)))
            else:
                d = datetime(int(m.group(7)), int(m.group(8)), int(m.group(9)))
        except ValueError:
            continue
        resto = texto[m.end(): m.end() + 25]
        h = RE_HORA.search(resto)
        if h:
            try:
                d = d.replace(hour=int(h.group(1)), minute=int(h.group(2)))
            except ValueError:
                pass
        res.append((d, m.group(0), m.start()))
    return res


RE_REL = re.compile(r"(?:dentro de|en|quedan?|faltan?|tienes|dispones de)[\s|]+(\d+)[\s|]*"
                    r"(minutos?|mins?|horas?|h\b|d[ií]as?)"
                    r"(?:[\s|]*y[\s|]*(\d+)[\s|]*(?:minutos?|mins?))?", re.I)


def tiempo_libre(texto):
    """Momento en que se libera: el mas cercano de los que indique el texto
    ("Disponible el 27 sep 2026 a las 10:21", "dentro de 12 minutos", "en 2 horas y 5 min")."""
    ahora = datetime.now()
    cands = []
    for k in re.finditer(r"disponib|estimad|aprox|libera|devoluci|vuelve|te toca|turno", texto, re.I):
        for d, _, _ in parse_fechas(texto[k.start():k.start() + 90]):
            if d > ahora - timedelta(minutes=1):
                cands.append(d)
            break
    for m in RE_REL.finditer(texto):
        n, u = int(m.group(1)), m.group(2).lower()
        mins = n if u.startswith("min") else n * 60 if u.startswith("h") else n * 1440
        if m.group(3):
            mins += int(m.group(3))
        cands.append(ahora + timedelta(minutes=mins))
    return min(cands) if cands else None


def fecha_cerca(texto, patron):
    """Primera fecha que aparece despues de una palabra clave (patron regex)."""
    for k in re.finditer(patron, texto, re.I):
        for d, _, pos in parse_fechas(texto[k.start():k.start() + 80]):
            return d
    return None


def fmt(d, hora=False):
    if not d:
        return ""
    if hora and (d.hour or d.minute):
        return d.strftime("%d/%m/%Y %H:%M")
    return d.strftime("%d/%m/%Y")


ANCHO_PANTALLA = None   # -ancho N fija el ancho; si no, el de la pantalla de Termux


def tabla_fija(filas, columnas):
    cols = list(columnas)
    anchos = [w for _, _, w in cols]
    limite = ancho_pantalla() - 1
    sobra = sum(anchos) + 2 * (len(anchos) - 1) - limite
    if sobra > 0:   # 1) acortar solo el titulo (hasta 12)
        for i, (k, _, _) in enumerate(cols):
            if k == "titulo":
                quita = min(sobra, anchos[i] - 12)
                anchos[i] -= quita
                sobra -= quita
    while sobra > 0:   # 2) pantalla muy estrecha: estrechar un poco las demas, sin quitar ninguna
        i = max(range(len(anchos)), key=lambda j: anchos[j] - (12 if cols[j][0] == "titulo" else 5))
        if anchos[i] <= (12 if cols[i][0] == "titulo" else 5):
            break
        anchos[i] -= 1
        sobra -= 1
    linea = lambda vals: "  ".join(recortar(v, a).ljust(a) for v, a in zip(vals, anchos))
    print(linea([t for _, t, _ in cols]).rstrip())
    print("-" * min(sum(anchos) + 2 * (len(anchos) - 1), limite))
    for f in filas:
        print(linea([str(f.get(k, "") if f.get(k, "") is not None else "") for k, _, _ in cols]).rstrip())
    print("\nTotal: %d" % len(filas))


def ancho_pantalla():
    """Ancho real de la pantalla de Termux, aunque la salida vaya a una tuberia
    (python3 ebib.py ... | while read ...): se mira tambien la pantalla de stderr, stdin o /dev/tty."""
    if ANCHO_PANTALLA:
        return ANCHO_PANTALLA
    if os.environ.get("COLUMNS", "").isdigit():
        return max(40, int(os.environ["COLUMNS"]))
    for fd in (1, 2, 0):
        try:
            return max(40, os.get_terminal_size(fd).columns)
        except OSError:
            pass
    try:
        with open("/dev/tty") as tty:
            return max(40, os.get_terminal_size(tty.fileno()).columns)
    except OSError:
        pass
    return 120          # sin pantalla (cron, archivo): ancho generoso


def tabla(filas, columnas, fijas=False):
    """Tabla que cabe SIEMPRE en una linea de la pantalla: quita columnas vacias y acorta
    las mas anchas (primero titulo/autor) hasta que quepa. Lo que no cabe acaba en '…'.
    Con fijas=True (prestamos y reservas): SIEMPRE las mismas columnas, en el mismo orden y con el
    mismo ancho, haya o no datos; si no cabe, solo se acorta el titulo."""
    # en pantalla, una fecha-hora de HOY se muestra solo con la hora (13:21); el CSV la guarda completa
    hoy_txt = datetime.now().strftime("%d/%m/%Y") + " "
    filas = [{k: (v[len(hoy_txt):] if isinstance(v, str) and v.startswith(hoy_txt)
                  and re.fullmatch(r"\d{1,2}:\d{2}", v[len(hoy_txt):]) else v)
              for k, v in f.items()} for f in filas]
    if fijas:
        return tabla_fija(filas, columnas)
    cols = [c for c in columnas if c[0] in ("cuenta", "titulo", "n") or any(str(f.get(c[0], "")).strip() for f in filas)]
    if not cols:
        cols = list(columnas)
    anchos = []
    for k, t, w in cols:   # no mas ancho del necesario
        maxdato = max([len(str(f.get(k, ""))) for f in filas] + [len(t)])
        anchos.append(max(3, min(w, maxdato)))
    limite = ancho_pantalla() - 1

    def total():
        return sum(anchos) + 2 * (len(anchos) - 1)
    # 1) acortar titulo/autor hasta 15; 2) quitar columnas menos importantes; 3) acortar todo
    for i, (k, _, _) in enumerate(cols):
        if k in ("titulo", "autor", "detalle") and total() > limite:
            anchos[i] = max(min(anchos[i], 15), anchos[i] - (total() - limite))
    for quitar in ("origen", "reservado", "inicio", "autor", "detalle", "posicion", "disponible", "libre", "libre_a",
                   "formato", "tipo", "anio", "id", "edicion", "hoy", "periodicidad", "accion"):
        if total() <= limite:
            break
        for i, (k, _, _) in enumerate(cols):
            if k == quitar:
                del cols[i]
                del anchos[i]
                break
    minimos = [max(3, min(6, a)) if k not in ("titulo", "autor", "detalle") else min(a, 12)
               for (k, _, _), a in zip(cols, anchos)]
    while sum(anchos) + 2 * (len(anchos) - 1) > limite:
        cand = [i for i in range(len(anchos)) if anchos[i] > minimos[i]]
        if not cand:
            break
        prio = [i for i in cand if cols[i][0] in ("titulo", "autor", "detalle")] or cand
        i = max(prio, key=lambda j: anchos[j])
        anchos[i] -= 1
    cab = "  ".join(recortar(t, a).ljust(a) for (_, t, _), a in zip(cols, anchos)).rstrip()
    print(cab[:limite])
    print("-" * min(len(cab), limite))
    for f in filas:
        print("  ".join(recortar(f.get(k, ""), a).ljust(a) for (k, _, _), a in zip(cols, anchos)).rstrip()[:limite])
    print("\nTotal: %d" % len(filas))


def guardar_csv(filas, columnas, ruta):
    with open(ruta, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow([t for _, t, _ in columnas])
        for f in filas:
            w.writerow([f.get(k, "") for k, _, _ in columnas])
    print("CSV guardado en: %s" % os.path.abspath(ruta))


def notificar(titulo, texto):
    print("*** %s: %s" % (titulo, texto))
    try:
        subprocess.run(["termux-notification", "--title", titulo, "--content", texto],
                       timeout=10, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


# -------------------------------------------------------------- configuracion

AVISOS_CONFIG = []


def juntar_repetidos(pares):
    """Si un nombre se repite en cuentas.json (p. ej. "pais" una vez con la direccion de Madrid y otra
    con la de Castilla y Leon), JSON se quedaria solo con el ultimo. Aqui se juntan sus "url"."""
    d = {}
    for k, v in pares:
        if k in d and isinstance(d[k], dict) and isinstance(v, dict):
            viejo = d[k]
            urls = []
            for x in (viejo.get("url"), v.get("url")):
                urls += x if isinstance(x, list) else ([x] if x else [])
            nuevo = dict(viejo)
            nuevo.update(v)
            if urls:
                nuevo["url"] = list(dict.fromkeys(urls))
            d[k] = nuevo
            AVISOS_CONFIG.append('"%s" aparece repetido: junto sus direcciones (%d)' % (k, len(nuevo.get("url") or [])))
        else:
            d[k] = v
    return d


def cargar_config():
    if not os.path.exists(CONFIG_FILE):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
            json.dump(PLANTILLA, fh, indent=2, ensure_ascii=False)
        os.chmod(CONFIG_FILE, stat.S_IRUSR | stat.S_IWUSR)
        print("Se ha creado la plantilla de configuracion en:\n  %s" % CONFIG_FILE)
        print("Editala con tu usuario de cada cuenta:  nano %s" % CONFIG_FILE)
        print("Si dejas 'password' vacio, el script te la pedira al ejecutarse.")
        sys.exit(0)
    with open(CONFIG_FILE, encoding="utf-8-sig") as fh:
        texto = fh.read()
    # tolera comas sobrantes antes de } o ] y comas olvidadas entre dos bloques (errores tipicos al editar)
    texto = re.sub(r",(\s*[}\]])", r"\1", texto)
    texto = re.sub(r'(\}|")(\s*\n\s*)("[^"\n]+"\s*:)', r"\1,\2\3", texto)
    try:
        return json.loads(texto, object_pairs_hook=juntar_repetidos)
    except json.JSONDecodeError as e:
        lineas = texto.splitlines()
        linea = lineas[e.lineno - 1] if 0 < e.lineno <= len(lineas) else ""
        if '"password"' in linea:
            linea = re.sub(r'("password"\s*:\s*")[^"]*', r"\1****", linea)
        sys.exit("Error en %s, linea %d, columna %d: %s\n   %s\n"
                 "Revisa comillas, comas y llaves (nano %s)."
                 % (CONFIG_FILE, e.lineno, e.colno, e.msg, linea.strip(), CONFIG_FILE))


# ------------------------------------------------------------ cliente Cantook

class LoginFallido(Exception):
    def __init__(self, web="", otros="", frases="", servidor=False, detalle=""):
        Exception.__init__(self, web or frases or "login")
        self.web, self.otros, self.frases, self.servidor, self.detalle = web, otros, frases, servidor, detalle


class EBiblio:
    def __init__(self, nombre, cfg):
        self.nombre = nombre
        h = cfg["host"].rstrip("/")
        self.base = h if h.startswith("http") else "https://" + h
        self.host = urlparse(self.base).netloc
        self.usuario = cfg.get("usuario", "")
        self.password = cfg.get("password", "")
        self.login_url = cfg.get("login_url", "")
        self.biblioteca = cfg.get("biblioteca", "")
        self.prensa = cfg.get("prensa") or PRENSA_DEFECTO.get(urlparse(self.base).netloc, {})
        self.info_form = ""
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "es-ES,es;q=0.9"})
        self.enlaces_cuenta = []
        self.paginas = {}      # seccion -> url usada
        self._cache = {}

    def url(self, ruta):
        return urljoin(self.base + "/", ruta)

    def es_login(self, r):
        ruta = urlparse(r.url).path.rstrip("/")
        if ruta in ("/sign_in", "/sessions", "/sessions/new", "/ebiblio_auth_sign_in",
                    "/ebiblio_auth_create"):
            return True
        if urlparse(r.url).netloc != self.host:
            return True  # nos han mandado a otra web (pagina de identificacion externa)
        # formulario de acceso dentro de la pagina (no un formulario de "cambiar contrasena")
        return bool(re.search(r'<form[^>]+action=["\'][^"\']*(sessions|ebiblio_auth|sign_in)', r.text, re.I)
                    and re.search(r'<input[^>]+type=["\']?password', r.text, re.I))

    # -- conexion
    @staticmethod
    def form_login(sopa):
        """Formulario que tiene un campo de contrasena."""
        for f in sopa.find_all("form"):
            if f.find("input", attrs={"type": re.compile("^password$", re.I)}):
                return f
        return None

    @staticmethod
    def describir_form(form, url):
        lin = ["   pagina: %s" % url,
               "   FORM action=%s method=%s" % (form.get("action"), form.get("method", "get"))]
        for e in form.find_all(["input", "select", "button"]):
            v = "" if (e.get("type") or "").lower() in ("password", "hidden") else (e.get("value") or "")
            lin.append("      %s type=%s name=%s id=%s value=%r" % (e.name, e.get("type"), e.get("name"),
                                                                  e.get("id"), v[:30]))
            if e.name == "select":
                for o in e.find_all("option")[:40]:
                    lin.append("         opcion value=%r  %s" % (o.get("value"), limpiar(o.get_text())[:50]))
        return "\n".join(lin)

    def rellenar(self, form, url_pagina):
        datos, campo_usuario = {}, None
        for e in form.find_all(["input", "select", "textarea"]):
            n = e.get("name")
            if not n:
                continue
            t = (e.get("type") or "text").lower()
            if e.name == "select":
                if self.biblioteca:
                    datos[n] = self.biblioteca
                else:
                    sel = e.find("option", selected=True) or e.find("option")
                    datos[n] = sel.get("value", "") if sel else ""
            elif t == "password":
                datos[n] = self.password
            elif t in ("checkbox", "radio"):
                if e.has_attr("checked") or re.search(r"remember|recordar", n, re.I):
                    datos[n] = e.get("value", "on")
            elif t in ("submit", "button", "image", "reset"):
                continue
            elif t == "hidden":
                datos[n] = e.get("value", "")
            else:
                if campo_usuario is None or re.search(r"login|user|usuario|card|carne|carnet|barcode|dni|email|ident",
                                                      n, re.I):
                    campo_usuario = n
                datos[n] = e.get("value", "")
        if campo_usuario:
            datos[campo_usuario] = self.usuario
        boton = form.find(["button", "input"], attrs={"type": "submit"})
        if boton is not None and boton.get("name"):
            datos[boton["name"]] = boton.get("value", "")
        accion = urljoin(url_pagina, form.get("action") or url_pagina)
        metodo = (form.get("method") or "get").lower()
        return accion, metodo, datos, campo_usuario

    def login(self):
        """Inicia sesion. Si la web dice que su servidor de acceso falla (pasa a ratos en Madrid),
        lo reintenta unas veces antes de rendirse."""
        esperas = [0, 5, 15]
        for n, espera in enumerate(esperas, 1):
            if espera:
                print("[%s] eBiblio dice: \"%s\". Reintento en %d s (%d/%d)..."
                      % (self.nombre, ultimo.web.rstrip("."), espera, n, len(esperas)))
                time.sleep(espera)
                self.s.cookies.clear()
                self._cache.clear()
            try:
                return self._login_una_vez()
            except LoginFallido as e:
                ultimo = e
                if not e.servidor:
                    break
        e = ultimo
        lineas = ["[%s] No se pudo iniciar sesion." % self.nombre]
        if e.web:
            lineas.append('   La web de eBiblio dice: "%s"' % e.web)
        if e.servidor:
            lineas.append("   -> Es un fallo del servidor de acceso de eBiblio (tambien sale en la web),")
            lineas.append("      no de tu usuario ni contrasena. Probado %d veces. Vuelve a intentarlo mas tarde." % len(esperas))
        elif e.frases:
            lineas.append("   -> %s" % e.frases)
            lineas.append("   Revisa usuario/contrasena en %s (entran igual en la web?)." % CONFIG_FILE)
        else:
            lineas.append("   Revisa usuario/contrasena en %s (entran igual en la web?)." % CONFIG_FILE)
        if e.otros:
            lineas.append('   Otros avisos de la web: "%s"' % e.otros)
        if DEBUG or not (e.web or e.frases):
            lineas.append(e.detalle)
            lineas.append("   (Si no lo entiendes, envia este mensaje a Claude.)")
        raise SystemExit("\n".join(lineas))

    def _login_una_vez(self):
        if not self.usuario:
            raise SystemExit("La cuenta '%s' no tiene usuario en %s" % (self.nombre, CONFIG_FILE))
        if not self.password:
            self.password = getpass.getpass("Contrasena eBiblio (%s, %s): " % (self.nombre, self.usuario))
        urls = [self.login_url] if self.login_url else []
        urls += [self.url("/ebiblio_auth_sign_in"), self.url("/sign_in")]
        pagina = form = None
        for u in urls:
            try:
                r = self.s.get(u, timeout=30)
            except requests.RequestException as e:
                log_debug("GET %s error" % u, e)
                continue
            sopa = BeautifulSoup(r.text, "html.parser")
            f = self.form_login(sopa)
            log_debug("pagina de acceso %s -> HTTP %s, final %s, formulario %s"
                      % (u, r.status_code, r.url, "SI" if f else "no"))
            if f is not None:
                pagina, form = r.url, f
                break
        if form is None:
            raise SystemExit("[%s] No encuentro ningun formulario de acceso (probado: %s).\n"
                             "Pon en cuentas.json la direccion exacta con \"login_url\"."
                             % (self.nombre, ", ".join(urls)))
        self.info_form = self.describir_form(form, pagina)
        log_debug("formulario usado", self.info_form)
        accion, metodo, datos, campo = self.rellenar(form, pagina)
        log_debug("envio %s %s (usuario en campo '%s', campos: %s)"
                  % (metodo.upper(), accion, campo, ", ".join(datos)))
        cab = {"Referer": pagina, "Origin": "%s://%s" % (urlparse(pagina).scheme, urlparse(pagina).netloc)}
        if metodo == "post":
            r = self.s.post(accion, data=datos, headers=cab, timeout=30)
        else:
            r = self.s.get(accion, params=datos, headers=cab, timeout=30)
        # formularios intermedios que se envian solos (SSO / SAML / redirecciones por POST)
        for _ in range(5):
            sopa = BeautifulSoup(r.text, "html.parser")
            f = sopa.find("form")
            if f is None or self.form_login(sopa) is not None:
                break
            campos = f.find_all("input")
            if not campos or any((c.get("type") or "text").lower() not in ("hidden", "submit") for c in campos):
                break
            if not re.search(r"SAML|RelayState|code|state|token|onload|submit\(\)", r.text):
                break
            acc = urljoin(r.url, f.get("action") or r.url)
            d = {c["name"]: c.get("value", "") for c in campos if c.get("name")}
            log_debug("formulario automatico -> %s" % acc)
            r = self.s.post(acc, data=d, timeout=30)
        log_debug("tras el login: HTTP %s, final %s" % (r.status_code, r.url))
        d = self.get("/dashboard")
        if d is None and r.status_code == 200 and not self.es_login(r):
            d = r.text  # el login ha ido bien pero /dashboard no sirve: usar la pagina recibida
        if d is None:
            sopa = BeautifulSoup(r.text, "html.parser")
            for t in sopa(["script", "style", "noscript"]):
                t.decompose()
            avisos = [limpiar(e.get_text(" ")) for e in sopa.select(
                ".alert, .flash, .notice, .error, .errors, [role=alert], .callout, .message, "
                "[class*=flash], [class*=alert], [class*=error], [class*=notice], [class*=toast]")]
            texto = limpiar(sopa.get_text(" "))
            frases = re.findall(r"[^.|]{0,80}(?:incorrect|inv[aá]lid|no v[aá]lid|error|bloquead|no existe|"
                                r"no encontrad|no coincide|intent|caducad|expirad)[^.|]{0,80}", texto, re.I)
            titulo = limpiar(sopa.title.get_text()) if sopa.title else "-"
            cookies = ", ".join(sorted({c.name for c in self.s.cookies}))
            avisos_l = [a for a in dict.fromkeys(avisos) if a]
            errores = [a for a in avisos_l if not re.match(r"[¡!]?\s*Atenci", a)]
            otros = [a for a in avisos_l if a not in errores]
            raise LoginFallido(
                web=" / ".join(errores)[:300], otros=" / ".join(otros)[:300],
                frases=" / ".join(dict.fromkeys(f.strip() for f in frases))[:300],
                servidor=bool(re.search(r"servidor|server|temporalmente|int[eé]ntelo m[aá]s tarde|mantenimiento",
                                        " ".join(errores), re.I)),
                detalle=
                "   Respuesta: HTTP %s  pagina final: %s\n"
                "   Titulo: %s\n"
                "   Avisos: %s\n"
                "   Frases de error: %s\n"
                "   Cookies: %s\n"
                "Formulario usado (sin contrasena):\n%s"
                % (r.status_code, r.url, titulo,
                   " / ".join(avisos_l)[:300] or "-",
                   " / ".join(dict.fromkeys(f.strip() for f in frases))[:300] or "-",
                   cookies or "-", self.info_form))
        self.enlaces_cuenta = self.enlaces(d)
        return BeautifulSoup(d, "html.parser")

    def get(self, ruta):
        u = self.url(ruta)
        if u in self._cache:
            return self._cache[u]
        try:
            r = self.s.get(u, timeout=30)
        except requests.RequestException as e:
            log_debug("GET %s error" % u, e)
            return None
        ok = r.status_code == 200 and not self.es_login(r)
        log_debug("GET %s -> HTTP %s final %s %s" % (u, r.status_code, r.url, "" if ok else "(descartada)"))
        self._cache[u] = r.text if ok else None
        return self._cache[u]

    def enlaces(self, html):
        sopa = BeautifulSoup(html, "html.parser")
        out = []
        for a in sopa.find_all("a", href=True):
            h = a["href"]
            p = urlparse(urljoin(self.base + "/", h))
            if p.netloc and p.netloc != self.host:
                continue
            ruta = p.path + (("?" + p.query) if p.query else "")
            if p.path.startswith(("/resources", "/media", "/assets", "/sessions", "/sign_out",
                                  "/providers")) or p.path in ("", "/"):
                continue
            out.append((ruta, limpiar(a.get_text(" "))))
        vistos, unicos = set(), []
        for r, t in out:
            if r not in vistos:
                vistos.add(r)
                unicos.append((r, t))
        return unicos

    def reserva_mia(self, rid, nombre="", fecha=None):
        """Mi reserva de ese ejemplar (pagina de reservas de la cuenta), o None.
        Busca por identificador y, si no, por nombre de la publicacion + fecha de la edicion."""
        if not hasattr(self, "_mis_reservas"):
            try:
                self._mis_reservas = {"%d_%s" % (i, f["id"]): f for i, f in enumerate(filas_reservas(self))}
            except Exception as e:
                log_debug("no pude leer mis reservas", e)
                self._mis_reservas = {}
            log_debug("mis reservas en %s" % self.nombre,
                      "; ".join("%s %s -> %s" % (k, f["titulo"], f["disponible"]) for k, f in self._mis_reservas.items()))
        for f in self._mis_reservas.values():
            if coincide({"id": f["id"], "titulo": f["titulo"], "texto": f.get("_texto", ""),
                         "edicion": f.get("_edicion") or next((d for d, _, _ in parse_fechas(f["titulo"])), None)},
                        rid, nombre, fecha):
                return f
        return None

    def prestamo_mio(self, rid, nombre="", fecha=None):
        """Mi prestamo activo de esa edicion (pagina de prestamos de la cuenta), o None."""
        if not hasattr(self, "_mis_prestamos"):
            try:
                self._mis_prestamos = filas_prestamos(self)
            except Exception as e:
                log_debug("no pude leer mis prestamos", e)
                self._mis_prestamos = []
            log_debug("mis prestamos en %s" % self.nombre,
                      "; ".join("%s -> vence %s" % (f["titulo"], f["vence"]) for f in self._mis_prestamos))
        for f in self._mis_prestamos:
            if coincide({"id": f["id"], "titulo": f["titulo"], "texto": f.get("_texto", ""),
                         "edicion": f.get("_edicion") or next((d for d, _, _ in parse_fechas(f["titulo"])), None)},
                        rid, nombre, fecha):
                return f
        return None

    def prestamo_sigue(self, fila):
        """True si ese prestamo sigue en la cuenta (se vuelve a leer la pagina)."""
        self.olvidar()
        ed = fila.get("_edicion") or next((d for d, _, _ in parse_fechas(fila["titulo"])), None)
        for f in filas_prestamos(self):
            if (fila.get("id") and f.get("id") == fila["id"]) or \
                    (clave(f["titulo"]) == clave(fila["titulo"]) and
                     (ed is None or (f.get("_edicion") or ed) == ed)):
                return True
        return False

    def olvidar(self):
        """Borra lo leido de la cuenta (tras pedir o reservar)."""
        self._cache.clear()
        for a in ("_mis_reservas", "_mis_prestamos", "_actividad"):
            if hasattr(self, a):
                delattr(self, a)

    def pagina_de(self, seccion):
        """Busca la pagina de una seccion: primero enlaces de la cuenta, luego rutas tipicas."""
        cfg = SECCIONES[seccion]
        candidatas = []
        for ruta, texto in self.enlaces_cuenta:
            s = ruta + " " + texto
            if re.search(cfg["claves"], s, re.I) and not re.search(cfg["excluir"], ruta, re.I):
                candidatas.append(ruta)
        candidatas += cfg["rutas"]
        for ruta in candidatas:
            html = self.get(ruta)
            if html:
                self.paginas[seccion] = ruta
                log_debug("seccion %s -> %s" % (seccion, ruta))
                return ruta, html
        return None, None


# ------------------------------------------------------- lectura de tarjetas

def id_recurso(href, media=False):
    m = re.search(r"/resources/([0-9a-f]{24}|[\w-]+)", href or "")
    if m:
        return m.group(1)
    if media:   # solo si la pagina no tiene enlaces /resources/<id>
        m = re.search(r"/media/([^/?#]+)", href or "")
        return ("media:" + m.group(1)) if m else ""
    return ""


def tarjetas(html):
    """Agrupa la pagina en 'tarjetas', una por libro (enlace a /resources/<id>)."""
    sopa = BeautifulSoup(html, "html.parser")
    for t in sopa(["script", "style", "noscript", "header", "footer", "nav"]):
        if t.name in ("header", "footer") and t.find_parent(["main", "section", "article"]) is not None:
            continue
        t.decompose()
    principal = sopa.find("main") or sopa.body or sopa
    media = not any(id_recurso(a["href"]) for a in principal.find_all("a", href=True))
    enlaces = [a for a in principal.find_all("a", href=True) if id_recurso(a["href"], media)]
    vistas, out = set(), []
    for a in enlaces:
        rid = id_recurso(a["href"], media)
        if rid in vistas:
            continue
        # subir hasta el contenedor mas grande que solo tiene este libro
        nodo = a
        while nodo.parent is not None and nodo.parent is not principal:
            ids = {id_recurso(x["href"], media) for x in nodo.parent.find_all("a", href=True)
                   if id_recurso(x["href"], media)}
            if len(ids) > 1:
                break
            nodo = nodo.parent
        vistas.add(rid)
        out.append((rid, nodo))
    return out


def encabezados(nodo, n=4):
    """Titulos de seccion que hay encima de una ficha (ej. 'Disponibles para el prestamo').
    Se mira desde el enlace del libro, saltando los titulos de otras fichas."""
    ancla = nodo if nodo.name == "a" else next(
        (a for a in nodo.find_all("a", href=True) if id_recurso(a["href"], True)), nodo)
    out = []
    for h in ancla.find_all_previous(["h1", "h2", "h3", "h4", "h5"]):
        if h in ancla.parents or any(id_recurso(a["href"], True) for a in h.find_all("a", href=True)):
            continue
        t = limpiar(h.get_text(" "))
        if t:
            out.append(t)
        if len(out) >= n:
            break
    return out


def datos_tarjeta(rid, nodo):
    texto = limpiar(nodo.get_text(" | "))
    titulo = ""
    for h in nodo.find_all(["h1", "h2", "h3", "h4", "h5"]):   # titulo que enlaza al libro
        if any(id_recurso(x["href"], True) for x in h.find_all("a", href=True)) and limpiar(h.get_text(" ")):
            titulo = limpiar(h.get_text(" "))
            break
    for sel in ([] if titulo else ["[class*=title]", "h1", "h2", "h3", "h4", "h5"]):
        e = nodo.select_one(sel)
        if e and limpiar(e.get_text(" ")):
            titulo = limpiar(e.get_text(" "))
            break
    if not titulo:
        for a in nodo.find_all("a", href=True):
            if id_recurso(a["href"]) and limpiar(a.get_text(" ")):
                titulo = limpiar(a.get_text(" "))
                break
    if not titulo:
        img = nodo.find("img", alt=True)
        titulo = limpiar(img["alt"]) if img else ""
    autores = [limpiar(a.get_text(" ")) for a in nodo.find_all("a", href=re.compile(r"author_keyword"))]
    autor = ", ".join(dict.fromkeys(x for x in autores if x))
    if not autor:
        e = nodo.select_one("[class*=author], [class*=contributor]")
        autor = limpiar(e.get_text(" ")) if e else ""
    fechas = [d for d, _, _ in parse_fechas(texto)]
    formato = ""
    m = re.search(r"\b(audiolibro|audio|epub|pdf|lectura en l[ií]nea|streaming|revista|peri[oó]dico)\b", texto, re.I)
    if m:
        formato = m.group(1).lower()
    return {"id": rid, "titulo": titulo, "autor": autor, "texto": texto, "secciones": encabezados(nodo),
            "fechas": fechas, "formato": formato, "nodo": nodo}


# ------------------------------------------------------------ transformacion

COL_HIST = [("cuenta", "Cuenta", 12), ("titulo", "Titulo", 38), ("autor", "Autor", 24),
            ("inicio", "Inicio", 10), ("fin", "Fin", 10), ("formato", "Formato", 10)]
COL_PREST = [("cuenta", "Cuenta", 12), ("titulo", "Titulo", 38), ("autor", "Autor", 24),
             ("inicio", "Inicio", 10), ("vence", "Vence", 16), ("dias", "Falta", 12)]
# En pantalla, prestamos y reservas con columnas FIJAS (mismo sitio siempre, haya dato o no)
COL_PREST_PANT = [("cuenta", "Cuenta", 12), ("titulo", "Titulo", 34), ("vence", "Vence", 16), ("dias", "Falta", 7)]
COL_RES_PANT = [("cuenta", "Cuenta", 12), ("titulo", "Titulo", 34), ("estado", "Estado", 10),
                ("posicion", "Cola", 4), ("disponible", "Libre aprox.", 16), ("dias", "Falta", 7),
                ("recoger", "Pedir antes", 11)]
COL_RES = [("cuenta", "Cuenta", 12), ("titulo", "Titulo", 34), ("estado", "Estado", 12),
           ("posicion", "Pos.cola", 8), ("disponible", "Disponible aprox.", 17),
           ("dias", "Falta", 12), ("recoger", "Pedir antes", 11),
           ("reservado", "Reservado", 10)]
COL_FAV = [("cuenta", "Cuenta", 12), ("titulo", "Titulo", 40), ("autor", "Autor", 26),
           ("estado", "Estado", 14)]


RE_VER_MAS = re.compile(r"ver (?:todo|todos|todas|m[aá]s)|mostrar (?:todo|m[aá]s)|cargar m[aá]s|see all|view all"
                        r"|show (?:all|more)|load more|historial completo|siguiente|next|^\s*[»›>]\s*$", re.I)


def con_pagina(url, n):
    p = urlparse(url)
    q = [kv for kv in p.query.split("&") if kv and not kv.startswith("page=")]
    q.append("page=%d" % n)
    return p._replace(query="&".join(q)).geturl()


def num_pagina(url):
    m = re.search(r"[?&]page=(\d+)", url)
    return int(m.group(1)) if m else 1


def enlaces_siguientes(cli, html, url, seccion):
    """Enlaces a la pagina siguiente o a 'ver todo' dentro de una pagina de la cuenta."""
    sopa = BeautifulSoup(html, "html.parser")
    claves = SECCIONES[seccion]["claves"]
    actual = num_pagina(url)
    out = []
    for a in sopa.find_all("a", href=True):
        destino = urljoin(url, a["href"])
        pd = urlparse(destino)
        if pd.netloc != cli.host or pd.path.startswith(("/resources", "/media", "/assets", "/providers")):
            continue
        texto = limpiar(a.get_text(" ")) + " " + " ".join(a.get("class", [])) + " " + (a.get("aria-label") or "")
        rel = " ".join(a.get("rel", [])) if isinstance(a.get("rel"), list) else (a.get("rel") or "")
        if "next" in rel:
            out.insert(0, destino)
        elif "page=" in destino and num_pagina(destino) == actual + 1:
            out.append(destino)
        elif RE_VER_MAS.search(texto) and (re.search(claves, destino, re.I) or "page=" in destino):
            out.append(destino)
    return list(dict.fromkeys(out))


def leer(cli, seccion, limite=None):
    ruta, html = cli.pagina_de(seccion)
    if not html:
        print("[%s] No encuentro la pagina de %s. Ejecuta: python3 ebib.py -acc %s -sondeo"
              % (cli.nombre, seccion, cli.nombre))
        return []
    items, vistos, visitadas = [], set(), set()
    url = cli.url(ruta)
    por_parametro = False
    for vuelta in range(300):
        visitadas.add(url)
        nuevos = 0
        for rid, n in tarjetas(html):
            it = datos_tarjeta(rid, n)
            clave = (it["id"], it["texto"])
            if clave in vistos:
                continue
            vistos.add(clave)
            items.append(it)
            nuevos += 1
            log_debug("tarjeta %s (%s)" % (it["id"], seccion), it["texto"])
        log_debug("pagina %s: %d libros nuevos (total %d)" % (url, nuevos, len(items)))
        if limite and len(items) >= limite:
            break
        if vuelta > 0 and nuevos == 0:
            break
        siguiente = next((u for u in enlaces_siguientes(cli, html, url, seccion) if u not in visitadas), None)
        if siguiente is None and (vuelta == 0 or por_parametro) and nuevos:
            siguiente = con_pagina(url, num_pagina(url) + 1)   # probar ?page=N aunque no haya enlace
            por_parametro = True
            if siguiente in visitadas:
                siguiente = None
        if siguiente is None:
            break
        html = cli.get(siguiente)
        if not html:
            break
        url = siguiente
    if not items:
        print("[%s] La pagina %s no tiene libros reconocibles (vacia o formato distinto; usa -sondeo)."
              % (cli.nombre, ruta))
    return items


def filas_historial(cli):
    filas = []
    for it in leer(cli, "historial", LIMITE):
        t, f = it["texto"], sorted(it["fechas"])
        inicio = fecha_cerca(t, r"prestad|tomad|inicio|desde|el pr[eé]stamo") or (f[0] if f else None)
        fin = fecha_cerca(t, r"devuel|fin|hasta|expir|venc|termin") or (f[-1] if len(f) > 1 else None)
        filas.append({"cuenta": cli.nombre, "titulo": it["titulo"], "autor": it["autor"],
                      "inicio": fmt(inicio), "fin": fmt(fin), "formato": it["formato"],
                      "id": it["id"], "_orden": inicio or datetime.min})
    filas.sort(key=lambda x: x["_orden"], reverse=True)
    return filas[:LIMITE] if LIMITE else filas


def filas_prestamos(cli):
    filas, hoy = [], datetime.now()
    act = actividad(cli)
    if act is not None:
        for it in act["prestamos"]:
            v = it.get("vence")
            filas.append({"cuenta": cli.nombre, "titulo": it["titulo"], "autor": "",
                          "inicio": "", "vence": fmt(v, hora=True) if v else "",
                          "dias": falta(v) if v else "", "id": it["id"],
                          "_texto": it["texto"], "_edicion": it["edicion"], "_nodo": it["nodo"],
                          "_pagina": it["pagina"]})
        return filas
    for it in leer(cli, "prestamos"):
        if seccion_de(it) in ("lista", "espera"):
            continue
        t, f = it["texto"], sorted(it["fechas"])
        vence = fecha_cerca(t, r"venc|expir|hasta|devol|termin|fin") or (f[-1] if f else None)
        inicio = fecha_cerca(t, r"prestad|tomad|desde|inicio")
        if not inicio and len(f) > 1:
            inicio = f[0]
        filas.append({"cuenta": cli.nombre, "titulo": it["titulo"], "autor": it["autor"],
                      "inicio": fmt(inicio), "vence": fmt(vence),
                      "dias": (vence.date() - hoy.date()).days if vence else "",
                      "id": it["id"], "_nodo": it["nodo"],
                      "_pagina": cli.url(cli.paginas.get("prestamos", "/dashboard"))})
    return filas



# ------------------------------------------------ pagina "Prestamos y reservas"

SECCIONES_ACTIVIDAD = [("prestamos", re.compile(r"^pr[eé]stamos activos$", re.I)),
                       ("lista", re.compile(r"^disponibles? para (?:el |su )?pr[eé]stamo$", re.I)),
                       ("espera", re.compile(r"^pendiente de disponibilidad$", re.I))]


def _nodos_items(cont, cabecera):
    """Una ficha por portada (<img>) dentro del bloque de una seccion."""
    nodos = []
    imgs = [i for i in cont.find_all("img") if cabecera not in i.parents]
    for img in imgs:
        nodo = img
        while nodo.parent is not None and nodo.parent is not cont and len(nodo.parent.find_all("img")) == 1 \
                and cabecera not in nodo.parent.descendants:
            nodo = nodo.parent
        if nodo not in nodos:
            nodos.append(nodo)
    if not nodos:   # sin portadas: una ficha por titulo con fecha o enlace al libro
        for h in cont.find_all(["h2", "h3", "h4", "h5", "a"]):
            if h is cabecera or cabecera in h.parents:
                continue
            if (h.name != "a" and parse_fechas(limpiar(h.get_text(" ")))) or \
                    (h.name == "a" and id_recurso(h.get("href", ""))):
                nodo = h
                while nodo.parent is not None and nodo.parent is not cont and \
                        cabecera not in nodo.parent.descendants and \
                        len([x for x in nodo.parent.find_all(["h2", "h3", "h4", "h5"]) if parse_fechas(limpiar(x.get_text(" ")))]) <= 1:
                    nodo = nodo.parent
                if nodo not in nodos:
                    nodos.append(nodo)
    return nodos


RE_BLOQUEO = re.compile(r"Has alcanzado tu l[ií]mite[^|]{0,160}|No puedes (?:tomar|recuperar|pedir)[^|]{0,160}", re.I)


def leer_contadores(sopa):
    """Contadores de 'Tienes todavia derecho a...' (bloque #counters). eBiblio da 'N / M' donde
    N es lo que TE QUEDA y M el maximo (la suma de N coincide con 'derecho a N prestamos/reservas').
    Devuelve [{'tipo','limite','quedan','max'}]."""
    caja = sopa.find(id="counters")
    if caja is None:
        return []
    out = []
    for li in caja.find_all("li"):
        spans = li.find_all("span", recursive=False)
        if len(spans) < 2:
            continue
        etiqueta = limpiar(spans[0].get_text(" ")).rstrip(": ")
        m = re.search(r"(\d+)\s*/\s*(\d+)", limpiar(spans[1].get_text(" ")))
        if not m or not etiqueta:
            continue
        grupo = li.find_parent("li", class_=re.compile("row-space"))
        tipo = ""
        if grupo is not None and grupo.find("strong") is not None:
            tipo = limpiar(grupo.find("strong").get_text(" ")).rstrip(": ")
        out.append({"tipo": tipo, "limite": etiqueta, "quedan": int(m.group(1)), "max": int(m.group(2))})
    return out


def filas_limites(cli):
    """Tabla de limites de la cuenta para -resumen, con aviso si queda poco."""
    act = actividad(cli) or {}
    filas = []
    for c in act.get("_contadores") or []:
        lim = c["limite"]
        if re.search(r"\bmes\b|30 d", lim, re.I):
            nombre, orden = "Prestamos en 30 dias", 1
        elif re.search(r"\bd[ií]a\b|24 ?h", lim, re.I):
            nombre, orden = "Prestamos en 24 horas", 0
        else:
            que = "prestamos" if re.search(r"pr[eé]stamo", lim, re.I) else "reservas" if re.search(r"reserva", lim, re.I) else lim
            nombre, orden = "%s: %s a la vez" % (c["tipo"] or "Total", que), 2
        q, mx = c["quedan"], c["max"]
        if q <= 0:
            aviso = "AGOTADO"
        elif orden <= 1 and q <= max(1, mx // 5):
            aviso = "quedan pocos"
        else:
            aviso = ""
        filas.append({"cuenta": cli.nombre, "limite": nombre, "quedan": q, "max": mx, "aviso": aviso, "_orden": orden})
    # si el limite de 24 h o de 30 dias esta agotado, los "prestamos a la vez" salen a 0 por eso: no repetir aviso
    if any(f["_orden"] <= 1 and f["quedan"] <= 0 for f in filas):
        for f in filas:
            if f["_orden"] == 2 and "prestamos" in f["limite"]:
                f["aviso"] = ""
    filas.sort(key=lambda f: f["_orden"])
    return filas


COL_LIMITES = [("cuenta", "Cuenta", 13), ("limite", "Limite", 34), ("quedan", "Quedan", 6), ("max", "Maximo", 6),
               ("aviso", "Aviso", 12)]


def limites_cuenta(cli):
    """'Tienes todavia derecho a 0 prestamos y 3 reservas. Podras volver a tomar un documento el ...'"""
    act = actividad(cli) or {}
    return ". ".join(x for x in (act.get("_derecho"), act.get("_podras")) if x)


def actividad(cli):
    """Lee /my_profile/activity: {'prestamos': [...], 'lista': [...], 'espera': [...]} o None."""
    if hasattr(cli, "_actividad"):
        return cli._actividad
    cli._actividad = None
    html = cli.get("/my_profile/activity")
    if not html:
        return None
    sopa = BeautifulSoup(html, "html.parser")
    for t in sopa(["script", "style", "noscript"]):
        t.decompose()
    cabeceras = []
    for h in sopa.find_all(["h1", "h2", "h3", "h4", "h5"]):
        t = limpiar(h.get_text(" "))
        for clave_, pat in SECCIONES_ACTIVIDAD:
            if pat.search(t):
                cabeceras.append((clave_, h))
    if not cabeceras:
        return None
    todas = [h for _, h in cabeceras]
    res = {"prestamos": [], "lista": [], "espera": []}
    pagina = cli.url("/my_profile/activity")
    for clave_, h in cabeceras:
        cont = h
        while cont.parent is not None and cont.parent.name not in ("body", "html", "[document]"):
            if any(x is not h and cont.parent in x.parents for x in todas):
                break
            cont = cont.parent
        for nodo in _nodos_items(cont, h):
            texto = limpiar(nodo.get_text(" | "))
            titulo = ""
            cands = []
            for x in nodo.find_all(["h1", "h2", "h3", "h4", "h5"]):
                tx = limpiar(x.get_text(" "))
                if not tx or any(pat.search(tx) for _, pat in SECCIONES_ACTIVIDAD) or \
                        re.match(r"^(pr[eé]stamos|reservas)\b", tx, re.I):
                    continue
                pts = (2 if any(id_recurso(a.get("href", "")) for a in x.find_all("a", href=True)) else 0) + \
                      (1 if parse_fechas(tx) else 0)
                cands.append((pts, tx))
            if cands:
                titulo = max(cands, key=lambda c: c[0])[1]
            if not titulo:   # sin encabezado: el texto del enlace al libro
                for a in nodo.find_all("a", href=True):
                    tx = limpiar(a.get_text(" "))
                    if id_recurso(a["href"]) and len(tx) > 3:
                        titulo = tx
                        break
            if not titulo:
                img = nodo.find("img", alt=True)
                titulo = limpiar(img["alt"]) if img is not None and img.get("alt") else texto[:60]
            rid = ""
            for a in nodo.find_all("a", href=True):
                rid = id_recurso(a["href"])
                if rid:
                    break
            fechas = parse_fechas(titulo) or parse_fechas(texto)
            it = {"clave": clave_, "titulo": titulo, "texto": texto, "nodo": nodo, "id": rid,
                  "edicion": fechas[0][0] if fechas else None, "pagina": pagina}
            if clave_ == "prestamos":
                it["vence"] = fecha_cerca(texto, r"expir|venc|hasta|devol") or tiempo_libre(texto) or limite_recoger(texto)
            elif clave_ == "lista":
                it["limite"] = limite_recoger(texto)
                it["form"] = form_de_prestamo(nodo) or prestar_cerca(nodo)
                mb = RE_BLOQUEO.search(texto)
                it["bloqueo"] = limpiar(mb.group(0)).strip(" |") if mb else ""
            else:
                m = RE_POSICION.search(texto)
                it["posicion"] = (m.group(1) or m.group(2)) if m else ""
                it["disp"] = tiempo_libre(texto)
            res[clave_].append(it)
            log_debug("actividad %s: %s" % (clave_, titulo), texto[:300])
    # reserva disponible sin boton dentro de su ficha: si en toda la pagina hay UN solo boton de
    # activar prestamo (".../activate") y una sola reserva disponible, es el suyo
    sin = [it for it in res["lista"] if it.get("form") is None]
    if sin:
        botones = [el for el in sopa.find_all(["a", "button", "form"]) if es_prestar(el)]
        log_debug("actividad: %d reservas disponibles sin boton; %d botones Prestar en la pagina"
                  % (len(sin), len(botones)), "\n".join(html_seguro(b_, 300) for b_ in botones[:5]))
        if len(sin) == 1 and len(res["lista"]) == 1 and len(botones) == 1:
            sin[0]["form"] = botones[0]
    txt_pag = limpiar(sopa.get_text(" "))
    m1 = re.search(r"Tienes todav[ií]a derecho a\s+.{0,60}?reservas?", txt_pag, re.I)
    m2 = re.search(r"Podr[aá]s volver a tomar un documento el\s+[^.]{0,60}", txt_pag, re.I)
    res["_derecho"] = limpiar(m1.group(0)) if m1 else ""
    res["_contadores"] = leer_contadores(sopa)
    res["_podras"] = limpiar(m2.group(0)) if m2 else ""
    cli._actividad = res
    return res


def coincide(it, rid, nombre, fecha):
    """La ficha de la cuenta (prestamo o reserva) es esa edicion."""
    if rid and it.get("id") and it["id"] == rid:
        return True
    k = clave(nombre)
    if not k or k not in clave(it.get("titulo", "") + " " + it.get("texto", "")[:200]):
        return False
    ed = it.get("edicion")
    return fecha is None or ed is None or ed.date() == fecha.date()


RE_SEC_RESERVA = re.compile(r"reserva|disponibles? para (?:el |su )?pr[eé]stamo|pendiente", re.I)
RE_SEC_LISTA = re.compile(r"disponibles? para (?:el |su )?pr[eé]stamo", re.I)


def seccion_de(it):
    """'lista' (reserva disponible), 'espera' (reserva pendiente), 'prestamo' u '' si no se sabe."""
    for h in it.get("secciones", []):
        if RE_SEC_LISTA.search(h):
            return "lista"
        if re.search(r"pendiente", h, re.I):
            return "espera"
        if re.search(r"reserva", h, re.I):
            return "espera"
        if re.search(r"pr[eé]stamo|loan", h, re.I):
            return "prestamo"
    return ""


def filas_reservas(cli):
    filas, hoy = [], datetime.now()
    act = actividad(cli)
    if act is not None:
        for clave_ in ("lista", "espera"):
            for it in act[clave_]:
                ya = clave_ == "lista"
                lim = it.get("limite") if ya else None
                disp = None if ya else it.get("disp")
                filas.append({"cuenta": cli.nombre, "titulo": it["titulo"], "autor": "", "id": it["id"],
                              "estado": "DISPONIBLE" if ya else "en espera",
                              "posicion": it.get("posicion", ""),
                              "disponible": "ya" if ya else fmt(disp, hora=True),
                              "dias": "ya" if ya else (falta(disp) if disp else ""),
                              "reservado": "", "recoger": falta(lim) if lim else ("?" if ya else ""),
                              "_disponible": ya, "_disp": disp, "_limite": lim, "_form": it.get("form"),
                              "_bloqueo": it.get("bloqueo", ""),
                              "_texto": it["texto"], "_nodo": it["nodo"], "_pagina": it["pagina"],
                              "_edicion": it["edicion"]})
        return filas
    for it in leer(cli, "reservas"):
        t = it["texto"]
        sec = seccion_de(it)
        if sec == "prestamo":
            continue           # en /my_profile/activity tambien salen los prestamos
        pos = ""
        m = RE_POSICION.search(t)
        if m:
            pos = m.group(1) or m.group(2)
        reservado = fecha_cerca(t, r"reservad|solicitad|desde|creada")
        form_prestamo = form_de_prestamo(it["nodo"])
        if sec == "lista" or RE_TU_TURNO.search(t):
            ya = True
        elif sec == "espera":
            ya = False
        else:
            ya = bool(RE_DISPONIBLE_YA.search(t)) or form_prestamo is not None
        if ya and form_prestamo is None:
            form_prestamo = prestar_cerca(it["nodo"])
        disp = None if ya else (tiempo_libre(t) or fecha_cerca(t, r"disponible|estimad|aprox"))
        limite = limite_recoger(t) if ya else None
        if ya and limite is None:        # el plazo puede estar fuera de la ficha detectada
            anc = it["nodo"]
            for _ in range(4):
                anc = anc.parent
                if anc is None or anc.name in ("body", "html", "[document]"):
                    break
                t2 = limpiar(anc.get_text(" | "))
                if len(RE_REL.findall(t2)) == 1:
                    limite = limite_recoger(t2)
                    break
                if len(RE_REL.findall(t2)) > 1:
                    break
        filas.append({"cuenta": cli.nombre, "titulo": it["titulo"], "autor": it["autor"],
                      "id": it["id"],
                      "estado": "DISPONIBLE" if ya else "en espera",
                      "posicion": pos,
                      "disponible": "ya" if ya else fmt(disp, hora=True),
                      "dias": "ya" if ya else (falta(disp) if disp else ""),
                      "reservado": fmt(reservado),
                      "recoger": falta(limite) if limite else ("?" if ya else ""),
                      "_disponible": ya, "_disp": disp, "_limite": limite, "_form": form_prestamo, "_texto": t,
                      "_nodo": it["nodo"],
                      "_pagina": cli.url(cli.paginas.get("reservas", "/dashboard"))})
    return filas


def es_prestar(el):
    """El elemento (form, a o button) es un boton de 'Prestar' / 'Tomar prestado'."""
    malo = re.compile(r"anular|cancelar|delete|devolver|return", re.I)
    bueno = re.compile(r"^\s*(prestar|tomar prestado|pedir prestado|pedir|borrow|tomar)\b", re.I)
    if el.name == "form":
        acc = el.get("action") or ""
        texto = limpiar(el.get_text(" ")) + " " + " ".join((b.get("value") or "") for b in el.find_all(["input", "button"]))
        if malo.search(texto + " " + acc):
            return False
        return bool(re.search(r"loans_confirmation|/loans\b|/checkout|/borrow", acc) or
                    re.search(r"prestar|tomar|pedir|borrow", texto, re.I))
    texto = limpiar(el.get_text(" ")) + " " + (el.get("value") or "")
    if el.name == "button" and el.find_parent("form") is not None:
        return False            # ya cuenta su formulario
    if RE_URL_ACTIVAR.search(url_elemento(el)) and not malo.search(texto):
        return True
    if malo.search(texto + " " + (el.get("href") or "")):
        return False
    if el.name == "a" and re.search(r"loans_confirmation|/loans\b|/checkout|/borrow", el.get("href") or ""):
        return True
    return bool(bueno.search(texto))


def prestar_cerca(nodo, niveles=5):
    """Si el boton Prestar no esta dentro de la ficha detectada, buscarlo en los contenedores
    de encima, siempre que alli haya UN solo boton Prestar (para no coger el de otro libro)."""
    actual = nodo
    for _ in range(niveles):
        actual = actual.parent
        if actual is None or actual.name in ("body", "html", "[document]"):
            return None
        cands = [el for el in actual.find_all(["form", "a", "button"]) if es_prestar(el)]
        if len(cands) == 1:
            return cands[0]
        if len(cands) > 1:
            return None
    return None


RE_URL_ACTIVAR = re.compile(r"/loans/[\w-]+/(?:activate|activation|borrow|checkout)|/activate\b|/bookings/[\w-]+/(?:loan|activate|checkout)", re.I)


def url_elemento(el):
    return el.get("href") or el.get("data-url") or el.get("data-href") or el.get("formaction") or el.get("action") or ""


def form_de_prestamo(nodo):
    """Boton 'Prestar' / 'Tomar prestado' de una ficha (formulario o enlace). Nunca Devolver/Anular."""
    malo = re.compile(r"anular|cancelar|delete|devolver|return", re.I)
    for el in nodo.find_all(["a", "button", "form"]):     # por la direccion: .../loans/<id>/activate
        if RE_URL_ACTIVAR.search(url_elemento(el)) and not malo.search(limpiar(el.get_text(" ")) + " " + url_elemento(el)):
            padre = el.find_parent("form") if el.name == "button" else None
            return padre if padre is not None and RE_URL_ACTIVAR.search(padre.get("action") or "") else el
    bueno_txt = re.compile(r"^\s*(prestar|tomar prestado|pedir prestado|pedir|borrow|tomar)\b", re.I)
    for f in nodo.find_all("form"):
        acc = f.get("action") or ""
        texto = limpiar(f.get_text(" ")) + " " + " ".join(
            (b.get("value") or "") for b in f.find_all(["input", "button"]))
        if malo.search(texto + " " + acc):
            continue
        if re.search(r"loans_confirmation|/loans\b|/checkout|/borrow", acc) or \
                re.search(r"prestar|tomar|pedir|borrow", texto, re.I):
            return f
    for a in nodo.find_all("a", href=True):
        texto = limpiar(a.get_text(" "))
        if malo.search(texto + " " + a["href"]):
            continue
        if re.search(r"loans_confirmation|/loans\b|/checkout|/borrow", a["href"]) or bueno_txt.search(texto):
            return a
    for b in nodo.find_all("button"):
        texto = limpiar(b.get_text(" "))
        if bueno_txt.search(texto) and not malo.search(texto):
            return b          # boton suelto (sin formulario): se informa, no se puede pulsar
    return None


def limite_recoger(texto):
    """Hasta cuando puedes tomar en prestamo una reserva que ya esta disponible."""
    ahora = datetime.now()
    d = fecha_cerca(texto, r"hasta|expir|caduc|antes d|l[ií]mite|recoger|retirar")
    if d and d > ahora - timedelta(minutes=1):
        return d
    m = RE_REL.search(texto)
    if m:
        n, u = int(m.group(1)), m.group(2).lower()
        mins = n if u.startswith("min") else n * 60 if u.startswith("h") else n * 1440
        if m.group(3):
            mins += int(m.group(3))
        return ahora + timedelta(minutes=mins)
    return None


def filas_favoritos(cli):
    filas = []
    for it in leer(cli, "favoritos"):
        nodo = it["nodo"]
        if nodo.find("form", action=re.compile(r"loans_confirmation")):
            estado = "se puede pedir"
        elif nodo.find("form", action=re.compile(r"/bookings")):
            estado = "solo reservar"
        else:
            estado = ""
        filas.append({"cuenta": cli.nombre, "titulo": it["titulo"], "autor": it["autor"],
                      "estado": estado, "id": it["id"]})
    return filas


# ------------------------------------------------------------------- sondeo

def sondeo(cli, sopa_dash):
    """Guarda las paginas de la cuenta (con sesion iniciada) para poder ajustar el programa."""
    carpeta = "ebib_paginas_%s" % cli.nombre
    os.makedirs(carpeta, exist_ok=True)
    lineas = ["ebib.py %s - sondeo con sesion de %s (%s)" % (VERSION, cli.nombre, cli.host),
              "Fecha: %s" % datetime.now().strftime("%d/%m/%Y %H:%M"), ""]
    lineas.append("ENLACES DE LA PAGINA /dashboard:")
    for ruta, texto in cli.enlaces_cuenta:
        lineas.append("   %-45s %s" % (ruta, texto[:50]))
    rutas = ["/dashboard"] + [r for r, _ in cli.enlaces_cuenta]
    for sec in SECCIONES.values():
        rutas += sec["rutas"]
    vistas = []
    for r in rutas:
        if r not in vistas and not re.search(r"sign_out|logout|desconect|l=\w", r):
            vistas.append(r)
    lineas.append("")
    lineas.append("PAGINAS PROBADAS:")
    n = 0
    for r in vistas[:40]:
        html = cli.get(r)
        if not html:
            lineas.append("   %-45s no existe o pide login" % r)
            continue
        n += 1
        nombre = "%02d_%s.html" % (n, re.sub(r"[^\w]+", "_", r).strip("_")[:50] or "raiz")
        with open(os.path.join(carpeta, nombre), "w", encoding="utf-8") as fh:
            fh.write(html)
        sopa = BeautifulSoup(html, "html.parser")
        tit = limpiar(sopa.title.get_text()) if sopa.title else "-"
        cards = tarjetas(html)
        lineas.append("   %-45s OK  titulo='%s'  libros=%d  -> %s" % (r, tit[:40], len(cards), nombre))
        for rid, nodo in cards[:3]:
            it = datos_tarjeta(rid, nodo)
            lineas.append("        libro: %s | texto: %s" % (it["titulo"][:40], it["texto"][:220]))
        clases = {}
        for e in sopa.find_all(class_=True):
            for c in e.get("class", []):
                if re.search(r"loan|hold|book|card|item|histor|resource|title|date|position|queue", c, re.I):
                    clases[c] = clases.get(c, 0) + 1
        if clases:
            top = sorted(clases.items(), key=lambda x: -x[1])[:15]
            lineas.append("        clases: " + ", ".join("%s(%d)" % kv for kv in top))
    res = os.path.join(carpeta, "RESUMEN_%s.txt" % cli.nombre)
    with open(res, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lineas) + "\n")
    print("\n".join(lineas))
    print("\nGuardado: %s  (y las paginas .html en la carpeta %s/)" % (res, carpeta))
    print("Envia el RESUMEN a Claude. Contiene titulos de tus libros y quiza tu nombre;")
    print("revisalo antes si quieres. Las .html solo si Claude te las pide.")


# -------------------------------------------------------------------- prensa

RE_FECHA_MEDIO = re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)")
RE_DISP_EN = re.compile(r"disponible\s+en\s+(\d+)\s*(minuto|min|hora|h\b|d[ií]a)", re.I)


def normaliza_nombre(t):
    import unicodedata
    t = unicodedata.normalize("NFD", t or "")
    return "".join(c for c in t if not unicodedata.combining(c)).lower().strip()


def rid_de_form(form):
    m = re.search(r"_resource_id=([\w-]+)", form.get("action") or "")
    if m:
        return m.group(1)
    h = form.find("input", attrs={"name": "_resource_id"})
    return h.get("value") if h else ""


def ejemplares(html):
    """Ejemplares de una pagina de resultados: cada uno con su boton de prestamo o de reserva."""
    sopa = BeautifulSoup(html, "html.parser")
    for t in sopa(["script", "style", "noscript", "header", "footer", "nav"]):
        if t.name in ("header", "footer") and t.find_parent(["main", "section", "article"]) is not None:
            continue
        t.decompose()
    forms = [f for f in sopa.find_all("form")
             if re.search(r"loans_confirmation|/bookings", f.get("action") or "")]
    grupos = {}
    for f in forms:
        rid = rid_de_form(f)
        if not rid:
            continue
        nodo = f
        while nodo.parent is not None and nodo.parent.name not in ("body", "html", "[document]"):
            otros = {rid_de_form(x) for x in nodo.parent.find_all("form")
                     if re.search(r"loans_confirmation|/bookings", x.get("action") or "")}
            otros.discard("")
            if len(otros) > 1:
                break
            nodo = nodo.parent
        g = grupos.setdefault(rid, {"rid": rid, "nodo": nodo, "libre": False, "reserva": False, "medio": "",
                                    "f_libre": None, "f_reserva": None, "mi_reserva": False})
        acc = f.get("action") or ""
        metodo = f.find("input", attrs={"name": "_method"})
        if re.search(r"/bookings/[\w-]+", urlparse(acc).path) or (metodo and metodo.get("value", "").lower() == "delete"):
            g["mi_reserva"] = True      # boton de anular una reserva que ya tienes
        elif "loans_confirmation" in acc:
            g["libre"] = True
            g["f_libre"] = f
        else:
            g["reserva"] = True
            g["f_reserva"] = f
        m = re.search(r"medium_id=([^&\s]+)|/media/([^/]+)/", acc)
        if m and not g["medio"]:
            g["medio"] = m.group(1) or m.group(2)
    # fichas sin boton de prestamo ni de reserva (p. ej. porque ya lo tienes prestado tu)
    for rid, nodo in tarjetas(html):
        if rid in grupos:
            continue
        medio = " ".join(a.get("href", "") for a in nodo.find_all("a", href=True))
        grupos[rid] = {"rid": rid, "nodo": nodo, "libre": False, "reserva": False, "medio": medio,
                       "f_libre": None, "f_reserva": None, "mi_reserva": False}
    out = []
    for g in grupos.values():
        texto = limpiar(g["nodo"].get_text(" | "))
        edicion = None
        m = RE_FECHA_MEDIO.search(g["medio"])
        if m:
            try:
                edicion = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                edicion = None
        if edicion is None:
            f = [d for d, _, _ in parse_fechas(texto)]
            edicion = f[0] if f else None
        libre_en = tiempo_libre(texto)
        tit = ""
        e = g["nodo"].select_one("[class*=title], h2, h3, h4")
        if e:
            tit = limpiar(e.get_text(" "))
        mi_reserva = g["mi_reserva"] or bool(re.search(r"anular (?:mi )?reserva|cancelar (?:la |mi )?reserva|"
                                                        r"reservado por ti|ya (?:lo )?has reservado", texto, re.I))
        sin_boton = not g["libre"] and not g["reserva"] and not mi_reserva
        ya_tuyo = sin_boton and bool(re.search(r"\bleer\b|devolver|tu pr[eé]stamo|prestado a ti|"
                                               r"ya lo tienes|en tu estanter|descargar|abrir|recuperar", texto, re.I))
        out.append({"rid": g["rid"], "edicion": edicion, "libre": g["libre"], "reserva": g["reserva"],
                    "libre_en": libre_en, "titulo": tit, "texto": texto, "ya_tuyo": ya_tuyo,
                    "sin_boton": sin_boton, "f_libre": g["f_libre"], "f_reserva": g["f_reserva"],
                    "mi_reserva": mi_reserva, "libre_en_mia": None})
    return out


def falta(dt):
    """Tiempo que falta hasta dt: minutos si es poco, horas si es mucho."""
    if not dt:
        return ""
    seg = (dt - datetime.now()).total_seconds()
    mins = int(-(-seg // 60)) if seg > 0 else 0   # redondeo hacia arriba, como la web
    if mins <= 0:
        return "ya"
    if mins < 60:
        return "%dm" % mins
    h, m = divmod(mins, 60)
    if h < 48:
        return "%dh %dm" % (h, m)
    d, h = divmod(h, 24)
    return "%dd %dh" % (d, h)


def estado_prensa(cli, nombre, url):
    hoy = datetime.now().date()
    html = cli.get(url)
    fila = {"cuenta": cli.nombre, "periodico": nombre, "edicion": "", "hoy": "", "estado": "",
            "falta": "", "libre_a": "", "_libre": False, "_libre_en": None, "_hoy": False, "_ed": None}
    if not html:
        fila["estado"] = "pagina no accesible"
        return fila
    eds = [e for e in ejemplares(html) if e["edicion"] and not e["sin_boton"]]
    if DEBUG:
        for e in eds:
            log_debug("prensa %s %s" % (nombre, fmt(e["edicion"])),
                      "libre=%s reserva=%s libre_en=%s | %s" % (e["libre"], e["reserva"], e["libre_en"], e["texto"][:300]))
    if not eds:
        fila["estado"] = "sin ediciones"
        return fila
    ultima = max(e["edicion"] for e in eds)
    # puede haber varias fichas de la misma fecha (suplementos): mejor la libre
    del_dia = [e for e in eds if e["edicion"] == ultima]
    e = next((x for x in del_dia if x["libre"]), del_dia[0])
    fila.update({"edicion": fmt(ultima), "_ed": ultima, "_hoy": ultima.date() == hoy,
                 "hoy": "si" if ultima.date() == hoy else "NO (aun no)"})
    mp = cli.prestamo_mio(e["rid"], nombre, e["edicion"])
    mr = None if mp else cli.reserva_mia(e["rid"], nombre, e["edicion"])
    if mp:
        fila.update({"estado": "PRESTADO tuyo", "falta": "", "libre_a": ("vence " + mp["vence"]) if mp.get("vence") else ""})
    elif mr and mr.get("_disponible"):
        lim = mr.get("_limite")
        fila.update({"estado": "TU TURNO", "_libre": True,
                     "falta": ("quedan " + falta(lim)) if lim else "ya",
                     "libre_a": ("hasta " + fmt(lim, hora=True)) if lim else ""})
    elif e["libre"]:
        fila.update({"estado": "LIBRE", "_libre": True, "falta": "ya"})
    elif mr or e["mi_reserva"]:
        le = (mr or {}).get("_disp") or e["libre_en"]
        fila.update({"estado": "reservado tuyo", "_libre_en": le,
                     "falta": falta(le) if le else "?",
                     "libre_a": fmt(le, hora=True) if le else "no lo indica"})
    elif e["reserva"]:
        fila.update({"estado": "ocupado", "_libre_en": e["libre_en"],
                     "falta": falta(e["libre_en"]) if e["libre_en"] else "?",
                     "libre_a": fmt(e["libre_en"], hora=True) if e["libre_en"] else "no lo indica"})
    else:
        fila["estado"] = "?"
    return fila


COL_PRENSA = [("cuenta", "Cuenta", 12), ("periodico", "Periodico", 16), ("edicion", "Edicion", 10),
              ("hoy", "De hoy", 11), ("estado", "Estado", 14), ("falta", "Falta", 14),
              ("libre_a", "Libre aprox.", 22)]


def filas_prensa(cli):
    filtro = normaliza_nombre(FILTRO_PRENSA)
    return [estado_prensa(cli, n, u) for n, u in cli.prensa.items()
            if not filtro or filtro in normaliza_nombre(n)]


def consejo_prensa(filas):
    """Para cada periodico: en que cuenta pedirlo, o cual se libera antes."""
    lineas = []
    grupos = {}
    for f in filas:
        grupos.setdefault(normaliza_nombre(f["periodico"]), []).append(f)
    for fs in grupos.values():
        nombre = fs[0]["periodico"]
        con_ed = [f for f in fs if f["_ed"]]
        if not con_ed:
            lineas.append("%s: no se pudo leer ninguna edicion." % nombre)
            continue
        ultima = max(f["_ed"] for f in con_ed)
        actuales = [f for f in con_ed if f["_ed"] == ultima]
        if ultima.date() == datetime.now().date():
            txt_ed = "edicion de hoy"
        else:
            txt_ed = "la de hoy aun no ha salido; ultima edicion %s" % fmt(ultima)
        prest = [f for f in actuales if f["estado"] == "PRESTADO tuyo"]
        if prest:
            f = prest[0]
            lineas.append("%s (%s): ya lo tienes PRESTADO en %s%s." % (nombre, txt_ed, f["cuenta"],
                                                                     (" (" + f["libre_a"] + ")") if f["libre_a"] else ""))
            continue
        turno = [f for f in actuales if f["estado"] == "TU TURNO"]
        if turno:
            f = turno[0]
            lineas.append("%s (%s): tu reserva YA ESTA DISPONIBLE en %s -> pidelo%s "
                          "(python3 ebib.py -acc %s -lista \"%s\" -pedir)."
                          % (nombre, txt_ed, f["cuenta"], (" " + f["libre_a"] + ", " + f["falta"]) if f["libre_a"] else "",
                             f["cuenta"], nombre))
            continue
        libres = [f for f in actuales if f["_libre"]]
        if libres:
            lineas.append("%s (%s): PIDELO en %s, esta libre."
                          % (nombre, txt_ed, " o ".join(f["cuenta"] for f in libres)))
            continue
        mias = [f for f in actuales if f["estado"] == "reservado tuyo"]
        if mias:
            f = min(mias, key=lambda x: x["_libre_en"] or datetime.max)
            lineas.append("%s (%s): ya lo tienes RESERVADO en %s%s."
                          % (nombre, txt_ed, f["cuenta"],
                             (", te toca dentro de %s (%s)" % (falta(f["_libre_en"]), fmt(f["_libre_en"], hora=True)))
                             if f["_libre_en"] else "; la web no dice cuando te toca"))
            continue
        con_hora = [f for f in actuales if f["_libre_en"]]
        if con_hora:
            f = min(con_hora, key=lambda x: x["_libre_en"])
            lineas.append("%s (%s): ocupado%s. Antes se libera en %s: dentro de %s (%s)."
                          % (nombre, txt_ed, " en todas" if len(actuales) > 1 else "",
                             f["cuenta"], falta(f["_libre_en"]), fmt(f["_libre_en"], hora=True)))
        else:
            lineas.append("%s (%s): ocupado en %s; la web no indica cuando se libera."
                          % (nombre, txt_ed, ", ".join(f["cuenta"] for f in actuales)))
    return lineas


# ------------------------------------------------------ lista de prioridades

# Nombres cortos para -lista. Se pueden anadir o cambiar con "_catalogo" en cuentas.json.
CATALOGO_DEFECTO = {
    "mundo":       {"nombre": "El Mundo", "serial": "El Mundo (Ed. Nacional)", "otros": ["El Mundo"],
                    "naturaleza": "newspaper", "periodicidad": "diaria"},
    "mundomadrid": {"nombre": "El Mundo Madrid", "serial": "El Mundo - Edición Madrid",
                    "naturaleza": "newspaper", "periodicidad": "diaria"},
    "pais":        {"nombre": "El Pais", "serial": "El País, edición Madrid", "otros": ["El País"],
                    "naturaleza": "newspaper", "periodicidad": "diaria"},
    "cincodias":   {"nombre": "Cinco Dias", "serial": "Cinco Días",
                    "naturaleza": "newspaper", "periodicidad": "diaria"},
    "diarioavila": {"nombre": "Diario de Avila", "serial": "Diario de Ávila",
                    "naturaleza": "newspaper", "periodicidad": "diaria"},
    "eljueves":    {"nombre": "El Jueves", "serial": "El Jueves",
                    "naturaleza": "magazine", "periodicidad": "mensual"},
}
PERIODICIDADES = {"diaria": 1, "diario": 1, "semanal": 7, "quincenal": 15, "mensual": 31,
                  "bimestral": 62, "trimestral": 92}


def clave(t):
    return re.sub(r"[^a-z0-9]", "", normaliza_nombre(t))


def compacto(t):
    """'El Pais, edicion Madrid' -> 'paismadrid' (sin articulos ni 'edicion')."""
    if not t:
        return ""
    return "".join(w for w in re.findall(r"[a-z0-9]+", normaliza_nombre(t)) if w not in PALABRAS_VACIAS)


RE_FECHA_TITULO = re.compile(r"\s*-?\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*$")


def fecha_titulo(d):
    """Fecha como la escribe eBiblio en los titulos: 3/10/2026 (sin ceros delante)."""
    return "%d/%d/%d" % (d.day, d.month, d.year)


def calidad(el, e):
    """Cuanto se parece el titulo de la ficha al nombre buscado (sin la fecha):
    3 = igual ("El País"), 2 = empieza igual ("El País Semanal"), 1 = lo contiene
    ("Suplemento El País"), 0 = nada."""
    base = clave(el.get("serial") or el["nombre"])
    if not base:
        return 0
    trozos = [e.get("titulo") or ""] + (e.get("texto") or "").split("|")
    mejor = 0
    for t in trozos:
        c = clave(RE_FECHA_TITULO.sub("", t.strip()))
        if not c:
            continue
        if c == base:
            return 3
        if c.startswith(base):
            mejor = max(mejor, 2)
        elif base in c:
            mejor = max(mejor, 1)
    return mejor


def resolver_elemento(token, catalogo):
    """'eljueves:mensual' -> datos del elemento (nombre, serial, naturaleza, periodicidad)."""
    nombre, _, per = token.partition(":")
    fecha = None
    m = RE_FECHA_TITULO.search(nombre)
    if m:
        try:
            fecha = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            fecha = None
    if fecha:
        # "El País - 3/10/2026": titulo exacto de una edicion; se busca tal cual, como en la web
        base = nombre[:m.start()].strip(" -")
        el = {"nombre": base, "serial": "", "naturaleza": "", "periodicidad": "diaria",
              "fecha": fecha, "literal": "%s - %s" % (base, fecha_titulo(fecha))}
        el["clave"] = nombre.strip()
        if per.strip() and per.strip().lower() in PERIODICIDADES:
            el["periodicidad"] = per.strip().lower()
        return el
    k = clave(nombre)
    el = None
    for alias, datos in catalogo.items():
        alias_extra = datos.get("alias") or []
        alias_extra = alias_extra if isinstance(alias_extra, list) else [alias_extra]
        if k in (clave(alias), clave(datos.get("nombre", "")), clave(datos.get("serial", "")),
                 compacto(datos.get("serial", "")), compacto(datos.get("nombre", "")),
                 sin_articulos(datos.get("serial", "")), sin_articulos(datos.get("nombre", ""))) or \
                k in {clave(x) for x in alias_extra} or k in {sin_articulos(x) for x in alias_extra}:
            el = dict(datos)
            break
    if el is None:
        for alias, datos in catalogo.items():   # coincidencia parcial: "jueves" -> eljueves
            if k and (k in clave(alias) or k in clave(datos.get("serial", ""))):
                el = dict(datos)
                break
    if el is None:
        el = {"nombre": nombre.strip(), "serial": "", "naturaleza": "", "periodicidad": "diaria"}
    el["clave"] = nombre.strip()
    if per.strip():
        el["periodicidad"] = per.strip().lower()
    if el.get("periodicidad") not in PERIODICIDADES:
        el["periodicidad"] = "diaria"
    return el


def ver_nombres(catalogo_usuario):
    """Tabla de nombres cortos validos para -lista."""
    filas = []
    for origen, cat in (("de serie", CATALOGO_DEFECTO), ("_catalogo", catalogo_usuario)):
        for alias, d in cat.items():
            if not isinstance(d, dict):
                continue
            filas = [f for f in filas if f["n"] != alias]      # el de _catalogo sustituye al de serie
            filas.append({"n": alias, "titulo": d.get("serial") or d.get("nombre", ""),
                          "tipo": {"newspaper": "periodico", "magazine": "revista"}.get(d.get("naturaleza", ""),
                                                                                     d.get("naturaleza", "")),
                          "periodicidad": d.get("periodicidad", "diaria"), "origen": origen})
    print("\n=== NOMBRES PARA -lista ===")
    tabla(filas, [("n", "Nombre", 12), ("titulo", "Publicacion en eBiblio", 30), ("tipo", "Tipo", 9),
                  ("periodicidad", "Periodo", 9), ("origen", "Origen", 9)])
    print('\nUso: python3 ebib.py -acc todas -lista "%s"' % ",".join(f["n"] for f in filas[:3]))
    print("Otros nombres: se buscan tal cual en eBiblio. Para fijarlos, anadelos a \"_catalogo\" en cuentas.json.")


def es_actual(edicion, periodicidad):
    """Si la ultima edicion es la vigente segun su periodicidad."""
    hoy = datetime.now().date()
    d = edicion.date()
    if periodicidad in ("diaria", "diario"):
        return d == hoy
    if periodicidad == "mensual":
        return (d.year, d.month) == (hoy.year, hoy.month) or (hoy - d).days < 28
    return (hoy - d).days < PERIODICIDADES[periodicidad]


def texto_periodo(periodicidad):
    return {"diaria": "la de hoy", "diario": "la de hoy", "semanal": "la de esta semana",
            "quincenal": "la de esta quincena", "mensual": "la de este mes",
            "bimestral": "la de este bimestre", "trimestral": "la de este trimestre"}[periodicidad]


PALABRAS_VACIAS = {"el", "la", "los", "las", "de", "del", "y", "ed", "edicion", "diario", "the"}


def palabras_clave(t):
    """'El Mundo - Edicion Madrid' -> {'mundo', 'madrid'}"""
    return {w for w in re.findall(r"[a-z0-9]+", normaliza_nombre(t)) if w not in PALABRAS_VACIAS and len(w) > 1}


def parecido(el, texto):
    """Si el texto de una ficha corresponde al elemento (nombre exacto o todas sus palabras)."""
    kt = clave(texto)
    n = el.get("serial") or el.get("nombre")
    if n and clave(n) and clave(n) in kt:
        return True
    pt = palabras_clave(texto)
    ps = palabras_clave(el.get("serial") or el["nombre"])
    return bool(ps) and ps <= pt


def urls_elemento(cli, el):
    urls = []
    dirs = el.get("url") or []
    for u in (dirs if isinstance(dirs, list) else [dirs]):   # "url" en _catalogo: solo si es de esta cuenta
        if isinstance(u, str) and urlparse(u).netloc == urlparse(cli.url("/")).netloc:
            urls.append(u)
    for n, u in cli.prensa.items():      # direcciones de "prensa" en cuentas.json
        if clave(n) in (clave(el["nombre"]), clave(el["clave"])):
            urls.append(u)
    if el.get("literal"):
        urls.append(cli.url("/resources?q=" + requests.utils.quote(el["literal"])))
    elif not el.get("serial"):
        # periodico que no esta en el catalogo: buscar la edicion de hoy con el formato de eBiblio
        urls.append(cli.url("/resources?q=" + requests.utils.quote(
            "%s - %s" % (el["nombre"], fecha_titulo(datetime.now())))))
    if el.get("serial"):
        q = "serial_name=" + requests.utils.quote(el["serial"])
        if el.get("naturaleza"):
            urls.append(cli.url("/resources?nature=%s&%s" % (el["naturaleza"], q)))
        urls.append(cli.url("/resources?" + q))
    urls.append(cli.url("/resources?q=" + requests.utils.quote(el.get("serial") or el["nombre"])))
    sencillo = " ".join(sorted(palabras_clave(el.get("serial") or el["nombre"]),
                               key=lambda w: normaliza_nombre(el.get("serial") or el["nombre"]).find(w)))
    if sencillo:
        urls.append(cli.url("/resources?q=" + requests.utils.quote(sencillo)))
    return list(dict.fromkeys(urls))


def estado_elemento(cli, el):
    """Como estado_elemento_1, probando tambien los otros nombres de la publicacion
    ("otros" en el catalogo: El Pais es "El País, edición Madrid" en Madrid y "El País" en otras)."""
    r = estado_elemento_1(cli, el)
    for otro in ([] if r["ed"] else (el.get("otros") or [])):
        r = estado_elemento_1(cli, dict(el, serial=otro))
        if r["ed"]:
            break
    return r


def estado_elemento_1(cli, el):
    """Ultima edicion del elemento en una cuenta, con su estado."""
    r = {"cuenta": cli.nombre, "cli": cli, "ed": None, "e": None, "url": ""}
    for u in urls_elemento(cli, el):
        html = cli.get(u)
        if not html:
            continue
        manana = datetime.now() + timedelta(days=1)
        eds = [e for e in ejemplares(html) if e["edicion"] and e["edicion"] <= manana]
        if "q=" in u or "serial_name=" in u:   # quedarse con lo que se parezca al nombre
            eds = [e for e in eds if parecido(el, e["texto"]) or calidad(el, e) >= 2]
        if el.get("fecha"):                    # edicion concreta pedida: "El País - 3/10/2026"
            eds = [e for e in eds if e["edicion"].date() == el["fecha"].date()]
        if eds:                                # el titulo mas parecido gana: "El País" antes que "Suplemento El País"
            for e in eds:
                e["calidad"] = calidad(el, e)
            mejor = max(e["calidad"] for e in eds)
            eds = [e for e in eds if e["calidad"] == mejor]
        if DEBUG:
            for e in eds:
                log_debug("lista %s en %s: %s" % (el["nombre"], cli.nombre, fmt(e["edicion"])),
                          "calidad=%s libre=%s reserva=%s tuyo=%s libre_en=%s | %s | %s"
                          % (e.get("calidad"), e["libre"], e["reserva"], e["ya_tuyo"], e["libre_en"], u,
                             e["texto"][:200]))
        if eds:
            ultima = max(e["edicion"] for e in eds)
            del_dia = [e for e in eds if e["edicion"] == ultima]
            for x in del_dia:
                if not x["ya_tuyo"]:
                    mp = cli.prestamo_mio(x["rid"], el.get("serial") or el["nombre"], x["edicion"])
                    if mp:
                        x["ya_tuyo"] = True
                        x["vence_mio"] = mp.get("vence")
                if not x["ya_tuyo"]:
                    mr = cli.reserva_mia(x["rid"], el.get("serial") or el["nombre"], x["edicion"])
                    if mr:
                        x["mi_reserva"] = True
                        x["libre_en_mia"] = mr.get("_disp")
                        if mr.get("_disponible"):     # ya te toca: se puede tomar en prestamo
                            x["reserva_lista"] = True
                            x["limite"] = mr.get("_limite")
                            x["bloqueo"] = mr.get("_bloqueo", "")
                            if mr.get("_form") is not None or x["f_libre"] is None:
                                x["f_libre"] = mr["_form"]
                                x["url_form"] = mr["_pagina"]
            e = (next((x for x in del_dia if x["ya_tuyo"]), None)
                 or next((x for x in del_dia if x.get("reserva_lista")), None)
                 or next((x for x in del_dia if x["libre"]), None)
                 or next((x for x in del_dia if x["mi_reserva"]), None)
                 or next((x for x in del_dia if x["reserva"]), None) or del_dia[0])
            if DEBUG:
                log_debug("elegido %s en %s" % (el["nombre"], cli.nombre),
                          "libre=%s mi_reserva=%s libre_en(ficha)=%s libre_en(mis reservas)=%s | %s"
                          % (e["libre"], e["mi_reserva"], e["libre_en"], e["libre_en_mia"], e["texto"][:400]))
            r.update({"ed": ultima, "e": e, "url": u})
            return r
    return r


def decidir(el, estados):
    """Elige cuenta y accion (prestamo / reserva) para un elemento."""
    con = [x for x in estados if x["ed"]]
    res = {"elemento": el, "cuenta": "", "accion": "", "estado": "", "falta": "", "libre_a": "",
           "edicion": "", "actual": "", "mensaje": "", "objetivo": None}
    if not con:
        res.update({"estado": "no encontrado",
                    "mensaje": "%s: no lo encuentro en ninguna cuenta. Mira su nombre exacto con "
                               "-buscar \"%s\" -en madrid (o castillaleon) y anadelo a _catalogo "
                               "en cuentas.json (ver -vernombres)." % (el["nombre"], el["nombre"])})
        return res
    ultima = max(x["ed"] for x in con)
    act = es_actual(ultima, el["periodicidad"])
    res.update({"edicion": fmt(ultima), "actual": "si" if act else "NO"})
    aviso = "" if act else " OJO: %s aun no ha salido; la ultima es del %s." % (texto_periodo(el["periodicidad"]),
                                                                              fmt(ultima))
    cands = [x for x in con if x["ed"] == ultima]
    tuyo = next((x for x in cands if x["e"]["ya_tuyo"]), None)
    lista_ = next((x for x in cands if x["e"].get("reserva_lista")), None)
    libre = next((x for x in cands if x["e"]["libre"]), None)
    mia = [x for x in cands if x["e"]["mi_reserva"]]
    reser = [x for x in cands if x["e"]["reserva"] and not x["e"]["mi_reserva"]]
    if tuyo:
        v = tuyo["e"].get("vence_mio") or ""
        res.update({"cuenta": tuyo["cuenta"], "accion": "-", "estado": "ya lo tienes", "falta": "",
                    "mensaje": "%s (%s): ya lo tienes PRESTADO en %s%s; no se vuelve a pedir ni a reservar.%s"
                               % (el["nombre"], fmt(ultima), tuyo["cuenta"], (" (vence: %s)" % v) if v else "", aviso)})
    elif lista_ and lista_["e"].get("bloqueo") and lista_["e"].get("f_libre") is None:
        # la web no deja tomarla: limite de prestamos de la biblioteca
        lim = lista_["e"].get("limite")
        extra = limites_cuenta(lista_["cli"])
        res.update({"cuenta": lista_["cuenta"], "accion": "-", "estado": "LIMITE",
                    "falta": ("quedan " + falta(lim)) if lim else "ya", "libre_a": fmt(lim, hora=True) if lim else "",
                    "bloqueo": True,
                    "mensaje": "%s (%s): tu reserva esta disponible en %s%s, pero eBiblio NO deja pedirla: %s%s%s"
                               % (el["nombre"], fmt(ultima), lista_["cuenta"],
                                  (" hasta %s (quedan %s)" % (fmt(lim, hora=True), falta(lim))) if lim else "",
                                  lista_["e"]["bloqueo"].rstrip("."), (". " + extra) if extra else "", ".")})
    elif lista_:
        lim = lista_["e"].get("limite")
        puede = lista_["e"].get("f_libre") is not None
        res.update({"cuenta": lista_["cuenta"], "accion": "PRESTAMO" if puede else "-", "estado": "TU TURNO",
                    "falta": ("quedan " + falta(lim)) if lim else "ya", "libre_a": fmt(lim, hora=True) if lim else "",
                    "objetivo": lista_ if puede else None, "candidato": lista_,
                    "mensaje": "%s (%s): tu reserva YA ESTA DISPONIBLE en %s -> pedir prestamo%s.%s%s"
                               % (el["nombre"], fmt(ultima), lista_["cuenta"],
                                  (" antes de %s (quedan %s)" % (fmt(lim, hora=True), falta(lim))) if lim else "",
                                  "" if puede else " No encuentro el boton: pidelo en la web.", aviso)})
    elif libre:
        res.update({"cuenta": libre["cuenta"], "accion": "PRESTAMO", "estado": "LIBRE", "falta": "ya",
                    "objetivo": libre, "alternativas": [x for x in cands if x["e"]["libre"] and x is not libre],
                    "mensaje": "%s (%s): LIBRE en %s -> pedir prestamo.%s" % (el["nombre"], fmt(ultima), libre["cuenta"], aviso)})
    elif mia:
        def cuando(x):
            return x["e"]["libre_en_mia"] or x["e"]["libre_en"]
        con_hora = [x for x in mia if cuando(x)]
        x = min(con_hora, key=cuando) if con_hora else mia[0]
        le = cuando(x)
        res.update({"cuenta": x["cuenta"], "accion": "-", "estado": "reservado tuyo",
                    "falta": falta(le) if le else "?", "libre_a": fmt(le, hora=True) if le else "",
                    "mensaje": "%s (%s): ya lo tienes RESERVADO en %s%s.%s"
                               % (el["nombre"], fmt(ultima), x["cuenta"],
                                  (", te toca dentro de %s (%s)" % (falta(le), fmt(le, hora=True))) if le else
                                  "; la web no dice cuando te toca", aviso)})
    elif reser:
        con_hora = [x for x in reser if x["e"]["libre_en"]]
        x = min(con_hora, key=lambda y: y["e"]["libre_en"]) if con_hora else reser[0]
        le = x["e"]["libre_en"]
        res.update({"cuenta": x["cuenta"], "accion": "RESERVA", "estado": "ocupado", "objetivo": x,
                    "alternativas": sorted([y for y in reser if y is not x],
                                           key=lambda y: y["e"]["libre_en"] or datetime.max),
                    "falta": falta(le) if le else "?", "libre_a": fmt(le, hora=True) if le else "",
                    "mensaje": "%s (%s): ocupado%s -> reservar en %s%s.%s"
                               % (el["nombre"], fmt(ultima), " en todas" if len(reser) > 1 else "", x["cuenta"],
                                  (", se libera dentro de %s (%s)" % (falta(le), fmt(le, hora=True))) if le else
                                  ", la web no dice cuando se libera", aviso)})
    else:
        res.update({"cuenta": cands[0]["cuenta"], "accion": "-", "estado": "sin boton",
                    "mensaje": "%s (%s): la web no ofrece prestamo ni reserva en %s.%s"
                               % (el["nombre"], fmt(ultima), cands[0]["cuenta"], aviso)})
    return res


def aviso_web(html):
    sopa = BeautifulSoup(html or "", "html.parser")
    for t in sopa(["script", "style", "noscript"]):
        t.decompose()
    avisos = [limpiar(e.get_text(" ")) for e in sopa.select(
        ".alert, .flash, .notice, .error, .errors, [role=alert], .callout, .message, "
        "[class*=flash], [class*=alert], [class*=notice], [class*=toast]")]
    avisos = [a for a in dict.fromkeys(avisos) if a]
    return " / ".join(avisos)[:250]


RE_MOTIVO = re.compile(r"(?:Imposible|No (?:es posible|puedes|se puede|se ha podido)|Has alcanzado|Ya tienes|"
                       r"Has superado|L[ií]mite)[^|]{0,170}", re.I)


def motivo_web(html):
    """Frases de la pagina que explican un rechazo ("Has alcanzado el maximo de reservas...")."""
    sopa = BeautifulSoup(html or "", "html.parser")
    for t in sopa(["script", "style", "noscript"]):
        t.decompose()
    txt = limpiar(sopa.get_text(" | "))
    frases = [limpiar(m.group(0)).strip(" |.") for m in RE_MOTIVO.finditer(txt)]
    return ". ".join(dict.fromkeys(f for f in frases if len(f) > 8))[:300]


RE_LIMITE = re.compile(r"alcanzado el m[aá]ximo|has superado|l[ií]mite de (?:reservas|pr[eé]stamos)", re.I)


RE_FALLO = re.compile(r"no (?:se ha podido|es posible|puede)|l[ií]mite|m[aá]ximo|error|no disponible|"
                      r"ya (?:tiene|tienes)|no permitid|suspendid", re.I)


RE_TOKEN = re.compile(r'(name="(?:authenticity_token|_csrf|csrf[^"]*)"[^>]*value=")[^"]*', re.I)


def html_seguro(tag, n=1500):
    """HTML de un elemento sin tokens de seguridad, para el informe."""
    t = RE_TOKEN.sub(r"\1***", str(tag))
    return t[:n]


def rellenar_form(form):
    """Datos que enviaria el navegador al pulsar el boton principal del formulario."""
    datos, radios = {}, {}
    for i in form.find_all(["input", "select", "textarea"]):
        n = i.get("name")
        if not n:
            continue
        t = (i.get("type") or "text").lower()
        if i.name == "select":
            o = i.find("option", selected=True) or i.find("option")
            datos[n] = o.get("value", limpiar(o.get_text())) if o else ""
        elif t == "radio":
            radios.setdefault(n, []).append(i)
        elif t == "checkbox":
            etiqueta = (i.get("id") or "") + " " + n
            if i.has_attr("checked") or i.has_attr("required") or re.search(r"acept|accept|terms|condic|agree", etiqueta, re.I):
                datos[n] = i.get("value", "on")
        elif t in ("submit", "button", "image", "reset", "file"):
            continue
        else:
            datos[n] = i.get("value", "")
    for n, grupo in radios.items():      # si ninguno esta marcado, el primero (p. ej. el formato)
        el = next((r for r in grupo if r.has_attr("checked")), grupo[0])
        datos[n] = el.get("value", "on")
    boton = next((b for b in form.find_all(["button", "input"])
                  if (b.get("type") or "submit").lower() == "submit" and b.get("name")
                  and not re.search(r"anular|cancel|devolver", limpiar(b.get_text(" ")) + (b.get("value") or ""), re.I)), None)
    if boton is not None:
        datos[boton["name"]] = boton.get("value", "")
    return datos


def puntuar_form(form):
    """Cuanto se parece un formulario al de 'confirmar el prestamo'."""
    acc = form.get("action") or ""
    txt = limpiar(form.get_text(" ")) + " " + " ".join((b.get("value") or "") for b in form.find_all("input"))
    metodo = form.find("input", attrs={"name": "_method"})
    malo = re.compile(r"anular|cancelar|devolver|delete|return|sign_out|logout", re.I)
    botones = [limpiar(b.get_text(" ")) + " " + (b.get("value") or "") for b in form.find_all(["button", "input"])
               if (b.get("type") or "submit").lower() == "submit" and (b.name == "button" or b.get("type"))]
    if malo.search(acc) or (metodo and metodo.get("value", "").lower() == "delete") or \
            re.search(r"/resources\b|/sessions", acc) or (botones and all(malo.search(b) for b in botones)) or \
            (not botones and malo.search(txt)):
        return -1
    p = 0
    if (form.get("method") or "get").lower() == "post":
        p += 1
    if re.search(r"loan|checkout|borrow|prest|activat", acc, re.I):
        p += 3
    if re.search(r"prestar|confirmar|tomar|aceptar|continuar|recuperar|activar|borrow|confirm", txt, re.I):
        p += 3
    return p


RE_BOTON_BUENO = re.compile(r"prestar|confirmar|tomar|aceptar|continuar|recuperar|activar|borrow|confirm", re.I)


def enlaces_accion(sopa, base):
    """Enlaces/botones que hacen una accion por JavaScript (Rails: data-method=post) con texto positivo."""
    out = []
    for el in sopa.find_all(["a", "button"]):
        txt = limpiar(el.get_text(" "))
        dm = (el.get("data-method") or "").lower()
        if not txt or re.search(r"anular|cancelar|devolver|return|delete", txt, re.I):
            continue
        if dm in ("post", "put", "patch") and RE_BOTON_BUENO.search(txt):
            u = el.get("href") or el.get("formaction") or el.get("data-url")
            if u:
                out.append((urljoin(base, u), dm, txt))
    return out


def url_de_boton(b, pagina):
    for att in ("formaction", "data-url", "data-href", "data-action", "href"):
        if b.get(att):
            return urljoin(pagina, b[att])
    m = re.search(r"""['"](/[^'"]+)['"]""", b.get("onclick") or "")
    return urljoin(pagina, m.group(1)) if m else None


def ejecutar(res):
    """Hace de verdad el prestamo o la reserva. Devuelve (ok, texto).
    Deja un informe ebib_pedir_<cuenta>.txt con cada paso (sin contrasena ni tokens)."""
    x = res["objetivo"]
    cli, e, pagina = x["cli"], x["e"], (x["e"].get("url_form") or x["url"])
    traza = ["ebib.py %s - %s en %s - %s" % (VERSION, res["accion"], cli.nombre, datetime.now().strftime("%d/%m/%Y %H:%M")),
             "Elemento: %s (%s)" % (res["elemento"]["nombre"], res["edicion"]), "Pagina de partida: %s" % pagina]

    def paso(nombre, r, datos=None):
        sopa = BeautifulSoup(r.text, "html.parser")
        tit = limpiar(sopa.title.get_text()) if sopa.title else "-"
        traza.append("\n--- %s: HTTP %s  %s  titulo='%s'" % (nombre, r.status_code, r.url, tit[:60]))
        if datos is not None:
            traza.append("    enviado: %s" % ", ".join("%s=%s" % (k, "***" if re.search("token|csrf", k, re.I) else v)
                                                      for k, v in datos.items()))
        av = aviso_web(r.text)
        if av:
            traza.append("    aviso de la web: %s" % av)
        for f in sopa.find_all("form"):
            if not re.search(r"^/resources\b|/resources\?", f.get("action") or ""):
                traza.append("    formulario (puntos %d):\n%s" % (puntuar_form(f), html_seguro(f, 900)))
        for el in sopa.find_all(["a", "button"]):
            t_ = limpiar(el.get_text(" "))
            if el.get("data-method") or re.search(r"prestar|recuperar|activar|confirmar|leer|descargar|abrir|"
                                                   r"devolver|cancelar", t_, re.I):
                traza.append("    boton/enlace: %s" % html_seguro(el, 300))
        principal = sopa.find("main") or sopa.body
        if principal is not None:
            traza.append("    texto de la pagina: %s" % limpiar(principal.get_text(" | "))[:1500])
        log_debug(nombre, "\n".join(traza[-4:]))
        return sopa

    try:
        if res["accion"] == "PRESTAMO":
            f = e["f_libre"]
            traza.append("\nBoton usado:\n" + html_seguro(f))
            if f.name == "button":
                padre = f.find_parent("form")
                u = url_de_boton(f, pagina)
                if padre is not None:
                    f = padre
                elif u:
                    r = cli.s.get(u, headers={"Referer": pagina}, timeout=30)
                    traza.append("    GET %s -> HTTP %s" % (urlparse(u).path, r.status_code))
                    if r.status_code in (404, 405):     # boton de JavaScript: la web lo envia por detras
                        cli.olvidar()
                        meta = BeautifulSoup(cli.get(pagina) or "", "html.parser").find("meta", attrs={"name": "csrf-token"})
                        tok = meta.get("content", "") if meta else ""
                        cab = {"Referer": pagina, "X-CSRF-Token": tok, "X-Requested-With": "XMLHttpRequest",
                               "Accept": "*/*"}
                        for metodo in ("PUT", "POST", "PATCH"):
                            r = cli.s.request(metodo, u, headers=cab, timeout=30)
                            traza.append("    %s %s -> HTTP %s %s" % (metodo, urlparse(u).path, r.status_code,
                                                                 (r.text or "")[:150].replace("\n", " ")))
                            if r.status_code not in (404, 405):
                                break
                    sopa = paso("paso 1 (boton Prestar)", r)
                    f = None
                else:
                    guardar_traza(cli, traza)
                    return False, "el boton Prestar funciona con JavaScript y no se donde envia; pidelo en la web"
            if f is not None and f.name == "a":
                r = cli.s.get(urljoin(pagina, f["href"]), headers={"Referer": pagina}, timeout=30)
                sopa = paso("paso 1 (enlace Prestar)", r)
            elif f is not None:
                datos = rellenar_form(f)
                u = urljoin(pagina, f.get("action") or pagina)
                if (f.get("method") or "get").lower() == "post":
                    r = cli.s.post(u, data=datos, headers={"Referer": pagina}, timeout=30)
                else:
                    r = cli.s.get(u, params=datos, headers={"Referer": pagina}, timeout=30)
                sopa = paso("paso 1 (formulario Prestar)", r, datos)
            else:
                sopa = paso("paso 1 (boton con direccion)", r)
            # pantallas de confirmacion (formato, condiciones...): hasta 3
            for n in range(2, 5):
                cands = sorted(((puntuar_form(g), g) for g in sopa.find_all("form")), key=lambda t: -t[0])
                cands = [g for p_, g in cands if p_ >= 3]
                if not cands:
                    acciones = enlaces_accion(sopa, r.url)
                    if not acciones:
                        break
                    u2, dm, txt = acciones[0]
                    meta = sopa.find("meta", attrs={"name": "csrf-token"})
                    tok = meta.get("content", "") if meta else ""
                    d2 = {"authenticity_token": tok}
                    if dm != "post":
                        d2["_method"] = dm
                    r = cli.s.post(u2, data=d2, headers={"Referer": r.url, "X-CSRF-Token": tok}, timeout=30)
                    sopa = paso("paso %d (enlace '%s', data-method=%s)" % (n, txt[:30], dm), r, d2)
                    continue
                fc = cands[0]
                d2 = rellenar_form(fc)
                u2 = urljoin(r.url, fc.get("action") or r.url)
                if (fc.get("method") or "get").lower() == "post":
                    r = cli.s.post(u2, data=d2, headers={"Referer": r.url}, timeout=30)
                else:
                    r = cli.s.get(u2, params=d2, headers={"Referer": r.url}, timeout=30)
                sopa = paso("paso %d (confirmacion)" % n, r, d2)
        else:
            f = e["f_reserva"]
            traza.append("\nBoton usado:\n" + html_seguro(f))
            datos = rellenar_form(f)
            u = urljoin(pagina, f.get("action"))
            r = cli.s.post(u, data=datos, headers={"Referer": pagina}, timeout=30)
            paso("reserva", r, datos)
    except requests.RequestException as ex:
        guardar_traza(cli, traza)
        return False, "error de red: %s" % ex
    msg = aviso_web(r.text) or motivo_web(r.text)

    # comprobar de verdad: volver a mirar el estado
    cli._cache.clear()
    cli.olvidar()
    nuevo = estado_elemento(cli, res["elemento"])
    ne = nuevo.get("e") or {}
    if res["accion"] == "PRESTAMO" and not ne.get("ya_tuyo"):
        try:
            cli._cache.clear()
            k = clave(res["elemento"].get("serial") or res["elemento"]["nombre"])
            for f in filas_prestamos(cli):
                if f.get("id") == e.get("rid") or (k and k in clave(f["titulo"] + " " + f.get("autor", ""))):
                    ne = dict(ne)
                    ne["ya_tuyo"] = True
                    break
        except Exception as ex:
            log_debug("comprobacion de prestamos", ex)
    if res["accion"] == "PRESTAMO":
        ok = bool(ne.get("ya_tuyo")) or (not ne.get("reserva_lista") and not ne.get("libre") and
                                         bool(msg) and not RE_FALLO.search(msg))
        estado_final = "ya lo tienes prestado" if ne.get("ya_tuyo") else (
            "sigue pendiente de pedir" if ne.get("reserva_lista") or ne.get("libre") else "no se sabe")
    else:
        positivo = bool(msg) and not RE_FALLO.search(msg) and re.search(r"registrad|realizad|correct|confirmad|reservad",
                                                                         msg, re.I)
        ok = bool(ne.get("mi_reserva")) or bool(positivo)
        estado_final = ("reservado" if ne.get("mi_reserva") else
                        "la web dice que esta hecho (no he podido verlo en tus reservas)" if positivo else
                        "no aparece como reservado")
    traza.append("\n--- Comprobacion final: %s" % estado_final)
    ruta = guardar_traza(cli, traza)
    texto = (msg + ". " if msg else "") + "Comprobado: %s." % estado_final
    if not ok and not RE_LIMITE.search(msg or ""):
        texto += " Informe: %s (enviaselo a Claude)" % ruta
    elif not ok:
        texto += " (Es un limite de tu biblioteca: espera a devolver o a que caduque algo.)"
    return ok, texto


def informe_sin_boton(res):
    x = res.get("candidato") or {}
    cli = x.get("cli")
    if cli is None:
        return "-"
    traza = ["ebib.py %s - TU TURNO sin boton en %s - %s" % (VERSION, cli.nombre, datetime.now().strftime("%d/%m/%Y %H:%M")),
             "Elemento: %s (%s)" % (res["elemento"]["nombre"], res["edicion"])]
    mr = None
    for f in getattr(cli, "_mis_reservas", {}).values():
        if f.get("_disponible"):
            mr = f
            traza.append("\nReserva disponible leida de %s:\n  titulo: %s\n  texto: %s"
                         % (f.get("_pagina"), f["titulo"], f.get("_texto", "")[:400]))
            if f.get("_nodo") is not None:
                traza.append("\nHTML de la ficha (sin claves):\n" + html_seguro(f["_nodo"], 4000))
                padre = f["_nodo"].parent
                if padre is not None:
                    traza.append("\nHTML del contenedor de encima (sin claves):\n" + html_seguro(padre, 6000))
    if mr is None:
        traza.append("\nNo encontre la reserva en tu pagina de reservas.")
    if x.get("e") and x["e"].get("nodo") is not None:
        traza.append("\nFicha del catalogo (sin claves):\n" + html_seguro(x["e"]["nodo"], 3000))
    elif x.get("e"):
        traza.append("\nFicha del catalogo (texto): %s" % (x["e"].get("texto") or "")[:1500])
    # pagina completa de actividad, por si hace falta
    try:
        html = cli.get(cli.paginas.get("reservas", "/my_profile/activity")) or ""
        with open(os.path.abspath("ebib_actividad_%s.html" % cli.nombre), "w", encoding="utf-8") as fh:
            fh.write(RE_TOKEN.sub(r"\1***", html))
        traza.append("\nPagina completa guardada en ebib_actividad_%s.html" % cli.nombre)
    except Exception:
        pass
    return guardar_traza(cli, traza)


def guardar_traza(cli, traza):
    ruta = os.path.abspath("ebib_pedir_%s.txt" % cli.nombre)
    try:
        with open(ruta, "w", encoding="utf-8") as fh:
            fh.write("\n".join(traza) + "\n")
    except OSError:
        pass
    return ruta


COL_LISTA = [("orden", "N", 2), ("nombre", "Elemento", 16), ("periodicidad", "Periodo", 8),
             ("edicion", "Ultima ed.", 10), ("actual", "Actual", 6), ("cuenta", "Cuenta", 12),
             ("estado", "Estado", 12), ("accion", "Accion", 9), ("falta", "Falta", 15)]


def sugerir_nombre(clientes, el, catalogo):
    """Si un elemento no se encuentra, busca en tus prestamos y reservas algo parecido
    (p. ej. pides 'mundomadrid' y lo que tienes reservado es 'El Mundo (Ed. Nacional)')."""
    buscadas = {w for w in palabras_clave(el.get("serial") or el["nombre"]) if not w.isdigit()}
    buscadas |= {w for w in palabras_clave(el["clave"]) if not w.isdigit()}
    orden = [w for w in re.findall(r"[a-z0-9]+", normaliza_nombre(el.get("serial") or el["nombre"]))
             if w in buscadas]
    primera = orden[0] if orden else ""     # la palabra principal ("mundo" en "El Mundo Madrid")
    vistos, out = set(), []
    for cli in clientes:
        act = actividad(cli) or {}
        for sec, que in (("lista", "reserva DISPONIBLE"), ("espera", "reserva"), ("prestamos", "prestamo")):
            for it in act.get(sec, []):
                tit = re.sub(r"\s*-\s*\d{1,2}/\d{1,2}/\d{2,4}.*$", "", it.get("titulo", "")).strip()
                pt = {w for w in palabras_clave(tit) if not w.isdigit()}
                comunes = buscadas & pt
                if not comunes or tit in vistos:
                    continue
                if not any(w in clave(el["clave"]) for w in comunes) or (primera and primera not in pt):
                    continue
                vistos.add(tit)
                alias = next((a for a, d in catalogo.items() if isinstance(d, dict)
                              and (d.get("serial") or d.get("nombre")) and parecido(d, tit)
                              and palabras_clave(d.get("serial") or d.get("nombre")) <= pt), None)
                out.append((tit, que, cli.nombre, alias))
    return out


def ejecutar_lista(clientes, lista, catalogo, pedir):
    elementos = [resolver_elemento(t, catalogo) for t in lista.split(",") if t.strip()]
    filas, resultados = [], []
    for i, el in enumerate(elementos, 1):
        estados = [estado_elemento(cli, el) for cli in clientes]
        res = decidir(el, estados)
        resultados.append(res)
        filas.append({"orden": i, "nombre": el["nombre"], "periodicidad": el["periodicidad"],
                      "edicion": res["edicion"], "actual": res["actual"], "cuenta": res["cuenta"],
                      "estado": res["estado"], "accion": res["accion"], "falta": res["falta"],
                      "libre_a": res["libre_a"]})
    print("\n=== LISTA (por orden de prioridad) ===")
    tabla(filas, COL_LISTA)
    print("\nQue hacer:")
    for i, res in enumerate(resultados, 1):
        print("  %d. %s" % (i, res["mensaje"]))
        if res["estado"] == "no encontrado":
            for tit, que, cuenta, alias in sugerir_nombre(clientes, res["elemento"], catalogo):
                if alias:
                    print('     -> Quizas es "%s" (tienes una %s en %s): usa -lista "%s"'
                          % (tit, que, cuenta, alias))
                else:
                    print('     -> Quizas es "%s" (tienes una %s en %s): usa -lista "%s" '
                          'o anadelo a _catalogo' % (tit, que, cuenta, tit))
    if not pedir:
        print("\n(Solo consulta. Para pedir/reservar de verdad en este orden, anade -pedir)")
        return filas
    print("\n=== PIDIENDO (en orden) ===")
    for i, res in enumerate(resultados, 1):
        if res["accion"] not in ("PRESTAMO", "RESERVA"):
            if res.get("bloqueo"):
                print("  %d. %s: no se puede pedir ahora (limite de prestamos de tu biblioteca; ver arriba)."
                      % (i, res["elemento"]["nombre"]))
            elif res["estado"] == "TU TURNO":
                ruta = informe_sin_boton(res)
                print("  %d. %s: tu reserva esta disponible pero NO ENCUENTRO el boton Prestar. "
                      "Pidelo en la web. Informe: %s (enviaselo a Claude)" % (i, res["elemento"]["nombre"], ruta))
            else:
                print("  %d. %s: nada que hacer (%s)." % (i, res["elemento"]["nombre"], res["estado"]))
            continue
        if res["actual"] == "NO" and res["estado"] != "TU TURNO":
            print("  %d. %s: no lo pido, %s aun no ha salido (la ultima es del %s)."
                  % (i, res["elemento"]["nombre"], texto_periodo(res["elemento"]["periodicidad"]), res["edicion"]))
            continue
        # ultima comprobacion: no pedir ni reservar algo que ya tienes prestado o reservado
        x = res["objetivo"]
        el = res["elemento"]
        x["cli"].olvidar()
        nombre_ = el.get("serial") or el["nombre"]
        if x["cli"].prestamo_mio(x["e"]["rid"], nombre_, x["e"]["edicion"]):
            print("  %d. %s: ya lo tienes PRESTADO en %s; no se vuelve a pedir." % (i, el["nombre"], res["cuenta"]))
            continue
        if res["accion"] == "RESERVA" and x["cli"].reserva_mia(x["e"]["rid"], nombre_, x["e"]["edicion"]):
            print("  %d. %s: ya lo tienes RESERVADO en %s; no se vuelve a reservar." % (i, el["nombre"], res["cuenta"]))
            continue
        ok, txt = ejecutar(res)
        que = "prestamo" if res["accion"] == "PRESTAMO" else "reserva"
        print("  %d. %s -> %s en %s: %s. %s" % (i, res["elemento"]["nombre"], que, res["cuenta"],
                                               "HECHO" if ok else "NO SE PUDO", txt))
        # limite de la biblioteca alcanzado en esa cuenta: probar en la otra, si tambien lo tiene
        for alt in (res.get("alternativas") or []) if (not ok and RE_LIMITE.search(txt)) else []:
            res2 = dict(res, cuenta=alt["cuenta"], objetivo=alt)
            alt["cli"].olvidar()
            if alt["cli"].prestamo_mio(alt["e"]["rid"], nombre_, alt["e"]["edicion"]) or \
                    alt["cli"].reserva_mia(alt["e"]["rid"], nombre_, alt["e"]["edicion"]):
                print("     -> en %s ya lo tienes; no se vuelve a pedir." % alt["cuenta"])
                break
            ok, txt = ejecutar(res2)
            print("     -> lo intento en %s: %s. %s" % (alt["cuenta"], "HECHO" if ok else "NO SE PUDO", txt))
            if ok or not RE_LIMITE.search(txt):
                break
    print("\nComprueba el resultado con:  python3 ebib.py -acc todas -resumen")
    return filas



# ------------------------------------------------------------------ devolver

RE_DEVOLVER = re.compile(r"devolver|return", re.I)
RE_NO_DEVOLVER = re.compile(r"anular|cancelar (?:la |mi )?reserva|reservar|prestar\b", re.I)


def es_devolver(el):
    if el.name == "button" and el.find_parent("form") is not None:
        return False
    txt = limpiar(el.get_text(" ")) + " " + (el.get("value") or "") + " " + " ".join(
        (b.get("value") or "") + " " + limpiar(b.get_text(" ")) for b in el.find_all(["button", "input"]))
    return bool(RE_DEVOLVER.search(txt)) and not RE_NO_DEVOLVER.search(txt)


def boton_devolver(nodo):
    """Boton 'Devolver prestamo' de la ficha de un prestamo (o, si no esta dentro, el unico de alrededor)."""
    actual = nodo
    for _ in range(4):
        if actual is None:
            return None
        cands = [el for el in actual.find_all(["form", "a", "button"]) if es_devolver(el)]
        if len(cands) == 1:
            return cands[0]
        if len(cands) > 1:
            return cands[0] if actual is nodo else None
        actual = actual.parent
        if actual is not None and actual.name in ("body", "html", "[document]"):
            return None
    return None


def puntuar_devolver(form):
    acc = form.get("action") or ""
    txt = limpiar(form.get_text(" ")) + " " + " ".join((b.get("value") or "") for b in form.find_all(["input", "button"]))
    if re.search(r"/resources\b|/sessions|sign_out", acc) or RE_NO_DEVOLVER.search(txt):
        return -1
    p = 0
    if re.search(r"return|devol|loans", acc, re.I):
        p += 3
    if re.search(r"devolver|confirmar|aceptar|\bs[ií]\b", txt, re.I):
        p += 3
    return p


def devolver_uno(cli, fila):
    """Devuelve un prestamo. (ok, texto). Deja informe ebib_devolver_<cuenta>.txt."""
    pagina = fila.get("_pagina") or cli.url("/my_profile/activity")
    traza = ["ebib.py %s - DEVOLVER en %s - %s" % (VERSION, cli.nombre, datetime.now().strftime("%d/%m/%Y %H:%M")),
             "Prestamo: %s" % fila["titulo"], "Pagina: %s" % pagina]
    b = boton_devolver(fila["_nodo"]) if fila.get("_nodo") is not None else None
    if b is None:
        traza.append("No encuentro el boton Devolver. Ficha:\n" + html_seguro(fila.get("_nodo") or "", 3000))
        ruta = guardar_traza_n(cli, traza, "devolver")
        return False, "no encuentro el boton 'Devolver prestamo'; devuelvelo en la web. Informe: %s" % ruta
    traza.append("\nBoton usado:\n" + html_seguro(b))
    html_pag = cli.get(pagina) or ""
    meta = BeautifulSoup(html_pag, "html.parser").find("meta", attrs={"name": "csrf-token"})
    tok = meta.get("content", "") if meta else ""
    try:
        if b.name == "form":
            datos = rellenar_form(b)
            u = urljoin(pagina, b.get("action") or pagina)
            if (b.get("method") or "get").lower() == "post":
                r = cli.s.post(u, data=datos, headers={"Referer": pagina}, timeout=30)
            else:
                r = cli.s.get(u, params=datos, headers={"Referer": pagina}, timeout=30)
        elif b.name == "a" and (b.get("data-method") or "").lower() in ("post", "put", "patch", "delete"):
            dm = b["data-method"].lower()
            d = {"authenticity_token": tok}
            if dm != "post":
                d["_method"] = dm
            r = cli.s.post(urljoin(pagina, b["href"]), data=d, headers={"Referer": pagina, "X-CSRF-Token": tok}, timeout=30)
        else:
            u = urljoin(pagina, b["href"]) if b.name == "a" and b.get("href") and not b["href"].startswith("#") \
                else url_de_boton(b, pagina)
            if not u:
                # boton que abre una ventana: buscar en la pagina el formulario de devolucion de este prestamo
                sopa_p = BeautifulSoup(html_pag, "html.parser")
                objetivo = (b.get("data-bs-target") or b.get("data-target") or b.get("href") or "").lstrip("#")
                cont = sopa_p.find(id=objetivo) if objetivo else None
                forms = (cont.find_all("form") if cont else []) or sopa_p.find_all("form")
                forms = sorted(((puntuar_devolver(f), f) for f in forms), key=lambda t: -t[0])
                forms = [f for p_, f in forms if p_ >= 6] if not cont else [f for p_, f in forms if p_ >= 3]
                if len(forms) != 1:
                    traza.append("El boton abre una ventana y no se que formulario usar (%d posibles)." % len(forms))
                    ruta = guardar_traza_n(cli, traza, "devolver")
                    return False, "el boton Devolver funciona con JavaScript; devuelvelo en la web. Informe: %s" % ruta
                f = forms[0]
                traza.append("Formulario de la ventana:\n" + html_seguro(f))
                r = cli.s.post(urljoin(pagina, f.get("action") or pagina), data=rellenar_form(f),
                               headers={"Referer": pagina}, timeout=30)
            elif b.get("data-url") and (b.name == "button" or "js-" in " ".join(b.get("class") or [])):
                # boton de JavaScript (Cantook: class js-loan__return, data-url=.../loans/<id>/return):
                # la web hace una peticion AJAX; se prueba igual que el navegador, metodo a metodo
                r = devolver_ajax(cli, u, tok, pagina, fila, traza)
            else:
                r = cli.s.get(u, headers={"Referer": pagina}, timeout=30)
        traza.append("\n--- paso 1: HTTP %s %s  aviso: %s" % (r.status_code, r.url, aviso_web(r.text) or "-"))
        # pantalla de confirmacion
        for n in range(2, 4):
            sopa = BeautifulSoup(r.text, "html.parser")
            forms = sorted(((puntuar_devolver(f), f) for f in sopa.find_all("form")), key=lambda t: -t[0])
            forms = [f for p_, f in forms if p_ >= 3]
            if not forms or cli.prestamo_sigue(fila) is False:
                break
            f = forms[0]
            traza.append("    confirmacion:\n" + html_seguro(f, 900))
            u2 = urljoin(r.url, f.get("action") or r.url)
            if (f.get("method") or "get").lower() == "post":
                r = cli.s.post(u2, data=rellenar_form(f), headers={"Referer": r.url}, timeout=30)
            else:
                r = cli.s.get(u2, params=rellenar_form(f), headers={"Referer": r.url}, timeout=30)
            traza.append("--- paso %d: HTTP %s %s  aviso: %s" % (n, r.status_code, r.url, aviso_web(r.text) or "-"))
    except requests.RequestException as ex:
        ruta = guardar_traza_n(cli, traza, "devolver")
        return False, "error de red: %s" % ex
    msg = aviso_web(r.text)
    sigue = cli.prestamo_sigue(fila)
    traza.append("\n--- Comprobacion final: %s" % ("sigue prestado" if sigue else "ya no esta en tus prestamos"))
    ruta = guardar_traza_n(cli, traza, "devolver")
    if sigue:
        return False, "%sComprobado: sigue en tus prestamos. Informe: %s (enviaselo a Claude)" % (
            (msg + ". ") if msg else "", ruta)
    return True, "%sComprobado: ya no esta en tus prestamos." % ((msg + ". ") if msg else "")


def js_devolucion(cli, pagina, traza):
    """Lee el JavaScript de la pagina y busca el codigo del boton Devolver (js-loan__return)
    para saber que peticion hace el navegador. Devuelve (metodos, trozos)."""
    html = cli.get(pagina) or ""
    sopa = BeautifulSoup(html, "html.parser")
    fuentes = [("(en la pagina)", sc.string or "") for sc in sopa.find_all("script") if not sc.get("src")]
    for sc in sopa.find_all("script", src=True)[:12]:
        src = urljoin(pagina, sc["src"])
        if re.search(r"google|gtag|analytics|recaptcha|cookie|facebook|hotjar|matomo|piwik", src, re.I):
            continue
        try:
            rr = cli.s.get(src, timeout=30)
            fuentes.append((src, rr.text if rr.ok else ""))
        except requests.RequestException:
            pass
    trozos, metodos = [], []
    claves = re.compile(r"loan__return|return-completed|returnCompleted|return_in_progress|returnInProgress|"
                        r"loans/\S{0,40}return|confirm-message", re.I)
    for src, js in fuentes:
        for m in claves.finditer(js):
            a, b = max(0, m.start() - 900), min(len(js), m.end() + 1500)
            t = js[a:b]
            if any(t[:200] in x for _, x in trozos):
                continue
            trozos.append((src, t))
            metodos += [x.upper() for x in re.findall(r"""(?:method|type)\s*:\s*["'](get|post|put|patch|delete)["']""", t, re.I)]
            metodos += [x.upper() for x in re.findall(r"""\.(post|put|patch|delete)\s*\(""", t)]
            if len(trozos) >= 6:
                break
    traza.append("\nJavaScript del boton (%d archivos leidos, %d trozos; metodos vistos: %s):"
                 % (len(fuentes), len(trozos), ", ".join(dict.fromkeys(metodos)) or "-"))
    for src, t in trozos:
        traza.append("  [%s]\n%s\n" % (src[-80:], t[:2400]))
    return list(dict.fromkeys(metodos)), trozos


def devolver_ajax(cli, u, tok, pagina, fila, traza):
    """Peticion AJAX de devolucion. Primero mira en el JavaScript de la web que metodo usa;
    despues prueba PUT, PATCH, POST, DELETE, con y sin .json. Un 404/405 = 'asi no es'."""
    cab = {"Referer": pagina, "X-CSRF-Token": tok, "X-Requested-With": "XMLHttpRequest",
           "Accept": "application/json, text/javascript, */*; q=0.01", "Origin": cli.url("/").rstrip("/")}
    metodos_js, _ = js_devolucion(cli, pagina, traza)
    base = u.split("?")[0]
    lid = re.search(r"/loans/([\w-]+)", base)
    host = urlparse(base).netloc
    # diagnostico: cookies de la sesion (solo nombre, dominio y ruta; nunca el valor)
    traza.append("\nCookies de la sesion: " + ", ".join(
        "%s (dominio=%s ruta=%s)" % (c.name, c.domain or "-", c.path or "/") for c in cli.s.cookies) or "ninguna")
    # token CSRF recien leido de la pagina, como lo tiene el navegador
    cli.olvidar()
    meta = BeautifulSoup(cli.get(pagina) or "", "html.parser").find("meta", attrs={"name": "csrf-token"})
    if meta and meta.get("content"):
        tok = meta["content"]
    cab["X-CSRF-Token"] = tok
    # 1) copia exacta de lo que hace la web: $.ajax(url, {method: "PUT"}) sin datos
    cab_jq = {"Referer": pagina, "X-CSRF-Token": tok, "X-Requested-With": "XMLHttpRequest", "Accept": "*/*",
              "Origin": "%s://%s" % (urlparse(base).scheme, host)}
    todas = "; ".join("%s=%s" % (c.name, c.value) for c in cli.s.cookies
                      if not c.domain or c.domain.lstrip(".") in host)
    for etiqueta, extra in (("como el navegador", {}), ("con todas las cookies", {"Cookie": todas})):
        try:
            r = cli.s.request("PUT", base, headers=dict(cab_jq, **extra), timeout=30)
        except requests.RequestException as ex:
            traza.append("    PUT (%s) -> error %s" % (etiqueta, ex))
            continue
        traza.append("    PUT %s (%s) -> HTTP %s %s" % (urlparse(base).path, etiqueta, r.status_code,
                                                     (r.text or "")[:150].replace("\n", " ")))
        cli.olvidar()
        if r.status_code < 400 or cli.prestamo_sigue(fila) is False:
            return r
    if lid:   # la web consulta despues este estado: dice si el prestamo se ve con esta sesion
        try:
            rv = cli.s.get(cli.url("/v1/loans/%s/returned.json" % lid.group(1)),
                           headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}, timeout=30)
            traza.append("    GET /v1/loans/<id>/returned.json -> HTTP %s %s"
                         % (rv.status_code, (rv.text or "")[:150].replace("\n", " ")))
        except requests.RequestException:
            pass
    intentos = [(m, base, "form") for m in metodos_js]
    intentos += [("PUT", base, "form"), ("PATCH", base, "form"), ("POST", base, "form"),
                 ("PUT", base, "json"), ("POST", base, "json"),
                 ("PUT", base + ".json", "form"), ("PATCH", base + ".json", "form"), ("POST", base + ".json", "form"),
                 ("POST", base, "_put"), ("POST", base, "_patch"), ("DELETE", base, "form")]
    if lid:
        lu = cli.url("/loans/%s" % lid.group(1))
        intentos += [("DELETE", lu, "form"), ("PATCH", lu, "form"), ("PUT", lu, "form")]
    vistos, r = set(), None
    for metodo, url_, modo in intentos:
        if (metodo, url_, modo) in vistos:
            continue
        vistos.add((metodo, url_, modo))
        datos = {"authenticity_token": tok}
        if modo in ("_put", "_patch"):
            datos["_method"] = modo[1:]
        if modo == "json":
            r = cli.s.request(metodo, url_, json=datos, headers=dict(cab, **{"Content-Type": "application/json"}),
                              timeout=30)
        else:
            r = cli.s.request(metodo, url_, data=datos, headers=cab, timeout=30)
        traza.append("    %s %s (%s) -> HTTP %s %s" % (metodo, url_.replace(cli.url("/").rstrip("/"), ""), modo,
                                                     r.status_code, (r.text or "")[:150].replace("\n", " ")))
        if r.status_code in (404, 405):
            continue
        if r.status_code in (401, 403, 422):      # token caducado: recargar la pagina y repetir una vez
            cli.olvidar()
            html_pag = cli.get(pagina)
            if html_pag:
                meta = BeautifulSoup(html_pag, "html.parser").find("meta", attrs={"name": "csrf-token"})
                if meta and meta.get("content") and meta["content"] != tok:
                    tok = cab["X-CSRF-Token"] = meta["content"]
                    datos["authenticity_token"] = tok
                    r = cli.s.request(metodo, url_, data=datos, headers=cab, timeout=30)
                    traza.append("    (token nuevo) %s -> HTTP %s" % (metodo, r.status_code))
        cli.olvidar()
        if r.status_code < 400 or cli.prestamo_sigue(fila) is False:
            break
    return r


def guardar_traza_n(cli, traza, que):
    ruta = os.path.abspath("ebib_%s_%s.txt" % (que, cli.nombre))
    try:
        with open(ruta, "w", encoding="utf-8") as fh:
            fh.write("\n".join(traza) + "\n")
    except OSError:
        pass
    return ruta


# ------------------------------------------------------------- anular reserva

RE_ANULAR_TXT = re.compile(r"anular|cancelar|eliminar (?:la |mi )?reserva|quitar (?:la |mi )?reserva|"
                           r"renunciar|rechazar|devolver pr[eé]stamo", re.I)
RE_ANULAR_URL = re.compile(r"/bookings/[\w-]+(?:/(?:cancel|destroy|delete))?(?:[?#]|$)|/holds?/[\w-]+|"
                           r"/loans/[\w-]+/(?:return|cancel|decline)", re.I)
RE_NO_ANULAR = re.compile(r"prestar|tomar prestado|pedir prestado|reservar\b|leer|descargar|abrir", re.I)


def boton_anular(fila):
    """Boton para anular una reserva, dentro de su ficha:
    - pendiente: 'Anular mi reserva' (formulario/enlace a /bookings/<id> con DELETE)
    - disponible: en eBiblio sale como 'Devolver prestamo' (/loans/<id>/return): la deja pasar al siguiente."""
    nodo = fila.get("_nodo")
    if nodo is None:
        return None
    cands = []
    for el in nodo.find_all(["form", "a", "button"]):
        if el.name == "button" and el.find_parent("form") is not None and el.find_parent("form") in cands:
            continue
        txt = limpiar(el.get_text(" ")) + " " + (el.get("value") or "") + " " + (el.get("title") or "")
        u = url_elemento(el)
        metodo = (el.get("data-method") or "").lower()
        if el.name == "form":
            h = el.find("input", attrs={"name": "_method"})
            metodo = (h.get("value") or "").lower() if h else metodo
        if RE_NO_ANULAR.search(txt) and not RE_ANULAR_TXT.search(txt):
            continue
        if RE_ANULAR_TXT.search(txt) or (RE_ANULAR_URL.search(u) and metodo in ("delete", "")):
            cands.append(el)
    return cands[0] if cands else None


def pulsar_anular(cli, el, pagina, traza, disponible):
    """Pulsa el boton de anular como lo haria la web. Devuelve la ultima respuesta."""
    meta = BeautifulSoup(cli.get(pagina) or "", "html.parser").find("meta", attrs={"name": "csrf-token"})
    tok = meta.get("content", "") if meta else ""
    cab = {"Referer": pagina, "X-CSRF-Token": tok, "X-Requested-With": "XMLHttpRequest", "Accept": "*/*",
           "Origin": cli.url("/").rstrip("/")}
    if el.name == "form":
        datos = rellenar_form(el)
        u = urljoin(pagina, el.get("action") or pagina)
        if (el.get("method") or "get").lower() == "post":
            r = cli.s.post(u, data=datos, headers={"Referer": pagina}, timeout=30)
        else:
            r = cli.s.get(u, params=datos, headers={"Referer": pagina}, timeout=30)
        traza.append("    formulario %s %s -> HTTP %s" % ((el.get("method") or "get").upper(), urlparse(u).path,
                                                         r.status_code))
        return r
    u = urljoin(pagina, url_elemento(el)) if url_elemento(el) and not url_elemento(el).startswith("#") else ""
    if not u:
        traza.append("    el boton no tiene direccion: no se que pulsar")
        return None
    dm = (el.get("data-method") or "").lower()
    if dm in ("delete", "post", "put", "patch"):
        intentos = [("POST", {"_method": dm} if dm != "post" else {})]
    elif el.name == "a" and el.get("href") and not el.get("data-url"):
        intentos = [("GET", None)]
    else:      # boton de JavaScript: probar como la web (PUT para /return, DELETE para /bookings)
        intentos = ([("PUT", None), ("DELETE", None), ("POST", {"_method": "delete"})] if "/return" in u
                    else [("DELETE", None), ("POST", {"_method": "delete"}), ("PUT", None)])
    r = None
    for metodo, extra in intentos:
        datos = dict({"authenticity_token": tok}, **(extra or {})) if metodo != "GET" else None
        r = cli.s.request(metodo, u, data=datos, headers=cab if metodo != "GET" else {"Referer": pagina}, timeout=30)
        traza.append("    %s %s%s -> HTTP %s %s" % (metodo, urlparse(u).path, (" " + str(extra)) if extra else "",
                                                  r.status_code, (r.text or "")[:150].replace("\n", " ")))
        if r.status_code not in (404, 405):
            break
    # pantalla de confirmacion ("Seguro que quieres anular?")
    if r is not None and r.ok and "text/html" in (r.headers.get("Content-Type") or ""):
        sopa = BeautifulSoup(r.text, "html.parser")
        for f in sopa.find_all("form"):
            txt = limpiar(f.get_text(" ")) + " " + " ".join((b.get("value") or "") for b in f.find_all(["input", "button"]))
            if re.search(r"anular|cancelar|confirmar|aceptar|\bs[ií]\b", txt, re.I) and not RE_NO_ANULAR.search(txt) \
                    and re.search(r"booking|hold|loan|reserva", f.get("action") or "", re.I):
                u2 = urljoin(r.url, f.get("action") or r.url)
                r = cli.s.post(u2, data=rellenar_form(f), headers={"Referer": r.url}, timeout=30)
                traza.append("    confirmacion %s -> HTTP %s" % (urlparse(u2).path, r.status_code))
                break
    return r


def reserva_sigue(cli, fila):
    cli.olvidar()
    for f in filas_reservas(cli):
        mismo = (fila.get("id") and f.get("id") == fila["id"]) or f["titulo"] == fila["titulo"]
        if mismo and bool(f.get("_disponible")) == bool(fila.get("_disponible")):
            return True
    return False


def anular_uno(cli, fila):
    pagina = fila.get("_pagina") or cli.url("/my_profile/activity")
    disponible = bool(fila.get("_disponible"))
    traza = ["ebib.py %s - ANULAR RESERVA en %s - %s" % (VERSION, cli.nombre, datetime.now().strftime("%d/%m/%Y %H:%M")),
             "Reserva: %s (%s)" % (fila["titulo"], "disponible" if disponible else "pendiente"), "Pagina: %s" % pagina]
    b = boton_anular(fila)
    if b is None:
        traza.append("No encuentro el boton de anular. Ficha:\n" + html_seguro(fila.get("_nodo") or "", 4000))
        ruta = guardar_traza_n(cli, traza, "anular")
        return False, "no encuentro el boton para anularla; anulala en la web. Informe: %s (enviaselo a Claude)" % ruta
    traza.append("\nBoton usado:\n" + html_seguro(b))
    try:
        r = pulsar_anular(cli, b, pagina, traza, disponible)
    except requests.RequestException as ex:
        guardar_traza_n(cli, traza, "anular")
        return False, "error de red: %s" % ex
    msg = (aviso_web(r.text) or motivo_web(r.text)) if r is not None else ""
    sigue = reserva_sigue(cli, fila)
    traza.append("\n--- Comprobacion final: %s" % ("sigue en tus reservas" if sigue else "ya no esta en tus reservas"))
    ruta = guardar_traza_n(cli, traza, "anular")
    if sigue:
        return False, "%sComprobado: sigue en tus reservas. Informe: %s (enviaselo a Claude)" % (
            (msg + ". ") if msg else "", ruta)
    return True, "%sComprobado: ya no esta en tus reservas%s." % (
        (msg + ". ") if msg else "", "; pasa al siguiente de la lista" if disponible else "")


COL_ANU = [("n", "N", 3), ("cuenta", "Cuenta", 12), ("titulo", "Reserva", 34), ("estado", "Estado", 10),
           ("posicion", "Cola", 4), ("dias", "Falta", 7), ("recoger", "Pedir antes", 11)]


def cmd_anular(clientes, que, confirmar=True):
    """Anula una reserva (pendiente o ya disponible). Siempre pide confirmacion salvo -si."""
    todos = []
    for cli in clientes:
        for f in filas_reservas(cli):
            todos.append((cli, f))
    filas = [{"n": i, "cuenta": c.nombre, "titulo": f["titulo"], "estado": f["estado"], "posicion": f.get("posicion", ""),
              "dias": f.get("dias", ""), "recoger": f.get("recoger", "")} for i, (c, f) in enumerate(todos, 1)]
    print("\n=== TUS RESERVAS ===")
    tabla(filas, COL_ANU, fijas=True)
    if not todos:
        print("No tienes reservas: nada que anular.")
        return
    que = (que or "").strip()
    if not que:
        print('\nPara anular una:  python3 ebib.py -acc ... -anular N   (N = numero de la lista)')
        print('              o:  python3 ebib.py -acc ... -anular "parte del titulo"')
        return
    if que.isdigit():
        n = int(que)
        if not 1 <= n <= len(todos):
            print("\nNo hay reserva numero %d (hay %d)." % (n, len(todos)))
            return
        elegidos = [todos[n - 1]]
    else:
        elegidos = [(c, f) for c, f in todos if titulo_coincide(que, f["titulo"] + " " + c.nombre)]
        if len(elegidos) > 1:
            bases = {clave(RE_FECHA_TITULO.sub("", sin_ceros_fecha(n))) for n in nombres_posibles(que)}
            iguales = [(c, f) for c, f in elegidos
                       if clave(RE_FECHA_TITULO.sub("", sin_ceros_fecha(f["titulo"]))) in bases]
            if len(iguales) == 1:
                elegidos = iguales
        if not elegidos:
            print('\nNinguna reserva contiene "%s".' % que)
            return
        if len(elegidos) > 1:
            print('\nHay %d reservas que contienen "%s". Indica cual con su numero:' % (len(elegidos), que))
            for c, f in elegidos:
                print("   -anular %d   (%s, %s)" % (todos.index((c, f)) + 1, f["titulo"], c.nombre))
            return
    cli, f = elegidos[0]
    que_es = "DISPONIBLE para pedir: pasara al siguiente de la lista" if f.get("_disponible") else "pendiente"
    if confirmar:
        try:
            r = input('\n¿Anular la reserva "%s" (%s, %s)? Escribe s y pulsa Intro: ' % (f["titulo"], cli.nombre, que_es))
        except EOFError:
            r = ""
        if r.strip().lower() not in ("s", "si", "sí", "y", "yes"):
            print("Cancelado: no se ha anulado nada.")
            return
    ok, txt = anular_uno(cli, f)
    print("\n%s -> anular reserva en %s: %s. %s" % (f["titulo"], cli.nombre, "HECHO" if ok else "NO SE PUDO", txt))


COL_DEV = [("n", "N", 3), ("cuenta", "Cuenta", 12), ("titulo", "Prestamo", 34), ("vence", "Vence", 16),
           ("dias", "Falta", 7)]


def sin_ceros_fecha(t):
    """03/09/2026 -> 3/9/2026 (como escribe eBiblio las fechas en los titulos)."""
    return re.sub(r"\b0*(\d{1,2})/0*(\d{1,2})/(\d{4})\b", r"\1/\2/\3", t or "")


ARTICULOS = {"el", "la", "los", "las", "de", "del", "y", "e", "a", "en", "lo"}


def sin_articulos(t):
    """'Diario de Avila' -> 'diarioavila' (sin tildes, espacios, signos ni articulos/preposiciones)."""
    return "".join(w for w in re.findall(r"[a-z0-9]+", normaliza_nombre(t)) if w not in ARTICULOS)


CATALOGO_ACTUAL = {}     # catalogo (de serie + _catalogo) para entender nombres cortos en -devolver/-anular


def nombres_posibles(que):
    """Lo que escribe el usuario y, si es un nombre del catalogo ("diarioavila", "pais"...),
    tambien el nombre de la publicacion ("Diario de Ávila", "El País, edición Madrid"...)."""
    out = [que]
    base = RE_FECHA_TITULO.sub("", que).strip(" -")
    if CATALOGO_ACTUAL and base:
        el = resolver_elemento(base, CATALOGO_ACTUAL)
        for n in [el.get("serial"), el.get("nombre")] + list(el.get("otros") or []):
            if n and clave(n) != clave(base):
                out.append(n + que[len(base):] if que.startswith(base) else n)
    return list(dict.fromkeys(out))


def titulo_coincide(que, titulo):
    """Si lo que escribe el usuario corresponde al titulo, sin importar tildes, mayusculas,
    signos, articulos ni ceros en la fecha: "suplemento el pais - 03/10/2026" = "Suplemento El País - 3/10/2026",
    "diarioavila" = "Diario de Ávila". Tambien entiende los nombres cortos del catalogo y sus "alias"."""
    nombres = nombres_posibles(que)
    if len(nombres) > 1:
        return any(titulo_coincide_1(n, titulo) for n in nombres)
    return titulo_coincide_1(que, titulo)


def titulo_coincide_1(que, titulo):
    que, titulo = sin_ceros_fecha(que), sin_ceros_fecha(titulo)
    k = clave(que)
    if k and k in clave(titulo):
        return True
    k2 = sin_articulos(que)
    if len(k2) >= 4 and k2 in sin_articulos(titulo):
        return True
    pq = {w for w in re.findall(r"[a-z0-9]+", normaliza_nombre(que)) if w not in PALABRAS_VACIAS}
    pt = set(re.findall(r"[a-z0-9]+", normaliza_nombre(titulo)))
    return bool(pq) and pq <= pt


def cmd_devolver(clientes, que, confirmar=True):
    todos = []
    for cli in clientes:
        for f in filas_prestamos(cli):
            todos.append((cli, f))
    filas = [{"n": i, "cuenta": c.nombre, "titulo": f["titulo"], "vence": f["vence"], "dias": f["dias"]}
             for i, (c, f) in enumerate(todos, 1)]
    print("\n=== PRESTAMOS ACTIVOS ===")
    tabla(filas, COL_DEV, fijas=True)
    if not todos:
        print("No tienes prestamos activos: nada que devolver.")
        return
    que = (que or "").strip()
    if not que:
        print('\nPara devolver uno:  python3 ebib.py -acc ... -devolver N   (N = numero de la lista)')
        print('                o:  python3 ebib.py -acc ... -devolver "parte del titulo"')
        return
    if que.isdigit():
        n = int(que)
        if not 1 <= n <= len(todos):
            print("\nNo hay prestamo numero %d (hay %d)." % (n, len(todos)))
            return
        elegidos = [todos[n - 1]]
    else:
        elegidos = [(c, f) for c, f in todos if titulo_coincide(que, f["titulo"] + " " + c.nombre)]
        if not elegidos:
            print('\nNingun prestamo contiene "%s".' % que)
            return
        if len(elegidos) > 1:     # "el pais" -> mejor "El País" que "Suplemento El País"
            bases = {clave(RE_FECHA_TITULO.sub("", sin_ceros_fecha(n))) for n in nombres_posibles(que)}
            iguales = [(c, f) for c, f in elegidos
                       if clave(RE_FECHA_TITULO.sub("", sin_ceros_fecha(f["titulo"]))) in bases]
            if len(iguales) == 1:
                elegidos = iguales
        if len(elegidos) > 1:
            print('\nHay %d prestamos que contienen "%s". Indica cual con su numero:' % (len(elegidos), que))
            for c, f in elegidos:
                print("   -devolver %d   (%s, %s)" % (todos.index((c, f)) + 1, f["titulo"], c.nombre))
            return
    cli, f = elegidos[0]
    if confirmar:
        try:
            r = input('\n¿Devolver "%s" (%s)? Escribe s y pulsa Intro: ' % (f["titulo"], cli.nombre))
        except EOFError:
            r = ""
        if r.strip().lower() not in ("s", "si", "sí", "y", "yes"):
            print("Cancelado: no se ha devuelto nada.")
            return
    ok, txt = devolver_uno(cli, f)
    print("\n%s -> devolver en %s: %s. %s" % (f["titulo"], cli.nombre, "HECHO" if ok else "NO SE PUDO", txt))


# ------------------------------------------------------------------- buscar

# Catalogos (ademas de eBiblio). Se pueden anadir o cambiar con "_portales" en cuentas.json.
PORTALES_DEFECTO = {
    "rbpmadrid": {"nombre": "Bibliotecas publicas Comunidad de Madrid", "tipo": "absys",
                  "url": ["https://gestiona.comunidad.madrid/biblio_publicas/cgi-bin/abnetopac",
                          "https://gestiona3.madrid.org/biblio_publicas/cgi-bin/abnetopac"]},
    "rabel":     {"nombre": "Bibliotecas de Castilla y Leon (RABEL)", "tipo": "absys",
                  "url": ["https://rabel.jcyl.es/cgi-bin/abnetopac2"]},
    "bne":       {"nombre": "Biblioteca Nacional de Espana (BNE)", "tipo": "sru",
                  "url": ["https://catalogo.bne.es/view/sru/34BNE_INST"]},
    "ccbip":     {"nombre": "Catalogo colectivo bibliotecas publicas de Espana", "tipo": "absys",
                  "url": ["https://catalogos.cultura.gob.es/CCBIP/cgi-ccbip/abnetopac"]},
    "rebiun":    {"nombre": "REBIUN (bibliotecas universitarias)", "tipo": "oda",
                  "url": ["https://rebiun.baratz.es/OpacDiscovery/public/query/search/action"]},
    "dialnet":   {"nombre": "Dialnet (articulos de revistas, libros, tesis)", "tipo": "dialnet",
                  "url": ["https://dialnet.unirioja.es/buscar/documentos"], "defecto": False},
}
# -tipo con -en dialnet: valor del filtro de Dialnet y palabras para comprobarlo en cada resultado
TIPOS_DIALNET = {
    "revista":  ("ARTREV", "Articulo de revista"),
    "capitulo": ("ARTLIB", "Articulo de libro"),
    "libro":    ("LIBRO", "Libro"),
    "tesis":    ("TESIS", "Tesis"),
}
# Nombres faciles para -en
ALIAS_PORTALES = {
    "bibmadrid": "rbpmadrid", "bibliotecasmadrid": "rbpmadrid", "comunidadmadrid": "rbpmadrid",
    "madridpapel": "rbpmadrid", "papelmadrid": "rbpmadrid", "rbp": "rbpmadrid",
    "bibcyl": "rabel", "bibliotecascyl": "rabel", "bibcastillaleon": "rabel", "bibcastillayleon": "rabel",
    "castillaleonpapel": "rabel", "papelcastillaleon": "rabel", "cylpapel": "rabel", "jcyl": "rabel",
    "castillayleonpapel": "rabel", "papelcastillayleon": "rabel", "bibliotecascastillayleon": "rabel",
    "bibliotecascastillaleon": "rabel", "bibliotecascomunidaddemadrid": "rbpmadrid", "comunidaddemadrid": "rbpmadrid",
    # sin -acc (o si no es una cuenta abierta), estos nombres son las bibliotecas en papel:
    "madrid": "rbpmadrid", "castillaleon": "rabel", "castillayleon": "rabel", "cyl": "rabel",
    "bne": "bne", "bibliotecanacional": "bne", "nacional": "bne", "bn": "bne",
    "espana": "ccbip", "bibespana": "ccbip", "publicasespana": "ccbip",
    "universidades": "rebiun", "universitarias": "rebiun", "uni": "rebiun", "universidad": "rebiun",
    "dialnet": "dialnet", "articulos": "dialnet", "revistas": "dialnet", "unirioja": "dialnet",
}
GRUPOS_PORTALES = {
    "papel": ["rbpmadrid", "rabel"], "bibliotecas": ["rbpmadrid", "rabel"],
    "todaspapel": ["rbpmadrid", "rabel", "bne", "ccbip", "rebiun"],
}
PORTALES_TEMAS = ["rbpmadrid", "rabel", "bne"]   # por defecto para -temas y -tema
CAMPOS_ABSYS = {"todo": "xsqf99", "titulo": "xsqf02", "autor": "xsqf03", "materia": "xsqf05", "isbn": "xsqf99"}
CAMPOS_SRU = {"todo": 'alma.all_for_ui all "{q}"', "titulo": 'alma.title all "{q}"',
              "autor": 'alma.creator all "{q}"', "materia": 'alma.subjects all "{q}"', "isbn": 'alma.isbn = "{q}"'}
RE_ESTADO_FISICO = re.compile(r"no disponible|prestad[oa]s?|disponibles?|en sala|reservad[oa]|en reparaci[oó]n|"
                              r"excluido de pr[eé]stamo|solo consulta", re.I)
RE_NAV = re.compile(r"^(siguiente|anterior|primer|[uú]ltim|inicio|nueva b[uú]squeda|ver m[aá]s|\d+|>|<|»|«|"
                    r"mostrar|ordenar|exportar|imprimir|guardar|ayuda|volver)", re.I)
RE_TOTAL = re.compile(r"(\d[\d.]*)\s+(?:resultados?|registros?|documentos?|t[ií]tulos?|obras?)"
                      r"|\b\d+\s*(?:a|-|al|/)\s*\d+\s+de\s+(\d[\d.]*)", re.I)
RE_REFRESH = re.compile(r"""http-equiv=["']?refresh["']?[^>]*?content=["']?\s*\d+\s*;\s*url=([^"'>]+)""", re.I)
RE_JSLOC = re.compile(r"""(?:window\.|document\.)?location(?:\.href)?\s*=\s*["']([^"']+)["']""", re.I)
LIMITE_REGISTROS_TEMA = 300      # -tema: cuantos registros revisar como maximo por catalogo


def arreglar_codificacion(r):
    """Usar la codificacion que declara la pagina (AbsysNet usa iso-8859-1)."""
    m = re.search(rb"""<meta[^>]+charset=["']?([\w-]+)""", r.content[:3000], re.I)
    if m:
        r.encoding = m.group(1).decode("ascii", "ignore")
    elif not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding
    return r


def seguir_redirecciones(s, r, maximo=6):
    """Sigue las redirecciones que hace la pagina por su cuenta (meta refresh o JavaScript),
    como hace el navegador. AbsysNet lo usa para llevar a la pagina de resultados (ACC=161)."""
    for _ in range(maximo):
        arreglar_codificacion(r)
        cabeza = r.text[:4000]
        m = RE_REFRESH.search(cabeza)
        if not m and len(r.text) < 3000:
            m = RE_JSLOC.search(r.text)
        if not m:
            return r
        destino = urljoin(r.url, m.group(1).strip().strip("'\""))
        if destino == r.url:
            return r
        log_debug("redireccion de la pagina -> %s" % destino)
        r = s.get(destino, headers={"Referer": r.url}, timeout=40)
    return arreglar_codificacion(r)


def url_busqueda(portal, base, q, campo, inicio=1, n=10):
    if portal["tipo"] == "absys":
        return "%s?ACC=DOSEARCH&%s=%s" % (base, CAMPOS_ABSYS.get(campo, "xsqf99"), quote_plus(q))
    if portal["tipo"] == "oda":
        return "%s?q=%s" % (base, quote_plus(q))
    if portal["tipo"] == "sru":
        cql = CAMPOS_SRU.get(campo, CAMPOS_SRU["todo"]).format(q=q.replace('"', " "))
        return ("%s?version=1.2&operation=searchRetrieve&recordSchema=marcxml&maximumRecords=%d&startRecord=%d&query=%s"
                % (base, n, inicio, quote(cql)))
    return base.replace("{q}", quote_plus(q))


def consulta_dialnet(q, modo):
    """Texto de busqueda para Dialnet segun -modo:
    todas  -> todas las palabras, en cualquier orden (+palabra +palabra)
    alguna -> cualquiera de las palabras
    frase  -> el texto exacto, entre comillas
    Si el texto ya trae comillas, se respetan tal cual."""
    q = q.strip()
    if '"' in q or modo == "alguna":
        return q
    if modo == "frase":
        return '"%s"' % q
    palabras = q.split()
    return q if len(palabras) == 1 else " ".join("+" + w.lstrip("+") for w in palabras)


def url_dialnet(base, q, modo, tipo, inicio=1):
    u = "%s?querysDismax.DOCUMENTAL_TODO=%s" % (base, quote_plus(consulta_dialnet(q, modo)))
    if tipo in TIPOS_DIALNET:
        u += "&filtros.DOCUMENTAL_FACET_TIPO=" + TIPOS_DIALNET[tipo][0]
    if inicio > 1:
        u += "&inicio=%d" % inicio
    return u


RE_FICHA_DIALNET = re.compile(r"/servlet/(articulo|libro|tesis|extart)\?codigo=(\d+)")


def parse_dialnet(html, base_url):
    """Resultados de Dialnet: titulo, autores, revista o libro, ano, tipo y enlace."""
    sopa = BeautifulSoup(html, "html.parser")
    for t in sopa(["script", "style", "noscript", "nav", "footer"]):
        t.decompose()
    texto_pag = limpiar(sopa.get_text(" "))
    m = RE_TOTAL.search(texto_pag)
    total = (m.group(1) or m.group(2)) if m else ""
    out, vistos, bloques = [], set(), []
    enlaces = [a for a in sopa.find_all("a", href=True) if RE_FICHA_DIALNET.search(a["href"])]
    # si la pagina marca los titulos (class="titulo"), solo esos son resultados; el resto son
    # enlaces dentro del resultado (p. ej. el libro al que pertenece un capitulo)
    marcados = [a for a in enlaces if a.find_parent(class_=re.compile(r"titulo|title", re.I))]
    for a in (marcados or enlaces):
        mf = RE_FICHA_DIALNET.search(a["href"])
        if mf.group(2) in vistos or any(a in b.descendants for b in bloques):
            continue
        tit = limpiar(a.get_text(" ")) or limpiar(a.get("title") or "")
        if len(tit) < 4 or RE_NAV.search(tit) or re.match(r"(texto completo|pdf|ver |acceso)", tit, re.I):
            continue
        # subir hasta el bloque del resultado (sin meterse en el siguiente)
        nodo = a
        while nodo.parent is not None and nodo.parent.name not in ("body", "html", "[document]", "ul", "ol"):
            otros = {x.group(2) for x in (RE_FICHA_DIALNET.search(b["href"])
                                         for b in nodo.parent.find_all("a", href=True)) if x}
            if len(otros - {mf.group(2)}) > 1 or nodo.name in ("li", "article"):
                break
            nodo = nodo.parent
        vistos.add(mf.group(2))
        bloques.append(nodo)
        txt = limpiar(nodo.get_text(" | "))
        autores = [limpiar(x.get_text(" ")) for x in nodo.find_all("a", href=re.compile(r"/servlet/autor"))]
        revista = [limpiar(x.get_text(" ")) for x in nodo.find_all("a", href=re.compile(r"/servlet/revista"))]
        numero = [limpiar(x.get_text(" ")) for x in nodo.find_all("a", href=re.compile(r"/ejemplar/"))]
        libro = [limpiar(x.get_text(" ")) for x in nodo.find_all("a", href=re.compile(r"/servlet/libro\?"))
                 if x is not a and limpiar(x.get_text(" ")) != tit]
        anios = re.findall(r"\b(1[5-9]\d\d|20\d\d)\b", txt.replace(tit, ""))
        clase = mf.group(1)
        if clase == "tesis" or re.search(r"\btesis\b", txt[:400], re.I) and clase != "articulo":
            tipo = "Tesis"
        elif clase == "libro":
            tipo = "Libro"
        elif libro or re.search(r"art[ií]culo de libro|cap[ií]tulo|libro colectivo", txt, re.I):
            tipo = "Articulo de libro"
        else:
            tipo = "Articulo de revista"
        fuente = ""
        if revista:
            fuente = revista[0] + ((", " + numero[0]) if numero else "")
        elif libro:
            fuente = libro[0]
        completo = bool(nodo.find("a", href=re.compile(r"/descarga/|\.pdf", re.I))
                        or re.search(r"texto completo", txt, re.I))
        out.append(nuevo_item(titulo=tit, autor="; ".join(dict.fromkeys(x for x in autores if x))[:120],
                              anio=anios[-1] if anios else "", tipo=tipo, detalle=fuente,
                              estado="texto completo" if completo else "",
                              enlace=urljoin(base_url, a["href"])))
    sin = bool(re.search(r"no se (?:han )?encontr|0 resultados|ning[uú]n resultado|sin resultados|"
                         r"no hay resultados|no ha devuelto", texto_pag, re.I))
    return out, total, sin


def url_web_bne(q):
    return "https://catalogo.bne.es/discovery/search?query=any,contains,%s&vid=34BNE_INST:CATALOGO" % quote(q)


def nuevo_item(**kw):
    it = {"titulo": "", "autor": "", "anio": "", "tipo": "", "isbn": "", "materias": [], "enlace": "",
          "estado": "", "libre": "", "detalle": ""}
    it.update(kw)
    return it


def parse_absys(html, url):
    """Pagina de resultados de AbsysNet (lista ol.c-docs > li.c-docs_unit). Devuelve
    (items o None si no es esa pagina, total, url de la pagina siguiente, sin_resultados)."""
    sopa = BeautifulSoup(html, "html.parser")
    texto = limpiar(sopa.get_text(" "))
    m = RE_TOTAL.search(texto)
    total = (m.group(1) or m.group(2)) if m else ""
    sin = bool(re.search(r"no se (?:han )?encontr|0 registros|ning[uú]n (?:resultado|registro)|sin resultados",
                         texto, re.I))
    unidades = sopa.select("li.c-docs_unit")
    if not unidades:
        return (None if not sin else []), total, None, sin
    out = []
    for li in unidades:
        def t(sel):
            e = li.select_one(sel)
            return limpiar(e.get_text(" ")) if e else ""
        titulo = t(".c-docs_title").rstrip(" /:;").strip()
        resto = t(".c-docs_titleRest")
        if not titulo:
            titulo = t(".js-T245") or t("h3")
        dd = ""
        for dl in li.select(".c-docs_data dl"):
            dt = dl.find("dt")
            if dt and re.search(r"lugar|ed\.|a[nñ]o", dt.get_text(), re.I):
                d = dl.find("dd")
                dd = limpiar(d.get_text(" ")) if d else ""
                break
        anio = t(".js-FEPU") or t(".js-T260c") or (re.findall(r"(1[5-9]\d\d|20\d\d)", dd) or [""])[-1]
        tipo_e = li.select_one(".c-docs_mat i[title]")
        enl = li.select_one("a.c-docs_imgMedia[href]") or li.select_one("h3 a[href]")
        materias = [limpiar(x.get_text(" ")) for x in li.select(".js-MATE") if limpiar(x.get_text(" "))]
        out.append(nuevo_item(
            titulo=titulo, autor=t(".c-docs_author") or resto, anio=anio,
            tipo=tipo_e["title"] if tipo_e is not None else "",
            isbn=t(".js-T020a"), materias=list(dict.fromkeys(materias)),
            enlace=urljoin(url, enl["href"]) if enl is not None else "", detalle=dd))
    sig = None
    for a in sopa.find_all("a", href=True):
        lab = (a.get("aria-label") or "") + " " + (a.get("title") or "")
        if re.search(r"p[aá]gina siguiente", lab, re.I):
            sig = urljoin(url, a["href"])
            break
    return out, total, sig, sin


def contenedor_resultado(a):
    """Bloque (li, tr, div...) que contiene un resultado."""
    nodo = a
    for _ in range(6):
        if nodo.parent is None or nodo.parent.name in ("body", "html", "[document]"):
            break
        nodo = nodo.parent
        if nodo.name in ("li", "tr", "article") or len(limpiar(nodo.get_text(" "))) > 120:
            break
    return nodo


def parse_generico(html, base_url, patron_enlace):
    """Para catalogos de estructura desconocida: enlaces a fichas y su bloque."""
    sopa = BeautifulSoup(html, "html.parser")
    for t in sopa(["script", "style", "noscript", "nav", "footer"]):
        t.decompose()
    texto_pag = limpiar(sopa.get_text(" "))
    m = RE_TOTAL.search(texto_pag)
    total = (m.group(1) or m.group(2)) if m else ""
    out, vistos = [], set()
    for a in sopa.find_all("a", href=True):
        if not re.search(patron_enlace, a["href"]):
            continue
        tit = limpiar(a.get_text(" ")) or limpiar(a.get("title") or "")
        if len(tit) < 4 or RE_NAV.search(tit) or tit.lower() in vistos:
            continue
        vistos.add(tit.lower())
        txt = limpiar(contenedor_resultado(a).get_text(" | "))
        resto = limpiar(txt.replace(tit, "", 1)).strip(" |")
        est = RE_ESTADO_FISICO.findall(txt)
        out.append(nuevo_item(titulo=tit, detalle=resto[:160], enlace=urljoin(base_url, a["href"]),
                              estado=", ".join(dict.fromkeys(e.lower() for e in est))[:30]))
    sin = bool(re.search(r"no se (?:han )?encontr|0 resultados|ning[uú]n resultado|sin resultados|no hay resultados",
                         texto_pag, re.I))
    return out, total, sin


def parse_sru(xml_bytes):
    """Respuesta SRU (MARCXML) de la Biblioteca Nacional. (items, total)."""
    import xml.etree.ElementTree as ET
    raiz = ET.fromstring(xml_bytes)

    def loc(e):
        return e.tag.rsplit("}", 1)[-1]
    total = ""
    for e in raiz.iter():
        if loc(e) == "numberOfRecords":
            total = (e.text or "").strip()
            break
    out = []
    for rec in raiz.iter():
        if loc(rec) != "record" or not any(loc(c) in ("datafield", "controlfield", "leader") for c in rec):
            continue
        cf, df = {}, []
        for c in rec:
            if loc(c) == "controlfield":
                cf[c.get("tag")] = (c.text or "").strip()
            elif loc(c) == "datafield":
                subs = [(s.get("code"), (s.text or "").strip()) for s in c if loc(s) == "subfield"]
                df.append((c.get("tag"), subs))

        def campo(tag, codes):
            for tg, subs in df:
                if tg == tag:
                    v = " ".join(x for k, x in subs if k in codes and x)
                    if v:
                        return v
            return ""
        titulo = campo("245", "abnp").rstrip(" /:;.").strip()
        autor = campo("100", "a") or campo("110", "a") or campo("700", "a")
        anio = campo("264", "c") or campo("260", "c")
        anio = (re.findall(r"(1[5-9]\d\d|20\d\d)", anio) or [anio])[0]
        materias = []
        for tg, subs in df:
            if tg in ("600", "610", "630", "650", "651", "653", "655"):
                partes = [x.rstrip(" .") for k, x in subs if k in "avxyz" and x]
                if partes:
                    materias.append("-".join(partes))
        mms = cf.get("001", "")
        out.append(nuevo_item(titulo=titulo, autor=autor.rstrip(" ,."), anio=anio, isbn=campo("020", "a"),
                              materias=list(dict.fromkeys(materias)),
                              enlace=("https://catalogo.bne.es/discovery/fulldisplay?docid=alma%s&vid=34BNE_INST:CATALOGO"
                                      % mms) if mms else ""))
    return out, total


def obtener_dialnet(clave_p, portal, q, campo, maximo, modo="todas", tipo="todo"):
    """Dialnet: pasa paginas hasta tener 'maximo' resultados. -campo titulo/autor se comprueba
    en cada resultado; -tipo se pide a Dialnet y tambien se comprueba en cada resultado."""
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "es-ES,es;q=0.9"})
    base = portal["url"][0]
    palabras = [w for w in re.findall(r"[a-z0-9]+", normaliza_nombre(q.replace('"', " "))) if len(w) > 1]

    def vale(it):
        if tipo in TIPOS_DIALNET and normaliza_nombre(it["tipo"]) != normaliza_nombre(TIPOS_DIALNET[tipo][1]):
            return False
        if campo in ("titulo", "autor"):
            donde = normaliza_nombre(it["titulo"] if campo == "titulo" else it["autor"])
            if modo == "frase" or '"' in q:
                return " ".join(palabras) in " ".join(re.findall(r"[a-z0-9]+", donde))
            return (all if modo != "alguna" else any)(w in donde for w in palabras)
        return True

    aceptados, vistos, total, inicio, u0, nota = [], set(), "", 1, url_dialnet(base, q, modo, tipo), ""
    for pagina in range(40):
        u = url_dialnet(base, q, modo, tipo, inicio)
        try:
            r = s.get(u, timeout=40)
            arreglar_codificacion(r)
        except requests.RequestException as e:
            return aceptados, total, u0, "no se pudo conectar (%s)" % e.__class__.__name__
        log_debug("buscar dialnet -> HTTP %s %s" % (r.status_code, u))
        if r.status_code >= 400:
            return aceptados, total, u0, "Dialnet respondio HTTP %s" % r.status_code
        if DEBUG:
            guardar_pagina(clave_p + "_debug", r.text)
        items, tot, sin = parse_dialnet(r.text, r.url)
        total = total or tot
        nuevos = [it for it in items if it["enlace"] not in vistos]
        if not nuevos and not vistos and modo == "todas" and len(q.split()) > 1 and '"' not in q:
            # por si Dialnet no entiende "+palabra": repetir con las palabras tal cual
            modo, u0 = "alguna", url_dialnet(base, q, "alguna", tipo)
            log_debug("dialnet sin resultados con +palabra; repito sin +")
            continue
        if not nuevos:
            if not vistos and not sin:
                ruta = guardar_pagina(clave_p, r.text)
                nota = "no entiendo la pagina de resultados (guardada en %s; enviasela a Claude)" % ruta
            elif not vistos:
                nota = "sin resultados"
            break
        for it in nuevos:
            vistos.add(it["enlace"])
            if vale(it):
                aceptados.append(it)
                if len(aceptados) >= maximo:
                    return aceptados, total, u0, nota
        inicio += len(items)
        if total.replace(".", "").isdigit() and inicio > int(total.replace(".", "")):
            break
        if len(vistos) >= max(200, maximo * 10):      # no recorrer Dialnet entero
            break
    if not aceptados and not nota:
        nota = "ninguno cumple -campo/-tipo en los %d revisados" % len(vistos)
    return aceptados, total, u0, nota


def obtener(clave_p, portal, q, campo, maximo, filtro=None, limite=None, url_directa=None):
    """Busca en un catalogo recorriendo paginas hasta tener 'maximo' resultados (que pasen 'filtro')
    o haber revisado 'limite' registros. Devuelve (items, total, url_para_mostrar, nota)."""
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "es-ES,es;q=0.9"})
    limite = limite or maximo
    aceptados, revisados = [], 0

    def meter(items):
        nonlocal revisados
        for it in items:
            revisados += 1
            if filtro is None or filtro(it):
                aceptados.append(it)
            if len(aceptados) >= maximo or revisados >= limite:
                return True
        return False

    if portal["tipo"] == "sru":
        base = portal["url"][0]
        inicio, total = 1, ""
        while True:
            n = max(1, min(50, limite - revisados))
            u = url_busqueda(portal, base, q, campo, inicio, n)
            try:
                r = s.get(u, timeout=40)
            except requests.RequestException as e:
                return aceptados, total, url_web_bne(q), "no se pudo conectar (%s)" % e.__class__.__name__
            log_debug("buscar %s -> HTTP %s %s" % (clave_p, r.status_code, u))
            if r.status_code >= 400:
                return aceptados, total, url_web_bne(q), "el catalogo respondio HTTP %s" % r.status_code
            try:
                items, total = parse_sru(r.content)
            except Exception as e:
                ruta = guardar_pagina(clave_p, r.text)
                return aceptados, total, url_web_bne(q), "respuesta no valida (guardada en %s)" % ruta
            if not items or meter(items):
                break
            inicio += len(items)
            if total.isdigit() and inicio > int(total):
                break
        return aceptados, total, url_web_bne(q), ("sin resultados" if not aceptados and total in ("", "0") else "")

    bases = [url_directa] if url_directa else portal["url"]
    ultimo = ""
    for base in bases:
        u = url_directa or url_busqueda(portal, base, q, campo)
        ultimo = u
        try:
            r = seguir_redirecciones(s, s.get(u, timeout=40))
        except requests.RequestException as e:
            log_debug("buscar %s %s error" % (clave_p, u), e)
            continue
        log_debug("buscar %s -> HTTP %s %s" % (clave_p, r.status_code, r.url))
        if r.status_code >= 400:
            continue
        if DEBUG:
            guardar_pagina(clave_p + "_debug", r.text)
        if portal["tipo"] == "absys":
            items, total, sig, sin = parse_absys(r.text, r.url)
            if items is not None:
                paginas = 1
                fin = meter(items)
                while not fin and sig and paginas < 60:
                    try:
                        r = seguir_redirecciones(s, s.get(sig, headers={"Referer": r.url}, timeout=40))
                    except requests.RequestException:
                        break
                    items, _, sig, _ = parse_absys(r.text, r.url)
                    paginas += 1
                    if not items:
                        break
                    fin = meter(items)
                log_debug("buscar %s: %d paginas, %d registros revisados" % (clave_p, paginas, revisados))
                return aceptados, total, u, ("sin resultados" if sin and not aceptados else "")
        patron = r"catalog/detail|/detail/" if portal["tipo"] == "oda" else r"ACC=16[2-9]|DOC=\d|TITN=\d|ACC=DOC"
        items, total, sin = parse_generico(r.text, r.url, patron)
        if not items and not sin:
            ruta = guardar_pagina(clave_p, r.text)
            return [], total, u, "no entiendo la pagina de resultados (guardada en %s; enviasela a Claude)" % ruta
        meter(items)
        return aceptados, total, u, ("sin resultados" if sin and not aceptados else "")
    return [], "", ultimo, "no se pudo conectar"


def guardar_pagina(nombre, texto):
    ruta = os.path.abspath("ebib_buscar_%s.html" % nombre)
    try:
        with open(ruta, "w", encoding="utf-8") as fh:
            fh.write(texto)
    except OSError:
        pass
    return ruta


def abrir_url(u):
    try:
        subprocess.run(["termux-open-url", u], timeout=10, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def buscar_ebiblio(cli, q, campo, maximo):
    ruta = "/resources?author_keyword=%s" % quote(q) if campo == "autor" else "/resources?q=%s" % quote(q)
    html = cli.get(ruta)
    u = cli.url(ruta)
    if not html:
        return [], "", u, "no se pudo cargar"
    estado = {e["rid"]: e for e in ejemplares(html)}
    out = []
    for rid, nodo in tarjetas(html):
        it = datos_tarjeta(rid, nodo)
        e = estado.get(rid, {})
        if e.get("ya_tuyo"):
            est = "ya lo tienes"
        elif e.get("libre"):
            est = "LIBRE"
        elif e.get("mi_reserva"):
            est = "reservado tuyo"
        elif e.get("reserva"):
            est = "ocupado"
        else:
            est = ""
        le = e.get("libre_en")
        out.append(nuevo_item(titulo=it["titulo"], autor=it["autor"], tipo=it["formato"], estado=est,
                              libre=("%s (%s)" % (falta(le), fmt(le, hora=True))) if (le and est == "ocupado") else "",
                              enlace=cli.url("/resources/%s" % rid)))
        if len(out) >= maximo:
            break
    m = RE_TOTAL.search(limpiar(BeautifulSoup(html, "html.parser").get_text(" ")))
    return out, ((m.group(1) or m.group(2)) if m else ""), u, ("sin resultados" if not out else "")


def mostrar_items(titulo_p, q, total, u, nota, items, temas=True):
    print("\n=== %s: '%s'%s ===" % (titulo_p, q, (" (%s en total)" % total) if total else ""))
    print("    %s" % u)
    if nota:
        print("    -> %s" % nota)
    for i, x in enumerate(items, 1):
        print("\n%3d. %s" % (i, x["titulo"] or "(sin titulo)"))
        datos = [x.get("autor"), x.get("anio"), x.get("tipo"), ("ISBN " + x["isbn"]) if x.get("isbn") else ""]
        datos = [d for d in datos if d]
        if datos:
            print("     " + " . ".join(datos))
        elif x.get("detalle"):
            print("     " + recortar(x["detalle"], 110))
        if datos and x.get("detalle") and x.get("tipo", "").startswith(("Articulo", "Tesis")):
            print("     En: " + recortar(x["detalle"], 100))
        if x.get("estado"):
            print("     Estado: %s%s" % (x["estado"], (" - se libera en " + x["libre"]) if x.get("libre") else ""))
        if x.get("enlace") and "dialnet" in x["enlace"]:
            print("     " + x["enlace"].replace("https://", ""))
        if temas and x.get("materias"):
            print("     Temas: " + "; ".join(x["materias"][:6]))
    if items:
        print("\n    (%d mostrados)" % len(items))


def resolver_destinos(en, clientes, portales, defecto):
    destinos = []
    for x in (en or "").split(","):
        k = re.sub(r"[^a-z0-9]", "", normaliza_nombre(x))
        if not k:
            continue
        if any(c.nombre.lower() == x.strip().lower() for c in clientes):
            destinos.append(x.strip().lower())
        elif k in GRUPOS_PORTALES:
            destinos.extend(GRUPOS_PORTALES[k])
        else:
            destinos.append(ALIAS_PORTALES.get(k, x.strip().lower()))
    destinos = list(dict.fromkeys(destinos))
    return destinos or defecto


COL_CSV_BUSCAR = [("portal", "Portal", 0), ("n", "N", 0), ("titulo", "Titulo", 0), ("autor", "Autor", 0),
                  ("anio", "Ano", 0), ("tipo", "Tipo", 0), ("detalle", "Revista / libro", 0),
                  ("isbn", "ISBN", 0), ("estado", "Estado", 0),
                  ("libre", "Libre en", 0), ("temas", "Temas", 0), ("enlace", "Enlace", 0)]


def a_csv(d, items):
    return [dict(x, portal=d, n=i, temas="; ".join(x.get("materias", []))) for i, x in enumerate(items, 1)]


def nombre_csv(csv_ruta, que, q):
    if csv_ruta and csv_ruta != "auto":
        return csv_ruta
    return "ebiblio_%s_%s.csv" % (que, re.sub(r"\W+", "_", normaliza_nombre(q)).strip("_")[:30])


def cmd_buscar(clientes, q, en, campo, maximo, abrir, portales, csv_ruta, acc, modo="todas", tipo="todo"):
    defecto = [c.nombre for c in clientes] + [k for k, p in portales.items() if p.get("defecto", True)]
    destinos = resolver_destinos(en, clientes, portales, defecto)
    todas = []
    for d in destinos:
        cli = next((c for c in clientes if c.nombre.lower() == d), None)
        if cli is not None:
            titulo_p = "eBiblio %s" % cli.nombre
            res, total, u, nota = buscar_ebiblio(cli, q, campo, maximo)
        elif d in portales and portales[d]["tipo"] == "dialnet":
            titulo_p = portales[d]["nombre"] + {"todas": "", "alguna": " [alguna palabra]",
                                                 "frase": " [frase exacta]"}.get(modo, "") \
                + ((" [" + TIPOS_DIALNET[tipo][1] + "]") if tipo in TIPOS_DIALNET else "")
            res, total, u, nota = obtener_dialnet(d, portales[d], q, campo, maximo, modo, tipo)
        elif d in portales:
            titulo_p = portales[d]["nombre"]
            res, total, u, nota = obtener(d, portales[d], q, campo, maximo)
        else:
            print("\n[%s] no es una cuenta de -acc ni un portal conocido (%s)." % (d, ", ".join(list(portales))))
            continue
        mostrar_items(titulo_p, q, total, u, nota, res)
        if abrir:
            abrir_url(u)
        todas.extend(a_csv(d, res))
    if csv_ruta:
        guardar_csv(todas, COL_CSV_BUSCAR, nombre_csv(csv_ruta, "buscar", q))


def clave_tema(t):
    """Normaliza un tema: sin tildes, minusculas, sin espacios alrededor de los guiones."""
    return re.sub(r"\s*-+\s*", "-", re.sub(r"\s+", " ", normaliza_nombre(t))).strip(" .-")


def contiene_termino(tema, termino):
    tt = clave_tema(tema)
    return all(w in tt for w in clave_tema(termino).replace("-", " ").split())


def cmd_temas(termino, en, paginas, portales, csv_ruta, clientes):
    """Lista los temas (materias) que contienen el termino, con cuantos libros tiene cada uno."""
    destinos = resolver_destinos(en, clientes, portales, PORTALES_TEMAS)
    filas_csv = []
    for d in destinos:
        if d not in portales:
            print("\n[%s] -temas solo funciona con catalogos (bibmadrid, bibcyl, bne...)." % d)
            continue
        p = portales[d]
        items, total, u, nota = obtener(d, p, termino, "materia", paginas * 10, limite=paginas * 10)
        cuenta = {}
        nombre = {}
        for it in items:
            for m in it.get("materias", []):
                if contiene_termino(m, termino):
                    k = clave_tema(m)
                    cuenta[k] = cuenta.get(k, 0) + 1
                    nombre.setdefault(k, m)
        print("\n=== %s: temas que contienen '%s' ===" % (p["nombre"], termino))
        print("    (revisados %d de %s libros%s)" % (len(items), total or "?", "" if len(items) >= int(total or 0)
                                                     else "; para revisar mas: -paginas %d" % (paginas * 2)))
        if nota:
            print("    -> %s" % nota)
        orden = sorted(cuenta, key=lambda k: (-cuenta[k], k))
        for k in orden:
            print("   %4d  %s" % (cuenta[k], nombre[k]))
            filas_csv.append({"portal": d, "tema": nombre[k], "libros": cuenta[k]})
        if orden:
            print('\n    Para ver los libros de un tema:  python3 ebib.py -tema "%s" -en %s'
                  % (nombre[orden[0]], {"rbpmadrid": "bibmadrid", "rabel": "bibcyl"}.get(d, d)))
        elif not nota:
            print("    Ningun tema contiene '%s'." % termino)
    if csv_ruta and filas_csv:
        guardar_csv(filas_csv, [("portal", "Portal", 0), ("tema", "Tema", 0), ("libros", "Libros", 0)],
                    nombre_csv(csv_ruta, "temas", termino))


def cmd_tema(tema, en, maximo, portales, csv_ruta, clientes, subtemas=False):
    """Libros de un tema exacto (o de un enlace de tema/busqueda copiado del navegador)."""
    todas = []
    if re.match(r"https?://", tema.strip()):
        host = urlparse(tema).netloc
        d = next((k for k, p in portales.items() if any(urlparse(x).netloc == host for x in p["url"])), None)
        p = portales.get(d) or {"nombre": host, "tipo": "absys", "url": [tema]}
        items, total, u, nota = obtener(d or host, p, "", "todo", maximo, url_directa=tema.strip())
        m = re.search(r"SAUT=([^&]+)", tema)
        titulo_tema = requests.utils.unquote(m.group(1)).replace("+", " ") if m else "enlace"
        if not items and m and d:
            # el enlace caduca (lleva la sesion del navegador): buscar ese tema por su nombre
            print("\n(El enlace ya no vale o no se entiende; busco el tema '%s' por su nombre.)" % titulo_tema)
            return cmd_tema(titulo_tema, d, maximo, portales, csv_ruta, clientes, subtemas)
        mostrar_items(p["nombre"], titulo_tema, total, tema, nota, items)
        todas.extend(a_csv(d or host, items))
    else:
        destinos = resolver_destinos(en, clientes, portales, PORTALES_TEMAS)
        objetivo = clave_tema(tema)

        def es_del_tema(it):
            for m in it.get("materias", []):
                k = clave_tema(m)
                if k == objetivo or (subtemas and k.startswith(objetivo + "-")):
                    return True
            return False
        for d in destinos:
            if d not in portales:
                print("\n[%s] -tema solo funciona con catalogos (bibmadrid, bibcyl, bne...)." % d)
                continue
            p = portales[d]
            items, total, u, nota = obtener(d, p, tema, "materia", maximo, filtro=es_del_tema,
                                            limite=LIMITE_REGISTROS_TEMA)
            if not items and not nota:
                nota = ("ningun libro con el tema exacto '%s' en los primeros %d resultados; prueba "
                        "-temas \"%s\" para ver los temas parecidos%s"
                        % (tema, LIMITE_REGISTROS_TEMA, tema.split("-")[0].strip(), "" if subtemas else " o anade -subtemas"))
            mostrar_items(p["nombre"], "tema: " + tema + (" (y subtemas)" if subtemas else ""), "", u, nota, items)
            todas.extend(a_csv(d, items))
    if csv_ruta:
        guardar_csv(todas, COL_CSV_BUSCAR, nombre_csv(csv_ruta, "tema", tema if not tema.startswith("http") else "enlace"))


# ---------------------------------------------------------------- vigilancia

def vigilar(clientes):
    try:
        with open(ESTADO_FILE, encoding="utf-8") as fh:
            ya_avisadas = set(json.load(fh))
    except Exception:
        ya_avisadas = set()
    nuevas = 0
    for cli in clientes:
        for f in filas_reservas(cli):
            clave = "%s|%s" % (cli.nombre, f["id"])
            if f["_disponible"] and clave not in ya_avisadas:
                notificar("eBiblio %s" % cli.nombre, "%s: tu reserva ya esta disponible%s" % (
                    f["titulo"], (", pidelo antes de %s (quedan %s)" % (fmt(f["_limite"], hora=True), falta(f["_limite"])))
                    if f.get("_limite") else ""))
                ya_avisadas.add(clave)
                nuevas += 1
    # prensa: aviso cuando la edicion de hoy esta libre para prestamo
    for cli in clientes:
        if not cli.prensa:
            continue
        for f in filas_prensa(cli):
            if f["_hoy"] and f["_libre"]:
                clave = "prensa|%s|%s|%s" % (cli.nombre, normaliza_nombre(f["periodico"]), f["edicion"])
                if clave not in ya_avisadas:
                    notificar("eBiblio %s" % cli.nombre,
                              "%s de hoy LIBRE para prestamo" % f["periodico"])
                    ya_avisadas.add(clave)
                    nuevas += 1
    # borrar avisos de prensa de dias anteriores para que el archivo no crezca
    hoy = datetime.now().strftime("%d/%m/%Y")
    ya_avisadas = {k for k in ya_avisadas if not k.startswith("prensa|") or k.endswith(hoy)}
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(ESTADO_FILE, "w", encoding="utf-8") as fh:
        json.dump(sorted(ya_avisadas), fh)
    if not nuevas:
        print("Sin novedades (reservas y prensa).")


# ---------------------------------------------------------------------- main

def main():
    global DEBUG
    ap = argparse.ArgumentParser(description="Consulta cuentas eBiblio (v%s, plataforma Cantook)" % VERSION)
    ap.add_argument("-acc", default=None,
                    help="cuenta definida en cuentas.json (madrid, castillaleon...) o 'todas'. "
                         "No hace falta para -buscar solo en bibliotecas (papel)")
    ap.add_argument("-verhistorial", action="store_true", help="libros leidos (historial)")
    ap.add_argument("-verprestamos", action="store_true", help="prestamos activos y vencimiento")
    ap.add_argument("-verreservas", action="store_true", help="reservas, posicion y fecha aprox.")
    ap.add_argument("-verfavoritos", action="store_true", help="favoritos / lista de deseos")
    ap.add_argument("-verprensa", action="store_true",
                    help="prensa del dia: si ha salido, si esta libre y cuanto falta")
    ap.add_argument("-p", metavar="NOMBRE", default="",
                    help="con -verprensa o -vigilar: solo ese periodico (ej. -p cinco)")
    ap.add_argument("-lista", metavar="ELEMENTOS", default="",
                    help='ultima edicion de varios elementos por prioridad, ej. -lista "mundo,pais,cincodias,eljueves:mensual"')
    ap.add_argument("-vernombres", action="store_true",
                    help="lista los nombres cortos que se pueden usar en -lista (de serie y los de _catalogo)")
    ap.add_argument("-pedir", action="store_true",
                    help="con -lista: pide prestamo (o reserva si esta ocupado) de verdad, en ese orden")
    ap.add_argument("-devolver", metavar="N_O_TITULO", nargs="?", const="", default=None,
                    help='devolver un prestamo: sin nada lista tus prestamos numerados; '
                         '-devolver 2 o -devolver "diario" devuelve ese')
    ap.add_argument("-anular", metavar="N_O_TITULO", nargs="?", const="", default=None,
                    help='anular una reserva (pendiente o ya disponible): sin nada lista tus reservas numeradas; '
                         '-anular 2 o -anular "diario" anula esa (pide confirmacion)')
    ap.add_argument("-si", action="store_true", help="con -devolver o -anular: no pedir confirmacion")
    ap.add_argument("-buscar", metavar="TEXTO", default=None,
                    help='buscar en eBiblio y en catalogos de bibliotecas, ej. -buscar "el nombre del viento"')
    ap.add_argument("-en", metavar="DONDE", default="",
                    help="con -buscar: donde. bibmadrid (bibliotecas Comunidad de Madrid), bibcyl (Castilla y Leon), papel (las dos), universidades, espana, dialnet (articulos), o tus eBiblio (madrid, castillaleon). Por defecto: todos")
    ap.add_argument("-campo", default="todo", choices=["todo", "titulo", "autor", "materia", "isbn"],
                    help="con -buscar: buscar en todo (defecto), titulo, autor, materia o isbn")
    ap.add_argument("-max", type=int, default=10, help="con -buscar: resultados por portal (defecto 10)")
    ap.add_argument("-modo", default="todas", choices=["todas", "alguna", "frase"],
                    help="con -buscar en dialnet: todas las palabras (defecto), alguna de ellas, o la frase exacta")
    ap.add_argument("-tipo", default="todo", choices=["todo", "revista", "capitulo", "libro", "tesis"],
                    help="con -buscar en dialnet: articulo de revista, capitulo (articulo de libro), libro o tesis")
    ap.add_argument("-abrir", action="store_true", help="con -buscar: abrir tambien la busqueda en el navegador")
    ap.add_argument("-temas", metavar="TERMINO", default=None,
                    help='lista los temas (materias) que contienen el termino, ej. -temas "inteligencia artificial"')
    ap.add_argument("-tema", metavar="TEMA_O_ENLACE", default=None,
                    help='libros de un tema exacto, ej. -tema "Inteligencia artificial-Aplicaciones", '
                         'o pega el enlace del tema copiado del navegador')
    ap.add_argument("-subtemas", action="store_true", help="con -tema: incluir tambien sus subtemas (Tema-...)")
    ap.add_argument("-paginas", type=int, default=5,
                    help="con -temas: cuantas paginas de 10 libros revisar por catalogo (defecto 5)")
    ap.add_argument("-resumen", action="store_true", help="prestamos + reservas")
    ap.add_argument("-vigilar", action="store_true", help="notifica reservas disponibles y prensa de hoy libre")
    ap.add_argument("-sondeo", action="store_true",
                    help="diagnostico: guarda las paginas de tu cuenta para ajustar el programa")
    ap.add_argument("-csv", metavar="ARCHIVO", nargs="?", const="auto",
                    help="guardar en CSV (sin nombre: se genera uno automatico)")
    ap.add_argument("-n", metavar="N", default="todo",
                    help="con -verhistorial: cuantos libros mostrar (15, 30, 100...) o 'todo' (por defecto)")
    ap.add_argument("-ancho", type=int, default=0,
                    help="ancho de las tablas en caracteres (por defecto, el de la pantalla)")
    ap.add_argument("-debug", action="store_true", help="mostrar detalle de lo que hace")
    a = ap.parse_args()
    global LIMITE, FILTRO_PRENSA
    FILTRO_PRENSA = a.p
    if str(a.n).lower() in ("todo", "todos", "all", "0"):
        LIMITE = None
    elif str(a.n).isdigit():
        LIMITE = int(a.n)
    else:
        sys.exit("-n debe ser un numero (15, 30, 100...) o 'todo'")
    DEBUG = a.debug
    global ANCHO_PANTALLA
    ANCHO_PANTALLA = a.ancho if a.ancho and a.ancho >= 30 else None

    cfg = cargar_config()
    catalogo_usuario = cfg.get("_catalogo", {}) if isinstance(cfg.get("_catalogo"), dict) else {}
    for _k, _d in catalogo_usuario.items():
        if isinstance(_d, dict) and isinstance(_d.get("serial"), str):
            _d["serial"] = _d["serial"].strip().rstrip("-").strip()     # "El País -" -> "El País"
    if DEBUG:
        for _a in AVISOS_CONFIG:
            log_debug("cuentas.json", _a)
    portales = dict(PORTALES_DEFECTO)
    for k, v in (cfg.get("_portales") or {}).items():
        if isinstance(v, dict) and v.get("url"):
            v = dict(v)
            v["url"] = v["url"] if isinstance(v["url"], list) else [v["url"]]
            v.setdefault("tipo", "absys")
            v.setdefault("nombre", k)
            portales[k.lower()] = v
    cfg = {k: v for k, v in cfg.items() if not k.startswith("_")}
    if a.vernombres:
        ver_nombres(catalogo_usuario)
        return
    if a.acc is None:
        if a.buscar is None and a.temas is None and a.tema is None:
            sys.exit("Falta -acc (madrid, castillaleon... o todas). Ej: python3 ebib.py -acc madrid -resumen")
        nombres = []          # busqueda solo en catalogos de bibliotecas: sin iniciar sesion en eBiblio
    elif a.acc == "todas":
        nombres = list(cfg.keys())
    elif a.acc in cfg:
        nombres = [a.acc]
    else:
        sys.exit("Cuenta '%s' no encontrada. Disponibles: %s, todas" % (a.acc, ", ".join(cfg)))

    clientes = []
    for n in nombres:
        cli = EBiblio(n, cfg[n])
        try:
            sopa = cli.login()
        except SystemExit as ex:
            if len(nombres) == 1:
                raise
            print(str(ex))
            print("[%s] Sigo con las demas cuentas.\n" % n)
            continue
        print("[%s] Sesion iniciada (ebib.py %s)." % (n, VERSION))
        if a.sondeo:
            sondeo(cli, sopa)
        clientes.append(cli)
    if nombres and not clientes:
        sys.exit("No se pudo entrar en ninguna cuenta.")
    if a.sondeo:
        return
    if a.vigilar:
        vigilar(clientes)
        return
    if a.temas is not None:
        cmd_temas(a.temas, a.en, max(1, a.paginas), portales, a.csv, clientes)
        return
    if a.tema is not None:
        cmd_tema(a.tema, a.en, a.max, portales, a.csv, clientes, a.subtemas)
        return
    if a.buscar is not None:
        cmd_buscar(clientes, a.buscar, a.en, a.campo, a.max, a.abrir, portales, a.csv, a.acc, a.modo, a.tipo)
        return
    global CATALOGO_ACTUAL
    CATALOGO_ACTUAL = dict(CATALOGO_DEFECTO)
    CATALOGO_ACTUAL.update(catalogo_usuario)
    if a.anular is not None:
        cmd_anular(clientes, a.anular or a.lista, confirmar=not a.si)
        return
    if a.devolver is not None:
        # -lista "titulo" -devolver  ==  -devolver "titulo"
        cmd_devolver(clientes, a.devolver or a.lista, confirmar=not a.si)
        return
    if a.lista:
        catalogo = dict(CATALOGO_DEFECTO)
        catalogo.update(catalogo_usuario)
        filas = ejecutar_lista(clientes, a.lista, catalogo, a.pedir)
        if a.csv:
            ruta = a.csv if a.csv != "auto" else "ebiblio_%s_lista_%s.csv" % (a.acc, datetime.now().strftime("%Y%m%d"))
            guardar_csv(filas, COL_LISTA + [("libre_a", "Libre aprox.", 16)], ruta)
        return
    if a.pedir:
        sys.exit("-pedir solo se usa junto con -lista")

    trabajos = []
    if a.verhistorial:
        trabajos.append(("historial", filas_historial, COL_HIST))
    if a.verprestamos or a.resumen:
        trabajos.append(("prestamos", filas_prestamos, COL_PREST))
    if a.verreservas or a.resumen:
        trabajos.append(("reservas", filas_reservas, COL_RES))
    if a.verfavoritos:
        trabajos.append(("favoritos", filas_favoritos, COL_FAV))
    if a.verprensa:
        trabajos.append(("prensa", filas_prensa, COL_PRENSA))
    mostrar_limites = a.resumen or not trabajos
    if not trabajos:
        trabajos = [("prestamos", filas_prestamos, COL_PREST), ("reservas", filas_reservas, COL_RES)]

    for nombre, funcion, cols in trabajos:
        filas = []
        for cli in clientes:
            filas.extend(funcion(cli))
        print("\n=== %s ===" % nombre.upper())
        pant = {"prestamos": COL_PREST_PANT, "reservas": COL_RES_PANT}.get(nombre)
        if pant:
            tabla(filas, pant, fijas=True)
        else:
            tabla(filas, cols)
        if nombre in ("prestamos", "reservas"):
            for cli in clientes:
                lc = limites_cuenta(cli)
                bloq = [f for f in filas if f.get("cuenta") == cli.nombre and f.get("_bloqueo")]
                if nombre == "reservas":
                    for f in bloq:
                        print("  OJO %s: \"%s\" esta disponible pero: %s" % (cli.nombre, recortar(f["titulo"], 40),
                                                                           f["_bloqueo"]))
                elif lc and not mostrar_limites:
                    print("  [%s] %s." % (cli.nombre, lc.rstrip(".")))
        if nombre == "prensa" and filas:
            print("\nQue hacer:")
            for l in consejo_prensa(filas):
                print("  - " + l)
        if a.csv:
            ruta = a.csv
            if ruta == "auto" or len(trabajos) > 1:
                base = "" if ruta == "auto" else os.path.splitext(ruta)[0] + "_"
                ruta = "%sebiblio_%s_%s_%s.csv" % (base, a.acc, nombre, datetime.now().strftime("%Y%m%d"))
            guardar_csv(filas, cols, ruta)

    if mostrar_limites:
        filas = []
        for cli in clientes:
            filas.extend(filas_limites(cli))
        print("\n=== LIMITES (lo que te queda) ===")
        if filas:
            # primero lo importante: 24 h, 30 dias y lo agotado; el resto, en una linea por cuenta
            for cli in clientes:
                propias = [f for f in filas if f["cuenta"] == cli.nombre]
                if not propias:
                    continue
                print()
                tabla([f for f in propias if f["_orden"] <= 1 or f["aviso"]], COL_LIMITES)
                resto = [f for f in propias if f["_orden"] > 1 and not f["aviso"]]
                if resto:     # una linea corta por tipo: "Revistas: prestamos 0/2, reservas 1/1"
                    print("  A la vez (quedan/maximo):")
                    tipos = {}
                    for f in resto:
                        t_, _, q_ = f["limite"].replace(" a la vez", "").partition(": ")
                        tipos.setdefault(t_, []).append("%s %d/%d" % (q_, f["quedan"], f["max"]))
                    for t_, v_ in tipos.items():
                        print(recortar("    %s: %s" % (t_, ", ".join(v_)), ancho_pantalla() - 1))
                act = actividad(cli) or {}
                if act.get("_podras"):
                    print("  OJO: %s." % act["_podras"].rstrip("."))
                avisos = [f for f in propias if f["aviso"] and f["_orden"] <= 1]
                for f in avisos:
                    print("  AVISO %s: %s -> %s (quedan %d de %d)." % (cli.nombre, f["limite"], f["aviso"],
                                                                     f["quedan"], f["max"]))
        else:
            print("No he encontrado los contadores de limites en tu cuenta.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelado.")
    except requests.RequestException as e:
        sys.exit("Error de red: %s" % e)
