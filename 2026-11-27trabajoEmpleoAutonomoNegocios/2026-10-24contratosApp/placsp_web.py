#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
placsp_web.py  -  Consulta desde Termux la web de la Plataforma de Contratacion
del Sector Publico (contrataciondelestado.es) sin navegador.

Hace lo mismo que haces a mano en la web:
   1) Licitaciones          (formulario de busqueda de licitaciones)
   2) Contratos menores     (formulario de busqueda de contratos menores)
   3) Documentos            (buscador documental / gestor documental)
y ademas abre la ficha de un expediente y descarga sus documentos (pliegos...).

Solo usa la biblioteca estandar de Python. No hace falta instalar nada.

USO
  python placsp_web.py                      menu interactivo
  python placsp_web.py menores --organo "La Adrada"
  python placsp_web.py menores --organo "La Adrada" --desde 01-01-2025 --hasta 31-12-2025
  python placsp_web.py licitaciones --organo "La Adrada" --estado PUB
  python placsp_web.py documentos --texto "La Adrada pliego"
  python placsp_web.py detalle "<enlace deeplink de un resultado>" --descargar
  python placsp_web.py ayuda

Si algo falla, anade --debug : guarda en la carpeta debug_placsp/ el HTML de cada
paso de la navegacion (util para ajustar el programa si la web cambia).
"""
import os, re, sys, csv, time, html, argparse, urllib.request, urllib.parse, urllib.error
import http.cookiejar
from html.parser import HTMLParser

BASE = os.environ.get("PLACSP_BASE", "https://contrataciondelestado.es")
URL_BUSCADORES = BASE + "/wps/portal/plataforma/buscadores"
URL_LICITACIONES_DIRECTA = BASE + "/wps/portal/licitaciones"
URL_DOCUMENTOS = BASE + "/wps/portal/plataforma/buscador/"
UA = ("Mozilla/5.0 (Android 13; Mobile; rv:128.0) Gecko/128.0 Firefox/128.0")
AQUI = os.path.dirname(os.path.abspath(__file__))
DEBUG_DIR = os.path.join(AQUI, "debug_placsp")
SALIDA = os.path.join(AQUI, "salida_placsp")
DESCARGAS = os.path.join(AQUI, "descargas_placsp")

# sufijos de los campos de cada formulario (tal como aparecen en los HTML guardados)
CAMPOS_COMUNES = {
    "expediente": "text71ExpMAQ",
    "pais": "menu1MAQ1",
    "tipo": "combo1MAQ",
    "organo": "texoorganoMAQ",
    "estado": "estadoLici",
    "importe_desde": "textEstimadoDesde18MAQ",
    "importe_hasta": "textEstimado19MAQ",
    "adjudicatario": "texAdjudicatarioMAQ",
    "desde": "textMinFecAnuncioMAQ2",
    "hasta": "textMaxFecAnuncioMAQ",
    "presentacion_desde": "textMinFecLimite",
    "presentacion_hasta": "textMaxFecLimite",
}
CAMPOS_LICIT = dict(CAMPOS_COMUNES, procedimiento="menu111MAQ",
                    sistema="tipoSistemaContratacion",
                    forma_presentacion="menuFormaPresentacionMAQ1_SistPresent",
                    financiacion_ue="Financiacion", fuente_fondos="Fuente",
                    tramitacion="menuTipoTramitacionMAQ", cpv="cpvMultiple:codigoCpv")
CAMPOS_MENORES = dict(CAMPOS_COMUNES, organizacion="texOrgContMAQ")
TIPOS = {"suministros": "1", "servicios": "2", "obras": "3", "administrativo especial": "7",
         "privado": "8", "gestion de servicios publicos": "21", "concesion de servicios": "22",
         "concesion de obras": "32", "patrimonial": "50"}


def norm(s):
    import unicodedata
    s = unicodedata.normalize("NFD", str(s or ""))
    return "".join(c for c in s if unicodedata.category(c) != "Mn").lower().strip()


def limpia(s):
    return re.sub(r"\s+", " ", html.unescape(s or "")).strip()


# ------------------------------------------------------------ parseo de HTML
class Form:
    def __init__(self, attrs):
        self.attrs = attrs
        self.ctrls = []   # dicts
        self.id = attrs.get("id") or attrs.get("name") or ""
        self.action = attrs.get("action") or ""

    def buscar(self, sufijo):
        for c in self.ctrls:
            n = c.get("name") or ""
            if n == sufijo or n.endswith(":" + sufijo):
                return c
        return None

    def payload(self):
        """lo que enviaria el navegador (sin botones)."""
        d = []
        for c in self.ctrls:
            n = c.get("name")
            if not n or c.get("disabled"):
                continue
            t = c["tag"]
            if t == "input":
                ty = (c.get("type") or "text").lower()
                if ty in ("submit", "button", "image", "reset", "file"):
                    continue
                if ty in ("checkbox", "radio"):
                    if c.get("checked"):
                        d.append((n, c.get("value") if c.get("value") is not None else "on"))
                    continue
                d.append((n, c.get("value") or ""))
            elif t == "select":
                sel = [o[0] for o in c["options"] if o[2]]
                if not sel and c["options"] and not c.get("multiple"):
                    sel = [c["options"][0][0]]
                for v in sel:
                    d.append((n, v))
            elif t == "textarea":
                d.append((n, c.get("text") or ""))
        return d


class PaginaParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms = []
        self.cur = None
        self.sel = None
        self.opt = None
        self.ta = None
        self.links = []     # (href, texto, id, onclick)
        self._a = None
        self.tables = []
        self._tstack = []
        self._row = None
        self._cell = None
        self.title = ""
        self._intitle = False

    def handle_starttag(self, t, a):
        a = dict(a)
        if t == "title":
            self._intitle = True
        if t == "form":
            self.cur = Form(a)
            self.forms.append(self.cur)
        elif t == "input" and self.cur is not None:
            self.cur.ctrls.append(dict(tag="input", type=a.get("type", "text"), name=a.get("name"),
                                       id=a.get("id"), value=a.get("value"),
                                       checked=("checked" in a), disabled=("disabled" in a),
                                       readonly=("readonly" in a), title=a.get("title") or a.get("alt") or ""))
        elif t == "select" and self.cur is not None:
            self.sel = dict(tag="select", name=a.get("name"), id=a.get("id"), options=[],
                            multiple=("multiple" in a), disabled=("disabled" in a))
            self.cur.ctrls.append(self.sel)
        elif t == "option" and self.sel is not None:
            self.opt = [a.get("value"), "", "selected" in a]
            self.sel["options"].append(self.opt)
        elif t == "textarea" and self.cur is not None:
            self.ta = dict(tag="textarea", name=a.get("name"), id=a.get("id"), text="",
                           disabled=("disabled" in a))
            self.cur.ctrls.append(self.ta)
        elif t == "a":
            self._a = [a.get("href") or "", "", a.get("id") or "", a.get("onclick") or ""]
        elif t == "table":
            self._tstack.append([])
        elif t == "tr" and self._tstack:
            self._row = []
        elif t in ("td", "th") and self._row is not None:
            self._cell = {"txt": "", "links": [], "th": t == "th"}
        if self._cell is not None and t == "a" and a.get("href"):
            self._cell["links"].append(a.get("href"))
        if self._cell is not None and t == "input" and (a.get("type") or "").lower() in ("submit", "image"):
            self._cell["links"].append("submit:" + (a.get("name") or ""))

    def handle_endtag(self, t):
        if t == "title":
            self._intitle = False
        if t == "form":
            self.cur = None
        elif t == "select":
            self.sel = None
        elif t == "option":
            if self.opt is not None and self.opt[0] is None:
                self.opt[0] = self.opt[1].strip()
            self.opt = None
        elif t == "textarea":
            self.ta = None
        elif t == "a" and self._a is not None:
            self.links.append(tuple(self._a))
            self._a = None
        elif t in ("td", "th") and self._cell is not None and self._row is not None:
            self._cell["txt"] = limpia(self._cell["txt"])
            self._row.append(self._cell)
            self._cell = None
        elif t == "tr" and self._row is not None and self._tstack:
            self._tstack[-1].append(self._row)
            self._row = None
        elif t == "table" and self._tstack:
            self.tables.append(self._tstack.pop())

    def handle_data(self, d):
        if self._intitle:
            self.title += d
        if self.opt is not None:
            self.opt[1] += d
        if self.ta is not None:
            self.ta["text"] += d
        if self._a is not None:
            self._a[1] += d
        if self._cell is not None:
            self._cell["txt"] += d + " "


class Pagina:
    def __init__(self, texto, url):
        self.html = texto
        self.url = url
        p = PaginaParser()
        try:
            p.feed(texto)
        except Exception:
            pass
        self.forms = p.forms
        self.links = p.links
        self.tables = p.tables
        self.title = limpia(p.title)

    def form_con(self, sufijo):
        for f in self.forms:
            if f.buscar(sufijo):
                return f
        return None

    def texto(self):
        t = re.sub(r"(?is)<(script|style).*?</\1>", " ", self.html)
        t = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h\d|table)>", "\n", t)
        t = re.sub(r"(?s)<[^>]+>", " ", t)
        t = html.unescape(t)
        lin = [re.sub(r"[ \t\xa0]+", " ", l).strip() for l in t.split("\n")]
        return "\n".join(l for l in lin if l)


# --------------------------------------------------------------- sesion http
class Sesion:
    def __init__(self, debug=False, pausa=0.6):
        self.cj = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cj))
        self.op.addheaders = [("User-Agent", UA), ("Accept-Language", "es-ES,es;q=0.9"),
                              ("Accept", "text/html,application/xhtml+xml,*/*;q=0.8")]
        self.debug = debug
        self.n = 0
        self.pausa = pausa
        self.pagina = None

    def _volcar(self, nombre, texto):
        if not self.debug:
            return
        os.makedirs(DEBUG_DIR, exist_ok=True)
        self.n += 1
        ruta = os.path.join(DEBUG_DIR, "%02d_%s.html" % (self.n, re.sub(r"\W+", "_", nombre)[:40]))
        with open(ruta, "w", encoding="utf-8") as f:
            f.write(texto)

    def _decodifica(self, r, datos):
        cs = r.headers.get_content_charset() or "utf-8"
        try:
            return datos.decode(cs, "replace")
        except LookupError:
            return datos.decode("utf-8", "replace")

    def abrir(self, url, datos=None, referer=None, nombre="pagina"):
        time.sleep(self.pausa)   # no saturar el servidor
        cuerpo = None
        if datos is not None:
            cuerpo = urllib.parse.urlencode(datos, doseq=True).encode("utf-8")
        rq = urllib.request.Request(url, data=cuerpo)
        if cuerpo is not None:
            rq.add_header("Content-Type", "application/x-www-form-urlencoded")
        if referer:
            rq.add_header("Referer", referer)
        try:
            r = self.op.open(rq, timeout=60)
            datos_r = r.read()
        except urllib.error.HTTPError as e:
            datos_r = e.read()
            print("   [aviso] HTTP %s en %s" % (e.code, url[:90]))
            r = e
        texto = self._decodifica(r, datos_r)
        self._volcar(nombre, texto)
        pg = Pagina(texto, r.geturl() if hasattr(r, "geturl") else url)
        self.pagina = pg
        return pg

    def descargar(self, url, destino_dir, referer=None):
        os.makedirs(destino_dir, exist_ok=True)
        rq = urllib.request.Request(url)
        if referer:
            rq.add_header("Referer", referer)
        r = self.op.open(rq, timeout=120)
        nombre = None
        cd = r.headers.get("Content-Disposition") or ""
        m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', cd)
        if m:
            nombre = urllib.parse.unquote(m.group(1))
        if not nombre:
            nombre = os.path.basename(urllib.parse.urlparse(url).path) or "documento"
            if "." not in nombre:
                nombre += ".bin"
        nombre = re.sub(r'[\\/:*?"<>|]+', "_", nombre)
        ruta = os.path.join(destino_dir, nombre)
        with open(ruta, "wb") as f:
            while True:
                b = r.read(1 << 18)
                if not b:
                    break
                f.write(b)
        return ruta


# ------------------------------------------------------------- navegacion JSF
def url_abs(base, href):
    return urllib.parse.urljoin(base, html.unescape(href))


def enviar_form(ses, pg, form, extra, nombre):
    """POST del formulario (con todos sus campos ocultos) mas los campos extra."""
    datos = form.payload()
    claves = {k for k, _ in extra}
    datos = [(k, v) for k, v in datos if k not in claves] + list(extra)
    return ses.abrir(url_abs(pg.url, form.action), datos, referer=pg.url, nombre=nombre)


def pulsar_enlace_jsf(ses, pg, sufijo_id, marca_destino, nombre):
    """Simula el clic en un enlace JSF (<a href="#" id="...">). Prueba las variantes
    habituales de JSF/IBM Faces hasta que aparece el formulario esperado."""
    form = None
    idenlace = None
    for f in pg.forms:
        for (href, txt, i, onc) in pg.links:
            if i.endswith(sufijo_id) and (i.startswith(f.id) or f.id in i):
                form, idenlace = f, i
    if form is None:
        for (href, txt, i, onc) in pg.links:
            if i.endswith(sufijo_id):
                idenlace = i
        for f in pg.forms:
            if idenlace and idenlace.startswith(f.id):
                form = f
    if form is None or idenlace is None:
        raise RuntimeError("No encuentro el enlace '%s' en la pagina (¿cambio la web?)" % sufijo_id)
    ocultos = [c["name"] for c in form.ctrls if c["tag"] == "input" and c.get("name")
               and (c.get("type") or "").lower() == "hidden"]
    candidatos = [n for n in ocultos if re.search(r"link_hidden|_idcl|idcl$|_link", n)]
    intentos = []
    for n in candidatos:
        intentos.append([(n, idenlace)])
    intentos.append([(form.id + ":_idcl", idenlace)])
    intentos.append([(form.id + ":_link_hidden_", idenlace)])
    intentos.append([(form.id + ":_idcl", idenlace), (form.id + ":_link_hidden_", idenlace)])
    # algunas variantes anaden el marcador "<form>_SUBMIT=1"
    ultimo = None
    for k, ex in enumerate(intentos):
        ex2 = ex + [(form.id + "_SUBMIT", "1")]
        r = enviar_form(ses, pg, form, ex2, "%s_intento%d" % (nombre, k + 1))
        ultimo = r
        if r.form_con(marca_destino):
            return r
    return ultimo


def abrir_formulario(ses, tipo):
    """tipo: 'licitaciones' | 'menores'. Devuelve la pagina con el formulario."""
    if tipo == "licitaciones":
        suf, marca, camp = "linkFormularioBusqueda", "text71ExpMAQ", "textMinFecAnuncioMAQ2"
    else:
        suf, marca, camp = "linkFormularioBusquedaContratosMenores", "texOrgContMAQ", "texAdjudicatarioMAQ"
    print("Abriendo la pagina de buscadores...")
    pg = ses.abrir(URL_BUSCADORES, nombre="buscadores")
    if pg.form_con(marca):
        return pg
    print("Seleccionando el buscador de %s..." % tipo)
    r = pulsar_enlace_jsf(ses, pg, suf, marca, "clic_" + tipo)
    if r is not None and r.form_con(marca):
        return r
    if tipo == "licitaciones":
        print("Probando acceso directo a la busqueda avanzada de licitaciones...")
        r = ses.abrir(URL_LICITACIONES_DIRECTA, nombre="licitaciones_directa")
        if r.form_con(marca):
            return r
    raise RuntimeError(
        "No he podido abrir el formulario de %s. Repite con --debug y envia el contenido\n"
        "de la carpeta debug_placsp/ para ajustar el programa." % tipo)


def fecha_web(s):
    """acepta AAAA-MM-DD o DD-MM-AAAA o DD/MM/AAAA y devuelve DD-MM-AAAA."""
    if not s:
        return s
    s = s.strip().replace("/", "-")
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", s)
    if m:
        return "%s-%s-%s" % (m.group(3), m.group(2), m.group(1))
    return s


def importe_web(x):
    if x in (None, ""):
        return x
    return str(x).replace(".", ",") if isinstance(x, float) else str(x)


def rellenar(form, mapa, valores):
    avisos = []
    extra = {}
    for clave, val in valores.items():
        if val in (None, ""):
            continue
        suf = mapa.get(clave)
        if not suf:
            avisos.append("filtro '%s' no existe en este formulario" % clave)
            continue
        c = form.buscar(suf)
        if c is None:
            avisos.append("campo '%s' no encontrado (¿cambio la web?)" % clave)
            continue
        if clave == "tipo":
            val = TIPOS.get(norm(val), val)
        if clave in ("desde", "hasta", "presentacion_desde", "presentacion_hasta"):
            val = fecha_web(val)
        if c["tag"] == "select":
            ops = c["options"]
            elegido = None
            for v, t, _ in ops:
                if norm(v) == norm(val) or norm(t) == norm(val):
                    elegido = v
                    break
            if elegido is None:
                for v, t, _ in ops:
                    if norm(val) in norm(t):
                        elegido = v
                        break
            if elegido is None:
                avisos.append("valor '%s' no valido para '%s'. Opciones: %s" % (
                    val, clave, ", ".join("%s=%s" % (v, limpia(t)) for v, t, _ in ops[:15])))
                continue
            extra[c["name"]] = elegido
        else:
            extra[c["name"]] = str(val)
    return extra, avisos


def boton_buscar(form):
    """el boton 'Buscar' del formulario (el de abajo; si no, el de arriba)."""
    cand = [c for c in form.ctrls if c["tag"] == "input" and (c.get("type") or "").lower() == "submit"
            and (c.get("value") or "").strip().lower() == "buscar"]
    if not cand:
        cand = [c for c in form.ctrls if c["tag"] == "input" and (c.get("type") or "").lower() == "submit"
                and re.search(r"busc", (c.get("name") or "") + (c.get("value") or ""), re.I)]
    if not cand:
        return None
    cand.sort(key=lambda c: ("Arriba" in c["name"], c["name"]))
    return cand[0]


# --------------------------------------------------------------- resultados
def tabla_resultados(pg):
    """Elige la tabla de resultados: la que mas filas con enlaces/datos tiene."""
    mejor, pts = None, 0
    for t in pg.tables:
        filas = [r for r in t if len(r) >= 3 and any(c["txt"] for c in r)]
        if len(filas) < 2:
            continue
        enl = sum(1 for r in filas for c in r if c["links"])
        p = len(filas) * 2 + enl
        if p > pts:
            mejor, pts = filas, p
    return mejor


def filas_a_dicts(filas):
    if not filas:
        return [], []
    cab = None
    datos = []
    for r in filas:
        if all(c["th"] for c in r) and cab is None:
            cab = [c["txt"] for c in r]
            continue
        datos.append(r)
    return cab, datos


def pie_paginas(pg):
    """texto tipo 'Resultados 1 - 10 de 53' si aparece."""
    m = re.search(r"(?:Resultados?|Mostrando)[^\n<>]{0,60}\d+\s*(?:-|a|de)\s*\d+(?:\s*de\s*\d+)?", pg.texto(), re.I)
    return limpia(m.group(0)) if m else ""


def siguiente_pagina(ses, pg):
    for f in pg.forms:
        for c in f.ctrls:
            if c["tag"] != "input" or (c.get("type") or "").lower() not in ("submit", "image", "button"):
                continue
            etiqueta = norm((c.get("value") or "") + " " + (c.get("title") or "") + " " + (c.get("name") or ""))
            if re.search(r"siguiente|next|>>|sigu", etiqueta) and not re.search(r"anterior|prev|primer|ultim", etiqueta):
                if c.get("disabled"):
                    continue
                return enviar_form(ses, pg, f, [(c["name"], c.get("value") or "")], "siguiente")
    for (href, txt, i, onc) in pg.links:
        if re.search(r"siguiente|next|^>>?$|»", norm(txt)) and i:
            for f in pg.forms:
                if i.startswith(f.id):
                    return pulsar_enlace_jsf(ses, pg, i, "", "siguiente_enlace")
    return None


def mostrar_resultados(pg, n0=1, cortar=34):
    filas = tabla_resultados(pg)
    cab, datos = filas_a_dicts(filas)
    if not datos:
        print("(No he reconocido una tabla de resultados en la pagina.)")
        t = pg.texto()
        print(t[:1500])
        return []
    out = []
    for i, r in enumerate(datos, n0):
        celdas = [c["txt"] for c in r]
        enl = [l for c in r for l in c["links"]]
        det = next((l for l in enl if "deeplink" in l or "detalle" in l.lower()), enl[0] if enl else "")
        out.append({"n": i, "celdas": celdas, "enlace": url_abs(pg.url, det) if det and not det.startswith("submit:") else det})
    ncol = max(len(o["celdas"]) for o in out)
    anch = [len(cab[j]) if cab and j < len(cab) else 0 for j in range(ncol)]
    anch = [min(x, cortar) for x in anch]
    for o in out:
        for j, c in enumerate(o["celdas"]):
            anch[j] = max(anch[j], min(len(c), cortar))
    if cab:
        print("  ".join(["#".ljust(4)] + [(cab[j] if j < len(cab) else "")[:anch[j]].ljust(anch[j]) for j in range(ncol)]))
    for o in out:
        cel = [(o["celdas"][j] if j < len(o["celdas"]) else "") for j in range(ncol)]
        print("  ".join([str(o["n"]).ljust(4)] + [c[:anch[j]].ljust(anch[j]) for j, c in enumerate(cel)]))
    return out


def exportar(res, nombre, cab=None):
    os.makedirs(SALIDA, exist_ok=True)
    ruta = nombre if os.path.dirname(nombre) else os.path.join(SALIDA, nombre)
    if ruta.endswith(".csv"):
        with open(ruta, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            for o in res:
                w.writerow([o["n"]] + o["celdas"] + [o["enlace"]])
    else:
        with open(ruta, "w", encoding="utf-8") as f:
            for o in res:
                f.write("=" * 78 + "\n# %s\n" % o["n"])
                for c in o["celdas"]:
                    f.write("  " + c + "\n")
                f.write("  Enlace: %s\n" % o["enlace"])
    print("Exportado a:", ruta)


# ------------------------------------------------------------ operaciones
def buscar_formulario(tipo, filtros, max_paginas=3, debug=False, exportar_a=None):
    ses = Sesion(debug)
    pg = abrir_formulario(ses, tipo)
    mapa = CAMPOS_LICIT if tipo == "licitaciones" else CAMPOS_MENORES
    form = pg.form_con(mapa["expediente"]) or pg.form_con("texOrgContMAQ")
    extra, avisos = rellenar(form, mapa, filtros)
    for a in avisos:
        print("  [aviso]", a)
    # si no se indican fechas, vaciamos las que la web pone por defecto (ultimos meses)
    for clave in ("desde", "hasta"):
        if not filtros.get(clave):
            c = form.buscar(mapa[clave])
            if c is not None and c.get("value"):
                extra[c["name"]] = ""
    b = boton_buscar(form)
    if b is None:
        raise RuntimeError("No encuentro el boton Buscar")
    print("Buscando...")
    r = enviar_form(ses, pg, form, list(extra.items()) + [(b["name"], b.get("value") or "Buscar")], "resultados_1")
    todo = []
    for p in range(1, max_paginas + 1):
        print("\n--- pagina %d %s" % (p, pie_paginas(r)))
        res = mostrar_resultados(r, len(todo) + 1)
        todo += res
        if not res or p == max_paginas:
            break
        nxt = siguiente_pagina(ses, r)
        if nxt is None:
            break
        r = nxt
    if exportar_a and todo:
        exportar(todo, exportar_a)
    return ses, todo


def buscar_documentos(texto, max_paginas=3, debug=False, exportar_a=None):
    ses = Sesion(debug)
    print("Abriendo el buscador documental...")
    pg = ses.abrir(URL_DOCUMENTOS, nombre="docs")
    form = pg.form_con("boxSearch")
    if form is None:
        raise RuntimeError("No encuentro el buscador documental. Repite con --debug.")
    caja = form.buscar("boxSearch")
    boton = form.buscar("botonSubmitSearch")
    ex = [(caja["name"], texto)]
    if boton:
        ex.append((boton["name"], boton.get("value") or "Buscar"))
    r = enviar_form(ses, pg, form, ex, "docs_resultados_1")
    todo = []
    for p in range(1, max_paginas + 1):
        print("\n--- pagina %d %s" % (p, pie_paginas(r)))
        res = mostrar_resultados(r, len(todo) + 1, cortar=60)
        if not res:
            # el buscador documental puede mostrar una lista en vez de una tabla
            enl = [(h, limpia(t)) for (h, t, i, o) in r.links
                   if h and re.search(r"GetDocument|deeplink|Document|\.pdf", h, re.I) and limpia(t)]
            for k, (h, t) in enumerate(enl, len(todo) + 1):
                print("%-4d %s\n     %s" % (k, t[:100], url_abs(r.url, h)))
                res.append({"n": k, "celdas": [t], "enlace": url_abs(r.url, h)})
        todo += res
        if not res or p == max_paginas:
            break
        nxt = siguiente_pagina(ses, r)
        if nxt is None:
            break
        r = nxt
    if exportar_a and todo:
        exportar(todo, exportar_a)
    return ses, todo


def detalle(url, descargar=False, debug=False, ses=None):
    ses = ses or Sesion(debug)
    pg = ses.abrir(url, nombre="detalle")
    print(pg.texto()[:6000])
    docs = []
    for (h, t, i, o) in pg.links:
        if h and re.search(r"GetDocumentByIdServlet|GetDocument|FileSystem|\.pdf|\.zip|\.docx?|\.xlsx?", h, re.I):
            docs.append((limpia(t) or os.path.basename(h), url_abs(pg.url, h)))
    # (en la web real, los documentos de la ficha suelen colgar de enlaces 'Documento ...')
    print("\n=== Documentos enlazados en la ficha: %d ===" % len(docs))
    for k, (t, h) in enumerate(docs, 1):
        print("%-3d %s\n    %s" % (k, t[:90], h))
    if descargar and docs:
        d = os.path.join(DESCARGAS, re.sub(r"\W+", "_", urllib.parse.urlparse(url).query)[-40:] or "expediente")
        for t, h in docs:
            try:
                print("Descargando", t[:60], "...")
                print("   ->", ses.descargar(h, d, referer=pg.url))
            except Exception as ex:
                print("   fallo:", ex)
    return docs


# --------------------------------------------------------------------- CLI
def ayuda():
    print(__doc__)
    print("""FILTROS (licitaciones y menores)
  --expediente TEXTO     --organo "La Adrada"   (nombre del organo de contratacion)
  --organizacion TEXTO   (solo menores: organizacion contratante)
  --tipo suministros|servicios|obras|...      --estado PUB|ADJ|RES|EV|ANUL|...
  --adjudicatario TEXTO
  --desde 01-01-2025 --hasta 31-12-2025      (fecha de publicacion/adjudicacion)
  --importe-desde 0 --importe-hasta 15000
  --presentacion-desde / --presentacion-hasta   (plazo de presentacion de ofertas)
  --procedimiento abierto|negociado...   --cpv 72000000   (solo licitaciones)
  --paginas N       numero de paginas de resultados a leer (por defecto 3)
  --exportar fichero.txt|fichero.csv
  --debug           guarda el HTML de cada paso en debug_placsp/
Los valores de las listas desplegables se reconocen por codigo o por texto
(sin tildes, parcial). Si no se reconoce, el programa muestra las opciones validas.""")


def preguntar(m, d=""):
    try:
        r = input(m).strip()
    except EOFError:
        r = ""
    return r or d


def menu(debug):
    ultimo = []
    ses = None
    while True:
        print("\n" + "=" * 60)
        print(" PLATAFORMA DE CONTRATACION DEL SECTOR PUBLICO (web)")
        print("=" * 60)
        print(" 1) Buscar CONTRATOS MENORES")
        print(" 2) Buscar LICITACIONES (no menores)")
        print(" 3) Buscar DOCUMENTOS (gestor documental)")
        print(" 4) Atajo: menores del Ayuntamiento de La Adrada")
        print(" 5) Atajo: licitaciones del Ayuntamiento de La Adrada")
        print(" 6) Ver ficha / documentos de un resultado")
        print(" 7) Ayuda")
        print(" 0) Salir")
        op = preguntar("Opcion: ")
        try:
            if op == "0":
                return
            elif op in ("1", "2", "4", "5"):
                tipo = "menores" if op in ("1", "4") else "licitaciones"
                f = {}
                if op in ("4", "5"):
                    f["organo"] = "La Adrada"
                    d = preguntar("Desde (DD-MM-AAAA, vacio = sin limite): ")
                    if d:
                        f["desde"] = d
                else:
                    for k, m in (("expediente", "Expediente: "), ("organo", "Organo de contratacion: "),
                                 ("tipo", "Tipo (suministros/servicios/obras): "),
                                 ("estado", "Estado (PUB, ADJ, RES, EV...): "),
                                 ("adjudicatario", "Adjudicatario: "),
                                 ("desde", "Fecha desde (DD-MM-AAAA): "), ("hasta", "Fecha hasta (DD-MM-AAAA): "),
                                 ("importe_desde", "Importe desde: "), ("importe_hasta", "Importe hasta: ")):
                        v = preguntar(m)
                        if v:
                            f[k] = v
                ses, ultimo = buscar_formulario(tipo, f, 3, debug)
                _post(ses, ultimo)
            elif op == "3":
                t = preguntar("Texto a buscar: ")
                if t:
                    ses, ultimo = buscar_documentos(t, 3, debug)
                    _post(ses, ultimo)
            elif op == "6":
                u = preguntar("Numero del ultimo resultado o enlace completo: ")
                if u.isdigit() and ultimo and 1 <= int(u) <= len(ultimo):
                    u = ultimo[int(u) - 1]["enlace"]
                if u.startswith("http"):
                    detalle(u, preguntar("¿Descargar documentos? (s/N): ").lower() == "s", debug, ses)
            elif op == "7":
                ayuda()
        except Exception as ex:
            print("\n[ERROR]", ex)
            print("Prueba con --debug (python placsp_web.py --debug) para guardar el HTML de cada paso.")


def _post(ses, res):
    while res:
        x = preguntar("\nNumero para abrir su ficha, 'e' exportar, Enter para volver: ")
        if not x:
            return
        if x.lower() == "e":
            exportar(res, preguntar("Nombre (.txt/.csv) [resultados.txt]: ", "resultados.txt"))
        elif x.isdigit() and 1 <= int(x) <= len(res) and res[int(x) - 1]["enlace"].startswith("http"):
            detalle(res[int(x) - 1]["enlace"], preguntar("¿Descargar documentos? (s/N): ").lower() == "s", ses.debug, ses)


def main():
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--debug", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    for nombre in ("menores", "licitaciones"):
        s = sub.add_parser(nombre)
        for k in ("expediente", "organo", "organizacion", "tipo", "estado", "adjudicatario", "desde", "hasta",
                  "presentacion_desde", "presentacion_hasta", "procedimiento", "cpv", "exportar"):
            s.add_argument("--" + k.replace("_", "-"), dest=k)
        s.add_argument("--importe-desde", dest="importe_desde")
        s.add_argument("--importe-hasta", dest="importe_hasta")
        s.add_argument("--paginas", type=int, default=3)
        s.add_argument("--debug", action="store_true")
    d = sub.add_parser("documentos")
    d.add_argument("--texto", required=True)
    d.add_argument("--paginas", type=int, default=3)
    d.add_argument("--exportar")
    d.add_argument("--debug", action="store_true")
    t = sub.add_parser("detalle")
    t.add_argument("url")
    t.add_argument("--descargar", action="store_true")
    t.add_argument("--debug", action="store_true")
    sub.add_parser("ayuda")
    ar = ap.parse_args()
    dbg = getattr(ar, "debug", False)
    try:
        if ar.cmd in ("menores", "licitaciones"):
            f = {k: v for k, v in vars(ar).items() if v and k not in ("cmd", "exportar", "paginas", "debug")}
            buscar_formulario(ar.cmd, f, ar.paginas, dbg, ar.exportar)
        elif ar.cmd == "documentos":
            buscar_documentos(ar.texto, ar.paginas, dbg, ar.exportar)
        elif ar.cmd == "detalle":
            detalle(ar.url, ar.descargar, dbg)
        elif ar.cmd == "ayuda":
            ayuda()
        else:
            menu(dbg)
    except Exception as ex:
        print("\n[ERROR]", ex)
        sys.exit(1)


if __name__ == "__main__":
    main()
