#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cnmv_oir.py -- version 1.5 (7-nov-2026)
Herramienta Termux para consultar "Otra Informacion Relevante" (OIR) de la
CNMV: https://www.cnmv.es/Portal/Otra-Informacion-Relevante/Consulta-OIR.aspx

Que cambia en la 1.5 respecto a la 1.4 (a partir de las pruebas reales del
7-nov-2026 con Merlin, ACS, Ferrovial, Grenergy y Solaria):
  - Nuevo comando "vigilar": recorre su lista de empresas (fichero
    vigilar.txt, una por linea) con la busqueda por nombre de la CNMV y
    muestra SOLO lo nuevo desde la ultima vez.
  - Los titulos largos ya NO se cortan en pantalla: se parten en varias
    lineas. (El texto completo siempre estuvo guardado; solo la pantalla
    lo recortaba a 110 caracteres.)
  - Los volcados de diagnostico guardan ahora TAMBIEN las paginas 2, 3...
    de una busqueda (hasta 5 paginas), no solo la primera.
  - La busqueda por nombre y los dos formatos de fecha quedan confirmados
    en vivo; --filtro sigue siendo un recurso local de apoyo.

Que cambia en la 1.4 respecto a la 1.3 (a partir de los tres volcados HTML
reales del 22-oct-2026):
  - La LISTA de resultados de la CNMV ya se conoce y se lee tal como es: no
    es una tabla, es una serie de bloques con fecha, hora, entidad (con su
    NIF), tipo, titulo con enlace directo al documento, numero de registro
    y, a veces, documentos adjuntos. La 1.3 leia solo los enlaces y por eso
    una fila salia sin fecha y un adjunto aparecia como un registro mas.
  - PAGINACION: la CNMV muestra 10 registros por pagina (un dia puede tener
    4 paginas) y la 1.3 solo leia la primera. Ahora se leen todas (hasta
    --max-paginas, 30 por defecto).
  - La CNMV avisa "No se han encontrado resultados" cuando no hay registros
    y la 1.3 ocultaba ese mensaje. Ahora se muestra tal cual. La busqueda
    "Merlin desde 2026-10-01" del 22-oct NO fallo: la CNMV contesto asi.
  - Nuevo: --filtro TEXTO en "buscar": filtra en LOCAL por entidad, NIF,
    tipo o titulo, sin depender de como interprete la CNMV el nombre.
  - Nuevo: comando "sonda", que prueba la busqueda por nombre y fecha en
    varias formas y dice cual funciona.
  - La base de datos guarda ahora numero de registro, entidad, NIF, tipo y
    hora, y los adjuntos van aparte, ligados a su registro. Las filas de la
    1.3 se completan solas al volver a buscar.
  - "listar" muestra esos datos y limita la salida (--limite).
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
import textwrap
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

VERSION = "1.6"
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
PAUSA_PAGINAS_S = 0.4      # pausa entre paginas de resultados
MAX_PAGINAS_DEFECTO = 30   # paginas de 10 registros: 30 paginas = 300 registros
MAX_DESCARGAS_DEFECTO = 60 # documentos que se bajan como maximo por ejecucion

USER_AGENT = (
    f"Mozilla/5.0 (Linux; Android 10; Termux) cnmv_oir.py/{VERSION} "
    "(uso personal, consulta de informacion publica)"
)

log = logging.getLogger("cnmv_oir")

# Categorias de "tipo de otra informacion relevante", confirmadas contra el
# HTML real de la pagina (19-oct y 22-oct-2026). El campo real de cada una es
# ctl00$ContentPrincipal$wucTipoEntidad$lstTipoentidad2$<indice>. La propia
# CNMV permite marcar como maximo 3 a la vez; si no se marca ninguna, busca
# en todas.
TIPOS_OIR = [
    "Informes financieros anuales y de auditoría",
    "Informes financieros semestrales y de auditoría / revisiones limitadas",
    "Declaración Intermedia de Gestión",
    "Informe anual de gobierno corporativo",
    "Informe anual de remuneraciones de los consejeros",
    "Informe de pagos a las administraciones públicas",
    "Estado miembro de origen",
    "Total número de derechos de voto y de capital",
    "Cambios en los derechos vinculados a las clases de acciones u otros valores",
    "Comunicación de operaciones vinculadas",
    "Convocatoria de Junta o Asamblea",
    "Otra información relevante",
    "Sobre instrumentos financieros",
    "Ofertas públicas de suscripción y venta",
    "Ofertas públicas de adquisición de acciones",
    "Ofertas públicas de adquisición de otros valores",
    "Programas de recompra de acciones, estabilización y autocartera",
    "Contratos de liquidez y contrapartida",
    "Sobre negocio y situación financiera",
    "Sobre operaciones corporativas",
    "Sobre Gobierno corporativo",
    "Pactos parasociales",
    "Reglamentos del Consejo",
    "Reglamentos de la Junta",
    "Sobre fondos de titulización",
    "Sobre suspensiones, levantamientos y exclusiones de negociación",
    "Sobre calificaciones crediticias",
    "Acciones lealtad. Aprobación/cancelación régimen",
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
               tipos=None, lei=None, ultimos=None, volcar_formulario=True):
    """
    Rellena y envia el formulario como lo haria un navegador. Devuelve
    (html_de_la_respuesta, url_final).
    """
    html, url_form = obtener_formulario(session)
    if volcar_formulario:
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
# Lo que SE SABE de la web real (comprobado con los volcados del 22-oct-2026):
#
#   LISTA DE RESULTADOS (la devuelve el formulario)
#     No es una tabla. Es <ul id="listaElementosPrimernivel"> con un
#     <li class="blocks-single"> por registro, que contiene:
#         <li class="fecha-con-hora">22/10/2026</li>
#         <li class="time">19:33</li>
#         <a id="..._hlTituloCabecera" href="../consultas/datosentidad.aspx
#            ?nif=A-28092583"><span>TECNICAS REUNIDAS, S.A.</span></a>
#         <span id="..._descripcionSubtituloCabecera">tipo de OIR</span>
#         <a id="..._subtituloRegistroEnlace" href="https://www.cnmv.es/
#            webservices/verdocumento/ver?t=%7b{GUID}%7d">titulo</a>
#         <li>Numero de registro: 43275</li>
#         <a class="smallTag" href="...verdocumento...">adjunto</a>  (0..n)
#     Hay 10 registros por pagina. La paginacion es <ul class="pagination">
#     con enlaces ...resultado-oir.aspx?dias=1&page=N (N empieza en 0) y el
#     texto "Pagina 1 de 4". Los enlaces de la lista NO llevan nreg: el
#     numero de registro va como texto.
#   FICHA DE UN REGISTRO
#         .../Otra-Informacion-Relevante/Resultado-OIR?nreg=NUMERO
#     con la fecha, la hora, la entidad, el tipo, el titulo y el enlace al
#     documento; en registros antiguos el titulo enlaza antes a
#     /aldia/detalleifialdia.aspx?nReg=...
#   SIN RESULTADOS
#     La CNMV devuelve el formulario otra vez, con los datos escritos, y un
#     <p class="titcont" role="alert">No se han encontrado resultados</p>.
#   OTROS
#     - Resultado-OIR SIN numero (o con ?nif=...) NO es una lista: devuelve la
#       plantilla vacia con el menu de la web.
#     - el menu y el pie de TODAS las paginas traen enlaces fijos a PDF
#       (/DocPortal/...) y al buscador antiguo (hr/busquedahr.aspx): no son
#       resultados ni documentos y no se tocan nunca.
#     - el formulario admite como mucho 3 tipos a la vez.
# Si la CNMV cambia la estructura, extraer_filas_lista() no encontrara filas y
# se vuelve al reconocimiento por enlaces (extraer_resultados), que es menos
# preciso, y se guarda siempre un volcado en diagnostico/.
# ---------------------------------------------------------------------------

FECHA_RE = re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b")
HORA_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
META_FECHA_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?")
RE_NREG_TEXTO = re.compile(r"N[uú]mero de registro\s*:?\s*(\d{3,})", re.I)
RE_PAGINA_DE = re.compile(r"P[aá]gina\s+(\d+)\s+de\s+(\d+)", re.I)
RE_NIF_URL = re.compile(r"[?&]nif=([A-Za-z0-9-]+)", re.I)
RE_PARAM_PAGINA = re.compile(r"([?&](?:page|pagina|pag))=(\d+)", re.I)
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


CLAVES_REGISTRO = ("titulo", "pdf_url", "fecha_notificacion", "hora",
                   "entidad", "nif", "tipo", "nreg", "adjuntos",
                   "contexto_bruto")


def completar_registro(r):
    """Rellena con None las claves que falten (los registros reconocidos por
    enlaces, sin fila completa, no traen hora, entidad, NIF, etc.)."""
    for clave in CLAVES_REGISTRO:
        r.setdefault(clave, [] if clave == "adjuntos" else None)
    return r


def nif_limpio(texto):
    """NIF en mayusculas y sin guiones ni espacios ('A-28092583' ->
    'A28092583'). La CNMV lo escribe de las dos formas."""
    return re.sub(r"[^A-Za-z0-9]", "", texto or "").upper()


def _recortar_contenido(html):
    """
    Cada pagina de la CNMV pesa unos 250 KB y casi todo es menu. Si trae el
    bloque de contenido (<article ...>), se analiza solo ese trozo (unos 30
    KB): es varias veces mas rapido en el movil. Si no, se usa entera.
    """
    ini = re.search(r"<article\b", html, re.I)
    if ini:
        fin = html.rfind("</article>")
        if fin > ini.start():
            return html[ini.start():fin + len("</article>")]
    return html


def _registro_de_fila(fila, url_base):
    """Datos de un <li class="blocks-single"> de la lista (None si la fila
    no trae ningun documento ni numero de registro)."""
    texto_fila = _limpio(fila)

    li_fecha = fila.find("li", class_="fecha-con-hora")
    fecha = extraer_fecha(_limpio(li_fecha)) if li_fecha is not None else None
    if fecha is None:
        fecha = extraer_fecha(texto_fila)
    hora = None
    li_hora = fila.find("li", class_="time")
    if li_hora is not None:
        mh = HORA_RE.search(_limpio(li_hora))
        if mh:
            hora = f"{int(mh.group(1)):02d}:{mh.group(2)}"

    a_ent = fila.find("a", href=re.compile(r"datosentidad", re.I))
    entidad = nif = None
    if a_ent is not None:
        entidad = _limpio(a_ent) or None
        mn = RE_NIF_URL.search(a_ent["href"])
        if mn:
            nif = nif_limpio(mn.group(1)) or None

    tipo = None
    el_tipo = (fila.find("span", id=re.compile(r"descripcionSubtituloCabecera$"))
               or fila.find("span", class_="negrita"))
    if el_tipo is not None:
        tipo = _limpio(el_tipo) or None

    mr = RE_NREG_TEXTO.search(texto_fila)
    nreg = mr.group(1) if mr else None

    # Documentos: el enlace del titulo es el principal; los "smallTag" son
    # adjuntos del mismo registro (p.ej. "Notificacion que modifica al numero
    # de registro 43252..."), NO registros distintos.
    principal, adjuntos, vistos = None, [], set()
    for a in fila.find_all("a", href=True):
        if a is a_ent:
            continue
        u = clasificar_enlace(a["href"], url_base)
        if not u or u in vistos:
            continue
        vistos.add(u)
        titulo_a = _limpio(a)
        es_adjunto = "smalltag" in " ".join(a.get("class") or []).lower()
        if principal is None and not es_adjunto:
            principal = (u, titulo_a)
        else:
            adjuntos.append({"titulo": titulo_a or u, "pdf_url": u})
    if principal is None and adjuntos:
        primero = adjuntos.pop(0)
        principal = (primero["pdf_url"], primero["titulo"])
    if principal is None:
        if not nreg:
            return None
        principal = (url_ficha(nreg), "")
    url_doc, titulo = principal
    if not titulo or normalizar(titulo) in TITULOS_GENERICOS:
        titulo = tipo or (f"Registro {nreg}" if nreg else url_doc)

    partes = [f"{fecha or ''} {hora or ''}".strip(), entidad, nif, tipo,
              titulo, f"nreg={nreg}" if nreg else None]
    return {
        "titulo": titulo,
        "pdf_url": url_doc,
        "fecha_notificacion": fecha,
        "hora": hora,
        "entidad": entidad,
        "nif": nif,
        "tipo": tipo,
        "nreg": nreg,
        "adjuntos": adjuntos,
        "contexto_bruto": " | ".join(p for p in partes if p),
    }


def extraer_filas_lista(html, url_base=None):
    """Registros de la LISTA de resultados de la CNMV, leida fila a fila
    (vease el comentario de arriba). Devuelve [] si la pagina no es una
    lista o si la estructura ha cambiado."""
    url_base = url_base or RESULT_URL
    soup = BeautifulSoup(_recortar_contenido(html), "html.parser")
    contenedor = soup.find(id="listaElementosPrimernivel")
    filas = []
    if contenedor is not None:
        filas = contenedor.find_all("li", class_="blocks-single")
    if not filas:
        filas = soup.find_all("li", id=re.compile(r"elementoPrimerNivel$"))
    registros, vistos = [], set()
    for fila in filas:
        r = _registro_de_fila(fila, url_base)
        if r is None or r["pdf_url"] in vistos:
            continue
        vistos.add(r["pdf_url"])
        registros.append(r)
    return registros


def extraer_registros(html, url_base=None):
    """
    Registros de una pagina de resultados. Primero por filas (lo exacto);
    si la pagina no tiene esa estructura, por enlaces (extraer_resultados,
    menos preciso). Devuelve (lista, origen) con origen 'filas' o 'enlaces'.
    """
    filas = extraer_filas_lista(html, url_base)
    if filas:
        return filas, "filas"
    return [completar_registro(r)
            for r in extraer_resultados(html, url_base)], "enlaces"


def info_paginacion(html, url_base=None):
    """
    Paginacion de una lista: 'actual' y 'total' (del texto "Pagina 1 de 4"),
    'enlaces' {indice_de_pagina: direccion} (el indice empieza en 0, como el
    parametro page= de la CNMV) y 'plantilla' (una direccion de pagina, para
    construir las que el paginador no muestre).
    """
    soup = BeautifulSoup(_recortar_contenido(html), "html.parser")
    actual = total = None
    m = RE_PAGINA_DE.search(soup.get_text(" ", strip=True))
    if m:
        actual, total = int(m.group(1)), int(m.group(2))
    enlaces, plantilla = {}, None
    for ul in soup.find_all("ul", class_="pagination"):
        for a in ul.find_all("a", href=True):
            u = urljoin(url_base or RESULT_URL, a["href"].strip())
            mp = RE_PARAM_PAGINA.search(u)
            if mp:
                enlaces[int(mp.group(2))] = u
                plantilla = plantilla or u
    if total is None and enlaces:
        total = max(enlaces) + 1
    return {"actual": actual, "total": total, "enlaces": enlaces,
            "plantilla": plantilla}


def url_de_pagina(info, indice):
    """Direccion de la pagina 'indice' (desde 0). Se usa el enlace que da la
    propia CNMV; si el paginador solo muestra una ventana de paginas, se
    construye cambiando el numero en otro de sus enlaces."""
    if indice in info["enlaces"]:
        return info["enlaces"][indice]
    if info["plantilla"]:
        return RE_PARAM_PAGINA.sub(lambda m: f"{m.group(1)}={indice}",
                                   info["plantilla"], count=1)
    return None


VOLCAR_PAGINAS_HASTA = 5   # se guarda el HTML de las paginas 2..5 de una busqueda


def leer_paginas(session, html, url_resp, max_paginas=MAX_PAGINAS_DEFECTO,
                 pausa=PAUSA_PAGINAS_S, volcar=False):
    """
    Lee la primera pagina (ya descargada) y las siguientes hasta max_paginas.
    Devuelve (registros, resumen). 'resumen' lleva: total de paginas segun la
    CNMV, leidas, truncado (hay mas de las que se leyeron por el limite),
    aviso (texto si algo fallo a mitad) y origen ('filas' o 'enlaces').
    """
    registros, origen = extraer_registros(html, url_resp)
    info = info_paginacion(html, url_resp)
    total = info["total"] or 1
    resumen = {"total": total, "leidas": 1, "truncado": False,
               "aviso": None, "origen": origen}
    if not registros or total <= 1:
        return registros, resumen

    a_leer = min(total, max(1, int(max_paginas)))
    resumen["truncado"] = total > a_leer
    vistos = {r["pdf_url"] for r in registros}
    anterior = url_resp
    for i in range(1, a_leer):
        url_i = url_de_pagina(info, i)
        if not url_i:
            resumen["aviso"] = (f"No encuentro el enlace de la pagina "
                                f"{i + 1} en el paginador de la CNMV.")
            break
        time.sleep(pausa)
        try:
            resp = session.get(url_i, headers={"User-Agent": USER_AGENT,
                                               "Referer": anterior},
                               timeout=30)
        except Exception as e:
            registrar_traza()
            resumen["aviso"] = (f"Fallo al pedir la pagina {i + 1}: "
                                f"{type(e).__name__}: {e}")
            break
        if resp.status_code != 200:
            resumen["aviso"] = (f"La CNMV respondio {resp.status_code} a la "
                                f"pagina {i + 1}.")
            break
        url_real = getattr(resp, "url", None) or url_i
        nuevos, _ = extraer_registros(resp.text, url_real)
        if nuevos and volcar and i + 1 <= VOLCAR_PAGINAS_HASTA:
            dump_html(resp.text, f"resultados_p{i + 1}", nivel=logging.INFO)
        if not nuevos:
            dump_html(resp.text, "pagina_sin_filas")
            resumen["aviso"] = (f"La pagina {i + 1} no trajo ninguna fila "
                                f"(se guardo el HTML en diagnostico/).")
            break
        inedito = [r for r in nuevos if r["pdf_url"] not in vistos]
        if not inedito:
            resumen["aviso"] = (f"La pagina {i + 1} repite registros ya "
                                f"leidos; paro para no dar vueltas.")
            break
        registros.extend(inedito)
        vistos.update(r["pdf_url"] for r in inedito)
        resumen["leidas"] = i + 1
        anterior = url_real
        log.info("Pagina %d de %d leida (%d registros hasta ahora)",
                 i + 1, total, len(registros))
    return registros, resumen


def coincide_filtro(r, filtros):
    """
    ¿El registro coincide con ALGUNO de los textos de 'filtros'? Se busca,
    sin distinguir mayusculas ni acentos, en entidad, NIF, tipo, titulo,
    numero de registro y titulos de adjuntos. Un NIF o LEI se reconoce con o
    sin guiones. Sin filtros, todo coincide.
    """
    if not filtros:
        return True
    partes = [r.get("entidad"), r.get("nif"), r.get("tipo"), r.get("titulo"),
              r.get("nreg")]
    partes += [a.get("titulo") for a in (r.get("adjuntos") or [])]
    pajar = normalizar(" | ".join(str(p) for p in partes if p))
    pajar_compacto = re.sub(r"[^a-z0-9]", "", pajar)
    for f in filtros:
        nf = normalizar(f).strip()
        if not nf:
            continue
        if nf in pajar:
            return True
        compacto = re.sub(r"[^a-z0-9]", "", nf)
        if len(compacto) >= 8 and compacto in pajar_compacto:
            return True
    return False


def fuera_de_rango(registros, desde_iso=None, hasta_iso=None):
    """Registros cuya fecha cae fuera del rango pedido (aviso: la CNMV pudo
    interpretar las fechas de otra forma)."""
    salida = []
    for r in registros:
        f = r.get("fecha_notificacion")
        if f and ((desde_iso and f < desde_iso)
                  or (hasta_iso and f > hasta_iso)):
            salida.append(r)
    return salida


def mensajes_pagina(html):
    """Avisos que la propia CNMV escribe en la pagina (p.ej. 'No se han
    encontrado resultados', con role="alert") y mensajes de validacion que
    no esten vacios."""
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style"]):
        t.decompose()
    mensajes = []
    for el in soup.find_all(attrs={"role": "alert"}):
        t = _limpio(el)
        if t and t not in mensajes:
            mensajes.append(t)
    for el in soup.find_all(id=re.compile(r"(_val[A-Z]|Validat)")):
        t = _limpio(el)
        if t and t not in mensajes:
            mensajes.append(t)
    return mensajes[:4]


def subtitulo_busqueda(html):
    """Texto con el que la CNMV describe la busqueda en la lista (p.ej.
    '1 ultimos dias'): confirma que filtros aplico el servidor."""
    soup = BeautifulSoup(_recortar_contenido(html), "html.parser")
    el = (soup.find(id=re.compile(r"SubtituloDatos$"))
          or soup.find("p", class_="SubTitDatos"))
    return _limpio(el) if el is not None else None


def describir_pagina(html):
    """Resumen corto de una pagina que no se pudo interpretar: permite
    diagnosticar desde una captura de pantalla."""
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style"]):
        t.decompose()
    enlaces = soup.find_all("a", href=True)
    fichas = {m.group(1) for a in enlaces
              for m in [RE_FICHA.search(a["href"])] if m}
    docs = [a for a in enlaces if RE_VERDOC.search(a["href"])]
    h1 = soup.find("h1")
    es_formulario = (soup.find(id="frmBusqueda") is not None
                     or soup.find("input", id=re.compile(r"buttonOk$"))
                     is not None)
    return {
        "titulo": (soup.title.get_text(" ", strip=True)
                   if soup.title else "(sin titulo)"),
        "h1": _limpio(h1) if h1 is not None else None,
        "tablas": len(soup.find_all("table")),
        "filas": len(soup.find_all("tr")),
        "bloques": len(soup.find_all("li", class_="blocks-single")),
        "enlaces": len(enlaces),
        "fichas": len(fichas),
        "documentos": len(docs),
        "es_formulario": es_formulario,
        "mensajes": mensajes_pagina(html),
        "subtitulo": subtitulo_busqueda(html),
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
        if len(cand) > 1 and "Otra información relevante" in cand:
            cand.remove("Otra información relevante")
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


# ---------------------------------------------------------------------------
# Base de datos local
# ---------------------------------------------------------------------------

# Columnas anadidas en la 1.4 a la tabla de la 1.3 (se crean solas si faltan).
COLUMNAS_14 = (("nreg", "TEXT"), ("entidad", "TEXT"), ("nif", "TEXT"),
               ("tipo", "TEXT"), ("hora", "TEXT"), ("adjunto_de", "TEXT"))


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


def migrar_db(conn):
    """Anade a una base de datos de la 1.3 las columnas nuevas de la 1.4."""
    existentes = {fila[1] for fila in conn.execute("PRAGMA table_info(oir)")}
    anadidas = []
    for nombre, tipo in COLUMNAS_14:       # constantes del programa, no datos
        if nombre not in existentes:
            conn.execute(f"ALTER TABLE oir ADD COLUMN {nombre} {tipo}")
            anadidas.append(nombre)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_oir_nreg ON oir(nreg)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_oir_fecha "
                 "ON oir(fecha_notificacion)")
    conn.commit()
    if anadidas:
        log.info("Base de datos actualizada a la version 1.4 (columnas "
                 "nuevas: %s).", ", ".join(anadidas))
    return anadidas


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
            fecha_guardado TEXT,
            nreg TEXT,
            entidad TEXT,
            nif TEXT,
            tipo TEXT,
            hora TEXT,
            adjunto_de TEXT
        )
    """)
    conn.commit()
    migrar_db(conn)
    limpiar_registros_erroneos(conn)
    return conn


def guardar_resultado(conn, entidad_buscada, resultado, adjunto_de=None):
    """
    Guarda un registro (o un adjunto, si 'adjunto_de' lleva el numero del
    registro al que pertenece). Devuelve True si es nuevo. Si ya estaba (misma
    direccion de documento) devuelve False y COMPLETA sus datos: asi las filas
    que dejo la 1.3 ganan entidad, NIF, tipo, hora y numero de registro.
    """
    r = completar_registro(dict(resultado))
    ahora = dt.datetime.now().isoformat(timespec="seconds")
    try:
        conn.execute("""
            INSERT INTO oir (entidad_buscada, titulo, pdf_url,
                              fecha_notificacion, contexto_bruto,
                              fecha_guardado, nreg, entidad, nif, tipo, hora,
                              adjunto_de)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            entidad_buscada, r["titulo"], r["pdf_url"],
            r["fecha_notificacion"], r["contexto_bruto"], ahora, r["nreg"],
            r["entidad"], r["nif"], r["tipo"], r["hora"], adjunto_de,
        ))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        conn.execute("""
            UPDATE oir SET
                titulo = COALESCE(?, titulo),
                fecha_notificacion = COALESCE(?, fecha_notificacion),
                contexto_bruto = COALESCE(?, contexto_bruto),
                nreg = COALESCE(?, nreg),
                entidad = COALESCE(?, entidad),
                nif = COALESCE(?, nif),
                tipo = COALESCE(?, tipo),
                hora = COALESCE(?, hora),
                adjunto_de = COALESCE(?, adjunto_de)
            WHERE pdf_url = ?
        """, (r["titulo"], r["fecha_notificacion"], r["contexto_bruto"],
              r["nreg"], r["entidad"], r["nif"], r["tipo"], r["hora"],
              adjunto_de, r["pdf_url"]))
        conn.commit()
        return False


def actualizar_texto(conn, pdf_url, pdf_local, texto_extraido):
    conn.execute(
        "UPDATE oir SET pdf_local = ?, texto_extraido = ? WHERE pdf_url = ?",
        (pdf_local, texto_extraido, pdf_url),
    )
    conn.commit()


COLUMNAS_LISTADO = ("id, fecha_notificacion, hora, nreg, entidad, "
                    "entidad_buscada, tipo, titulo, adjunto_de, pdf_url")
ORDEN_LISTADO = "ORDER BY fecha_notificacion DESC, hora DESC, id DESC"


def listar_db(conn, entidad=None, limite=30, con_adjuntos=False):
    """Devuelve (filas, total). Los adjuntos solo salen si se piden."""
    donde, params = [], []
    if entidad:
        donde.append("(entidad LIKE ? OR entidad_buscada LIKE ? "
                     "OR nif LIKE ?)")
        params += [f"%{entidad}%"] * 3
    if not con_adjuntos:
        donde.append("adjunto_de IS NULL")
    clausula = ("WHERE " + " AND ".join(donde)) if donde else ""
    total = conn.execute(f"SELECT COUNT(*) FROM oir {clausula}",
                         params).fetchone()[0]
    sql = f"SELECT {COLUMNAS_LISTADO} FROM oir {clausula} {ORDEN_LISTADO}"
    if limite:
        sql += " LIMIT ?"
        params = params + [int(limite)]
    return conn.execute(sql, params).fetchall(), total


def buscar_texto_db(conn, patron):
    comodin = f"%{patron}%"
    cur = conn.execute(
        f"SELECT {COLUMNAS_LISTADO} FROM oir WHERE texto_extraido LIKE ? "
        "OR contexto_bruto LIKE ? OR titulo LIKE ? OR entidad LIKE ? "
        f"{ORDEN_LISTADO}",
        (comodin, comodin, comodin, comodin),
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
    Convierte con el pdf2txt.py que haya. Se prueban dos modos:
      - "url": el script propio de Jose (usr/bin/pdf2txt.py), que recibe la
        direccion del PDF y deja un .txt; es el que funciona en su movil;
      - "archivo": el estandar de pdfminer.six, que recibe un ARCHIVO local
        y escribe el texto en pantalla.
    Si ninguno funciona devuelve None y se usa el conversor interno (pypdf).
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

def _corto(texto, n):
    t = re.sub(r"\s+", " ", texto or "").strip()
    return t if len(t) <= n else t[:n - 3].rstrip() + "..."


def lineas_registro(fecha, hora, nreg, entidad, tipo, titulo, n_adjuntos=0,
                    prefijo=""):
    """Hasta cuatro lineas por registro, pensadas para la pantalla estrecha
    del movil: fecha, hora, numero y entidad / tipo / titulo / adjuntos."""
    cab = fecha or "(sin fecha)"
    if hora:
        cab += f" {hora}"
    if nreg:
        cab += f"  #{nreg}"
    cab += "  " + (_corto(entidad, 60) if entidad
                   else "(entidad no reconocida)")
    lineas = [prefijo + cab]
    if tipo:
        lineas.append(f"      [{_corto(tipo, 90)}]")
    if titulo:
        # El titulo se parte en varias lineas en vez de cortarse.
        t = re.sub(r"\s+", " ", titulo).strip()
        for tr in textwrap.wrap(t, width=74) or [""]:
            lineas.append("      " + tr)
    if n_adjuntos:
        lineas.append(f"      (+{n_adjuntos} adjunto"
                      + ("s)" if n_adjuntos != 1 else ")"))
    return lineas


def mostrar_registros(registros, maximo):
    """Imprime los registros (todos si maximo es 0)."""
    visibles = registros if not maximo else registros[:maximo]
    for r in visibles:
        for linea in lineas_registro(
                r["fecha_notificacion"], r.get("hora"), r.get("nreg"),
                r.get("entidad"), r.get("tipo"), r["titulo"],
                len(r.get("adjuntos") or []), prefijo="  "):
            print(linea)
    resto = len(registros) - len(visibles)
    if resto > 0:
        print(f"  ... y {resto} mas (use 'listar --limite 0' para verlos "
              "todos).")


MENSAJE_NIF_RETIRADO = (
    "La opcion --nif ya no existe (desde la version 1.3).\n"
    "La direccion Resultado-OIR?nif=... no es una lista: la CNMV usa "
    "Resultado-OIR?nreg=NUMERO para UN solo registro.\n"
    "Para una empresa concreta use --entidad con el nombre (p.ej. "
    "--entidad \"Merlin\") o, sin depender del nombre, "
    "--ultimos 7 --filtro A86977790 (el filtro reconoce el NIF).")


def explicar_sin_resultados(args, d, ruta_volcado):
    """Explica por que no hay registros, con lo que dijo la propia CNMV."""
    # Pocos dias atras y sin nombre en un dia sin publicaciones: es normal.
    try:
        n_ult = int(args.ultimos) if args.ultimos else None
    except (TypeError, ValueError):
        n_ult = None
    if n_ult and n_ult <= 3 and not args.entidad:
        hoy = dt.date.today()
        fechas = [hoy - dt.timedelta(days=i) for i in range(n_ult)]
        if all(motivo_no_laborable(f) for f in fechas):
            motivos = ", ".join(f"{f:%d/%m} {motivo_no_laborable(f)}"
                                for f in fechas)
            print(f"Sin registros en los ultimos {n_ult} dia(s): {motivos}.")
            print("La CNMV no suele publicar en esos dias. Es normal, no es "
                  "un fallo del programa.")
            print("Para ver actividad reciente use, por ejemplo:  "
                  "python3 cnmv_oir.py buscar --ultimos 7")
            return
    for m in d["mensajes"]:
        print(f'La CNMV respondio: "{m}"')
    if d["es_formulario"] and d["mensajes"]:
        print("Es decir: la CNMV recibio la busqueda y no tiene registros "
              "con esos filtros. No es un fallo del programa.")
    elif d["es_formulario"]:
        print("La CNMV devolvio otra vez el formulario, sin resultados y "
              "sin ningun mensaje.")
    else:
        print("No se reconocieron resultados en la pagina que devolvio la "
              "CNMV.")
        print(f"  Titulo de la pagina: {d['titulo']}"
              + (f" | {d['h1']}" if d["h1"] else ""))
        print(f"  Bloques de lista: {d['bloques']} | tablas: {d['tablas']} "
              f"| enlaces: {d['enlaces']} | fichas (nreg): {d['fichas']} | "
              f"documentos: {d['documentos']}")
        if d["subtitulo"]:
            print(f"  La pagina dice: {d['subtitulo']}")
    if args.entidad and (args.desde or args.hasta or args.ultimos
                         or args.tipo):
        print("\nPara saber si falla el nombre o los otros filtros:")
        print(f'  1) solo el nombre:  python3 cnmv_oir.py buscar --entidad '
              f'"{args.entidad}"')
        print(f'  2) la sonda:        python3 cnmv_oir.py sonda --entidad '
              f'"{args.entidad}"')
        print(f'  3) sin depender del nombre que entienda la CNMV (lee todo '
              f'lo publicado\n     en los ultimos dias y filtra aqui):\n'
              f'                      python3 cnmv_oir.py buscar --ultimos 7 '
              f'--filtro "{args.entidad}"')
    elif args.entidad:
        print("\nPara probar con otras formas del nombre o sin depender de "
              "el:")
        print(f'  python3 cnmv_oir.py sonda --entidad "{args.entidad}"')
        print(f'  python3 cnmv_oir.py buscar --ultimos 7 --filtro '
              f'"{args.entidad}"')
    if not d["mensajes"] and not d["es_formulario"]:
        print(f"Se ha guardado la pagina recibida en: {ruta_volcado}\n"
              "Mandemela (ese archivo y esta pantalla) para que ajuste el "
              "programa.")
    else:
        print(f"(Pagina recibida, por si hace falta: {ruta_volcado})")


def guardar_con_adjuntos(conn, etiqueta, r):
    """Guarda un registro y sus adjuntos. True si el registro era nuevo."""
    nuevo = bool(guardar_resultado(conn, etiqueta, r))
    padre = r.get("nreg") or r["pdf_url"]
    for adj in r.get("adjuntos") or []:
        guardar_resultado(conn, etiqueta, {
            "titulo": adj["titulo"],
            "pdf_url": adj["pdf_url"],
            "fecha_notificacion": r["fecha_notificacion"],
            "hora": r.get("hora"),
            "entidad": r.get("entidad"),
            "nif": r.get("nif"),
            "tipo": r.get("tipo"),
            "contexto_bruto": " | ".join(
                x for x in (f"adjunto de #{padre}", r.get("entidad"),
                            adj["titulo"]) if x),
        }, adjunto_de=padre)
    return nuevo


def cmd_buscar(args):
    if args.nif:
        print(MENSAJE_NIF_RETIRADO)
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
    if args.max_paginas < 1:
        print("--max-paginas debe ser 1 o mas.")
        sys.exit(2)
    filtros = [f.strip() for f in (args.filtro or []) if f and f.strip()]
    if not any([args.entidad, args.lei, desde, hasta, args.ultimos,
                args.tipo]):
        if filtros:
            print("--filtro filtra EN LOCAL lo que devuelve la CNMV, asi "
                  "que hay que decirle a la CNMV que periodo pedir: anada "
                  "--ultimos N (por ejemplo --ultimos 7) o --desde.")
        else:
            print("Indique al menos un filtro (--entidad, --lei, --desde, "
                  "--hasta, --ultimos o --tipo): sin filtros se pediria "
                  "todo el historico de la CNMV.")
        sys.exit(2)

    session = requests.Session()
    conn = abrir_db()
    try:
        html, url_resp = buscar_oir(
            session, entidad=args.entidad, fecha_desde=desde,
            fecha_hasta=hasta, tipos=args.tipo, lei=args.lei,
            ultimos=args.ultimos)
        ruta_volcado = dump_html(html, "resultados", nivel=logging.INFO)
        registros, resumen = leer_paginas(
            session, html, url_resp, max_paginas=args.max_paginas,
            volcar=True)
    except Exception as e:
        log.error("La busqueda fallo: %s: %s", type(e).__name__, e)
        registrar_traza()
        print(f"\nLa busqueda fallo: {type(e).__name__}: {e}\n"
              f"Revise {LOG_PATH} (lleva la traza completa) y la carpeta "
              f"diagnostico/ y envieme ambos para que lo corrija.")
        sys.exit(1)

    if not registros:
        explicar_sin_resultados(args, describir_pagina(html), ruta_volcado)
        conn.close()
        return

    subtitulo = subtitulo_busqueda(html)
    if subtitulo:
        print(f"La CNMV describe la busqueda asi: {subtitulo}")
    print(f"Leidas {resumen['leidas']} de {resumen['total']} paginas "
          f"({len(registros)} registros).")
    if resumen["origen"] == "enlaces":
        print("AVISO: la pagina no tiene la estructura de lista conocida; "
              "los registros se han reconocido solo por sus enlaces, sin "
              "entidad ni hora. Mande el volcado de diagnostico/.")
    if resumen["truncado"]:
        print(f"AVISO: la CNMV tiene {resumen['total']} paginas y solo se "
              f"leyeron {resumen['leidas']} (limite --max-paginas "
              f"{args.max_paginas}). Para leerlas todas: --max-paginas "
              f"{resumen['total']}.")
    if resumen["aviso"]:
        print(f"AVISO: {resumen['aviso']}")
    fuera = fuera_de_rango(registros, desde, hasta) if (desde or hasta) \
        else []
    if fuera:
        print(f"AVISO: {len(fuera)} registro(s) caen fuera del rango pedido "
              f"({desde or '...'} a {hasta or '...'}); puede que la CNMV "
              "interprete las fechas de otro modo (pruebe 'sonda').")

    leidos = len(registros)
    if filtros:
        registros = [r for r in registros if coincide_filtro(r, filtros)]
        etiqueta_filtro = ", ".join(f'"{f}"' for f in filtros)
        print(f"Coinciden con el filtro {etiqueta_filtro}: "
              f"{len(registros)} de {leidos}.")
        if not registros:
            print("Ninguno coincide, asi que no se guarda nada. Pruebe con "
                  "otro trozo del nombre, con el NIF (p.ej. A86977790) o "
                  "con mas dias (--ultimos).")
            conn.close()
            return

    etiqueta = (args.entidad or args.lei
                or (f"ultimos{args.ultimos}dias" if args.ultimos else "")
                or ("filtro:" + "+".join(filtros) if filtros else ""))
    nuevos = 0
    for r in registros:
        if guardar_con_adjuntos(conn, etiqueta, r):
            nuevos += 1

    print(f"Registros: {len(registros)} | nuevos guardados: {nuevos} | "
          f"ya estaban: {len(registros) - nuevos}")
    mostrar_registros(registros, args.mostrar)

    if args.descargar:
        docs = []
        for r in registros:
            ent = r.get("entidad") or etiqueta
            docs.append((r["pdf_url"], r["titulo"], r["fecha_notificacion"],
                         ent))
            for adj in r.get("adjuntos") or []:
                docs.append((adj["pdf_url"], adj["titulo"],
                             r["fecha_notificacion"], ent))
        if len(docs) > args.max_descargas:
            print(f"Hay {len(docs)} documentos; se descargan solo los "
                  f"{args.max_descargas} primeros (cambie el limite con "
                  "--max-descargas N, o acote con --filtro).")
            docs = docs[:args.max_descargas]
        for i, (url, titulo, fecha, ent) in enumerate(docs, start=1):
            base = nombre_base(ent, fecha, url)
            ruta_local, texto = procesar_documento(
                session, url, args.convertir, base)
            if ruta_local:
                actualizar_texto(conn, url, ruta_local, texto or "")
                print(f"  [{i}/{len(docs)}] descargado: {_corto(titulo, 60)}")
            else:
                print(f"  [{i}/{len(docs)}] NO se pudo descargar: "
                      f"{_corto(titulo, 60)}")
            time.sleep(PAUSA_DESCARGAS_S)
    conn.close()


VIGILAR_ARCHIVO = HOME_DIR / "vigilar.txt"
VIGILAR_DEFECTO = ["ACS", "Ferrovial", "Merlin", "Grenergy", "Solaria"]


def leer_lista_vigilar():
    """Lee vigilar.txt (una empresa por linea; # = comentario). Si no
    existe lo crea con la lista por defecto."""
    if not VIGILAR_ARCHIVO.exists():
        cuerpo = ("# Empresas a vigilar con 'cnmv_oir.py vigilar'.\n"
                  "# Una por linea (nombre o parte del nombre). Las lineas "
                  "con # se ignoran.\n" + "\n".join(VIGILAR_DEFECTO) + "\n")
        VIGILAR_ARCHIVO.write_text(cuerpo, encoding="utf-8")
        print(f"He creado {VIGILAR_ARCHIVO.name} con la lista inicial; "
              "editelo para cambiarla.")
    lista = []
    for linea in VIGILAR_ARCHIVO.read_text(encoding="utf-8").splitlines():
        linea = linea.split("#", 1)[0].strip()
        if linea:
            lista.append(linea)
    return lista


def cmd_vigilar(args):
    """Busca cada empresa por nombre en la CNMV y muestra lo NUEVO."""
    if not 1 <= args.ultimos <= 99:
        print("--ultimos debe estar entre 1 y 99.")
        sys.exit(2)
    empresas = args.empresa or leer_lista_vigilar()
    if not empresas:
        print(f"{VIGILAR_ARCHIVO.name} esta vacio: anada empresas, una por "
              "linea.")
        sys.exit(2)
    session = requests.Session()
    conn = abrir_db()
    total_nuevos = 0
    fallos = 0
    print(f"Vigilando {len(empresas)} empresa(s), ultimos {args.ultimos} "
          "dias.\n")
    for k, nombre in enumerate(empresas):
        if k:
            time.sleep(1.0)
        try:
            html, url_resp = buscar_oir(session, entidad=nombre,
                                        ultimos=args.ultimos,
                                        volcar_formulario=False)
            registros, resumen = leer_paginas(
                session, html, url_resp, max_paginas=args.max_paginas)
        except Exception as e:
            fallos += 1
            log.error("vigilar %s fallo: %s: %s", nombre,
                      type(e).__name__, e)
            registrar_traza()
            print(f"{nombre}: ERROR ({type(e).__name__}: {e}); sigo con la "
                  "siguiente.\n")
            continue
        if not registros:
            dump_html(html, "vigilar_sin_resultados", nivel=logging.INFO)
            print(f"{nombre}: nada en los ultimos {args.ultimos} dias.\n")
            continue
        nuevos = [r for r in registros
                  if guardar_con_adjuntos(conn, f"vigilar:{nombre}", r)]
        aviso = ""
        if resumen["truncado"] or resumen["aviso"]:
            aviso = "  (AVISO: lectura incompleta; use buscar --entidad)"
        print(f"{nombre}: {len(registros)} registros, {len(nuevos)} "
              f"nuevos.{aviso}")
        mostrar = registros if args.todo else nuevos
        for r in mostrar:
            for linea in lineas_registro(
                    r["fecha_notificacion"], r.get("hora"), r.get("nreg"),
                    r.get("entidad"), r.get("tipo"), r["titulo"],
                    len(r.get("adjuntos") or []), prefijo="  "):
                print(linea)
        print()
        total_nuevos += len(nuevos)
    conn.close()
    print(f"Resumen: {total_nuevos} registro(s) nuevo(s) en total"
          + (f"; {fallos} empresa(s) con error." if fallos else "."))


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
        "hora": ficha["hora"],
        "entidad": ficha["entidad"],
        "nif": nif_limpio(ficha["nif"]) or None,
        "tipo": ficha["tipo"],
        "nreg": nreg,
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


def _imprimir_fila_db(fila):
    (id_, fecha, hora, nreg, entidad, buscada, tipo, titulo, adjunto_de,
     url) = fila
    nombre = entidad or (f"({buscada})" if buscada else None)
    for linea in lineas_registro(fecha, hora, nreg, nombre, tipo, titulo,
                                 prefijo=f"[{id_}] "):
        print(linea)
    if adjunto_de:
        print(f"      (adjunto del registro {adjunto_de})")
    print(f"      {url}")


def cmd_listar(args):
    conn = abrir_db()
    filas, total = listar_db(conn, entidad=args.entidad, limite=args.limite,
                             con_adjuntos=args.adjuntos)
    if not filas:
        print("Sin resultados guardados todavia.")
        conn.close()
        return
    for fila in filas:
        _imprimir_fila_db(fila)
    if total > len(filas):
        print(f"\nMostrando {len(filas)} de {total}. Use --limite 0 para "
              "verlos todos.")
    conn.close()


def cmd_texto(args):
    conn = abrir_db()
    filas = buscar_texto_db(conn, args.buscar)
    if not filas:
        print("Sin coincidencias.")
    for fila in filas:
        _imprimir_fila_db(fila)
    conn.close()


def _resumen_sonda(session, clave, **filtros):
    """Una busqueda de la sonda: devuelve lo que contesto la CNMV."""
    try:
        html, url_resp = buscar_oir(session, volcar_formulario=False,
                                    **filtros)
    except Exception as e:
        registrar_traza()
        return {"error": f"{type(e).__name__}: {e}", "filas": 0,
                "paginas": 0, "mensaje": None, "url": None,
                "subtitulo": None}
    dump_html(html, f"sonda_{clave}", nivel=logging.DEBUG)
    registros, _ = extraer_registros(html, url_resp)
    info = info_paginacion(html, url_resp)
    mensajes = mensajes_pagina(html)
    return {"error": None, "filas": len(registros),
            "paginas": info["total"] or (1 if registros else 0),
            "mensaje": mensajes[0] if mensajes else None, "url": url_resp,
            "subtitulo": subtitulo_busqueda(html)}


def _pascua(anio):
    """Domingo de Pascua (algoritmo gregoriano anonimo)."""
    a, b, c = anio % 19, anio // 100, anio % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mes = (h + l - 7 * m + 114) // 31
    dia = (h + l - 7 * m + 114) % 31 + 1
    return dt.date(anio, mes, dia)


def motivo_no_laborable(dia):
    """
    Devuelve el motivo por el que la CNMV (Madrid) normalmente no publica ese
    dia, o None si es laborable. Cubre fines de semana, festivos nacionales
    fijos, Jueves y Viernes Santo y los festivos de Madrid ciudad habituales.
    Es una aproximacion: el programa la contrasta con los datos reales.
    """
    if dia.weekday() == 5:
        return "sabado"
    if dia.weekday() == 6:
        return "domingo"
    fijos = {(1, 1): "Ano Nuevo", (1, 6): "Reyes", (5, 1): "Fiesta del Trabajo",
             (8, 15): "Asuncion", (10, 12): "Fiesta Nacional",
             (11, 1): "Todos los Santos", (12, 6): "Dia de la Constitucion",
             (12, 8): "Inmaculada", (12, 25): "Navidad",
             (5, 2): "Dia de la Comunidad de Madrid",
             (5, 15): "San Isidro (Madrid)", (11, 9): "La Almudena (Madrid)"}
    if (dia.month, dia.day) in fijos:
        return fijos[(dia.month, dia.day)]
    pascua = _pascua(dia.year)
    if dia == pascua - dt.timedelta(days=3):
        return "Jueves Santo"
    if dia == pascua - dt.timedelta(days=2):
        return "Viernes Santo"
    if dia == pascua + dt.timedelta(days=1):
        return "Lunes de Pascua (algunas comunidades)"
    # Un festivo que cae en domingo suele trasladarse al lunes
    ayer = dia - dt.timedelta(days=1)
    if ayer.weekday() == 6 and (ayer.month, ayer.day) in fijos:
        return f"traslado de {fijos[(ayer.month, ayer.day)]}"
    return None


def cmd_sonda(args):
    """
    Prueba la busqueda por nombre y fechas de varias formas y dice cual
    funciona. Hace 5 busquedas (con una pausa entre ellas); no guarda nada en
    la base de datos.
    """
    nombre = (args.entidad or "Merlin").strip()
    hoy = dt.date.today()
    hace_un_ano = hoy - dt.timedelta(days=365)
    pruebas = [
        ("E", "control: ultimos 1 dia, sin nombre", {"ultimos": 1}),
        ("A", "solo el nombre", {"entidad": nombre}),
        ("B", "nombre + desde hace un ano, fecha AAAA-MM-DD",
         {"entidad": nombre, "fecha_desde": hace_un_ano.isoformat()}),
        ("C", "nombre + desde hace un ano, fecha DD/MM/AAAA",
         {"entidad": nombre,
          "fecha_desde": hace_un_ano.strftime("%d/%m/%Y")}),
        ("D", "nombre + ultimos 30 dias",
         {"entidad": nombre, "ultimos": 30}),
    ]
    session = requests.Session()
    print(f'Sonda de la busqueda de la CNMV con el nombre "{nombre}" '
          f"(5 busquedas)\n")
    res = {}
    for i, (clave, descripcion, filtros) in enumerate(pruebas):
        if i:
            time.sleep(1.0)
        res[clave] = r = _resumen_sonda(session, clave, **filtros)
        enviados = " ".join(f"{k}={v}" for k, v in filtros.items())
        print(f"[{clave}] {descripcion}")
        print(f"    filtros   : {enviados}")
        if r["error"]:
            print(f"    ERROR     : {r['error']}")
        else:
            print(f"    resultado : {r['filas']} registros en la pagina 1; "
                  f"{r['paginas']} paginas")
            print(f"    la CNMV   : {r['mensaje'] or r['subtitulo'] or '-'}")
            print(f"    redirige a: {r['url']}")
        print()

    E, A, B, C, D = (res[k] for k in "EABCD")
    print("Lectura:")
    control_ok = True
    if E["filas"] == 0:
        motivo = motivo_no_laborable(hoy)
        if motivo:
            print(f"  - Control sin registros: hoy es {motivo}, dia en que la "
                  "CNMV no suele publicar. Es normal, no es un fallo.")
        else:
            # Dia laborable sin registros: se contrasta con los ultimos 7
            # dias para distinguir un festivo no previsto de un fallo real.
            time.sleep(1.0)
            E7 = _resumen_sonda(session, "E7", ultimos=7)
            if E7["error"] is None and E7["filas"] > 0:
                print("  - Control del dia sin registros pero los ultimos 7 "
                      f"dias si tienen {E7['filas']} (pagina 1): hoy parece "
                      "un dia sin publicaciones (festivo no previsto o aun "
                      "no hay avisos). Es normal.")
            else:
                control_ok = False
                print("  - La prueba de control (ultimos 1 dia y ultimos 7 "
                      "dias, sin nombre) no devolvio registros: la CNMV no "
                      "responde como se esperaba. Mande esta pantalla y el "
                      "log.")
    if not control_ok:
        pass
    elif A["filas"] == 0:
        print(f'  - El nombre "{nombre}" solo, sin fechas, no devuelve '
              "nada: la CNMV no lo reconoce tal como esta escrito. Pruebe "
              "con otro trozo del nombre o con el completo.")
    else:
        print("  - El filtro por nombre funciona.")
        if B["filas"] and C["filas"]:
            print("  - Las fechas funcionan en los dos formatos.")
        elif B["filas"]:
            print("  - Las fechas funcionan en AAAA-MM-DD (el que usa el "
                  "programa), no en DD/MM/AAAA.")
        elif C["filas"]:
            print("  - Las fechas solo funcionan en DD/MM/AAAA: hay que "
                  "cambiar el formato en el programa. Mande esta pantalla.")
        else:
            print("  - Con fechas no sale nada aunque el nombre si "
                  "funciona (y el periodo es de un ano): la CNMV no esta "
                  "entendiendo el campo de fechas en ninguno de los dos "
                  "formatos. Mande esta pantalla.")
        if D["filas"]:
            print("  - Nombre + ultimos N dias funciona.")
        else:
            print("  - Nombre + ultimos 30 dias no devuelve nada: puede que "
                  "no se puedan combinar o que no haya registros en esos "
                  "dias.")
    print("\n(Cada respuesta quedo guardada en diagnostico/, en archivos "
          "respuesta_sonda_*.html.)")


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


def main(argv=None):
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
    p_buscar.add_argument("--filtro", action="append",
                          help="Texto que debe aparecer en la entidad, el "
                               "NIF, el tipo o el titulo; se filtra AQUI, no "
                               "en la CNMV. Se puede repetir (basta que "
                               "coincida uno). Combinelo con --ultimos o "
                               "--desde")
    p_buscar.add_argument("--max-paginas", type=int,
                          default=MAX_PAGINAS_DEFECTO,
                          help="Paginas de resultados que se leen como "
                               f"maximo (10 registros por pagina; por "
                               f"defecto {MAX_PAGINAS_DEFECTO})")
    p_buscar.add_argument("--mostrar", type=int, default=20,
                          help="Registros que se muestran en pantalla (0 = "
                               "todos; por defecto 20)")
    p_buscar.add_argument("--descargar", action="store_true",
                          help="Descargar los PDF encontrados")
    p_buscar.add_argument("--max-descargas", type=int,
                          default=MAX_DESCARGAS_DEFECTO,
                          help="Documentos que se descargan como maximo por "
                               f"ejecucion (por defecto "
                               f"{MAX_DESCARGAS_DEFECTO})")
    p_buscar.add_argument("--convertir", action="store_true",
                          help="Convertir los PDF descargados a texto")
    p_buscar.add_argument("--tipo", action="append",
                          help="Tipo de informacion (vea 'tipos'); se "
                               "puede repetir hasta 3 veces")
    p_buscar.set_defaults(func=cmd_buscar)

    p_sonda = sub.add_parser(
        "sonda",
        help="Probar la busqueda por nombre y fechas de varias formas")
    p_sonda.add_argument("--entidad", default="Merlin",
                         help="Nombre con el que probar (por defecto "
                              "Merlin)")
    p_sonda.set_defaults(func=cmd_sonda)

    p_vig = sub.add_parser(
        "vigilar",
        help="Recorrer su lista de empresas (vigilar.txt) y mostrar lo nuevo")
    p_vig.add_argument("--empresa", action="append",
                       help="Vigilar solo esta (se puede repetir); sin ella "
                            "se usa vigilar.txt")
    p_vig.add_argument("--ultimos", type=int, default=7,
                       help="Dias hacia atras (1-99; por defecto 7)")
    p_vig.add_argument("--max-paginas", type=int,
                       default=MAX_PAGINAS_DEFECTO,
                       help="Paginas maximas por empresa")
    p_vig.add_argument("--todo", action="store_true",
                       help="Mostrar todo lo encontrado, no solo lo nuevo")
    p_vig.set_defaults(func=cmd_vigilar)

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
    p_listar.add_argument("--entidad",
                          help="Filtrar por entidad (o parte del nombre/NIF)")
    p_listar.add_argument("--limite", type=int, default=30,
                          help="Registros que se muestran (0 = todos; por "
                               "defecto 30)")
    p_listar.add_argument("--adjuntos", action="store_true",
                          help="Incluir tambien los documentos adjuntos")
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

    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    args.func(args)


if __name__ == "__main__":
    main()
