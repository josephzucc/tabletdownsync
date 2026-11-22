#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cnmv_oir.py -- version 1.3 (22-oct-2026)
Herramienta Termux para consultar "Otra Informacion Relevante" (OIR) de la
CNMV: https://www.cnmv.es/Portal/Otra-Informacion-Relevante/Consulta-OIR.aspx

Que cambia en la 1.3 respecto a la 1.2 (primera prueba real de la consulta
por NIF, 22-oct-2026):
  - RETIRADA la consulta por NIF (--nif). La direccion
    .../Resultado-OIR?nif=... no es una lista: la CNMV usa
    .../Resultado-OIR?nreg=NUMERO para la ficha de UN solo registro, y sin
    numero devuelve una pagina casi vacia con el menu de la web. La 1.2
    tomo un enlace del menu por resultado y descargo un PDF ajeno
    (Conducta.pdf).
  - Los resultados se reconocen solo por sus enlaces reales (fichas
    Resultado-OIR?nreg=..., documentos webservices/verdocumento/ver... y
    PDF sueltos). Ya no se toman enlaces del menu ni del pie de la web.
  - Al descargar se elige el documento de la ficha y se descartan los PDF
    fijos de la web (/DocPortal/...).
  - Nuevo: "ficha NUMERO" para ver o descargar un registro concreto.
  - Nuevo: --lei y --ultimos N en "buscar" (campos reales del formulario).
  - Si pdf2txt.py falla, el log dice por que.
  - Al arrancar se borran de la base de datos los registros erroneos que
    dejo la 1.2.
Sigue sin verificar: como se ve la LISTA de resultados que devuelve el
formulario (por eso se guarda siempre un volcado en diagnostico/).
Vease cnmvManual.txt.
"""

import argparse
import datetime as dt
import hashlib
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
import unicodedata
from pathlib import Path
from urllib.parse import urljoin, urlsplit

try:
    import requests
except ImportError:
    print("Falta el paquete 'requests'. Instale con: pip install requests")
    sys.exit(1)

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("Falta el paquete 'beautifulsoup4'. Instale con: "
          "pip install beautifulsoup4")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Configuracion
# ---------------------------------------------------------------------------

VERSION = "1.3"
BASE_URL = "https://www.cnmv.es/Portal/Otra-Informacion-Relevante/Consulta-OIR.aspx"
RESULT_URL = "https://www.cnmv.es/Portal/Otra-Informacion-Relevante/Resultado-OIR"
HOME_DIR = Path(__file__).resolve().parent
DB_PATH = HOME_DIR / "cnmv_oir.db"
PDF_DIR = HOME_DIR / "cnmv_oir_pdfs"
DIAG_DIR = HOME_DIR / "diagnostico"
LOG_PATH = HOME_DIR / "cnmv_oir_diagnostico.log"
PDF2TXT_ENV = "PDF2TXT_PATH"

MAX_VOLCADOS = 30          # cuantos volcados de HTML se conservan en diagnostico/
PAUSA_DESCARGAS_S = 0.5    # pausa entre descargas, por cortesia con el servidor

USER_AGENT = (
    f"Mozilla/5.0 (Linux; Android 10; Termux) cnmv_oir.py/{VERSION} "
    "(uso personal, consulta de informacion publica)"
)

log = logging.getLogger("cnmv_oir")

# Categorias de "tipo de otra informacion relevante", confirmadas contra el
# HTML real de la pagina (19-oct-2026). El campo real de cada una es
# ctl00$ContentPrincipal$wucTipoEntidad$lstTipoentidad2$<indice>. La propia
# CNMV permite marcar como maximo 3 a la vez; si no se marca ninguna, busca
# en todas.
TIPOS_OIR = [
    "Informes financieros anuales y de auditoria",
    "Informes financieros semestrales y de auditoria / revisiones limitadas",
    "Declaracion Intermedia de Gestion",
    "Informe anual de gobierno corporativo",
    "Informe anual de remuneraciones de los consejeros",
    "Informe de pagos a las administraciones publicas",
    "Estado miembro de origen",
    "Total numero de derechos de voto y de capital",
    "Cambios en los derechos vinculados a las clases de acciones u otros valores",
    "Comunicacion de operaciones vinculadas",
    "Convocatoria de Junta o Asamblea",
    "Otra informacion relevante",
    "Sobre instrumentos financieros",
    "Ofertas publicas de suscripcion y venta",
    "Ofertas publicas de adquisicion de acciones",
    "Ofertas publicas de adquisicion de otros valores",
    "Programas de recompra de acciones, estabilizacion y autocartera",
    "Contratos de liquidez y contrapartida",
    "Sobre negocio y situacion financiera",
    "Sobre operaciones corporativas",
    "Sobre Gobierno corporativo",
    "Pactos parasociales",
    "Reglamentos del Consejo",
    "Reglamentos de la Junta",
    "Sobre fondos de titulizacion",
    "Sobre suspensiones, levantamientos y exclusiones de negociacion",
    "Sobre calificaciones crediticias",
    "Acciones lealtad. Aprobacion/cancelacion regimen",
    "Acciones lealtad. Movimientos libro registro",
    "Informe de paridad",
]


def campo_tipo(indice):
    return f"ctl00$ContentPrincipal$wucTipoEntidad$lstTipoentidad2${indice}"


def setup_logging(verbose=False):
    DIAG_DIR.mkdir(exist_ok=True)
    handlers = [
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
        force=True,
    )


def registrar_traza():
    """Añade al log la traza completa de la excepcion en curso."""
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(traceback.format_exc())
            f.write("\n")
    except OSError:
        pass


def _podar_diagnostico(maximo=MAX_VOLCADOS):
    try:
        archivos = sorted(DIAG_DIR.glob("respuesta_*.html"),
                          key=lambda p: p.stat().st_mtime)
        for viejo in archivos[:-maximo]:
            viejo.unlink()
    except OSError:
        pass


def dump_html(html, motivo, nivel=logging.WARNING):
    """Guarda el HTML recibido para diagnostico, con marca de tiempo."""
    DIAG_DIR.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = DIAG_DIR / f"respuesta_{motivo}_{stamp}.html"
    path.write_text(html, encoding="utf-8", errors="replace")
    log.log(nivel, "Volcado de HTML guardado para diagnostico: %s", path)
    _podar_diagnostico()
    return path


# ---------------------------------------------------------------------------
# Utilidades de texto y de fechas
# ---------------------------------------------------------------------------

def normalizar(s):
    """minusculas y sin acentos, para comparar nombres de campos/etiquetas."""
    if s is None:
        return ""
    s = s.lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s


def normalizar_fecha_cli(texto):
    """Acepta AAAA-MM-DD o DD/MM/AAAA y devuelve AAAA-MM-DD (o None)."""
    if not texto:
        return None
    t = texto.strip()
    for formato in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return dt.datetime.strptime(t, formato).date().isoformat()
        except ValueError:
            continue
    raise ValueError(
        f"Fecha no valida: {texto!r}. Use AAAA-MM-DD (por ejemplo "
        f"2026-10-19) o DD/MM/AAAA.")


# ---------------------------------------------------------------------------
# Resolucion de los tipos de informacion
# ---------------------------------------------------------------------------

def resolver_tipos(solicitados, disponibles=None):
    """
    Traduce etiquetas (o trozos de ellas, sin distinguir mayusculas/acentos)
    a los nombres de campo reales. 'disponibles' es la lista (campo,
    etiqueta) descubierta en la pagina real; si no se da, se usa la lista
    TIPOS_OIR. Avisa si alguna etiqueta no se reconoce o es ambigua, y
    recorta a 3 porque es el maximo que admite la propia CNMV.
    """
    if not solicitados:
        return []
    if not disponibles:
        disponibles = [(campo_tipo(i), et) for i, et in enumerate(TIPOS_OIR)]
    etiqueta_de = dict(disponibles)
    orden = {nombre: i for i, (nombre, _) in enumerate(disponibles)}
    resueltos = []
    for pedido in solicitados:
        np = normalizar(pedido)
        coincidencias = [n for n, et in disponibles if np in normalizar(et)]
        if not coincidencias:
            log.warning("No reconozco el tipo '%s' (vea 'python cnmv_oir.py "
                        "tipos' para la lista exacta); se ignora.", pedido)
            continue
        if len(coincidencias) > 1:
            log.warning("'%s' coincide con varias categorias (%s); se "
                        "usan todas las que coinciden.", pedido,
                        ", ".join(etiqueta_de[n] for n in coincidencias))
        resueltos.extend(coincidencias)
    resueltos = sorted(set(resueltos), key=lambda n: orden[n])
    if len(resueltos) > 3:
        log.warning("La CNMV solo admite 3 tipos a la vez; se usan los "
                    "3 primeros de: %s",
                    ", ".join(etiqueta_de[n] for n in resueltos))
        resueltos = resueltos[:3]
    return resueltos


# ---------------------------------------------------------------------------
# Descubrimiento del formulario
#
# No se asumen nombres fijos de campo: se buscan por palabras clave sobre el
# nombre, el id y el valor de cada control. Los nombres reales ya se
# comprobaron contra el HTML de la pagina; si algo cambia, los volcados de
# diagnostico/ permiten ajustarlo rapido.
# ---------------------------------------------------------------------------

def obtener_formulario(session):
    """Devuelve (html, url_final): la CNMV redirige la direccion .aspx."""
    log.info("Descargando formulario de busqueda: %s", BASE_URL)
    resp = session.get(BASE_URL, headers={"User-Agent": USER_AGENT},
                       timeout=30)
    url_final = getattr(resp, "url", None) or BASE_URL
    log.debug("GET %s -> %s (%d bytes)", BASE_URL, resp.status_code,
              len(resp.content))
    if url_final != BASE_URL:
        log.info("La CNMV redirige el formulario a: %s", url_final)
    if resp.status_code != 200:
        dump_html(resp.text, "get_formulario_error")
        raise RuntimeError(
            f"La CNMV respondio {resp.status_code} al pedir el formulario")
    return resp.text, url_final


def _indice_final(nombre):
    m = re.search(r"\$(\d+)$", nombre)
    return int(m.group(1)) if m else None


def descubrir_campos(html):
    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form", id="aspnetForm") or soup.find("form")
    if form is None:
        dump_html(html, "sin_formulario")
        raise RuntimeError(
            "No se encontro ninguna etiqueta <form> en la pagina. Se ha "
            "guardado el HTML en diagnostico/ -- mandeselo a Claude."
        )

    etiquetas = {}
    for lab in soup.find_all("label"):
        destino = lab.get("for")
        if destino:
            etiquetas[destino] = lab.get_text(" ", strip=True)

    ocultos, textos, checkboxes, botones = {}, {}, {}, {}

    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        tipo = (inp.get("type") or "text").lower()
        valor = inp.get("value", "")
        ident = inp.get("id") or ""
        # Se incluye el VALOR en la clave de busqueda porque en controles
        # ASP.NET tipicos el texto visible ("Buscar") suele estar solo en
        # value=, no en name= ni en id= (confirmado con el HTML real).
        clave = normalizar(f"{name} {ident} {valor}")
        if tipo == "hidden":
            ocultos[name] = valor
        elif tipo in ("submit", "button", "image"):
            botones[name] = {"valor": valor or "Buscar", "norm": clave,
                             "tipo": tipo}
        elif tipo == "checkbox":
            checkboxes[name] = {"valor": valor or "on", "norm": clave,
                                "etiqueta": etiquetas.get(ident, "")}
        elif tipo in ("text", "date", "search", "number", ""):
            textos[name] = {"valor": valor, "norm": clave}

    # Casillas de "tipo": se reconocen por el nombre del grupo. La pagina
    # real trae ademas otras casillas (probablemente de cookies) que no
    # deben contarse como tipos ni enviarse.
    nombres_tipo = [n for n in checkboxes
                    if "lsttipoentidad2" in normalizar(n)]
    if not nombres_tipo and checkboxes:
        # Plan B por si CNMV renombra el control: el grupo mas numeroso de
        # casillas que comparten prefijo.
        grupos = {}
        for n in checkboxes:
            grupos.setdefault(n.rsplit("$", 1)[0], []).append(n)
        mayor = max(grupos.values(), key=len)
        if len(mayor) >= 5:
            nombres_tipo = mayor
    nombres_tipo.sort(key=lambda n: (_indice_final(n) is None,
                                     _indice_final(n) or 0, n))
    tipos_disponibles = []
    for n in nombres_tipo:
        etiqueta = checkboxes[n]["etiqueta"]
        if not etiqueta:
            i = _indice_final(n)
            if i is not None and 0 <= i < len(TIPOS_OIR):
                etiqueta = TIPOS_OIR[i]
        tipos_disponibles.append((n, etiqueta or n))
    conjunto_tipo = set(nombres_tipo)
    otras_casillas = [n for n in checkboxes if n not in conjunto_tipo]

    log.debug("Campos ocultos: %d | texto: %d | checkbox: %d (tipo: %d, "
              "otras: %d) | botones: %d", len(ocultos), len(textos),
              len(checkboxes), len(tipos_disponibles), len(otras_casillas),
              len(botones))
    log.debug("Ocultos: %s", ", ".join(sorted(ocultos)) or "(ninguno)")
    log.debug("Texto: %s", ", ".join(textos) or "(ninguno)")
    log.debug("Botones: %s", "; ".join(
        f"{n} [{v['tipo']}] valor={v['valor']!r}"
        for n, v in botones.items()) or "(ninguno)")
    if otras_casillas:
        log.debug("Casillas que no son de tipo (se ignoran): %s",
                  ", ".join(otras_casillas))

    def buscar_por_palabras(diccionario, *grupos_palabras):
        # Prioridad por GRUPO de palabras (no por posicion en la pagina):
        # primero se prueba el primer grupo en todos los controles, luego
        # el segundo, etc.
        for grupo in grupos_palabras:
            for nombre, info in diccionario.items():
                if all(p in info["norm"] for p in grupo):
                    return nombre
        return None

    campo_entidad = buscar_por_palabras(
        textos, ["entidad"], ["denominacion"], ["emisor"])
    campo_fecha_desde = buscar_por_palabras(
        textos, ["fecha", "desde"], ["fechadesde"])
    campo_fecha_hasta = buscar_por_palabras(
        textos, ["fecha", "hasta"], ["fechahasta"])
    campo_lei = buscar_por_palabras(textos, ["codigolei"], ["lei"])
    campo_ult_dias = buscar_por_palabras(
        textos, ["ult_dias"], ["ultdias"], ["ultimos", "dias"])

    # Se busca primero SOLO entre botones type=submit: un type=button normal
    # (como el buscador generico de cabecera de la web) suele actuar por
    # JavaScript y no sirve para un POST sencillo como el que hace este
    # programa. Si no se encuentra ninguno asi, como ultimo recurso se
    # prueba con cualquier boton.
    botones_submit = {n: v for n, v in botones.items()
                      if v.get("tipo") == "submit"}
    campo_buscar = buscar_por_palabras(
        botones_submit, ["buscar"], ["consultar"], ["submit"])
    if campo_buscar is None:
        campo_buscar = buscar_por_palabras(
            botones, ["buscar"], ["consultar"], ["submit"])
    log.debug("Boton elegido para buscar: %s", campo_buscar)

    faltantes = [n for n, v in (
        ("entidad", campo_entidad),
        ("fecha desde", campo_fecha_desde),
        ("fecha hasta", campo_fecha_hasta),
        ("boton buscar", campo_buscar),
    ) if v is None]
    if faltantes:
        dump_html(html, "campos_no_detectados")
        log.warning(
            "No se detectaron estos campos por nombre: %s. Se ha guardado "
            "el HTML en diagnostico/ para ajustar la deteccion.",
            ", ".join(faltantes),
        )

    if len(tipos_disponibles) != len(TIPOS_OIR):
        dump_html(html, "tipos_distintos_de_30")
        log.warning(
            "La pagina tiene %d casillas de 'tipo' y se esperaban %d; "
            "puede que la CNMV haya cambiado la lista. --tipo usara las "
            "etiquetas de la pagina real; revise con 'python cnmv_oir.py "
            "tipos --en-vivo'.", len(tipos_disponibles), len(TIPOS_OIR),
        )

    return {
        "ocultos": ocultos,
        "textos": textos,
        "checkboxes": checkboxes,
        "botones": botones,
        "tipos_disponibles": tipos_disponibles,
        "otras_casillas": otras_casillas,
        "action": form.get("action") or "",
        "campo_entidad": campo_entidad,
        "campo_fecha_desde": campo_fecha_desde,
        "campo_fecha_hasta": campo_fecha_hasta,
        "campo_lei": campo_lei,
        "campo_ult_dias": campo_ult_dias,
        "campo_buscar": campo_buscar,
    }


def buscar_oir(session, entidad=None, fecha_desde=None, fecha_hasta=None,
               tipos=None, lei=None, ultimos=None):
    """
    Rellena y envia el formulario como lo haria un navegador. Devuelve
    (html_de_la_respuesta, url_final).
    """
    html, url_form = obtener_formulario(session)
    dump_html(html, "formulario", nivel=logging.INFO)
    campos = descubrir_campos(html)

    if not campos["campo_buscar"]:
        raise RuntimeError(
            "No se pudo identificar el boton de busqueda del formulario; "
            "se ha guardado el HTML en diagnostico/ -- mandeselo a Claude.")

    # Igual que un navegador: ocultos, todos los campos de texto con su
    # valor por defecto, las casillas marcadas y el boton pulsado.
    datos = dict(campos["ocultos"])
    for nombre, info in campos["textos"].items():
        datos[nombre] = info["valor"]

    if entidad and campos["campo_entidad"]:
        datos[campos["campo_entidad"]] = entidad
    elif entidad:
        log.warning(
            "No se pudo identificar el campo de entidad; la busqueda se "
            "enviara sin filtrar por '%s'.", entidad)
    if lei and campos["campo_lei"]:
        datos[campos["campo_lei"]] = lei
    elif lei:
        log.warning("No se pudo identificar el campo del codigo LEI; la "
                    "busqueda se enviara sin filtrar por '%s'.", lei)
    if ultimos and campos["campo_ult_dias"]:
        datos[campos["campo_ult_dias"]] = str(int(ultimos))
    elif ultimos:
        log.warning("No se pudo identificar el campo de 'ultimos dias'; "
                    "la busqueda se enviara sin ese filtro.")

    # Los campos de fecha son type=date: el navegador los envia en ISO
    # (AAAA-MM-DD), que es tambien lo que se envia aqui.
    if fecha_desde and campos["campo_fecha_desde"]:
        datos[campos["campo_fecha_desde"]] = fecha_desde
    if fecha_hasta and campos["campo_fecha_hasta"]:
        datos[campos["campo_fecha_hasta"]] = fecha_hasta

    for campo in resolver_tipos(tipos, campos["tipos_disponibles"]):
        datos[campo] = campos["checkboxes"][campo]["valor"]

    datos[campos["campo_buscar"]] = campos["botones"][
        campos["campo_buscar"]]["valor"]

    # El formulario se envia a SU direccion de accion, resuelta contra la
    # direccion FINAL de la pagina (la CNMV redirige la .aspx): un POST que
    # atraviesa una redireccion 301 se convierte en GET y pierde los datos.
    url_post = urljoin(url_form, campos["action"] or url_form)
    partes = urlsplit(url_form)
    cabeceras = {
        "User-Agent": USER_AGENT,
        "Referer": url_form,
        "Origin": f"{partes.scheme}://{partes.netloc}",
    }

    log.info("Enviando busqueda: entidad=%r lei=%r desde=%r hasta=%r "
             "ultimos=%r tipos=%r", entidad, lei, fecha_desde, fecha_hasta,
             ultimos, tipos)
    log.debug("POST %s con %d campos", url_post, len(datos))
    resp = session.post(url_post, data=datos, headers=cabeceras, timeout=30)
    url_resp = getattr(resp, "url", None) or url_post
    log.debug("POST %s -> %s (%d bytes) url final: %s", url_post,
              resp.status_code, len(resp.content), url_resp)

    historial = [f"{r.status_code} {r.url}"
                 for r in getattr(resp, "history", [])]
    if historial:
        log.info("El envio paso por redirecciones: %s -> %s",
                 " | ".join(historial), url_resp)
        if urlsplit(url_resp).path == urlsplit(url_form).path:
            log.warning(
                "El servidor devolvio la misma pagina del formulario tras "
                "una redireccion: puede que los filtros no se hayan "
                "aplicado. Revise el volcado de diagnostico/.")

    if resp.status_code != 200:
        dump_html(resp.text, "post_busqueda_error")
        raise RuntimeError(f"La CNMV respondio {resp.status_code} al buscar")
    return resp.text, url_resp


# ---------------------------------------------------------------------------
# Extraccion de resultados
#
# Lo que SI se sabe de la web real (comprobado el 22-oct-2026):
#   - cada registro tiene su propia ficha:
#         .../Otra-Informacion-Relevante/Resultado-OIR?nreg=NUMERO
#     con la fecha, la hora, la entidad, el tipo, el titulo y el enlace al
#     documento (/webservices/verdocumento/ver?t={GUID}); en registros
#     antiguos el titulo enlaza antes a /aldia/detalleifialdia.aspx?nReg=...
#   - Resultado-OIR SIN numero (o con ?nif=...) NO es una lista: devuelve la
#     plantilla vacia con el menu de la web.
#   - el menu y el pie de TODAS las paginas traen enlaces fijos a PDF
#     (/DocPortal/...) y al buscador antiguo (hr/busquedahr.aspx): no son
#     resultados ni documentos y no se tocan nunca.
# Lo que todavia NO se ha visto: la lista que devuelve el formulario. Por eso
# solo se reconocen los tres tipos de enlace anteriores (ficha, documento y
# PDF suelto fuera de /DocPortal/) y, si no hay ninguno, se dice con claridad
# y se guarda el HTML, en vez de adivinar.
# ---------------------------------------------------------------------------

FECHA_RE = re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b")
HORA_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
META_FECHA_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?")
RE_FICHA = re.compile(r"resultado-oir[^\"'<>\s]*?[?&]nreg=(\d+)", re.I)
RE_VERDOC = re.compile(r"/webservices/verdocumento/ver|verdoc\.axd", re.I)
RE_ALDIA = re.compile(r"/aldia/detalleifialdia\.aspx", re.I)
RE_PDF = re.compile(r"\.pdf($|[?#])", re.I)
RE_ESTATICO = re.compile(r"/DocPortal/|busquedahr\.aspx|/images/", re.I)
PATRON_SALTAR = re.compile(r"^\s*(#|javascript:|mailto:|tel:)", re.I)
TITULOS_GENERICOS = {
    "ver", "detalle", "ver detalle", "mas informacion", "documento", "pdf",
    "descargar", "ver documento", "ficha", "ver ficha",
}


def url_ficha(nreg):
    """Direccion canonica de la ficha de un registro."""
    return f"{RESULT_URL}?nreg={int(nreg)}&lang=es"


def extraer_fecha(texto):
    m = FECHA_RE.search(texto)
    if not m:
        return None
    d, mo, y = m.groups()
    try:
        return dt.date(int(y), int(mo), int(d)).isoformat()
    except ValueError:
        return None


def _limpio(nodo):
    return re.sub(r"\s+", " ", nodo.get_text(" ", strip=True)).strip()


def clasificar_enlace(valor, url_base, es_onclick=False):
    """
    Devuelve la direccion normalizada si 'valor' apunta a la ficha de un
    registro, a un documento de la CNMV o a un PDF suelto; None en cualquier
    otro caso (menu, pie, PDF fijos de la web, JavaScript, anclas...).
    """
    if not valor:
        return None
    m = RE_FICHA.search(valor)
    if m:
        return url_ficha(m.group(1))
    if es_onclick or PATRON_SALTAR.match(valor):
        return None
    u = urljoin(url_base, valor.strip())
    if RE_ESTATICO.search(u):
        return None
    if RE_VERDOC.search(u) or RE_PDF.search(u):
        return u
    return None


def _registros_en(nodo, url_base):
    """(fichas, documentos): direcciones distintas que contiene un nodo."""
    fichas, docs = set(), set()
    for a in nodo.find_all("a", href=True):
        u = clasificar_enlace(a["href"], url_base)
        if not u:
            continue
        (fichas if RE_FICHA.search(u) else docs).add(u)
    return fichas, docs


def _fila_de(elem, url_base):
    """
    El contenedor mas pequeno (fila o tarjeta) que lleva una fecha y no
    abarca mas de UN registro (como mucho una ficha y un documento). Si no
    lo hay, el propio texto del enlace.
    """
    nodo = elem
    for _ in range(6):
        nodo = nodo.parent
        if nodo is None or nodo.name in ("body", "html", "form"):
            break
        texto = _limpio(nodo)
        if len(texto) > 800:
            break
        fichas, docs = _registros_en(nodo, url_base)
        if len(fichas) > 1 or len(docs) > 1:
            break
        if FECHA_RE.search(texto):
            return texto
    return _limpio(elem)


def extraer_resultados(html, url_base=None):
    url_base = url_base or RESULT_URL
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style"]):
        t.decompose()

    encontrados = []                       # (direccion, elemento)
    for a in soup.find_all("a", href=True):
        u = clasificar_enlace(a["href"], url_base)
        if u:
            encontrados.append((u, a))
    for el in soup.find_all(attrs={"onclick": True}):
        u = clasificar_enlace(el["onclick"], url_base, es_onclick=True)
        if u:
            encontrados.append((u, el))

    vistos, resultados = set(), []
    for u, el in encontrados:
        if u in vistos:
            continue
        vistos.add(u)
        contexto = _fila_de(el, url_base)
        titulo = _limpio(el) if el.name == "a" else ""
        if len(titulo) < 15 or normalizar(titulo) in TITULOS_GENERICOS:
            titulo = FECHA_RE.sub("", contexto).strip()[:160] or u
        resultados.append({
            "titulo": titulo,
            "pdf_url": u,
            "contexto_bruto": contexto,
            "fecha_notificacion": extraer_fecha(contexto),
        })

    # Si una misma fila lleva ficha Y documento basta la ficha: de ella se
    # llega al documento y su direccion no cambia.
    con_ficha = {r["contexto_bruto"] for r in resultados
                 if RE_FICHA.search(r["pdf_url"])}
    return [r for r in resultados
            if RE_FICHA.search(r["pdf_url"])
            or r["contexto_bruto"] not in con_ficha]


def describir_pagina(html):
    """Resumen corto de una pagina para el aviso 'no se reconocieron
    resultados': permite diagnosticar desde una captura de pantalla."""
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style"]):
        t.decompose()
    enlaces = soup.find_all("a", href=True)
    fichas = {m.group(1) for a in enlaces
              for m in [RE_FICHA.search(a["href"])] if m}
    docs = [a for a in enlaces if RE_VERDOC.search(a["href"])]
    texto = soup.get_text("\n", strip=True)
    pistas = []
    for linea in texto.splitlines():
        if re.search(r"resultado|encontr|registros|no (hay|existe)",
                     normalizar(linea)) and 8 < len(linea) < 160:
            pistas.append(linea)
        if len(pistas) >= 4:
            break
    return {
        "titulo": (soup.title.get_text(" ", strip=True)
                   if soup.title else "(sin titulo)"),
        "tablas": len(soup.find_all("table")),
        "filas": len(soup.find_all("tr")),
        "enlaces": len(enlaces),
        "fichas": len(fichas),
        "documentos": len(docs),
        "pistas": pistas,
    }


def parse_ficha(html, url=None):
    """
    Datos de la ficha de UN registro (Resultado-OIR?nreg=N). Los enlaces al
    documento son lo importante y se reconocen por su direccion; el resto
    (fecha, entidad, tipo, titulo) es 'mejor esfuerzo' y puede quedar a None
    sin que eso impida descargar el documento.
    """
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style"]):
        t.decompose()
    base = url or RESULT_URL
    ficha = {"nreg": None, "fecha": None, "hora": None, "entidad": None,
             "nif": None, "tipo": None, "titulo": None, "documentos": []}

    m = re.search(r"nreg=(\d+)", url or "", re.I)
    if m:
        ficha["nreg"] = m.group(1)

    ancla = soup.find("a", href=re.compile(r"datosentidad", re.I))
    if ancla is not None:
        ficha["entidad"] = _limpio(ancla) or None
        mn = re.search(r"nif=([A-Za-z0-9-]+)", ancla["href"], re.I)
        if mn:
            nif = mn.group(1).upper()
            if re.fullmatch(r"[A-Z]\d{8}", nif):
                nif = f"{nif[0]}-{nif[1:]}"
            ficha["nif"] = nif
    if not ficha["entidad"] and soup.title:
        mt = re.search(r"informaci[oó]n relevante\s*[-–]\s*(.+)$",
                       soup.title.get_text(" ", strip=True), re.I)
        if mt:
            ficha["entidad"] = mt.group(1).strip()

    # Zona de la ficha: el ancestro mas pequeno de la entidad que ya lleva una
    # fecha. Se descarta si es enorme (arrastraria el menu de la web, que
    # contiene la fecha 08/02/2020 del buscador antiguo).
    region = ""
    if ancla is not None:
        nodo = ancla
        for _ in range(6):
            nodo = nodo.parent
            if nodo is None or nodo.name in ("body", "html", "form"):
                break
            texto = _limpio(nodo)
            if len(texto) > 1500:
                break
            if FECHA_RE.search(texto):
                region = texto
                break
    if region:
        ficha["fecha"] = extraer_fecha(region)
        mh = HORA_RE.search(region)
        if mh:
            ficha["hora"] = f"{int(mh.group(1)):02d}:{mh.group(2)}"
        if not ficha["nreg"]:
            mr = re.search(r"n[uú]mero de registro\D{0,20}(\d{3,})", region,
                           re.I)
            if mr:
                ficha["nreg"] = mr.group(1)
        norm = normalizar(region)
        cand = [et for et in TIPOS_OIR if normalizar(et) in norm]
        if len(cand) > 1 and "Otra informacion relevante" in cand:
            cand.remove("Otra informacion relevante")
        if cand:
            ficha["tipo"] = max(cand, key=len)

    if not ficha["fecha"]:                 # plan B: metadatos de la pagina
        for meta in soup.find_all("meta"):
            clave = normalizar(" ".join(
                x for x in (meta.get("name"), meta.get("property"),
                            meta.get("itemprop")) if x))
            if not re.search(r"date|fecha|published|time", clave):
                continue
            mm = META_FECHA_RE.search(meta.get("content") or "")
            if mm:
                ficha["fecha"] = f"{mm.group(1)}-{mm.group(2)}-{mm.group(3)}"
                if mm.group(4) and not ficha["hora"]:
                    ficha["hora"] = f"{mm.group(4)}:{mm.group(5)}"
                break

    ficha["documentos"] = enlaces_a_documento(html, base)
    for a in soup.find_all("a", href=True):
        if urljoin(base, a["href"].strip()) in ficha["documentos"]:
            t = _limpio(a)
            if len(t) >= 15 and normalizar(t) not in TITULOS_GENERICOS:
                ficha["titulo"] = t
                break
    return ficha


def filtrar_por_fechas(resultados, desde_iso=None, hasta_iso=None):
    """Filtro en local (para la consulta por NIF, que no admite fechas)."""
    salida = []
    for r in resultados:
        f = r["fecha_notificacion"]
        if f is not None:
            if desde_iso and f < desde_iso:
                continue
            if hasta_iso and f > hasta_iso:
                continue
        salida.append(r)
    return salida


def hay_paginacion(html):
    """Aviso suave: ¿la pagina parece tener mas resultados en otra pagina?"""
    soup = BeautifulSoup(html, "html.parser")
    for el in soup.find_all(["a", "input", "button"]):
        texto = normalizar(el.get_text(" ", strip=True)
                           or el.get("value") or el.get("title") or "")
        if re.search(r"\b(siguiente|next|mas resultados|ver mas)\b", texto):
            return True
        if "page$" in (el.get("href") or "").lower():
            return True
    return False


# ---------------------------------------------------------------------------
# Base de datos local
# ---------------------------------------------------------------------------

def limpiar_registros_erroneos(conn):
    """
    La 1.2 guardo como resultado un enlace del menu de la web (buscador
    antiguo, hr/busquedahr.aspx) y descargo un PDF fijo de la web
    (/DocPortal/...). Se eliminan esos registros y sus archivos locales.
    """
    filas = conn.execute(
        "SELECT id, pdf_local FROM oir WHERE pdf_url LIKE '%busquedahr.aspx%' "
        "OR pdf_url LIKE '%/DocPortal/%'").fetchall()
    for id_, local in filas:
        if local:
            for p in (Path(local), Path(local).with_suffix(".txt")):
                try:
                    p.unlink()
                except OSError:
                    pass
        conn.execute("DELETE FROM oir WHERE id = ?", (id_,))
    if filas:
        conn.commit()
        log.info("Limpieza: eliminados %d registro(s) erroneo(s) de la "
                 "version 1.2.", len(filas))
    return len(filas)


def abrir_db(ruta=None):
    conn = sqlite3.connect(ruta or DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS oir (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entidad_buscada TEXT,
            titulo TEXT,
            pdf_url TEXT UNIQUE,
            fecha_notificacion TEXT,
            contexto_bruto TEXT,
            texto_extraido TEXT,
            pdf_local TEXT,
            fecha_guardado TEXT
        )
    """)
    conn.commit()
    limpiar_registros_erroneos(conn)
    return conn


def guardar_resultado(conn, entidad_buscada, resultado):
    try:
        conn.execute("""
            INSERT INTO oir (entidad_buscada, titulo, pdf_url,
                              fecha_notificacion, contexto_bruto,
                              fecha_guardado)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            entidad_buscada, resultado["titulo"], resultado["pdf_url"],
            resultado["fecha_notificacion"], resultado["contexto_bruto"],
            dt.datetime.now().isoformat(timespec="seconds"),
        ))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False  # ya existia (mismo pdf_url)


def actualizar_texto(conn, pdf_url, pdf_local, texto_extraido):
    conn.execute(
        "UPDATE oir SET pdf_local = ?, texto_extraido = ? WHERE pdf_url = ?",
        (pdf_local, texto_extraido, pdf_url),
    )
    conn.commit()


def listar_db(conn, entidad=None):
    if entidad:
        cur = conn.execute(
            "SELECT id, entidad_buscada, titulo, fecha_notificacion, pdf_url "
            "FROM oir WHERE entidad_buscada LIKE ? "
            "ORDER BY fecha_notificacion DESC", (f"%{entidad}%",))
    else:
        cur = conn.execute(
            "SELECT id, entidad_buscada, titulo, fecha_notificacion, pdf_url "
            "FROM oir ORDER BY fecha_notificacion DESC")
    return cur.fetchall()


def buscar_texto_db(conn, patron):
    cur = conn.execute(
        "SELECT id, entidad_buscada, titulo, fecha_notificacion, pdf_url "
        "FROM oir WHERE texto_extraido LIKE ? OR contexto_bruto LIKE ? "
        "OR titulo LIKE ?",
        (f"%{patron}%", f"%{patron}%", f"%{patron}%"),
    )
    return cur.fetchall()


# ---------------------------------------------------------------------------
# Descarga y conversion de PDF (reutiliza pdf2txt.py si esta disponible)
# ---------------------------------------------------------------------------

def nombre_base(entidad, fecha, url):
    """Nombre de archivo unico y legible: fecha_entidad_hash."""
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    ent = re.sub(r"[^a-z0-9]+", "_", normalizar(entidad or "cnmv")).strip("_")
    return f"{fecha or 'sin-fecha'}_{ent[:30] or 'cnmv'}_{h}"


def localizar_pdf2txt():
    candidato = os.environ.get(PDF2TXT_ENV)
    if candidato and Path(candidato).exists():
        return candidato
    local = HOME_DIR / "pdf2txt.py"
    if local.exists():
        return str(local)
    en_path = shutil.which("pdf2txt.py")
    if en_path:
        return en_path
    return None


# Estado de la conversion externa durante UNA ejecucion: None = sin probar,
# "url" / "archivo" = el modo que funciono, "ninguno" = no funciono ninguno
# (se deja de insistir con cada documento y se usa el conversor interno).
_MODO_P2T = {"modo": None}


def _probar_pdf2txt(ruta, modo, url_documento, ruta_pdf, base):
    """Ejecuta pdf2txt.py en el modo 'url' o 'archivo'. Devuelve el texto o None."""
    PDF_DIR.mkdir(exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="p2t_", dir=str(PDF_DIR)))
    texto = None
    try:
        argumento = url_documento if modo == "url" else str(ruta_pdf)
        log.info("Usando pdf2txt.py (%s), modo %s", ruta, modo)
        proc = subprocess.run(
            [sys.executable, ruta, argumento],
            check=True, capture_output=True, text=True, timeout=180,
            cwd=str(tmp),
        )
        encontrados = sorted(tmp.glob("*.txt"))
        if not encontrados and modo == "url":
            # por si pdf2txt.py escribe junto a si mismo o en el cwd actual
            stem = Path(url_documento.split("?")[0]).stem
            ahora = time.time()
            for p in (Path(ruta).parent / f"{stem}.txt",
                      Path.cwd() / f"{stem}.txt"):
                if p.exists() and ahora - p.stat().st_mtime < 180:
                    encontrados = [p]
                    break
        if encontrados:
            texto = encontrados[0].read_text(encoding="utf-8",
                                             errors="replace")
        elif modo == "archivo":
            texto = proc.stdout or ""      # pdfminer escribe el texto en pantalla
        if texto and texto.strip():
            (PDF_DIR / f"{base}.txt").write_text(texto, encoding="utf-8")
        else:
            texto = None
            log.warning("pdf2txt.py (modo %s) se ejecuto pero no dejo texto.",
                        modo)
    except subprocess.CalledProcessError as e:
        salida = ((e.stderr or "") + " " + (e.stdout or "")).strip()
        salida = re.sub(r"\s+", " ", salida)[-500:] or "(sin mensaje)"
        log.warning("pdf2txt.py (modo %s) fallo (codigo de salida %s). Su "
                    "mensaje: %s", modo, e.returncode, salida)
    except Exception as e:
        log.warning("pdf2txt.py (modo %s) fallo (%s).", modo, e)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return texto


def convertir_con_pdf2txt(url_documento, base, ruta_pdf=None):
    """
    Convierte con el pdf2txt.py que haya. Existen DOS programas con ese
    nombre y se prueban los dos modos:
      - "url": el propio de Jose, que recibe la direccion del PDF y deja un
        .txt;
      - "archivo": el estandar de pdfminer.six (se instala con pip y cae en
        usr/bin), que recibe un ARCHIVO local y escribe el texto en pantalla.
    La 1.2 solo conocia el primero: con el de pdfminer fallaba siempre. Si
    ninguno funciona devuelve None y se usa el conversor interno (pypdf).
    """
    ruta = localizar_pdf2txt()
    if not ruta:
        return None
    modos = []
    if _MODO_P2T["modo"] in (None, "url"):
        modos.append("url")
    if ruta_pdf and _MODO_P2T["modo"] in (None, "archivo"):
        modos.append("archivo")
    for modo in modos:
        texto = _probar_pdf2txt(ruta, modo, url_documento, ruta_pdf, base)
        if texto:
            _MODO_P2T["modo"] = modo
            return texto
    if modos and _MODO_P2T["modo"] is None:
        _MODO_P2T["modo"] = "ninguno"
        log.warning("Ningun modo de pdf2txt.py funciono; en esta ejecucion se "
                    "usara el conversor interno (pypdf).")
    return None


def convertir_interno(ruta_pdf):
    try:
        from pypdf import PdfReader
    except ImportError:
        log.error("Falta 'pypdf' para el conversor interno. "
                  "Instale con: pip install pypdf")
        return None
    try:
        lector = PdfReader(str(ruta_pdf))
        return "\n".join((p.extract_text() or "") for p in lector.pages)
    except Exception as e:
        log.error("No se pudo extraer texto de %s: %s", ruta_pdf, e)
        return None


def enlaces_a_documento(html, url_base):
    """
    Enlaces que pueden llevar al documento de un registro, por orden de
    preferencia: 1) /webservices/verdocumento/ver... (el documento mismo),
    2) PDF sueltos, 3) la pagina del dia /aldia/detalleifialdia.aspx (en
    registros antiguos el documento esta un paso mas alla). Se descartan SIEMPRE
    los PDF fijos del menu y del pie de la web (/DocPortal/...), que la 1.2
    confundio con el documento (descargo Conducta.pdf).
    """
    soup = BeautifulSoup(html, "html.parser")
    documentos, pdfs, del_dia = [], [], []
    for a in soup.find_all("a", href=True):
        h = a["href"]
        if PATRON_SALTAR.match(h):
            continue
        u = urljoin(url_base, h.strip())
        if RE_ESTATICO.search(u):
            continue
        if RE_VERDOC.search(u):
            lista = documentos
        elif RE_PDF.search(u):
            lista = pdfs
        elif RE_ALDIA.search(u):
            lista = del_dia
        else:
            continue
        if u not in lista:
            lista.append(u)
    return documentos + pdfs + del_dia


def descargar_documento(session, url, base, nivel=0):
    """
    Descarga 'url'. Si es un PDF, lo guarda y devuelve (ruta, url_real). Si es
    una pagina HTML (la ficha del registro o la pagina del dia), prueba, por
    orden, los enlaces a documento que contenga (hasta 3 y hasta 2 saltos).
    Devuelve (None, url) si no consigue un PDF.
    """
    PDF_DIR.mkdir(exist_ok=True)
    log.info("Descargando %s", url)
    resp = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=60)
    if resp.status_code != 200:
        log.warning("No se pudo descargar %s (codigo %s)", url,
                    resp.status_code)
        return None, url
    cabeceras = getattr(resp, "headers", None) or {}
    tipo = str(cabeceras.get("Content-Type", "")).lower()
    if resp.content[:5] == b"%PDF-" or "pdf" in tipo:
        destino = PDF_DIR / f"{base}.pdf"
        destino.write_bytes(resp.content)
        return destino, url
    if nivel < 2:
        url_pagina = getattr(resp, "url", url)
        for enlace in enlaces_a_documento(resp.text, url_pagina)[:3]:
            if enlace in (url, url_pagina):
                continue
            log.info("La direccion era una pagina, no un PDF; pruebo el "
                     "enlace al documento: %s", enlace)
            ruta, url_real = descargar_documento(session, enlace, base,
                                                 nivel=nivel + 1)
            if ruta:
                return ruta, url_real
    dump_html(resp.text, "documento_no_pdf")
    log.warning("El enlace %s no devolvio un PDF (tipo %r); se guardo el "
                "HTML en diagnostico/.", url, tipo)
    return None, url


def procesar_documento(session, url, convertir, base):
    destino, url_real = descargar_documento(session, url, base)
    texto = None
    if convertir and destino:
        texto = convertir_con_pdf2txt(url_real, base, ruta_pdf=destino)
        if texto is None:
            texto = convertir_interno(destino)
    return (str(destino) if destino else None), texto


# ---------------------------------------------------------------------------
# Comandos CLI
# ---------------------------------------------------------------------------

def cmd_buscar(args):
    if args.nif:
        print("La opcion --nif ya no existe (desde la version 1.3).\n"
              "La direccion Resultado-OIR?nif=... no es una lista: la CNMV "
              "usa Resultado-OIR?nreg=NUMERO para UN solo registro, y sin "
              "numero devuelve una pagina casi vacia con el menu de la web.\n"
              "Use --entidad con el nombre (p.ej. --entidad \"Merlin\") o "
              "--lei con el codigo LEI de la empresa.")
        sys.exit(2)
    try:
        desde = normalizar_fecha_cli(args.desde)
        hasta = normalizar_fecha_cli(args.hasta)
    except ValueError as e:
        print(e)
        sys.exit(2)
    if args.ultimos is not None and not 1 <= args.ultimos <= 99:
        print("--ultimos debe estar entre 1 y 99 (es el limite de la propia "
              "CNMV).")
        sys.exit(2)
    if not any([args.entidad, args.lei, desde, hasta, args.ultimos,
                args.tipo]):
        print("Indique al menos un filtro (--entidad, --lei, --desde, "
              "--hasta, --ultimos o --tipo): sin filtros se pediria todo el "
              "historico de la CNMV.")
        sys.exit(2)

    session = requests.Session()
    conn = abrir_db()
    try:
        html, url_resp = buscar_oir(
            session, entidad=args.entidad, fecha_desde=desde,
            fecha_hasta=hasta, tipos=args.tipo, lei=args.lei,
            ultimos=args.ultimos)
    except Exception as e:
        log.error("La busqueda fallo: %s: %s", type(e).__name__, e)
        registrar_traza()
        print(f"\nLa busqueda fallo: {type(e).__name__}: {e}\n"
              f"Revise {LOG_PATH} (lleva la traza completa) y la carpeta "
              f"diagnostico/ y envieme ambos para que lo corrija.")
        sys.exit(1)

    ruta_volcado = dump_html(html, "resultados", nivel=logging.INFO)
    resultados = extraer_resultados(html, url_resp)
    if hay_paginacion(html):
        log.warning("La pagina parece tener mas resultados en otra pagina; "
                    "esta version solo lee la primera.")

    if not resultados:
        d = describir_pagina(html)
        print("No se reconocieron resultados en la pagina que devolvio la "
              "CNMV.")
        print(f"  Titulo de la pagina: {d['titulo']}")
        print(f"  Tablas: {d['tablas']} | filas: {d['filas']} | enlaces: "
              f"{d['enlaces']} | fichas (nreg): {d['fichas']} | "
              f"documentos: {d['documentos']}")
        for p in d["pistas"]:
            print(f"  La pagina dice: {p}")
        print(f"Se ha guardado la pagina recibida en: {ruta_volcado}\n"
              "Mandemela (ese archivo y esta pantalla) para que ajuste el "
              "programa. Si la propia CNMV dice que no hay registros, no "
              "hace falta.")
        conn.close()
        return

    etiqueta = (args.entidad or args.lei
                or (f"ultimos{args.ultimos}dias" if args.ultimos else ""))
    nuevos = 0
    for r in resultados:
        if guardar_resultado(conn, etiqueta, r):
            nuevos += 1

    print(f"Encontrados: {len(resultados)} | nuevos guardados: {nuevos}")
    for r in resultados[:15]:
        print(f"  {r['fecha_notificacion'] or '(sin fecha)'}  "
              f"{r['titulo'][:70]}")
    if len(resultados) > 15:
        print(f"  ... y {len(resultados) - 15} mas (use 'listar').")

    if args.descargar:
        for r in resultados:
            base = nombre_base(etiqueta, r["fecha_notificacion"],
                               r["pdf_url"])
            ruta_local, texto = procesar_documento(
                session, r["pdf_url"], args.convertir, base)
            if ruta_local:
                actualizar_texto(conn, r["pdf_url"], ruta_local, texto or "")
                print(f"  descargado: {r['titulo'][:60]}")
            else:
                print(f"  NO se pudo descargar: {r['titulo'][:60]}")
            time.sleep(PAUSA_DESCARGAS_S)
    conn.close()


def cmd_ficha(args):
    """Consulta (y si se pide, descarga) UN registro por su numero 'nreg'."""
    nreg = str(args.nreg).strip()
    if not re.fullmatch(r"\d{1,9}", nreg):
        print("El numero de registro debe ser solo cifras, por ejemplo "
              "40753 (es el 'nreg' de la direccion Resultado-OIR?nreg=40753).")
        sys.exit(2)
    url = url_ficha(nreg)
    session = requests.Session()
    conn = abrir_db()
    try:
        log.info("Consultando la ficha %s", url)
        resp = session.get(url, headers={"User-Agent": USER_AGENT},
                           timeout=30)
        if resp.status_code != 200:
            dump_html(resp.text, "ficha_error")
            raise RuntimeError(
                f"La CNMV respondio {resp.status_code} a la ficha")
        url_resp = getattr(resp, "url", None) or url
        ruta_volcado = dump_html(resp.text, "ficha", nivel=logging.INFO)
        ficha = parse_ficha(resp.text, url_resp)
    except Exception as e:
        log.error("La consulta de la ficha fallo: %s: %s", type(e).__name__, e)
        registrar_traza()
        print(f"\nLa consulta fallo: {type(e).__name__}: {e}\n"
              f"Revise {LOG_PATH} y la carpeta diagnostico/.")
        sys.exit(1)

    nif = f"  (NIF {ficha['nif']})" if ficha["nif"] else ""
    print(f"Ficha {nreg}")
    print(f"  Entidad : {ficha['entidad'] or '(no reconocida)'}{nif}")
    print(f"  Fecha   : {ficha['fecha'] or '(no reconocida)'} "
          f"{ficha['hora'] or ''}".rstrip())
    print(f"  Tipo    : {ficha['tipo'] or '(no reconocido)'}")
    print(f"  Titulo  : {ficha['titulo'] or '(no reconocido)'}")
    if not ficha["documentos"]:
        print("La ficha no trae ningun enlace a un documento reconocible "
              "(puede ser un numero que no existe).\n"
              f"Se ha guardado la pagina en: {ruta_volcado}")
        conn.close()
        return
    for i, d in enumerate(ficha["documentos"][:3], start=1):
        print(f"  Enlace {i}: {d}")

    titulo = ficha["titulo"] or f"Registro {nreg}"
    guardar_resultado(conn, ficha["entidad"] or "", {
        "titulo": titulo,
        "pdf_url": url,
        "fecha_notificacion": ficha["fecha"],
        "contexto_bruto": " | ".join(
            x for x in (f"nreg={nreg}", ficha["entidad"], ficha["tipo"],
                        titulo) if x),
    })
    if args.descargar:
        base = nombre_base(ficha["entidad"] or "cnmv", ficha["fecha"], url)
        ruta_local, texto = procesar_documento(session, url, args.convertir,
                                               base)
        if ruta_local:
            actualizar_texto(conn, url, ruta_local, texto or "")
            print(f"  descargado: {ruta_local}")
            if args.convertir:
                print("  convertido a texto: "
                      + ("si" if texto else "NO (vea el log)"))
        else:
            print("  NO se pudo descargar el documento (vea el log y la "
                  "carpeta diagnostico/).")
    conn.close()


def cmd_listar(args):
    conn = abrir_db()
    filas = listar_db(conn, entidad=args.entidad)
    if not filas:
        print("Sin resultados guardados todavia.")
    for id_, entidad, titulo, fecha, url in filas:
        print(f"[{id_}] {fecha or '(sin fecha)'} -- {entidad} -- "
              f"{titulo[:70]}")
        print(f"      {url}")
    conn.close()


def cmd_texto(args):
    conn = abrir_db()
    filas = buscar_texto_db(conn, args.buscar)
    if not filas:
        print("Sin coincidencias.")
    for id_, entidad, titulo, fecha, url in filas:
        print(f"[{id_}] {fecha or '(sin fecha)'} -- {entidad} -- "
              f"{titulo[:70]}")
        print(f"      {url}")
    conn.close()


def cmd_tipos(args):
    if args.en_vivo:
        session = requests.Session()
        try:
            html, _ = obtener_formulario(session)
            campos = descubrir_campos(html)
        except Exception as e:
            log.error("No se pudo leer el formulario: %s: %s",
                      type(e).__name__, e)
            registrar_traza()
            print(f"\nNo se pudo leer el formulario: {type(e).__name__}: "
                  f"{e}\nRevise {LOG_PATH}.")
            sys.exit(1)
        print(f"Categorias de 'tipo' que ofrece AHORA la pagina de la CNMV "
              f"({len(campos['tipos_disponibles'])}):\n")
        for i, (_, etiqueta) in enumerate(campos["tipos_disponibles"]):
            print(f"  [{i:2d}] {etiqueta}")
        if campos["otras_casillas"]:
            print("\nOtras casillas del formulario que NO son de tipo "
                  "(se ignoran):")
            for n in campos["otras_casillas"]:
                print(f"  {n}")
        return
    print("Categorias de 'tipo de otra informacion relevante' (use el "
          "texto, o un trozo, con --tipo; hasta 3 a la vez):\n")
    for i, etiqueta in enumerate(TIPOS_OIR):
        print(f"  [{i:2d}] {etiqueta}")
    print("\n(Lista guardada en el programa. Para ver la que ofrece la "
          "pagina ahora mismo: tipos --en-vivo)")


def cmd_info_suscripcion(args):
    print("""
Alternativa sin programar nada, y mas robusta que este programa porque no
depende de la estructura de la pagina:

  1. Vaya a:
     https://www.cnmv.es/Portal/Otra-Informacion-Relevante/Consulta-OIR.aspx
  2. Pulse "SUSCRIBETE" e introduzca su correo.
  3. Elija las entidades (Ferrovial, ACS, Merlin, Grenergy, Solaria,
     Acciona...) o el tipo de informacion que le interese.
  4. Recibira un correo en tiempo real cada vez que se publique algo nuevo.
  5. Use su propia herramienta de correo (exportar_eml.py / leerCorreo.py)
     para buscar luego dentro de esos avisos.

Conviene usar esto en paralelo a este script, no como sustituto: la
suscripcion avisa de lo nuevo; este programa sirve para consultar el
historial y guardarlo todo en la misma base de datos local.
""")


def main():
    parser = argparse.ArgumentParser(
        description="Consulta 'Otra Informacion Relevante' de la CNMV.")
    parser.add_argument("--verbose", action="store_true",
                        help="Registro detallado (depuracion)")
    parser.add_argument("--version", action="version",
                        version=f"cnmv_oir.py {VERSION}")
    sub = parser.add_subparsers(dest="comando", required=True)

    p_buscar = sub.add_parser("buscar",
                              help="Buscar en la CNMV y guardar resultados")
    p_buscar.add_argument("--entidad",
                          help="Nombre del emisor (o parte), p.ej. Ferrovial")
    p_buscar.add_argument("--lei",
                          help="Codigo LEI del emisor (alternativa al nombre)")
    p_buscar.add_argument("--ultimos", type=int,
                          help="Registros de los ultimos N dias (1 a 99)")
    p_buscar.add_argument("--nif", help=argparse.SUPPRESS)
    p_buscar.add_argument("--desde", help="Fecha desde, AAAA-MM-DD o DD/MM/AAAA")
    p_buscar.add_argument("--hasta", help="Fecha hasta, AAAA-MM-DD o DD/MM/AAAA")
    p_buscar.add_argument("--descargar", action="store_true",
                          help="Descargar los PDF encontrados")
    p_buscar.add_argument("--convertir", action="store_true",
                          help="Convertir los PDF descargados a texto")
    p_buscar.add_argument("--tipo", action="append",
                          help="Tipo de informacion (vea 'tipos'); se "
                               "puede repetir hasta 3 veces")
    p_buscar.set_defaults(func=cmd_buscar)

    p_ficha = sub.add_parser(
        "ficha", help="Ver (y descargar) UN registro por su numero")
    p_ficha.add_argument("nreg", help="Numero de registro, p.ej. 40753")
    p_ficha.add_argument("--descargar", action="store_true",
                         help="Descargar el documento del registro")
    p_ficha.add_argument("--convertir", action="store_true",
                         help="Convertir el PDF descargado a texto")
    p_ficha.set_defaults(func=cmd_ficha)

    p_tipos = sub.add_parser("tipos",
                             help="Listar las categorias de tipo disponibles")
    p_tipos.add_argument("--en-vivo", action="store_true",
                         help="Leer las categorias directamente de la "
                              "pagina de la CNMV")
    p_tipos.set_defaults(func=cmd_tipos)

    p_listar = sub.add_parser("listar", help="Listar lo ya guardado en local")
    p_listar.add_argument("--entidad", help="Filtrar por entidad")
    p_listar.set_defaults(func=cmd_listar)

    p_texto = sub.add_parser("texto",
                             help="Buscar texto dentro de lo ya guardado")
    p_texto.add_argument("--buscar", required=True,
                         help="Palabra o frase a buscar")
    p_texto.set_defaults(func=cmd_texto)

    p_sub = sub.add_parser(
        "info-suscripcion",
        help="Explica la alternativa de suscripcion por correo de la CNMV")
    p_sub.set_defaults(func=cmd_info_suscripcion)

    args = parser.parse_args()
    setup_logging(args.verbose)
    args.func(args)


if __name__ == "__main__":
    main()
