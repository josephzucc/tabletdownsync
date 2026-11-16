#!/usr/bin/env python3
"""
correo.py — exporta correos a formato .eml, o simplemente míralos como
texto, conectando directamente al servidor de correo (IMAP), sin pasar por
Thunderbird.

La configuración (cuentas y valores ajustables) está en un fichero aparte:
leerCorreoConfiguracion.py, en la misma carpeta que este programa. Edita
ESE fichero para añadir/quitar cuentas o cambiar cualquier ajuste — no
hace falta tocar este.

Modos de uso:
    python3 correo.py <cuenta>
        Trabaja con UNA cuenta (yahoo, gmx, gmail, hotmail, o cualquier otra
        clave que añadas en leerCorreoConfiguracion.py). Si no se indica
        ninguna, usa la primera. Menú, dejando elegir INBOX / una carpeta /
        todas las carpetas:
          1) Ver los más recientes. Con "todas las carpetas": TODAS a la vez,
             en una sola lista ordenada por fecha real (no pregunta por carpeta).
          2) Buscar por remitente/destinatario/asunto/cuerpo. Con "todas las
             carpetas": una sola pasada, resultados en una lista por fecha.

    python3 correo.py todas
        Conecta a TODAS las cuentas configuradas a la vez.
          1) Últimos 10/20/50 (o la cantidad que quieras) combinando TODAS
             las cuentas/carpetas.
          2) Buscar en TODAS las cuentas y carpetas a la vez, de una vez.

En cualquiera de los casos anteriores, cada mensaje listado se muestra con
una pequeña vista previa del cuerpo (en texto, aunque el original sea HTML)
y un ID corto y estable (derivado del Message-ID del correo, no cambia
nunca) — después se puede DESCARGAR como .eml, VER el texto completo (con
lista de adjuntos si los tiene), EXPORTAR la lista a CSV, ELIMINAR
(¡irreversible!) o RESPONDER (compone y envía por SMTP con los datos de la
cuenta). Buscando por el campo "id" (o con -id en modo comando) se puede
volver a encontrar ese mismo correo más adelante.

En modo de comandos directos también se puede ESCRIBIR un correo nuevo
desde cero, sin partir de ningún mensaje existente (-escribir — ver el
manual de comandos directos, sección 5.11).

En búsquedas largas, el aviso "¿seguir buscando?" indica cada cuántos
segundos pregunta; escribe "2x", "4x", etc. para espaciarlo más. Pasados
5 minutos de intervalo, solo espera unos segundos tu respuesta y sigue
sola si no la hay (mostrando la hora de cada comprobación) — en modo
comando directo, esa espera breve se usa siempre, no solo pasados 5 min.

Modo de comandos directos (sin menús) — ver el manual
(MANUAL_2_comandos_directos.txt) para todos los parámetros:
    python correo.py -acc gmx1 -buscar bbva -campos to,from,asunto -folders all
    python correo.py -acc todas -id a3f9e21b4c78 -folders all
    python correo.py -acc gmx1 -ver "id:a3f9e21b4c78"

Si "password" está vacío en CUENTAS (leerCorreoConfiguracion.py), se pide
de forma segura al arrancar (no queda visible en pantalla ni en el fichero).
"""

import imaplib
import email
import base64
import contextlib
import csv
import getpass
import hashlib
import html
import io
import json
import mimetypes
import os
import re
import select
import shutil
import smtplib
import subprocess
import sys
import textwrap
import time
import unicodedata
import urllib.request
import zlib
from datetime import datetime, timedelta, timezone
from email import policy
from email.message import EmailMessage
from email.utils import getaddresses, parsedate_to_datetime, format_datetime
from html.parser import HTMLParser

from leerCorreoConfiguracion import (
    CUENTAS,
    SALIDA,
    MAX_INICIAL,
    INTERVALO_AVISO_INICIAL,
    TAMANO_RONDA_BUSQUEDA,
    LIMITE_ESPERA_RESPUESTA,
    ANCHO_TERMINAL,
    LINEAS_CONTEXTO,
    LARGO_LINEA_PREVIEW,
    LARGO_URL_MAX,
    ANCHO_CAPTURA_PANTALLA,
    ALTO_CAPTURA_PANTALLA,
    TAMANO_FUENTE_CAPTURA,
    RETRASO_ENTRE_ENVIOS_LISTA,
    CARPETA_PROGRESO_BUSQUEDAS,
    CARPETA_REGISTRO_NUEVOS,
)


# --- Conexión/reconexión robusta con MUCHAS cuentas (Ronda Z, pedido
# 2026-11-03: "cuando pido los 10, 20 o 30 ultimos correos y tengo 10 o 15
# cuentas [...] en la 2da pasada, ve que las cuentas estan desactivadas, y
# algunas veces da error al intentar reactivar") ---
TIMEOUT_CONEXION_IMAP = 30      # segundos máximos para CONECTAR y autenticar (después se quita el límite: un SEARCH grande puede tardar más)
LIMITE_COMPROBAR_CONEXION = 20  # segundos máximos para el NOOP con el que se comprueba que una conexión sigue viva (una conexión "medio muerta" -- típico al dormirse Termux/el móvil -- ya no cuelga el programa)
REINTENTOS_RECONEXION = 2       # reintentos extra ante fallos TEMPORALES (red, servidor ocupado, "demasiadas conexiones"); un fallo de contraseña NUNCA se reintenta
PAUSAS_REINTENTO = (2, 5)       # segundos de espera antes del reintento 1 y 2
PAUSA_ENTRE_RECONEXIONES = 1.0  # segundos entre una reconexión y la siguiente al refrescar muchas cuentas (para no disparar los límites antiabuso del proveedor)

_ESTADO_CONEXION = {"silencioso": False, "transitorio": False, "mensaje": "", "dormir": time.sleep}
_PASSWORDS_EN_SESION = {}   # cuenta -> contraseña tecleada (solo en memoria, nunca a disco): sin esto, cada RECONEXIÓN volvía a pedirla (cuentas con "password": "")

_MARCAS_FALLO_TEMPORAL = (
    "unavailable", "too many", "temporar", "try again", "trylater", "busy", "limit",
    "throttl", "overload", "maximum number", "service not available", "internal error",
    "timeout", "timed out", "connection", "eof", "socket", "bye",
)


def _es_error_transitorio_imap(e):
    """True si el fallo de IMAP parece TEMPORAL (conexión cortada, servidor
    ocupado, demasiadas conexiones simultáneas...) y por tanto merece un
    reintento; False si parece de credenciales (que NO se reintenta: repetir
    una contraseña mala solo arriesga que el proveedor bloquee la cuenta)."""
    if isinstance(e, imaplib.IMAP4.abort):
        return True
    texto = str(e).lower()
    if any(m in texto for m in ("authenticationfailed", "authentication failed", "invalid credentials", "login failed", "invalid login", "wrong password")):
        return False
    return any(m in texto for m in _MARCAS_FALLO_TEMPORAL)


def _quitar_limite_de_espera(imap):
    try:
        imap.sock.settimeout(None)
    except Exception:
        pass


def _aviso_conexion(texto):
    _ESTADO_CONEXION["mensaje"] = texto
    if not _ESTADO_CONEXION["silencioso"]:
        print(texto)


def conectar(cfg, nombre_cuenta):
    """Devuelve la conexión IMAP ya autenticada, o None si falla (avisando por pantalla).
    Deja en _ESTADO_CONEXION["transitorio"] si el fallo parece temporal (ver
    _conectar_con_reintentos). La contraseña tecleada (cuentas con
    "password": "") se recuerda EN MEMORIA durante la sesión, para no volver
    a pedirla en cada reconexión."""
    _ESTADO_CONEXION["transitorio"] = False
    password = cfg.get("password") or _PASSWORDS_EN_SESION.get(nombre_cuenta) or getpass.getpass(
        f"Contraseña para {nombre_cuenta} ({cfg['user']}): "
    )
    try:
        imap = imaplib.IMAP4_SSL(cfg["server"], cfg["port"], timeout=TIMEOUT_CONEXION_IMAP)
        imap.login(cfg["user"], password)
        _quitar_limite_de_espera(imap)
        if not cfg.get("password"):
            _PASSWORDS_EN_SESION[nombre_cuenta] = password
        return imap
    except imaplib.IMAP4.error as e:
        if _es_error_transitorio_imap(e):
            _ESTADO_CONEXION["transitorio"] = True
            _aviso_conexion(
                f"El servidor de '{nombre_cuenta}' ha cortado o rechazado la conexión "
                f"(parece un fallo temporal). Detalle del servidor: {e}"
            )
        else:
            _PASSWORDS_EN_SESION.pop(nombre_cuenta, None)
            _aviso_conexion(
                f"Error de autenticación en '{nombre_cuenta}'. Revisa:\n"
                "  1) que sea una contraseña de aplicación (no la normal de la cuenta)\n"
                "  2) que IMAP/POP3 esté activado en la web del proveedor\n"
                "  3) usuario y servidor correctos\n"
                f"  Detalle del servidor: {e}"
            )
        return None
    except OSError as e:
        _ESTADO_CONEXION["transitorio"] = True
        _aviso_conexion(f"No se pudo conectar a {cfg['server']}:{cfg['port']} ('{nombre_cuenta}'). Detalle: {e}")
        return None


def _conectar_con_reintentos(cfg, nombre_cuenta, reintentos=None):
    """conectar() con hasta REINTENTOS_RECONEXION reintentos (esperando
    PAUSAS_REINTENTO entre ellos) SOLO si el fallo parece temporal. Los
    intentos intermedios no imprimen su error (solo el último, o el de un
    fallo no temporal, que corta al momento)."""
    if reintentos is None:
        reintentos = REINTENTOS_RECONEXION
    for intento in range(reintentos + 1):
        ultimo = intento == reintentos
        _ESTADO_CONEXION["silencioso"] = not ultimo
        _ESTADO_CONEXION["transitorio"] = False
        _ESTADO_CONEXION["mensaje"] = ""
        try:
            imap = conectar(cfg, nombre_cuenta)
        finally:
            _ESTADO_CONEXION["silencioso"] = False
        if imap is not None:
            return imap
        if ultimo:
            return None
        if not _ESTADO_CONEXION["transitorio"]:
            if _ESTADO_CONEXION["mensaje"]:
                print(_ESTADO_CONEXION["mensaje"])
            return None
        pausa = PAUSAS_REINTENTO[min(intento, len(PAUSAS_REINTENTO) - 1)]
        _ESTADO_CONEXION["dormir"](pausa)
    return None


def _noop_con_limite(imap):
    """imap.noop() con un límite de espera (LIMITE_COMPROBAR_CONEXION): una
    conexión medio muerta ya no deja colgado el programa."""
    sock = getattr(imap, "sock", None)
    anterior = None
    puesto = False
    if sock is not None:
        try:
            anterior = sock.gettimeout()
            sock.settimeout(LIMITE_COMPROBAR_CONEXION)
            puesto = True
        except Exception:
            pass
    try:
        return imap.noop()
    finally:
        if puesto:
            try:
                sock.settimeout(anterior)
            except Exception:
                pass


def _cerrar_imap_rapido(imap):
    """Cierra una conexión que ya está muerta o a punto de ser sustituida,
    sin esperar mucho: así no se acumulan conexiones medio abiertas (los
    proveedores limitan cuántas se pueden tener a la vez por cuenta)."""
    try:
        try:
            imap.sock.settimeout(3)
        except Exception:
            pass
        imap.logout()
    except Exception:
        try:
            imap.shutdown()
        except Exception:
            pass


def _con_conexion_viva(imap, cfg, nombre_cuenta, estado_linea=None):
    """Comprueba que 'imap' sigue viva y, si el servidor la cerró (típico
    tras varios minutos de inactividad — 'Server logging out'), reconecta
    en silencio y devuelve la conexión nueva. Si no se puede reconectar,
    devuelve None (avisando por pantalla).

    estado_linea (opcional -- pedido 2026-10-03; el mismo dict
    {"ds_abierta": ...} que ya usa _ejecutar_busqueda para el progreso de
    -ds): si se da, el aviso de "reconectando" se imprime como UNA sola
    línea que se autosobreescribe (igual que ese progreso), en vez de una
    línea nueva de siempre por cada reconexión -- así, con -acc todas y
    varias cuentas reconectando seguidas bajo -ds, en pantalla solo queda
    la ÚLTIMA, nunca se acumulan. El fallo DE VERDAD ("no se pudo
    reconectar") sigue siendo SIEMPRE una línea aparte y permanente -- no
    se sobrescribe -- igual que el resto de avisos de fallo bajo -ds (ver
    _avisar, dentro de _ejecutar_busqueda): primero cierra (con un salto
    de línea) esa línea autosobreescrita si se había dejado abierta."""
    try:
        estado, _ = _noop_con_limite(imap)
        if estado == "OK":
            return imap
    except Exception:
        pass
    if estado_linea is not None:
        print(f"\r  Reconectando: {nombre_cuenta}\033[K", end="", flush=True)
        estado_linea["ds_abierta"] = True
    else:
        print(f"La conexión con '{nombre_cuenta}' se había cerrado (probable inactividad); reconectando...")
    nuevo_imap = _conectar_con_reintentos(cfg, nombre_cuenta)
    if nuevo_imap is None:
        if estado_linea is not None and estado_linea["ds_abierta"]:
            print()
            estado_linea["ds_abierta"] = False
        print(f"No se pudo reconectar con '{nombre_cuenta}'.")
        return None
    # Ronda AC (2026-11-05): una conexión recién abierta está en estado AUTH (sin carpeta
    # abierta) y FETCH solo vale en SELECTED -> "command FETCH illegal in state AUTH" al
    # preparar la vista previa tras una búsqueda larga. Se reabre la carpeta que tenía la
    # vieja, en el mismo modo.
    previa = getattr(imap, "_carpeta_seleccionada", None)
    if previa:
        try:
            _seleccionar_carpeta(nuevo_imap, previa[0], readonly=previa[1])
        except Exception:
            pass
    _cerrar_imap_rapido(imap)  # la vieja, ya muerta: que no se quede abierta en el servidor
    return nuevo_imap


def _imap_vivo_para(conexiones, cuenta, imap_de_reserva, estado_linea=None):
    """Devuelve una conexión viva para 'cuenta' (reconectando sola si hace
    falta — ver _con_conexion_viva), o None si no se pudo reconectar. Sin
    'conexiones' (o sin esa cuenta en él), confía tal cual en
    imap_de_reserva. Si reconecta, actualiza 'conexiones' EN SITIO, así
    que quien llama (y cualquier otro código que comparta ese mismo dict)
    ve la conexión nueva también — imprescindible para operaciones largas
    (muchas cuentas/carpetas) donde una conexión puede morir a mitad,
    mucho después de la única comprobación inicial de turno.
    estado_linea (opcional): ver _con_conexion_viva."""
    if conexiones is None or cuenta not in conexiones:
        return imap_de_reserva
    imap_actual, cfg_actual = conexiones[cuenta]
    imap_vivo = _con_conexion_viva(imap_actual, cfg_actual, cuenta, estado_linea=estado_linea)
    if imap_vivo is not None and imap_vivo is not imap_actual:
        conexiones[cuenta] = (imap_vivo, cfg_actual)
    return imap_vivo


# ---------- Utilidades para leer asunto/cuerpo/direcciones/fecha ----------

_ESQUEMAS_ENLACE_UTILES = ("http://", "https://", "mailto:")


class _HTMLaTexto(HTMLParser):
    """Convierte HTML a texto plano simple, para poder buscar y previsualizar.

    Los enlaces (<a href="...">texto</a>) añaden la URL de destino entre
    paréntesis justo después del texto visible del enlace -- por ejemplo
    "Haz clic aquí (https://ejemplo.com)" -- salvo que el texto visible ya
    la contenga (para no duplicarla cuando el remitente ya escribió la URL
    tal cual como texto del enlace). Sin esto, un enlace de botón típico de
    correo HTML ("Ver factura", "Confirmar", "Unsubscribe"...) perdía la
    dirección de destino por completo al convertir a texto plano -- no
    había forma de saber a dónde llevaba. Solo se añaden esquemas útiles
    para abrir de verdad (http/https/mailto); se ignoran href vacíos,
    "#ancla", "javascript:", etc. Pedido 2026-09-29, junto con hacer
    clicables estos enlaces en el PDF -- ver _generar_pdf_simple."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._trozos = []
        self._omitir = False
        self._href_actual = None
        self._inicio_texto_ancla = None

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._omitir = True
        elif tag in ("br", "p", "div", "tr", "li"):
            self._trozos.append("\n")
        elif tag == "a" and self._href_actual is None:
            href = (dict(attrs).get("href") or "").strip()
            if href.lower().startswith(_ESQUEMAS_ENLACE_UTILES):
                self._href_actual = href
                self._inicio_texto_ancla = len(self._trozos)

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._omitir = False
        elif tag == "a" and self._href_actual is not None:
            texto_visible = "".join(self._trozos[self._inicio_texto_ancla:])
            if self._href_actual not in texto_visible:
                self._trozos.append(f" ({self._href_actual})")
            self._href_actual = None
            self._inicio_texto_ancla = None

    def handle_data(self, data):
        if not self._omitir:
            self._trozos.append(data)

    def texto(self):
        return "".join(self._trozos)


def html_a_texto(html_contenido):
    parser = _HTMLaTexto()
    try:
        parser.feed(html_contenido)
    except Exception:
        pass
    return parser.texto()


_PATRON_PARECE_HTML = re.compile(r"<(html|body|div|p|br|table|span|a\s|img)\b", re.IGNORECASE)


def _parece_html(texto):
    """Heurística: si un 'text/plain' contiene etiquetas típicas de HTML, es
    que el remitente lo etiquetó mal — en realidad es HTML sin convertir."""
    return bool(_PATRON_PARECE_HTML.search(texto))


def texto_cuerpo(msg):
    """Extrae el texto del cuerpo: texto plano si existe (y de verdad lo es),
    si no, convierte el HTML — incluido un 'text/plain' que en realidad
    contenga HTML sin convertir (algunos remitentes lo etiquetan mal)."""
    partes_texto, partes_html = [], []
    for parte in msg.walk():
        if parte.is_multipart():
            continue
        tipo = parte.get_content_type()
        try:
            contenido = parte.get_content()
        except Exception:
            continue
        if not isinstance(contenido, str):
            continue
        if tipo == "text/plain":
            if _parece_html(contenido):
                partes_html.append(contenido)
            else:
                partes_texto.append(contenido)
        elif tipo == "text/html":
            partes_html.append(contenido)

    if partes_texto:
        return "\n".join(partes_texto)
    if partes_html:
        return "\n".join(html_a_texto(h) for h in partes_html)
    return ""


def direcciones_de_campo(msg, campo):
    """Devuelve la lista de direcciones (solo el email, en minúsculas) de To/From/Cc/Bcc."""
    header = msg.get(campo)
    if header is None:
        return []
    try:
        return [a.addr_spec.lower() for a in header.addresses if a.addr_spec]
    except AttributeError:
        return [d.lower() for _, d in getaddresses([str(header)]) if d]


def campos_que_coinciden(msg, campos, texto_low, modo_exacto, ids_multiples=None):
    """Devuelve el conjunto de campos (de los buscados) donde realmente se
    encontró el texto. Vacío si no hay coincidencia en ninguno.
    ids_multiples (opcional): lista de prefijos de ID válidos — si se da,
    el campo 'id' coincide si el ID del mensaje empieza por CUALQUIERA de
    ellos, en vez de comparar contra el único texto_low (para poder pedir
    varios mensajes concretos por ID a la vez, ej. "id:X,id:Y")."""
    coincidentes = set()
    for campo in campos:
        if campo in ("to", "from", "cc", "bcc"):
            direcciones = direcciones_de_campo(msg, campo)
            if modo_exacto:
                if texto_low in direcciones:
                    coincidentes.add(campo)
            else:
                # "incluye": también en el nombre mostrado, no solo en la dirección
                cabecera_completa = str(msg.get(campo, "") or "").lower()
                if any(texto_low in d for d in direcciones) or texto_low in cabecera_completa:
                    coincidentes.add(campo)
        elif campo == "subject":
            if texto_low in str(msg.get("subject", "") or "").lower():
                coincidentes.add(campo)
        elif campo == "body":
            if texto_low in texto_cuerpo(msg).lower():
                coincidentes.add(campo)
        elif campo == "id":
            if ids_multiples is not None:
                id_msg = id_amigable(msg)
                if any(id_msg.startswith(p) for p in ids_multiples):
                    coincidentes.add(campo)
            elif id_amigable(msg).startswith(texto_low):
                coincidentes.add(campo)
    return coincidentes


def mensaje_coincide(msg, campos, texto_low, modo_exacto):
    return bool(campos_que_coinciden(msg, campos, texto_low, modo_exacto))


_FECHA_MUY_ANTIGUA = datetime(1, 1, 1, tzinfo=timezone.utc)


def fecha_mensaje(msg):
    """Devuelve un datetime comparable a partir de la cabecera Date (o 'muy antiguo' si falta/falla)."""
    valor = msg.get("date")
    if valor is None:
        return _FECHA_MUY_ANTIGUA
    try:
        dt = parsedate_to_datetime(str(valor))
        if dt is None:
            return _FECHA_MUY_ANTIGUA
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return _FECHA_MUY_ANTIGUA


def _ultimo_domingo(anio, mes):
    """Último domingo de 'mes' en 'anio', a las 00:00 UTC."""
    if mes == 12:
        ultimo_dia = datetime(anio + 1, 1, 1, tzinfo=timezone.utc) - timedelta(days=1)
    else:
        ultimo_dia = datetime(anio, mes + 1, 1, tzinfo=timezone.utc) - timedelta(days=1)
    while ultimo_dia.weekday() != 6:  # 6 = domingo
        ultimo_dia -= timedelta(days=1)
    return ultimo_dia


def _utc_a_madrid_manual(dt_utc):
    """Cálculo manual del horario de España peninsular (CET/UTC+1 en
    invierno, CEST/UTC+2 en verano — la UE cambia el último domingo de
    marzo y de octubre, a la 01:00 UTC), por si el sistema no tiene
    instalada la base de datos de zonas horarias (puede faltar en algunas
    instalaciones de Termux)."""
    dt_utc = dt_utc.astimezone(timezone.utc)
    anio = dt_utc.year
    inicio_verano = _ultimo_domingo(anio, 3).replace(hour=1)
    fin_verano = _ultimo_domingo(anio, 10).replace(hour=1)
    desplazamiento = 2 if inicio_verano <= dt_utc < fin_verano else 1
    return dt_utc.astimezone(timezone(timedelta(hours=desplazamiento)))


def _a_hora_espana_peninsular(dt):
    """Convierte un datetime con zona horaria a la hora civil de España
    PENINSULAR (Europe/Madrid — NO Canarias, que va una hora por detrás).
    Usa la base de datos de zonas horarias del sistema si está disponible
    (maneja el cambio de hora automáticamente); si no, calcula el horario
    de verano de la UE a mano."""
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo("Europe/Madrid"))
    except Exception:
        return _utc_a_madrid_manual(dt)


def _madrid_tzinfo_para(anio, mes, dia):
    """Tzinfo de España peninsular para esa fecha concreta (CET/UTC+1 o
    CEST/UTC+2 según corresponda) — con reserva manual si no hay zoneinfo."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("Europe/Madrid")
    except Exception:
        inicio_verano = _ultimo_domingo(anio, 3)
        fin_verano = _ultimo_domingo(anio, 10)
        fecha_aprox_utc = datetime(anio, mes, dia, 12, tzinfo=timezone.utc)  # mediodía: evita el filo del cambio
        desplazamiento = 2 if inicio_verano <= fecha_aprox_utc < fin_verano else 1
        return timezone(timedelta(hours=desplazamiento))


def _parsear_fecha_busqueda(valor, fin_de_rango):
    """Parsea 'AAAA-MM-DD', 'AAAA-MM-DD-HH' o 'AAAA-MM-DD-HH-MM' (hora de
    España peninsular) a un datetime con zona horaria. Si faltan horas o
    minutos: 00:00:00 si es el INICIO de un rango, 23:59:59 (o :59 si solo
    falta el minuto) si es el FIN — para que un solo día cubra el día
    entero, o una hora cubra esa hora completa."""
    partes = valor.split("-")
    try:
        numeros = [int(p) for p in partes]
    except ValueError:
        numeros = None
    if not numeros or len(numeros) not in (3, 4, 5):
        raise ValueError(f"Fecha '{valor}' no reconocida (usa AAAA-MM-DD, AAAA-MM-DD-HH o AAAA-MM-DD-HH-MM).")
    if len(numeros) == 3:
        anio, mes, dia = numeros
        hora, minuto = (23, 59) if fin_de_rango else (0, 0)
    elif len(numeros) == 4:
        anio, mes, dia, hora = numeros
        minuto = 59 if fin_de_rango else 0
    else:
        anio, mes, dia, hora, minuto = numeros
    segundo = 59 if fin_de_rango else 0
    return datetime(anio, mes, dia, hora, minuto, segundo, tzinfo=_madrid_tzinfo_para(anio, mes, dia))


def _partes_adjuntas(msg):
    """Generador de las partes que son adjuntos de verdad (no imágenes
    incrustadas en el cuerpo, tipo firma, que van 'inline' con Content-ID)."""
    for parte in msg.walk():
        if parte.get_content_maintype() == "multipart":
            continue
        disposicion = str(parte.get("Content-Disposition", "") or "").lower()
        if disposicion.startswith("attachment"):
            yield parte
        elif parte.get_filename() and not (disposicion.startswith("inline") and parte.get("Content-ID")):
            yield parte


def _tiene_adjuntos(msg):
    """True si el mensaje tiene 1 o más archivos adjuntos de verdad."""
    return next(_partes_adjuntas(msg), None) is not None


def _lista_adjuntos(msg):
    """Lista de (nombre, tamaño en bytes) de los adjuntos de verdad del
    mensaje. El tamaño es aproximado (el del contenido ya decodificado)."""
    adjuntos = []
    for parte in _partes_adjuntas(msg):
        nombre = parte.get_filename() or "(sin nombre)"
        try:
            contenido = parte.get_payload(decode=True)
            tamano = len(contenido) if contenido else 0
        except Exception:
            tamano = 0
        adjuntos.append((nombre, tamano))
    return adjuntos


def _formatear_tamano(num_bytes):
    """Da un tamaño en bytes en una unidad legible (B/KB/MB)."""
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.0f} KB"
    return f"{num_bytes / (1024 * 1024):.1f} MB"


# Estado de "-aqui" (comando directo): cuando está activo, TODO lo que se
# guarda en esta ejecución (correos .eml/.txt/.pdf, adjuntos, CSV) va
# directo a la carpeta actual (la de donde se llamó al programa), SIN la
# subcarpeta por cuenta de siempre (SALIDA/nombre_cuenta) — lo pedido tal
# cual: "la misma carpeta desde donde estás llamando al .py", tanto para
# los correos como para los adjuntos. Dict de 1 elemento (en vez de una
# variable module-level normal) para poder activarlo desde comando_directo
# sin declarar 'global' — mismo patrón que _ESTADO_ENTRADA.
_ESTADO_SALIDA = {"aqui": False}

# Cuánto se muestra de cada mensaje al listar (Ronda X, pedido 2026-10-19):
#   "completo" (por defecto): título + vista previa de LINEAS_CONTEXTO líneas;
#   "titulo"  (-ver-titulos): solo la línea de título (id + origen/destino + asunto);
#   "id"      (-ver-ids):     solo " N. id:XXXXXXXXXXXX".
# Lo fija comando_directo al empezar, a partir de las acciones pedidas.
_ESTADO_LISTADO = {"modo": "completo"}


def _carpeta_base_salida():
    """La carpeta RAÍZ de salida (para lo que no es por-cuenta, ej. el
    CSV): SALIDA normalmente, o el directorio actual si se pidió -aqui."""
    return os.getcwd() if _ESTADO_SALIDA["aqui"] else SALIDA


_DIR_PROGRAMA = os.path.dirname(os.path.abspath(__file__))


def _resolver_carpeta_estado(carpeta):
    """Ruta absoluta de una carpeta de estado configurada: si es relativa, respecto a la carpeta
    donde está correo.py (no respecto al directorio desde el que se lanza el programa)."""
    return carpeta if os.path.isabs(carpeta) else os.path.join(_DIR_PROGRAMA, carpeta)


def _ruta_fichero_de_estado(carpeta_configurada, prefijo, nombre):
    """Ruta del fichero de ESTADO propio del programa (progreso de
    -reanudable, registro de -nuevos): normalmente CARPETA/<nombre>.json;
    con -aqui (Ronda Y, pedido 2026-10-22: "cuando utilizo -aqui aun asi
    creas la carpeta [...] y ademas creas subcarpetas dentro de ella, no
    lo hagas pues utilizo -aqui") NO se crea ninguna carpeta ni
    subcarpeta: el fichero va directo al directorio actual, oculto
    (.<prefijo>_<nombre>.json), para que la próxima vez, repitiendo la orden
    desde ese mismo directorio, se siga encontrando. NO crea nada en disco:
    quien guarda crea la carpeta (si hace falta) justo antes de escribir."""
    # Ronda AD (2026-11-05): la carpeta configurada (CARPETA_REGISTRO_NUEVOS...) es RELATIVA
    # ("registro_nuevos_inbox") y se resolvía contra el directorio ACTUAL: llamando al programa
    # desde carpetas distintas (p. ej. una carpeta nueva por fecha) cada una tenía su propio
    # registro vacío y -nuevos daba SIEMPRE los mismos correos como nuevos. Ahora el estado
    # vive en un sitio fijo: junto a correo.py (o la ruta absoluta que se haya configurado).
    # -nuevos (el registro) ni siquiera se ata al directorio actual con -aqui. Si existe
    # el fichero en el sitio antiguo (directorio actual) y no en el nuevo, se copia (migración).
    fijo = prefijo == "registro_nuevos"
    if _ESTADO_SALIDA["aqui"] and not fijo:
        return os.path.join(os.getcwd(), f".{prefijo}_{nombre}.json")
    if _ESTADO_SALIDA["aqui"]:
        nueva = os.path.join(_DIR_PROGRAMA, f".{prefijo}_{nombre}.json")
        antigua = os.path.join(os.getcwd(), f".{prefijo}_{nombre}.json")
    else:
        nueva = os.path.join(_resolver_carpeta_estado(carpeta_configurada), f"{nombre}.json")
        antigua = os.path.join(carpeta_configurada, f"{nombre}.json")   # relativa al directorio actual, como antes
    try:
        if not os.path.isfile(nueva) and os.path.isfile(antigua) and os.path.abspath(antigua) != os.path.abspath(nueva):
            _asegurar_carpeta_de(nueva)
            shutil.copy2(antigua, nueva)
    except Exception:
        pass
    return nueva


def _asegurar_carpeta_de(ruta):
    carpeta = os.path.dirname(ruta)
    if carpeta:
        os.makedirs(carpeta, exist_ok=True)


class _CarpetaDeCuenta(str):
    """Ruta de salida que además recuerda a qué CUENTA pertenece. Hace falta
    con -aqui: ahí la carpeta es el directorio actual (sin subcarpeta por
    cuenta), así que ya no se puede recuperar el nombre de la cuenta desde
    el último trozo de la ruta (_cuenta_de_carpeta_salida) -- y -nuevos
    guardaba su registro bajo el nombre del directorio en vez del de la
    cuenta. Es un str normal para todo lo demás (open, makedirs, join...)."""
    cuenta = None


def _carpeta_salida_para_cuenta(nombre_cuenta):
    """Carpeta donde se guardan los ficheros de 'nombre_cuenta': SALIDA/
    nombre_cuenta normalmente (para no mezclar varias cuentas en la misma
    carpeta); con -aqui, el directorio actual A SECAS (sin subcarpeta por
    cuenta) — si en la misma orden intervienen varias cuentas, todas
    comparten esa misma carpeta actual."""
    if _ESTADO_SALIDA["aqui"]:
        carpeta = _CarpetaDeCuenta(os.getcwd())
        carpeta.cuenta = nombre_cuenta
        return carpeta
    return os.path.join(SALIDA, nombre_cuenta)


def _guardar_adjuntos(msg, carpeta_salida, carpeta_imap, extensiones=None, modo_si_existe="numerar"):
    """Guarda todos los adjuntos de verdad del mensaje (ver
    _partes_adjuntas) en carpeta_salida, con el nombre
    <nombre_del_correo>_adj_NombreAdjuntoCamelCase.ext
    (_nombre_fichero_adjunto — carpeta_imap hace falta para ese nombre,
    igual que para el del propio correo). extensiones (opcional): conjunto
    de extensiones (en minúsculas, sin el punto) a las que limitarse —
    None guarda cualquier tipo. modo_si_existe: ver _accion_si_existe.
    Devuelve (rutas, hubo_alguno): 'rutas' es la lista de rutas GUARDADAS
    de verdad (no incluye las que -si-existe dejó sin tocar);
    'hubo_alguno' indica si el mensaje tenía algún adjunto de verdad, se
    haya guardado o no -- para que quien llama distinga "no tiene
    adjuntos" de "los tenía, pero -si-existe pidió no tocarlos"."""
    os.makedirs(carpeta_salida, exist_ok=True)
    rutas = []
    hubo_alguno = False
    for parte in _partes_adjuntas(msg):
        nombre_original = parte.get_filename() or "adjunto"
        ext_original = os.path.splitext(nombre_original)[1].lstrip(".").lower()
        if extensiones is not None and ext_original not in extensiones:
            continue
        try:
            contenido = parte.get_payload(decode=True)
        except Exception:
            contenido = None
        if not contenido:
            continue
        hubo_alguno = True
        nombre = _nombre_fichero_adjunto(msg, carpeta_imap, nombre_original)
        ruta_deseada = os.path.join(carpeta_salida, nombre)
        ruta = _ruta_sin_colision(ruta_deseada, _accion_si_existe(modo_si_existe, "adjunto"))
        if ruta is None:
            print(f"Ya existe, no se sobrescribe: {ruta_deseada}")
            _registrar_fichero(msg, "adjuntos", ruta_deseada, "ya_existia", nombre_original=nombre_original)
            continue
        with open(ruta, "wb") as f:
            f.write(contenido)
        print(f"Guardado: {ruta}")
        _registrar_fichero(msg, "adjuntos", ruta, nombre_original=nombre_original)
        rutas.append(ruta)
    return rutas, hubo_alguno


# ---------- Imágenes incrustadas, OCR y tablas -> CSV (Ronda AA, pedido 2026-11-03) ----------
# Una imagen "incrustada" (inline) es la que va DENTRO del cuerpo del correo (típico: una
# tabla de Excel pegada como imagen, una firma, un cartel), no un adjunto de verdad
# (_partes_adjuntas las deja fuera a propósito). Todo esto es OPT-IN: sin -imagenes/-ocr/
# -tabla/-describir el comportamiento de -ver-txt y -adjuntos no cambia.
# Herramientas externas (todas OPCIONALES; si faltan se avisa una vez con la forma de
# instalarlas y el resto de la orden sigue): tesseract (OCR), Pillow (tratar la imagen) y,
# solo para -describir, un modelo de visión local (llama-mtmd-cli + SmolVLM, o la orden que
# el usuario ponga en VISION_CMD de la configuración -- opcional, se lee con getattr).
MIN_BYTES_IMAGEN_INCRUSTADA = 3000   # por debajo: píxeles de seguimiento, viñetas, líneas
MIN_LADO_IMAGEN_INCRUSTADA = 40      # px (si Pillow está): más pequeña = adorno
FACTOR_AMPLIACION_OCR = 3
UMBRAL_BINARIZADO_OCR = 150          # <150 = tinta; lo demás (fondos de color, rejilla gris clara) = blanco
TIMEOUT_OCR = 120
TIMEOUT_DESCRIBIR_IMAGEN = 600
SEPARADOR_TABLA_POR_DEFECTO = ";"
_MODELO_VISION_POR_DEFECTO = "ggml-org/SmolVLM-500M-Instruct-GGUF:Q8_0"
_PROMPT_VISION_POR_DEFECTO = "Describe this image in detail. Mention any text, people, place, objects and what is happening."
_AVISOS_IMAGENES = set()


def _avisar_una_vez(clave, texto):
    if clave not in _AVISOS_IMAGENES:
        _AVISOS_IMAGENES.add(clave)
        print(f"AVISO: {texto}", file=sys.stderr if _JSON_ESTADO.get("activo") else sys.stdout)


def _valor_config_opcional(nombre, defecto=""):
    """Constante OPCIONAL de leerCorreoConfiguracion.py (no hace falta añadirla)."""
    try:
        import leerCorreoConfiguracion as _cfg
        return getattr(_cfg, nombre, defecto)
    except Exception:
        return defecto


def _importar_pillow():
    try:
        from PIL import Image
        return Image
    except Exception:
        _avisar_una_vez("pillow", "falta Pillow (necesario para leer tablas en imágenes). En Termux: pkg install python-pillow")
        return None


def _hay_tesseract():
    if shutil.which("tesseract"):
        return True
    _avisar_una_vez("tesseract", "no se encuentra 'tesseract' (OCR). En Termux: pkg install tesseract")
    return False


_IDIOMAS_OCR_CACHE = []


def _idiomas_ocr():
    """'spa+eng' si ambos están instalados; si no 'eng' (o el primero que haya).
    Constante opcional OCR_IDIOMAS en la configuración manda sobre todo."""
    forzado = str(_valor_config_opcional("OCR_IDIOMAS", "") or "").strip()
    if forzado:
        return forzado
    if _IDIOMAS_OCR_CACHE:
        return _IDIOMAS_OCR_CACHE[0]
    instalados = []
    try:
        r = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, timeout=30)
        instalados = [l.strip() for l in (r.stdout or "").splitlines()[1:] if l.strip() and l.strip() != "osd"]
    except Exception:
        pass
    if "spa" in instalados and "eng" in instalados:
        elegido = "spa+eng"
    elif "spa" in instalados:
        elegido = "spa"
    elif "eng" in instalados:
        elegido = "eng"
        _avisar_una_vez("spa", "tesseract no tiene el idioma español: tildes y ñ saldrán sin acento. Instala spa.traineddata (en Termux: pkg search tesseract).")
    else:
        elegido = instalados[0] if instalados else "eng"
    _IDIOMAS_OCR_CACHE.append(elegido)
    return elegido


def _separador_tabla(valor):
    """Interpreta -sep: 'tab', 'coma', 'puntocoma', 'barra'/'pipe' o 1 carácter."""
    if valor is None:
        return SEPARADOR_TABLA_POR_DEFECTO
    v = str(valor)
    alias = {"tab": "\t", "\\t": "\t", "coma": ",", "puntocoma": ";", "barra": "|", "pipe": "|", "espacio": " "}
    v = alias.get(v.lower(), v)
    if len(v) != 1 or v in "\"\r\n":
        _error(f"-sep '{valor}' no es válido: usa un solo carácter (; , | ...) o tab/coma/puntocoma/barra.")
        return None
    return v


def _partes_imagen_incrustada(msg):
    """Partes image/* que NO son adjuntos de verdad (las inline de verdad)."""
    adjuntas = {id(p) for p in _partes_adjuntas(msg)}
    for parte in msg.walk():
        if parte.get_content_maintype() != "image" or id(parte) in adjuntas:
            continue
        yield parte


def _extension_imagen(parte):
    nombre = parte.get_filename() or ""
    ext = os.path.splitext(nombre)[1].lstrip(".").lower()
    if ext in ("png", "jpg", "jpeg", "gif", "bmp", "webp", "tif", "tiff"):
        return ext
    sub = parte.get_content_subtype().lower()
    return {"jpeg": "jpg", "x-png": "png"}.get(sub, sub if sub.isalnum() else "img")


def _lado_minimo_imagen(contenido):
    """Lado más pequeño en px, o None si no se puede saber (sin Pillow o formato raro)."""
    try:
        from PIL import Image
        with Image.open(io.BytesIO(contenido)) as im:
            return min(im.size)
    except Exception:
        return None


def _guardar_imagenes_incrustadas(msg, carpeta_salida, carpeta_imap, modo_si_existe="numerar"):
    """Guarda las imágenes incrustadas relevantes como <correo>_img_N_<nombre>.<ext>.
    Devuelve lista de dicts {ruta, nombre_original, content_id, tipo, bytes, existia}."""
    guardadas = []
    n = 0
    for parte in _partes_imagen_incrustada(msg):
        try:
            contenido = parte.get_payload(decode=True)
        except Exception:
            contenido = None
        if not contenido or len(contenido) < MIN_BYTES_IMAGEN_INCRUSTADA:
            continue
        lado = _lado_minimo_imagen(contenido)
        if lado is not None and lado < MIN_LADO_IMAGEN_INCRUSTADA:
            continue
        n += 1
        original = parte.get_filename() or str(parte.get("Content-ID") or "").strip("<> ") or f"imagen{n}"
        nombre = f"{_nombre_base_mensaje(msg, carpeta_imap)}_img_{n}_{_camel_case(os.path.splitext(original)[0], 40) or 'imagen'}.{_extension_imagen(parte)}"
        os.makedirs(carpeta_salida, exist_ok=True)
        deseada = os.path.join(carpeta_salida, nombre)
        ruta = _ruta_sin_colision(deseada, _accion_si_existe(modo_si_existe, "adjunto"))
        existia = False
        if ruta is None:
            print(f"Ya existe, no se sobrescribe: {deseada}")
            ruta, existia = deseada, True
            _registrar_fichero(msg, "imagenes", ruta, "ya_existia", nombre_original=original)
        else:
            with open(ruta, "wb") as f:
                f.write(contenido)
            print(f"Guardado: {ruta}")
            _registrar_fichero(msg, "imagenes", ruta, nombre_original=original)
        guardadas.append({"ruta": ruta, "nombre_original": original, "content_id": str(parte.get("Content-ID") or "").strip("<> "),
                          "tipo": parte.get_content_type(), "bytes": len(contenido), "existia": existia})
    return guardadas


# --- OCR y reconstrucción de tablas ---

def _preparar_para_ocr(im):
    """Escala de grises, ampliada y binarizada (tinta negra sobre blanco) con margen: así
    los fondos de color (p. ej. la fila de cabecera resaltada) y la rejilla gris clara
    dejan de estorbar a tesseract."""
    Image = _importar_pillow()
    g = im.convert("L")
    g = g.resize((g.width * FACTOR_AMPLIACION_OCR, g.height * FACTOR_AMPLIACION_OCR), Image.LANCZOS)
    b = g.point(lambda v: 0 if v < UMBRAL_BINARIZADO_OCR else 255)
    lienzo = Image.new("L", (b.width + 40, b.height + 40), 255)
    lienzo.paste(b, (20, 20))
    return lienzo


def _ejecutar_tesseract(ruta_png, psm, formato=None):
    orden = ["tesseract", ruta_png, "-", "--psm", str(psm), "-l", _idiomas_ocr()]
    if formato:
        orden.append(formato)
    r = subprocess.run(orden, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=TIMEOUT_OCR)
    return r.stdout or ""


def _ocr_palabras(im):
    """Palabras con posición [{x,y,w,h,t}] en coordenadas de la imagen ORIGINAL."""
    import tempfile
    prep = _preparar_para_ocr(im)
    with tempfile.TemporaryDirectory() as tmp:
        ruta = os.path.join(tmp, "p.png")
        prep.save(ruta)
        salida = _ejecutar_tesseract(ruta, 6, "tsv")
    F = FACTOR_AMPLIACION_OCR
    palabras = []
    for fila in csv.DictReader(io.StringIO(salida), delimiter="\t", quoting=csv.QUOTE_NONE):
        try:
            texto = (fila.get("text") or "").strip()
            # '_', '|', '[' ... sueltos son trozos de rejilla mal leídos como letras: se descartan
            if fila.get("level") == "5" and texto and texto.strip("_|[]{}\\/") != "":
                palabras.append({"x": (int(fila["left"]) - 20) / F, "y": (int(fila["top"]) - 20) / F,
                                 "w": int(fila["width"]) / F, "h": int(fila["height"]) / F, "t": fila["text"].strip()})
        except (ValueError, KeyError, TypeError):
            continue
    return palabras


def _lineas_verticales(im):
    """x (px) de las líneas verticales de rejilla: columnas donde >60% de los píxeles son
    gris/negro neutro (no el azul del resaltado). Sin numpy: máscaras con Pillow."""
    from PIL import Image, ImageChops
    rgb = im.convert("RGB")
    r, g, b = rgb.split()
    mx = ImageChops.lighter(ImageChops.lighter(r, g), b)
    mn = ImageChops.darker(ImageChops.darker(r, g), b)
    sat = ImageChops.subtract(mx, mn)
    gris = rgb.convert("L")
    oscuro = gris.point(lambda v: 255 if v < 235 else 0)
    neutro = sat.point(lambda v: 255 if v < 30 else 0)
    mascara = ImageChops.multiply(oscuro, neutro).point(lambda v: 255 if v else 0)
    columnas = mascara.resize((mascara.width, 1), Image.BOX)   # media de cada columna
    datos = list(columnas.tobytes())
    xs = [x for x, v in enumerate(datos) if v > 0.6 * 255]
    grupos = []
    for x in xs:
        if grupos and x - grupos[-1][-1] <= 2:
            grupos[-1].append(x)
        else:
            grupos.append([x])
    return [sum(g) / len(g) for g in grupos]


def _agrupar_en_filas(palabras):
    palabras = sorted(palabras, key=lambda p: p["y"] + p["h"] / 2)
    filas = []
    for p in palabras:
        cy = p["y"] + p["h"] / 2
        if filas and abs(cy - filas[-1]["cy"]) < max(5, 0.6 * p["h"]):
            fila = filas[-1]
            fila["p"].append(p)
            fila["cy"] = (fila["cy"] * (len(fila["p"]) - 1) + cy) / len(fila["p"])
        else:
            filas.append({"cy": cy, "p": [p]})
    return [sorted(f["p"], key=lambda p: p["x"]) for f in filas]


def _bordes_por_huecos(palabras):
    """Sin rejilla: límites de columna = huecos verticales (en TODAS las filas a la vez)
    más anchos que ~1,2 veces la altura de letra. Aproximado: sirve para tablas sin líneas."""
    if not palabras:
        return []
    alturas = sorted(p["h"] for p in palabras)
    hueco_min = max(6.0, 1.2 * alturas[len(alturas) // 2])
    tramos = sorted((p["x"], p["x"] + p["w"]) for p in palabras)
    fusion = [list(tramos[0])]
    for a, b in tramos[1:]:
        if a - fusion[-1][1] < hueco_min:
            fusion[-1][1] = max(fusion[-1][1], b)
        else:
            fusion.append([a, b])
    if len(fusion) < 2:
        return []
    bordes = [fusion[0][0] - 1]
    for i in range(len(fusion) - 1):
        bordes.append((fusion[i][1] + fusion[i + 1][0]) / 2)
    bordes.append(fusion[-1][1] + 1)
    return bordes


def _tabla_desde_imagen(ruta):
    """Lee una imagen de tabla/hoja de cálculo y devuelve (filas, metodo) con filas = lista
    de listas de texto; metodo = 'rejilla' (columnas por líneas verticales) o 'huecos'.
    (None, motivo) si no se pudo."""
    Image = _importar_pillow()
    if Image is None or not _hay_tesseract():
        return None, "falta Pillow o tesseract"
    try:
        im = Image.open(ruta).convert("RGB")
        palabras = _ocr_palabras(im)
        if not palabras:
            return None, "no se leyó ningún texto"
        lineas = _lineas_verticales(im)
    except Exception as e:
        return None, f"error al leer la imagen: {e}"
    if len(lineas) >= 3:
        bordes, metodo = lineas, "rejilla"
    else:
        bordes, metodo = _bordes_por_huecos(palabras), "huecos"
    if len(bordes) < 3:
        return None, "no parece una tabla (menos de 2 columnas)"
    tabla = []
    for fila in _agrupar_en_filas(palabras):
        celdas = [[] for _ in range(len(bordes) - 1)]
        for p in fila:
            cx = p["x"] + p["w"] / 2
            for i in range(len(bordes) - 1):
                if bordes[i] <= cx < bordes[i + 1]:
                    celdas[i].append(p["t"])
                    break
        tabla.append([" ".join(c) for c in celdas])
    tabla = [f for f in tabla if any(f)]
    return (tabla, metodo) if tabla else (None, "tabla vacía")


def _escribir_csv_tabla(filas, ruta, sep):
    """CSV con BOM UTF-8 (Excel/LibreOffice abren bien tildes y ñ)."""
    with open(ruta, "w", encoding="utf-8-sig", newline="") as f:
        csv.writer(f, delimiter=sep, lineterminator="\n").writerows(filas)


def _unir_guiones_castellano(texto):
    """Une las palabras partidas con guion a final de línea (prensa a columnas):
    'sue-\ncos' -> 'suecos'. Solo se une si antes del guion hay una letra y la línea
    siguiente empieza en MINÚSCULA (si empieza en mayúscula o número es un guion de
    verdad: 'Madrid-\nBarajas', '2020-\n2021', y se deja tal cual). También quita el
    guion blando (U+00AD)."""
    texto = texto.replace("\u00ad\n", "")
    return re.sub(r"(?<=[^\W\d_])[-\u2010\u2011]\n[ \t]*(?=[a-záéíóúüñ])", "", texto)


def _ocr_texto_plano(ruta):
    """Texto de una imagen que no es tabla (cartel, captura, firma)."""
    Image = _importar_pillow()
    import tempfile
    if Image is None:
        return ""
    im = Image.open(ruta).convert("RGB")
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "p.png")
        _preparar_para_ocr(im).save(p)
        return _unir_guiones_castellano(_ejecutar_tesseract(p, 3).strip())


def _describir_imagen_local(ruta):
    """Descripción con un modelo de visión local (SmolVLM...). (texto, error)."""
    orden = str(_valor_config_opcional("VISION_CMD", "") or "").strip()
    if not orden:
        exe = shutil.which("llama-mtmd-cli")
        if not exe:
            return None, "no hay modelo de visión local (instala llama.cpp: pkg install llama-cpp, o define VISION_CMD)"
        orden = f"{exe} -hf {_MODELO_VISION_POR_DEFECTO} --image {{img}} -p {{prompt}} -n 200 --temp 0.2"
    prompt = str(_valor_config_opcional("VISION_PROMPT", "") or _PROMPT_VISION_POR_DEFECTO)
    import shlex
    try:
        cmd = [x.replace("{img}", ruta).replace("{prompt}", prompt) for x in shlex.split(orden)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_DESCRIBIR_IMAGEN)
    except subprocess.TimeoutExpired:
        return None, "el modelo tardó demasiado"
    except Exception as e:
        return None, str(e)
    txt = re.sub(r"\s+", " ", re.sub(r"(?m)^(main|llama|clip|load|encoding|decoding|image|mtmd|ggml|build|sched)[^\n]*$", "", r.stdout or "")).strip()
    if not txt:
        return None, ((r.stderr or "").strip().splitlines() or [f"error {r.returncode}"])[-1][:200]
    return txt, ""


def _tabla_a_texto(filas):
    return "\n".join(" | ".join(f) for f in filas)


def _procesar_imagenes_incrustadas(msg, carpeta_salida, carpeta_imap, args, modo_si_existe="numerar"):
    """Guarda las imágenes incrustadas y, según los flags, les hace OCR (-ocr), las convierte
    en CSV (-tabla, separador -sep) o las describe (-describir). Devuelve el texto a añadir
    al final del .txt ('' si no hay imágenes)."""
    sep = _separador_tabla(args.get("sep"))
    if sep is None:
        return ""
    quiere_ocr, quiere_tabla, quiere_desc = bool(args.get("ocr")), bool(args.get("tabla")), bool(args.get("describir"))
    imagenes = _guardar_imagenes_incrustadas(msg, carpeta_salida, carpeta_imap, modo_si_existe)
    if not imagenes:
        return ""
    lineas = ["", "=" * 60, f"IMÁGENES INCRUSTADAS EN EL CUERPO ({len(imagenes)})", "=" * 60]
    for i, img in enumerate(imagenes, 1):
        lineas.append(f"[{i}] {os.path.basename(img['ruta'])}  ({img['tipo']}, {_formatear_tamano(img['bytes'])})")
        base = os.path.splitext(img["ruta"])[0]
        filas = None
        if quiere_tabla or quiere_ocr:
            filas, info = _tabla_desde_imagen(img["ruta"]) if (_importar_pillow() and _hay_tesseract()) else (None, "sin herramientas")
            if quiere_tabla:
                if filas:
                    ruta_csv = _ruta_sin_colision(base + ".csv", _accion_si_existe(modo_si_existe, "adjunto")) or base + ".csv"
                    _escribir_csv_tabla(filas, ruta_csv, sep)
                    print(f"Guardado: {ruta_csv}  ({len(filas)} filas x {len(filas[0])} columnas, columnas por {info})")
                    _registrar_fichero(msg, "tablas", ruta_csv, nombre_original=img["nombre_original"])
                    lineas.append(f"    Tabla CSV: {os.path.basename(ruta_csv)} ({len(filas)} filas, separador {sep!r}, columnas por {info})")
                else:
                    print(f"No se pudo convertir en tabla {os.path.basename(img['ruta'])}: {info}")
                    lineas.append(f"    (no se pudo convertir en tabla: {info})")
        if quiere_ocr:
            # tabla con rejilla fiable -> texto por filas; si no, OCR plano
            if filas and _lineas_verticales_rapido(img["ruta"]):
                texto = _tabla_a_texto(filas)
            else:
                texto = _ocr_texto_plano(img["ruta"]) if (_importar_pillow() and _hay_tesseract()) else ""
            if texto:
                ruta_txt = _ruta_sin_colision(base + "_ocr.txt", _accion_si_existe(modo_si_existe, "adjunto")) or base + "_ocr.txt"
                with open(ruta_txt, "w", encoding="utf-8") as f:
                    f.write(texto + "\n")
                print(f"Guardado: {ruta_txt}")
                _registrar_fichero(msg, "ocr", ruta_txt, nombre_original=img["nombre_original"])
                lineas.append("    Texto leído (OCR):")
                lineas.extend("      " + l for l in texto.splitlines())
            else:
                lineas.append("    (OCR: no se leyó texto)")
        if quiere_desc:
            desc, err = _describir_imagen_local(img["ruta"])
            if desc:
                ruta_d = _ruta_sin_colision(base + "_descripcion.txt", _accion_si_existe(modo_si_existe, "adjunto")) or base + "_descripcion.txt"
                with open(ruta_d, "w", encoding="utf-8") as f:
                    f.write(desc + "\n")
                print(f"Guardado: {ruta_d}")
                _registrar_fichero(msg, "descripciones", ruta_d, nombre_original=img["nombre_original"])
                lineas.append(f"    Descripción (modelo local, en inglés): {desc}")
            else:
                print(f"No se pudo describir {os.path.basename(img['ruta'])}: {err}")
                lineas.append(f"    (no se pudo describir: {err})")
    return "\n".join(lineas)


def _lineas_verticales_rapido(ruta):
    try:
        from PIL import Image
        return len(_lineas_verticales(Image.open(ruta).convert("RGB"))) >= 3
    except Exception:
        return False


def _quiere_imagenes(args):
    return any(args.get(k) for k in ("imagenes", "ocr", "tabla", "describir"))


def formatear_fecha(dt):
    """AAAA-MM-DD-HH-MM, sin segundos ni corchetes, en hora de España
    peninsular. '(sin fecha)' si no se pudo leer."""
    if dt <= _FECHA_MUY_ANTIGUA:
        return "(sin fecha)"
    return _a_hora_espana_peninsular(dt).strftime("%Y-%m-%d-%H-%M")



def id_amigable(msg):
    """Identificador corto (12 caracteres) y ESTABLE del correo, derivado de
    su Message-ID (que el remitente asigna y no cambia nunca). Sirve para
    volver a encontrar este correo más adelante (hoy, en una semana, en un
    mes...) buscando por el campo 'id' — ver CAMPOS_DISPONIBLES."""
    message_id = str(msg.get("message-id", "") or "").strip()
    if not message_id:
        # Respaldo si el correo no trae Message-ID (raro): usa datos que no
        # deberían cambiar para ese mismo correo.
        message_id = f"{msg.get('from','')}|{msg.get('date','')}|{msg.get('subject','')}"
    return hashlib.sha256(message_id.encode("utf-8", errors="replace")).hexdigest()[:12]


_CARACTERES_RELLENO_INVISIBLE = {
    0x034F,  # COMBINING GRAPHEME JOINER (relleno típico de los "preheader" de newsletters)
    0x115F, 0x1160, 0x3164, 0xFFA0,  # rellenos Hangul (invisibles)
    0x180E,  # separador mongol (invisible)
}


def _sanear_texto_pantalla(texto, una_linea=False):
    """Deja el texto "seguro" para pintarlo en una terminal (Termux/tmux).

    Muchos correos (newsletters de Discord, Quora, InfoJobs...) llevan, sobre
    todo en el "preheader", cientos de caracteres INVISIBLES o de control:
    espacios de ancho cero (U+200B/200C/200D), marcas de dirección
    (U+200E/200F, U+202A-202E, U+2066-2069), guion blando (U+00AD), BOM
    (U+FEFF), unidor de grafemas (U+034F), rellenos Hangul, selectores de
    variación, caracteres de control (ESC, BEL, BS...), etc. Python los
    cuenta como caracteres (así que gastan ancho/presupuesto de la línea),
    pero la terminal les da ancho 0 -- o, peor, tmux/Termux calculan MAL
    la posición del cursor con ellos y pintan las líneas siguientes encima
    de las anteriores (letras cambiadas, líneas que "desaparecen").

    - NFC primero (une 'e'+acento suelto en 'é', que sí se pinta bien).
    - Elimina: controles (salvo salto de línea y tabulador), formato (Cf),
      selectores de variación, uso privado/sustitutos/sin asignar y los
      rellenos invisibles de arriba.
    - Espacios "raros" (NBSP, U+2000-200A, U+202F, U+205F, U+3000, braille
      en blanco) -> espacio normal. U+2028/2029, VT, FF, NEL -> salto de línea.
    - una_linea=True: todo salto de línea/tabulador pasa a espacio (asuntos,
      remitentes...).
    No toca el .eml descargado (ese se guarda tal cual llegó)."""
    if not texto:
        return texto if texto is not None else ""
    texto = unicodedata.normalize("NFC", str(texto)).replace("\r\n", "\n").replace("\r", "\n")
    salida = []
    for ch in texto:
        cp = ord(ch)
        if ch == "\n":
            salida.append(" " if una_linea else "\n")
        elif ch == "\r":
            continue
        elif ch == "\t":
            salida.append(" " if una_linea else "\t")
        elif cp in (0x0B, 0x0C, 0x85, 0x2028, 0x2029):
            salida.append(" " if una_linea else "\n")
        elif cp in _CARACTERES_RELLENO_INVISIBLE or 0xFE00 <= cp <= 0xFE0F or 0xE0100 <= cp <= 0xE01EF:
            continue
        elif cp == 0x2800:
            salida.append(" ")
        else:
            cat = unicodedata.category(ch)
            if cat in ("Cc", "Cf", "Co", "Cs", "Cn"):
                continue
            salida.append(" " if cat == "Zs" else ch)
    resultado = "".join(salida)
    return resultado


def _limpiar_direccion(valor, largo):
    """Quita comillas dobles (habituales en '\"Nombre\" <email>') y recorta a 'largo'."""
    return _sanear_texto_pantalla(str(valor or ""), una_linea=True).replace('"', "")[:largo]


_PATRONES_CARPETA_ENVIADOS = ("sent", "enviad", "gesendet")


def _es_carpeta_enviados(nombre_carpeta):
    """Heurística: ¿esta carpeta es la de 'Enviados' de la cuenta? (varios
    idiomas/proveedores: Sent, Sent Items, Enviados, Gesendet...)."""
    nombre_low = (nombre_carpeta or "").lower()
    return any(p in nombre_low for p in _PATRONES_CARPETA_ENVIADOS)


def _direccion_compacta(header_valor, largo_max):
    """Comprime una cabecera De/Para/Cc tipo 'Juan Jose <jose@x.com>' a
    'JuanJose jose@x.com' — sin espacios en el nombre, sin '<' '>' en el
    correo — recortado a largo_max caracteres. Sin nombre, deja solo el
    correo."""
    if header_valor is None:
        return ""
    nombre, correo = "", ""
    try:
        direcciones = header_valor.addresses
        if direcciones:
            nombre = (direcciones[0].display_name or "").replace('"', "")
            correo = direcciones[0].addr_spec or ""
    except AttributeError:
        pares = getaddresses([str(header_valor)])
        if pares:
            nombre, correo = pares[0]
    nombre = _sanear_texto_pantalla(nombre, una_linea=True)
    correo = _sanear_texto_pantalla(correo, una_linea=True)
    nombre_compacto = nombre.replace(" ", "")
    texto = f"{nombre_compacto} {correo}" if nombre_compacto and correo else (correo or nombre_compacto)
    return texto[:largo_max]


def _campo_a_mostrar(msg, carpeta, campos_coincidentes):
    """Decide qué campo mostrar (etiqueta, cabecera) en la línea de título:
    1. Si la búsqueda encontró el término en from/to/cc: se muestra ESE
       campo (prioridad De > Para > Cc si coincidió en varios a la vez).
    2. Si no (sin búsqueda, o el término solo apareció en asunto/cuerpo):
       Para (to) si el mensaje está en una carpeta de Enviados, De (from)
       en cualquier otra carpeta (mensaje recibido)."""
    if campos_coincidentes:
        if "from" in campos_coincidentes:
            return "De", msg.get("from")
        if "to" in campos_coincidentes:
            return "Para", msg.get("to")
        if "cc" in campos_coincidentes:
            return "Cc", msg.get("cc")
    if _es_carpeta_enviados(carpeta):
        return "Para", msg.get("to")
    return "De", msg.get("from")


def _lineas_no_vacias(texto):
    return [l.strip() for l in texto.splitlines() if l.strip()]


_PATRON_CID = re.compile(r"\[cid:[^\]]*\]", re.IGNORECASE)
_PATRON_URL = re.compile(r"https?://\S+")


def _limpiar_cuerpo_texto(texto, recortar_urls=True):
    """Decodifica entidades HTML sueltas (&nbsp;, &amp;, etc. — a veces
    quedan sin convertir en partes 'texto plano' que en realidad vinieron
    de HTML), quita referencias [cid:...] (típicas de imágenes incrustadas)
    y, salvo que recortar_urls=False, recorta los enlaces http(s) largos a
    LARGO_URL_MAX caracteres (pensado para la vista previa en listas —
    vista_previa() — donde un enlace largo entero descolocaría la línea;
    'ver completo'/.txt/.pdf ya NO usa este recorte — ver
    texto_cuerpo_limpio — porque ahí un enlace cortado a medias ya no sirve
    ni para leerlo entero ni, en el PDF, para pulsarlo — ver
    _generar_pdf_simple)."""
    texto = html.unescape(texto)
    texto = _sanear_texto_pantalla(texto)
    texto = texto.replace("\xa0", " ")  # espacio de no separación -> espacio normal
    texto = _PATRON_CID.sub("", texto)
    if not recortar_urls:
        return texto

    def _recortar_url(m):
        url = m.group(0)
        return url[:LARGO_URL_MAX] + ("…" if len(url) > LARGO_URL_MAX else "")

    return _PATRON_URL.sub(_recortar_url, texto)


def texto_cuerpo_limpio(msg, recortar_urls=True):
    """texto_cuerpo(msg), pero sin [cid:...] y (por defecto) con los
    enlaces recortados — para la vista previa en listas. recortar_urls=
    False conserva los enlaces COMPLETOS, sin recortar — usado por 'ver
    completo'/.txt/.pdf (_texto_completo_formateado). texto_cuerpo() sin
    limpiar se sigue usando para BUSCAR, para no afectar a las
    coincidencias."""
    return _limpiar_cuerpo_texto(texto_cuerpo(msg), recortar_urls=recortar_urls)


def _colapsar_lineas_en_blanco(texto):
    """2 o más líneas en blanco seguidas -> 1 sola línea en blanco."""
    lineas = texto.splitlines()
    resultado, previa_vacia = [], False
    for l in lineas:
        vacia = not l.strip()
        if vacia and previa_vacia:
            continue
        resultado.append(l)
        previa_vacia = vacia
    return "\n".join(resultado)


def formatear_preview(lineas, presupuesto_total=None):
    """Une las líneas de vista previa en un solo párrafo separado por '. ',
    hasta un presupuesto TOTAL de caracteres (no un número de líneas del
    original) — muchos correos vienen con saltos de línea "suaves" cada
    30-50 caracteres (sin líneas en blanco de por medio), así que contar
    "3 líneas" del original da muchísimo menos texto del disponible; aquí
    se sigue añadiendo texto hasta llenar el presupuesto, venga en 3
    líneas largas o en 10 cortas del original."""
    if not lineas:
        return ""
    if presupuesto_total is None:
        texto = ". ".join(l.rstrip(". ") for l in lineas)
        if texto and not texto.endswith((".", "!", "?", "…")):
            texto += "."
        return texto

    partes = []
    largo_acumulado = 0
    cortado = False
    for l in lineas:
        l = l.rstrip(". ")
        if not l:
            continue
        si_se_separa = 2 if partes else 0  # ". " que añadirá el join, salvo antes de la 1a parte
        if largo_acumulado + si_se_separa + len(l) > presupuesto_total:
            espacio_restante = presupuesto_total - largo_acumulado - si_se_separa
            if espacio_restante > 10:  # solo merece la pena si queda hueco razonable para un trozo con sentido
                partes.append(l[:espacio_restante].rstrip())
            cortado = True
            break
        partes.append(l)
        largo_acumulado += si_se_separa + len(l)

    texto = ". ".join(partes)
    if cortado:
        return (texto + "…") if texto else "…"
    if texto and not texto.endswith((".", "!", "?", "…")):
        texto += "."
    return texto


def vista_previa(msg, campos_coincidentes=None, texto_low=None):
    """Líneas de vista previa del cuerpo (sin líneas en blanco):
    - Si el término de búsqueda apareció en el CUERPO: LINEAS_CONTEXTO líneas
      antes y después de la primera línea donde aparece (incluida esa línea).
    - En cualquier otro caso (vista genérica, o el término solo apareció en
      to/from/cc/asunto): TODAS las líneas no vacías — es formatear_preview()
      quien decide cuánto entra según el presupuesto total de caracteres,
      no aquí por número de líneas del original (ver esa función para el
      porqué)."""
    lineas = _lineas_no_vacias(texto_cuerpo_limpio(msg))
    if not lineas:
        return []
    if campos_coincidentes and "body" in campos_coincidentes and texto_low:
        idx = next((i for i, l in enumerate(lineas) if texto_low in l.lower()), None)
        if idx is not None:
            inicio = max(0, idx - LINEAS_CONTEXTO)
            fin = min(len(lineas), idx + LINEAS_CONTEXTO + 1)
            return lineas[inicio:fin]
    return lineas


# ---------- Guardado y visualización de un mensaje ----------

def guardar_crudo(crudo, carpeta_salida, msg, carpeta_imap, modo_si_existe="numerar"):
    nombre = _nombre_fichero_mensaje(carpeta_imap, msg, "eml")
    ruta_deseada = os.path.join(carpeta_salida, nombre)
    ruta = _ruta_sin_colision(ruta_deseada, _accion_si_existe(modo_si_existe, "correo"))
    if ruta is None:
        print(f"Ya existe, no se sobrescribe: {ruta_deseada}")
        _registrar_fichero(msg, "eml", ruta_deseada, "ya_existia")
        return None
    with open(ruta, "wb") as f:
        f.write(crudo)
    print(f"Guardado: {ruta}")
    _registrar_fichero(msg, "eml", ruta)
    return ruta


# Cabeceras "importantes" además de De/Para/Asunto/Fecha (nota: "ID:" NO es
# ninguna de estas — es un id corto propio de este programa, ver
# id_amigable(); el Message-ID de aquí abajo es el original, completo, tal
# como lo puso el remitente). Cc y Reply-To son bastante autoexplicativas;
# Return-Path es el remitente "de sobre" (a menudo distinto del De: que se
# ve en pantalla, y más difícil de falsear); Authentication-Results (o, en
# servidores más viejos, Received-SPF) es el veredicto que el PROPIO
# servidor receptor dio sobre SPF/DKIM/DMARC al recibir el correo — lo más
# cercano a una respuesta rápida a "¿esto es de verdad de quien dice ser?".
_CABECERAS_IMPORTANTES = (
    "Message-ID", "Cc", "Reply-To", "Return-Path",
    "Authentication-Results", "Received-SPF",
)


def _limpiar_valor_cabecera(valor):
    """Colapsa a una sola línea: las cabeceras largas (Received,
    Authentication-Results...) llegan "plegadas" en varias líneas con
    espacio de sobra, y eso descuadraría el formato "Nombre: valor", una
    cabecera por línea, usado aquí."""
    return " ".join(_sanear_texto_pantalla(str(valor), una_linea=True).split())


def _lineas_cabeceras_importantes(msg):
    lineas = []
    for nombre in _CABECERAS_IMPORTANTES:
        valor = msg.get(nombre)
        if valor:
            lineas.append(f"{nombre}: {_limpiar_valor_cabecera(valor)}")
    return lineas


def _lineas_todas_las_cabeceras(msg):
    """TODAS las cabeceras del mensaje, en el orden en que llegaron
    (msg.items() respeta el orden original) — incluye por tanto, entre
    otras, las de _CABECERAS_IMPORTANTES, y también cada "Received" por el
    que pasó el correo (uno por servidor intermedio)."""
    lineas = ["", "Todas las cabeceras (tal como llegaron):"]
    for nombre, valor in msg.items():
        lineas.append(f"{nombre}: {_limpiar_valor_cabecera(valor)}")
    return lineas


def _texto_completo_formateado(msg, modo_cabeceras=None):
    """Arma el bloque De/Para/Asunto/Fecha/ID + cuerpo, tal como se muestra
    al 'ver el texto completo' — reutilizado para pantalla, .txt y .pdf.
    Si el mensaje tiene adjuntos, añade su lista (nombre y tamaño) antes
    del cuerpo.

    modo_cabeceras (opcional):
      None (por defecto): igual que siempre — solo ID/De/Para/Asunto/Fecha.
      "importantes": añade además Message-ID, Cc, Reply-To, Return-Path y
        Authentication-Results/Received-SPF (las que el mensaje realmente
        tenga — ver _CABECERAS_IMPORTANTES).
      "todas": en vez de lo anterior, vuelca TODAS las cabeceras del
        mensaje tal como llegaron (incluye las de "importantes" y todas
        las demás, p.ej. cada "Received" del recorrido).
      Pensado para cuando hace falta que un tercero no dude de la
      autenticidad del correo — aviso honesto: un .txt/.pdf con cabeceras
      no es, por sí mismo, algo verificable (cualquiera puede escribir
      texto en un fichero); lo único que sí se puede comprobar de verdad
      (la firma DKIM contra el DNS del dominio remitente) es el .eml
      original, así que para eso conviene acompañar esto del .eml
      (-descargar), no depender solo del .txt/.pdf."""
    de = _limpiar_direccion(msg.get("from", ""), 200)
    para = _limpiar_direccion(msg.get("to", ""), 200)
    asunto = _sanear_texto_pantalla(str(msg.get("subject", "") or "(sin asunto)"), una_linea=True)
    cuerpo = _colapsar_lineas_en_blanco(texto_cuerpo_limpio(msg, recortar_urls=False).strip())
    lineas = [
        f"ID: {id_amigable(msg)}",
        f"De: {de}",
        f"Para: {para}",
        f"Asunto: {asunto}",
        f"Fecha: {formatear_fecha(fecha_mensaje(msg))}",
    ]
    if modo_cabeceras == "importantes":
        lineas.extend(_lineas_cabeceras_importantes(msg))
    elif modo_cabeceras == "todas":
        lineas.extend(_lineas_todas_las_cabeceras(msg))
    adjuntos = _lista_adjuntos(msg)
    if adjuntos:
        lineas.append(f"Adjuntos ({len(adjuntos)}):")
        for nombre, tamano in adjuntos:
            lineas.append(f"  - {nombre} ({_formatear_tamano(tamano)})")
    lineas.append("-" * 60)
    lineas.append(cuerpo or "(sin cuerpo de texto legible)")
    return "\n".join(lineas)


def _mostrar_texto_completo(msg, modo_cabeceras=None):
    print("\n" + "=" * 60)
    print(_texto_completo_formateado(msg, modo_cabeceras))
    print("=" * 60)


def _camel_case(texto, largo_max=60):
    """Compacta un texto a 'camelCase': primera palabra en minúsculas, las
    siguientes con la inicial en mayúscula y el resto en minúsculas, sin
    espacios ni otros separadores. Ej: "nombre asunto largo" ->
    "nombreAsuntoLargo". Cualquier carácter no alfanumérico se trata como
    separador de palabra (se quita, no se conserva)."""
    limpio = "".join(c if c.isalnum() else " " for c in texto)
    palabras = limpio.split()
    if not palabras:
        return ""
    resultado = palabras[0].lower()
    for palabra in palabras[1:]:
        resultado += palabra[0].upper() + palabra[1:].lower()
    return resultado[:largo_max]


def _limpiar_para_fichero(texto, largo_max=40):
    """Dejar un texto seguro para usarlo tal cual en un nombre de fichero
    (sin espacios ni caracteres problemáticos de sistema de ficheros:
    barras, corchetes...) — para el nombre de carpeta (ej. las de Gmail,
    "[Gmail]/Todos los mensajes"), que NO se compacta a camelCase (se dejan
    las mayúsculas tal cual, solo se quita lo que no es seguro)."""
    limpio = "".join(c for c in texto if c.isalnum() or c in "_-")
    return limpio[:largo_max] or "carpeta"


def _accion_si_existe(modo_si_existe, categoria):
    """Traduce el modo ya validado de -si-existe (uno de _VALORES_SI_EXISTE,
    o "numerar" si no se dio el flag -- ver _validar_modo_si_existe) a qué
    hacer con UN fichero concreto, según su categoria -- "correo" para
    .eml/.txt/.pdf, "adjunto" para un adjunto -- devolviendo lo que
    entiende _ruta_sin_colision: "numerar"/"sobrescribir"/"omitir"."""
    if modo_si_existe == "numerar":
        return "numerar"
    if modo_si_existe == "sobrescribir":
        return "sobrescribir"
    if modo_si_existe == "no":
        return "omitir"
    if modo_si_existe == "correo":
        return "sobrescribir" if categoria == "correo" else "omitir"
    if modo_si_existe == "adjuntos":
        return "sobrescribir" if categoria == "adjunto" else "omitir"
    raise ValueError(f"modo_si_existe desconocido: {modo_si_existe!r}")


def _ruta_sin_colision(ruta, accion="numerar"):
    """Si 'ruta' ya existe (dos correos con la misma fecha/carpeta/asunto
    hasta el segundo, poco habitual pero posible, o simplemente una
    descarga repetida), qué hacer depende de 'accion' (ver
    _accion_si_existe):
      "numerar" (por defecto -- el comportamiento de siempre): añade _2,
        _3... antes de la extensión hasta encontrar un nombre libre, para
        no sobrescribir un fichero ya guardado.
      "sobrescribir": devuelve 'ruta' tal cual -- quien llama sobrescribirá
        el fichero existente.
      "omitir": devuelve None -- señal para quien llama de que NO debe
        escribir nada (el fichero ya existe y -si-existe pide dejarlo tal
        cual)."""
    if not os.path.exists(ruta):
        return ruta
    if accion == "sobrescribir":
        return ruta
    if accion == "omitir":
        return None
    base, ext = os.path.splitext(ruta)
    n = 2
    while os.path.exists(f"{base}_{n}{ext}"):
        n += 1
    return f"{base}_{n}{ext}"


def _nombre_base_mensaje(msg, carpeta_imap):
    """AAAA-MM-DD-HH-MM-SS_CARPETA_ID_AsuntoCamelCase — sin extensión,
    común a .eml/.txt/.pdf. La fecha es la del correo, en hora de España
    peninsular (Manual 1, nota sobre la hora de los correos). El ID
    completo (12 caracteres, siempre el mismo para ese correo) va justo
    después de la carpeta y antes del asunto, para poder usarlo luego
    como selector sin tener que abrir el fichero."""
    fecha_txt = _a_hora_espana_peninsular(fecha_mensaje(msg)).strftime("%Y-%m-%d-%H-%M-%S")
    carpeta_segura = _limpiar_para_fichero(carpeta_imap, 30)
    asunto = str(msg.get("subject", "") or "sinAsunto")
    asunto_camel = _camel_case(asunto, 60) or "sinAsunto"
    return f"{fecha_txt}_{carpeta_segura}_{id_amigable(msg)}_{asunto_camel}"


def _nombre_fichero_mensaje(carpeta_imap, msg, extension):
    return f"{_nombre_base_mensaje(msg, carpeta_imap)}.{extension}"


def _nombre_fichero_adjunto(msg, carpeta_imap, nombre_adjunto_original):
    """<nombre del correo>_adj_NombreAdjuntoCamelCase.ext — es decir,
    EXACTAMENTE el mismo nombre que tendría el .eml/.txt/.pdf de ese
    correo (_nombre_base_mensaje: fecha_hora_CARPETA_ID_Asunto), seguido
    de "_adj_" y el nombre del adjunto — para que, mirando el nombre de
    un adjunto, se vea de un vistazo a qué correo pertenece (misma fecha,
    carpeta, ID y asunto que su .eml), y para poder ordenarlos juntos
    alfabéticamente. La extensión es la del adjunto original."""
    base, ext = os.path.splitext(nombre_adjunto_original or "adjunto")
    ext = ext.lstrip(".").lower() or "bin"
    nombre_camel = _camel_case(base, 60) or "adjunto"
    return f"{_nombre_base_mensaje(msg, carpeta_imap)}_adj_{nombre_camel}.{ext}"


def _guardar_texto_completo_txt(msg, carpeta_salida, carpeta_imap, multi=None, modo_cabeceras=None, modo_si_existe="numerar", texto_extra=""):
    """Guarda el texto completo (De/Para/Asunto/Fecha/ID + cuerpo) en un
    fichero .txt. Devuelve la ruta creada (o None si ya existía y
    -si-existe pedía dejarlo tal cual). ('multi' ya no afecta al
    nombre — se conserva el parámetro solo por compatibilidad con quien
    llama.) modo_cabeceras: ver _texto_completo_formateado. modo_si_existe:
    ver _accion_si_existe."""
    os.makedirs(carpeta_salida, exist_ok=True)
    nombre = _nombre_fichero_mensaje(carpeta_imap, msg, "txt")
    ruta_deseada = os.path.join(carpeta_salida, nombre)
    ruta = _ruta_sin_colision(ruta_deseada, _accion_si_existe(modo_si_existe, "correo"))
    if ruta is None:
        print(f"Ya existe, no se sobrescribe: {ruta_deseada}")
        _registrar_fichero(msg, "txt", ruta_deseada, "ya_existia")
        return None
    with open(ruta, "w", encoding="utf-8") as f:
        f.write(_texto_completo_formateado(msg, modo_cabeceras))
        f.write("\n")
        if texto_extra:
            f.write(texto_extra + "\n")
    print(f"Guardado: {ruta}")
    _registrar_fichero(msg, "txt", ruta)
    return ruta


# ---------- Generador de PDF mínimo (sin dependencias externas) ----------
# Escribe directamente la estructura de un PDF sencillo (una fuente estándar,
# texto envuelto y paginado) — no hace falta instalar nada en Termux.

_PDF_ANCHO_PAGINA, _PDF_ALTO_PAGINA = 595, 842  # A4, en puntos
_PDF_MARGEN = 50
_PDF_TAM_FUENTE = 10
_PDF_ALTURA_LINEA = 13
_PDF_ANCHO_UTIL = _PDF_ANCHO_PAGINA - 2 * _PDF_MARGEN
_PDF_ANCHO_CARACTER = _PDF_TAM_FUENTE * 0.5  # ancho medio aproximado (Helvetica no es monoespaciada)
_PDF_CARACTERES_POR_LINEA = int(_PDF_ANCHO_UTIL / _PDF_ANCHO_CARACTER)
_PDF_LINEAS_POR_PAGINA = int((_PDF_ALTO_PAGINA - 2 * _PDF_MARGEN) / _PDF_ALTURA_LINEA)

# Enlaces clicables (pedido 2026-09-29): mismos esquemas que
# _ESQUEMAS_ENLACE_UTILES (ver _HTMLaTexto) -- http(s):// y mailto:.
_PDF_RE_URL = re.compile(r"(?:https?://|mailto:)\S+", re.IGNORECASE)
_PDF_PUNTUACION_FINAL_ENLACE = ".,;:!?\"'"


def _pdf_detectar_urls(texto):
    """Devuelve una lista de (inicio, fin, url) por cada URL/mailto: que
    aparezca en 'texto' -- 'inicio'/'fin' son índices de carácter dentro
    de 'texto' (fin EXCLUSIVE, como en slicing) y 'url' ya sin puntuación
    final que probablemente sea de la propia frase (un punto, una coma,
    un paréntesis o corchete de cierre que no se abrió DENTRO de la
    propia URL...) y no parte de la dirección. Usada tanto para decidir
    qué palabras no partir al envolver (_pdf_envolver_texto) como para
    calcular dónde poner el rectángulo clicable (_generar_pdf_simple)."""
    resultado = []
    for m in _PDF_RE_URL.finditer(texto):
        inicio, fin, url = m.start(), m.end(), m.group(0)
        while url:
            ultimo = url[-1]
            if ultimo in _PDF_PUNTUACION_FINAL_ENLACE:
                url, fin = url[:-1], fin - 1
            elif ultimo == ")" and url.count("(") < url.count(")"):
                url, fin = url[:-1], fin - 1
            elif ultimo == "]" and url.count("[") < url.count("]"):
                url, fin = url[:-1], fin - 1
            else:
                break
        if url:
            resultado.append((inicio, fin, url))
    return resultado


def _pdf_escapar(texto):
    """Escapa \\, ( y ) (obligatorio en strings PDF) y codifica en
    WinAnsi/cp1252 para que tildes/ñ/¿/¡ se vean bien con la fuente estándar."""
    texto = texto.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return texto.encode("cp1252", errors="replace")


def _pdf_envolver_texto(texto):
    """Envuelve el texto al ancho de página, respetando los saltos de línea
    ya existentes como límites de párrafo. Excepción: un párrafo con algún
    enlace (ver _pdf_detectar_urls) se envuelve SIN partir palabras largas
    (break_long_words=False) -- para que la URL nunca quede partida a
    medias entre dos líneas y pueda hacerse clicable entera en una sola
    (ver _generar_pdf_simple); en el caso raro de una URL más larga que el
    ancho de página, esa línea concreta se sale un poco del margen derecho
    -- precio aceptado a cambio de un enlace intacto. El resto de párrafos
    (sin ningún enlace) se sigue partiendo igual que siempre."""
    lineas_finales = []
    for parrafo in texto.split("\n"):
        if not parrafo.strip():
            lineas_finales.append("")
            continue
        romper_palabras_largas = not _pdf_detectar_urls(parrafo)
        envueltas = textwrap.wrap(
            parrafo, width=_PDF_CARACTERES_POR_LINEA,
            break_long_words=romper_palabras_largas, break_on_hyphens=False,
        )
        lineas_finales.extend(envueltas or [""])
    return lineas_finales


def _generar_pdf_simple(texto):
    """Genera un PDF (bytes) de una o varias páginas A4 con el texto dado,
    en Helvetica — solo con la librería estándar de Python. Cada URL o
    mailto: que aparezca en el texto (ver _pdf_detectar_urls) se marca
    ADEMÁS como enlace clicable de verdad (anotación /Link con acción
    /URI) encima del propio texto dibujado -- al pulsarla en un lector de
    PDF, se abre. El rectángulo clicable se calcula con el mismo ancho de
    carácter aproximado que ya se usa para decidir dónde envolver las
    líneas (_PDF_ANCHO_CARACTER) -- Helvetica no es monoespaciada, así que
    es una posición aproximada, no exacta al píxel, pero consistente con
    cómo ya se maqueta el resto del texto. Pedido 2026-09-29: "al utilizar
    ver-pdf deja todos los links completos y creados para pulsar sobre
    ellos y que se abran"."""
    lineas = _pdf_envolver_texto(texto)
    paginas = [
        lineas[i:i + _PDF_LINEAS_POR_PAGINA]
        for i in range(0, len(lineas), _PDF_LINEAS_POR_PAGINA)
    ] or [[]]

    objetos = []

    def _nuevo_objeto(contenido_bytes):
        objetos.append(contenido_bytes)
        return len(objetos)

    num_catalogo = _nuevo_objeto(b"")
    num_pages = _nuevo_objeto(b"")
    num_fuente = _nuevo_objeto(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    )

    num_paginas_obj = []
    for pagina in paginas:
        partes = [
            b"BT",
            f"/F1 {_PDF_TAM_FUENTE} Tf".encode("ascii"),
            f"{_PDF_ALTURA_LINEA} TL".encode("ascii"),
            f"{_PDF_MARGEN} {_PDF_ALTO_PAGINA - _PDF_MARGEN} Td".encode("ascii"),
        ]
        anotaciones_pagina = []
        y_linea = _PDF_ALTO_PAGINA - _PDF_MARGEN
        primero = True
        for linea in pagina:
            if not primero:
                partes.append(b"T*")
                y_linea -= _PDF_ALTURA_LINEA
            primero = False
            partes.append(b"(" + _pdf_escapar(linea) + b") Tj")
            for inicio, fin, url in _pdf_detectar_urls(linea):
                x1 = _PDF_MARGEN + inicio * _PDF_ANCHO_CARACTER
                x2 = _PDF_MARGEN + fin * _PDF_ANCHO_CARACTER
                y1 = y_linea - 0.2 * _PDF_TAM_FUENTE
                y2 = y_linea + 0.8 * _PDF_TAM_FUENTE
                num_enlace = _nuevo_objeto(
                    (
                        f"<< /Type /Annot /Subtype /Link "
                        f"/Rect [{x1:.2f} {y1:.2f} {x2:.2f} {y2:.2f}] "
                        f"/Border [0 0 0] "
                        f"/A << /Type /Action /S /URI /URI ("
                    ).encode("ascii") + _pdf_escapar(url) + b") >> >>"
                )
                anotaciones_pagina.append(num_enlace)
        partes.append(b"ET")
        stream = b"\n".join(partes)

        num_contenido = _nuevo_objeto(
            f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"\nendstream"
        )
        num_pagina = _nuevo_objeto(b"")
        anotaciones_txt = ""
        if anotaciones_pagina:
            anotaciones_txt = " /Annots [" + " ".join(f"{n} 0 R" for n in anotaciones_pagina) + "]"
        objetos[num_pagina - 1] = (
            f"<< /Type /Page /Parent {num_pages} 0 R "
            f"/Resources << /Font << /F1 {num_fuente} 0 R >> >> "
            f"/MediaBox [0 0 {_PDF_ANCHO_PAGINA} {_PDF_ALTO_PAGINA}] "
            f"/Contents {num_contenido} 0 R{anotaciones_txt} >>"
        ).encode("ascii")
        num_paginas_obj.append(num_pagina)

    hijos = " ".join(f"{n} 0 R" for n in num_paginas_obj)
    objetos[num_pages - 1] = (
        f"<< /Type /Pages /Kids [{hijos}] /Count {len(num_paginas_obj)} >>"
    ).encode("ascii")
    objetos[num_catalogo - 1] = f"<< /Type /Catalog /Pages {num_pages} 0 R >>".encode("ascii")

    salida = bytearray()
    salida += b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    offsets = [0] * (len(objetos) + 1)
    for i, contenido in enumerate(objetos, start=1):
        offsets[i] = len(salida)
        salida += f"{i} 0 obj\n".encode("ascii") + contenido + b"\nendobj\n"

    inicio_xref = len(salida)
    salida += f"xref\n0 {len(objetos) + 1}\n".encode("ascii")
    salida += b"0000000000 65535 f \n"
    for i in range(1, len(objetos) + 1):
        salida += f"{offsets[i]:010d} 00000 n \n".encode("ascii")

    salida += (
        f"trailer\n<< /Size {len(objetos) + 1} /Root {num_catalogo} 0 R >>\n"
        f"startxref\n{inicio_xref}\n%%EOF"
    ).encode("ascii")

    return bytes(salida)


def _guardar_texto_completo_pdf(msg, carpeta_salida, carpeta_imap, multi=None, modo_cabeceras=None, modo_si_existe="numerar", con_remotas=False):
    """Guarda el texto completo del mensaje como .pdf. Devuelve la ruta
    creada (o None si ya existía y -si-existe pedía dejarlo tal cual).
    modo_cabeceras: ver _texto_completo_formateado. modo_si_existe: ver
    _accion_si_existe.

    Pedido 2026-10-05/07 (verbatim): "no crees el pdf a partir del txt
    sino que sea como cuando generas una imagen, que pones tanto el
    texto como la imagen" -- el PDF incluye ahora las imágenes
    incrustadas del correo EN SU SITIO (misma posición/aspecto que
    -ver-img: _generar_pdf_con_imagenes, definida junto a -ver-img más
    abajo porque reutiliza su misma lógica de bloques/resolución de
    imágenes), con el texto como contenido de PDF de verdad (no una
    captura): se sigue pudiendo seleccionar/copiar, y los enlaces siguen
    clicables de verdad (pedido 2026-09-29, #56) -- ver el comentario de
    _generar_pdf_con_imagenes para el porqué de cada decisión de diseño.
    con_remotas: ver _resolver_imagen_para_captura -- por defecto NO se
    descargan imágenes remotas (http/https), igual que -ver-img.

    Si Pillow no está instalado (única dependencia externa del programa,
    igual que para -ver-img), o si algo falla al generar el PDF con
    imágenes, cae al generador de solo texto de siempre
    (_generar_pdf_simple) avisando por qué -- nunca se queda sin PDF."""
    os.makedirs(carpeta_salida, exist_ok=True)
    nombre = _nombre_fichero_mensaje(carpeta_imap, msg, "pdf")
    ruta_deseada = os.path.join(carpeta_salida, nombre)
    ruta = _ruta_sin_colision(ruta_deseada, _accion_si_existe(modo_si_existe, "correo"))
    if ruta is None:
        print(f"Ya existe, no se sobrescribe: {ruta_deseada}")
        _registrar_fichero(msg, "pdf", ruta_deseada, "ya_existia")
        return None
    pdf_bytes = None
    try:
        from PIL import Image  # noqa: F401 -- solo para comprobar que Pillow está instalada
        pdf_bytes = _generar_pdf_con_imagenes(msg, modo_cabeceras, con_remotas)
    except ImportError:
        print(
            "(Pillow no está instalado -- este PDF se genera solo con el texto, sin las "
            "imágenes incrustadas del correo; instala Pillow con 'pip install Pillow "
            "--break-system-packages' -- en Termux: 'pkg install python-pillow' -- para incluirlas.)"
        )
    except Exception as e:
        _error(f"No se pudieron incrustar las imágenes en este PDF ({e}); se genera solo con el texto.")
    if pdf_bytes is None:
        pdf_bytes = _generar_pdf_simple(_texto_completo_formateado(msg, modo_cabeceras))
    with open(ruta, "wb") as f:
        f.write(pdf_bytes)
    print(f"Guardado: {ruta}")
    _registrar_fichero(msg, "pdf", ruta)
    return ruta




# ---------- -ver-img: el correo "tal como se ve" convertido en imagen(es) ----------
# Pedido 2026-10-01: poder sacar 1, 2 o 3 "pantallas" (como ir bajando con
# el dedo en el móvil) de CUALQUIER correo, en formato imagen -- pensado
# sobre todo para cuando el correo es prácticamente UNA sola imagen grande
# (típico de muchos boletines/publicidad) y por eso -ver-txt/-ver-pdf no
# muestran nada de utilidad (el texto estaba todo DENTRO de la imagen, no
# como texto de verdad). Pero funciona con cualquier correo, no solo esos.
#
# Única excepción en todo el programa a "nada de dependencias externas":
# hace falta tener instalada la librería Pillow (pip install Pillow
# --break-system-packages; en Termux puede ir mejor "pkg install
# python-pillow") -- a diferencia del PDF (_generar_pdf_simple, más
# arriba), aquí SÍ hace falta decodificar imágenes de verdad tal como
# llegan en cualquier correo (jpeg/png/gif...), y eso no es viable a mano
# en Python puro. Se importa de forma "perezosa" (dentro de las propias
# funciones, no al principio del fichero) para que el resto del programa
# siga funcionando sin Pillow instalado mientras no se use -ver-img -- y
# si se usa sin tenerlo instalado, se avisa con claridad de qué instalar
# en vez de petar con un error críptico (ver _guardar_capturas_pantalla).
#
# Aproximado, no una réplica exacta de ningún cliente de correo concreto:
# el HTML se interpreta de forma sencilla (párrafos, negrita <b>/<strong>,
# enlaces <a> con la URL añadida entre paréntesis -- igual que
# _HTMLaTexto --, e imágenes <img> en su sitio), sin CSS de verdad ni
# diseños de tablas/columnas complejos. Para el caso que motivó el pedido
# (todo el correo es 1 imagen) el resultado es exacto, porque ahí no hay
# nada más que esa imagen de por medio.

_CAPTURA_MARGEN = 28
_CAPTURA_COLOR_TEXTO = (20, 20, 20)
_CAPTURA_COLOR_ENLACE = (20, 80, 200)
_CAPTURA_COLOR_GRIS = (120, 120, 120)
_CAPTURA_COLOR_FONDO = "white"
_CAPTURA_ALTO_EXTRA_LINEA = 10  # espacio vertical extra entre líneas, además del tamaño de letra
_CAPTURA_LIMITE_BYTES_REMOTA = 15 * 1024 * 1024  # 15 MB máx. para una imagen remota (-con-remotas) -- no colgarse con un archivo enorme
_CAPTURA_TIMEOUT_REMOTA = 10  # segundos de espera máxima al descargar una imagen remota

# Fuente incrustada: subconjunto de DejaVu Sans (regular y NEGRITA DE
# VERDAD, no "negrita simulada" a doble trazo), licencia Bitstream Vera
# (muy permisiva: permite copiar, modificar -- incluido recortar -- y
# redistribuir como parte de un programa -- https://dejavu-fonts.github.
# io/License.html), recortado con fontTools a solo los caracteres que
# hacen falta aquí (alfabeto latino básico + acentos/eñes/diéresis de
# español y otros idiomas europeos + símbolos de puntuación/moneda
# habituales: ¿¡€…–—""•©®™ etc.) para que quepa sin problema incrustado en
# este mismo fichero. La fuente integrada de Pillow (ImageFont.
# load_default) NO sirve para esto: es un juego de caracteres limitado
# que NO incluye "í"/"ó" y otras letras acentuadas -- se comprobó que
# salían como un cuadrado de "carácter no encontrado" en vez de la letra,
# inaceptable escribiendo en español. Un carácter de un alfabeto no latino
# (cirílico, árabe, chino...) puede seguir saliendo así; limitación
# conocida y razonable para un renderizador aproximado como este.
_CAPTURA_FUENTE_REGULAR_B64 = """
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
I/2ivNOLpqaLBfj+9rKyAAAB/M8E7v8xBfgABgAAAQMzFzczA/2i04umpovTBO4BCrKy/vYAAAH8
xwUG/zkF+AANAAABMx4BMzI2NzMOASMiJvzHdg1jU1JhEHYKoI+QnwX4Njk3OHd7egAB/ZoFDv5m
BdsAAwAAATMVI/2azMwF280AAAL85gTu/7IF9gADAAcAAAEzAyMDMwMj/vm55JmLueSZBfb++AEI
/vgAAAAAAQAAAXYDVAArAGgADAABAAAAAAAAAAAAAAAAAAgABAAAAAAAAAAWACoAZgCyAQABTgFc
AXkBlQG7AdQB5AHxAf0CCwI7AlICggK+AtsDCwNKA10DpQPjA/QECgQfBDIERgR/BPQFEAVHBXcF
nwW3BcwGAwYbBigGPgZZBmkGhwafBtMG9gczB2QHoQe0B9YH6wgLCCoIQQhYCGoIeQiLCKEIrgi+
CPYJJglSCYIJtAnUChMKNQpHCmIKfAqJCr0K3gsKCzoLaguJC8QL5QwJDB0MOgxaDHkMkAzCDNAN
Ag0yDTINSQ2GDbEN+w4pDj4OmQ6sDxsPWg98D4wPmRAPEBwQRxBnEJEQyxDZEQsRJhEyEVMRaRGW
EboRyhHaEeoSIxIvEjsSRxJTEl8SmRLBEs0S2RLlEvES/RMJExUTIRMtE18TaxN3E4MTjxObE6cT
yRQWFCIULhQ6FEYUUhR3FL0UyBTTFN4U6RT0FP8VWxVnFXMVfxWLFZcVoxWvFbsVxxYMFhgWIxYu
FjkWRBZPFmkWsRa8FscW0hbdFugXGBcjFy8XOhdGF1EXXRdpF3UXgReNF5kXpRexF70XyRfVF+EX
6RgiGC4YOhhGGFIYXhhqGHYYghiOGJoYphixGL0YyRjVGOEY7Rj5GQUZERk3GWIZbhl6GYYZkhme
GaoZthnCGc4Z3hnqGfYaAhoOGhoaJhpAGkwaWBpkGnAafBqIGpQaoBq9GtYa4hrtGvkbBRsRGx0b
KRtWG4YbkhudG6kbtBvAG8wb/hxQHFwcZxxzHH8cixyWHKIcrRy5HMQc0BzbHOcc8xz+HQkdFR0h
HT8dax13HYMdjx2bHacdsx2/Hcod1h3iHe4d+h4GHhIeHh4pHjUeQR5MHlgeZB5wHnselh6sHrQe
xh7ZHvMfGR85H2Yfex+KH58frR+6H8cf1B/lH/YgESAsIEMgWyBpIIUg0SD3IRMhICE8IVghZiGC
IZIhnyGwIcEhziHeIfgiJCI/IkwiZyKTIq0iuiLNItsjCSMYIyojPSNXI2QjeQAAAAEAAAACXrhz
uOEIXw889QAfCAAAAAAA4PrROQAAAADg+tE599b8TA5ZCdwAAAAIAAIAAAAAAAAEzQBmAosAAAM1
ATUDrgDFBrQAngUXAKoHmgBxBj0AgQIzAMUDHwCwAx8ApAQAAD0GtADZAosAngLjAGQCiwDbArIA
AAUXAIcFFwDhBRcAlgUXAJwFFwBkBRcAngUXAI8FFwCoBRcAiwUXAIECsgDwArIAnga0ANkGtADZ
BrQA2QQ/AJMIAACHBXkAEAV9AMkFlgBzBikAyQUOAMkEmgDJBjMAcwYEAMkCXADJAlz/lgU/AMkE
dQDJBucAyQX8AMkGTABzBNMAyQZMAHMFjwDJBRQAhwTj//oF2wCyBXkAEAfpAEQFewA9BOP//AV7
AFwDHwCwArIAAAMfAMcGtADZBAD/7AQAAKoE5wB7BRQAugRmAHEFFABxBOwAcQLRAC8FFABxBRIA
ugI5AMECOf/bBKIAugI5AMEHywC6BRIAugTlAHEFFAC6BRQAcQNKALoEKwBvAyMANwUSAK4EvAA9
BosAVgS8ADsEvAA9BDMAWAUXAQACsgEEBRcBAAa0ANkCiwAAAzUBNQUXAKwFFwCBBRcAXgUXAFIC
sgEEBAAAXAQAANcIAAEbA8UAcwTlAJ4GtADZAuMAZAgAARsEAADVBAAAwwa0ANkDNQBeAzUAYgQA
AXMFFwCuBRcAngKLANsEAAEjAzUAiQPFAGAE5QDBB8EAiQfBAIkHwQBiBD8AjwV5ABAFeQAQBXkA
EAV5ABAFeQAQBXkAEAfLAAgFlgBzBQ4AyQUOAMkFDgDJBQ4AyQJcADsCXACiAlz//gJcAAYGMwAK
BfwAyQZMAHMGTABzBkwAcwZMAHMGTABzBrQBGQZMAGYF2wCyBdsAsgXbALIF2wCyBOP//ATXAMkF
CgC6BOcAewTnAHsE5wB7BOcAewTnAHsE5wB7B9sAewRmAHEE7ABxBOwAcQTsAHEE7ABxAjn/xwI5
AJACOf/eAjn/9ATlAHEFEgC6BOUAcQTlAHEE5QBxBOUAcQTlAHEGtADZBOUASAUSAK4FEgCuBRIA
rgUSAK4EvAA9BRQAugS8AD0FeQAQBOcAewV5ABAE5wB7BXkAEATnAHsFlgBzBGYAcQWWAHMEZgBx
BZYAcwRmAHEFlgBzBGYAcQYpAMkFFABxBjMACgUUAHEFDgDJBOwAcQUOAMkE7ABxBQ4AyQTsAHEF
DgDJBOwAcQUOAMkE7ABxBjMAcwUUAHEGMwBzBRQAcQYzAHMFFABxBjMAcwUUAHEGBADJBRL/5QdU
AMkFjwB4Alz/5AI5/9MCXAADAjn/8gJc//UCOf/kAlwAsAI5AJYCXADJAjkAwQS4AMkEcgDBAlz/
lgI5/9sFPwDJBKIAugSiALoEdQDJAjkAwQR1AMkCOQCIBHUAyQMAAMEEdQDJArwAwQR///ICRgAC
BfwAyQUSALoF/ADJBRIAugX8AMkFEgC6BoIAzQX8AMkFEgC6BkwAcwTlAHEGTABzBOUAcQZMAHME
5QBxCI8AcwgvAHEFjwDJA0oAugWPAMkDSgCCBY8AyQNKALoFFACHBCsAbwUUAIcEKwBvBRQAhwQr
AG8FFACHBCsAbwTj//oDIwA3BOP/+gMjADcE4//6AyMANwXbALIFEgCuBdsAsgUSAK4F2wCyBRIA
rgXbALIFEgCuBdsAsgUSAK4F2wCyBRIArgfpAEQGiwBWBOP//AS8AD0E4//8BXsAXAQzAFgFewBc
BDMAWAV7AFwEMwBYAtEALwI5/9sCiwCyBAAAwQQAAMEEAADHBAAA7gQAAUwEAAC2BAAA8AAA/aIA
APzFAAD+HwAA/WoEAABkCAAAZAKLAK4CiwCyBCUArgQlAK4EuAEzCAAA7AFW/okDNQA/BRcAAAgA
AScEAACwBAAAsAQAALAEAACwBAACjQQAALAEAACwBAAAsAQAALAEAACwBAACjQQAALAGAAKjBgAA
qAYAAqMGAAKjBgAAqAYAAqMGAACoAAD/ufzX/XP8tv0M/M/8z/zH/Zr85gAAAAEAAAdt/h0AAA7+
99b6UQ5ZAAEAAAAAAAAAAAAAAAAAAAFtAAEEDgGQAAUAAAUzBZkAAAEeBTMFmQAAA9cAZgISAAAC
CwYDAwgEAgIEgAAABwAAAAoAAAAAAAAAAFBmRWQAQAAgISIGFP4UAZoHbQHjAAAAkwAAAAAAAAAA
AAIAAAADAAAAFAADAAEAAAAUAAQAYAAAABQAEAADAAQAfgF/IBQgGSAdICIgJiCsISL//wAAACAA
oCATIBggHCAiICYgrCEi////4f/A4TrhN+E14THhLuCr4DYAAQAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAABwBaAAMAAQQJAAABMAAAAAMAAQQJAAEAFgEwAAMAAQQJAAIACAFGAAMAAQQJAAMAFgEwAAMA
AQQJAAQAFgEwAAMAAQQJAAUAGAFOAAMAAQQJAAYAFAFmAEMAbwBwAHkAcgBpAGcAaAB0ACAAKABj
ACkAIAAyADAAMAAzACAAYgB5ACAAQgBpAHQAcwB0AHIAZQBhAG0ALAAgAEkAbgBjAC4AIABBAGwA
bAAgAFIAaQBnAGgAdABzACAAUgBlAHMAZQByAHYAZQBkAC4ACgBDAG8AcAB5AHIAaQBnAGgAdAAg
ACgAYwApACAAMgAwADAANgAgAGIAeQAgAFQAYQB2AG0AagBvAG4AZwAgAEIAYQBoAC4AIABBAGwA
bAAgAFIAaQBnAGgAdABzACAAUgBlAHMAZQByAHYAZQBkAC4ACgBEAGUAagBhAFYAdQAgAGMAaABh
AG4AZwBlAHMAIABhAHIAZQAgAGkAbgAgAHAAdQBiAGwAaQBjACAAZABvAG0AYQBpAG4ACgBEAGUA
agBhAFYAdQAgAFMAYQBuAHMAQgBvAG8AawBWAGUAcgBzAGkAbwBuACAAMgAuADMANwBEAGUAagBh
AFYAdQBTAGEAbgBzAAMAAAAAAAD/2ABaAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACAAgAAv//AAMA
AQAAAAwAAAAAAAAAAgADAAEBPwABAU0BVAABAVcBawABAAAAAQAAAAoAHAAeAAFERkxUAAgABAAA
AAD//wAAAAAAAAABAAAACgCSAJQAFERGTFQAemFyYWIAhGFybW4AhGJyYWkAhGNhbnMAhGNoZXIA
hGN5cmwAhGdlb3IAhGdyZWsAhGhhbmkAhGhlYnIAhGthbmEAhGxhbyAAhGxhdG4AhG1hdGgAhG5r
byAAhG9nYW0AhHJ1bnIAhHRmbmcAhHRoYWkAhAAEAAAAAP//AAAAAAAAAAAAAAAAAAEAAAAKAOAA
6ABQADwMAAfdAAAAAAKCAAAEYAAABdUAAAAAAAAEYAAAAAAAAAAAAAAAAAAABGAAAAAAAAABaAAA
BGAAAABVAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEOAAACdgAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAWgAAAQ4AAABaAAAAWgAAAQ4AAAAAAAAAAAAAAQ4AAABa
AAAAWgAAAQ4AAABaAAAAWgAAAFoAAAFyAAAAWgAAAFoAAAI4AAD7jwAAADwAAAAAAAAAAAAoAB4A
MAAHAAMAOgBiAIoAsgDaARYBNAFwAY4BrAABAAcACQAKADwAPgBcAF0AXgABAAMAHgBAAG8ABAAA
AAAAAAADAVsAAAAoCXUAAAFaACgAKAmNAAEBWQAoAAAJlgAAAAQAAAAAAAAAAwFeAAAAKAl1AAAB
XQAoACgJjQABAVwAKAAACZYAAAAEAAAAAAAAAAMBYQAAACgJdQAAAWAAKAAoCY0AAQFfACgAAAlx
AAAABAAAAAAAAAADAWQAAAAoCWYAAAFjACgAKAl+AAEBYgAoAAAJcQAAAAQAAAAAAAAABQFnAAAA
KAlyAAABaAAoACgJmAABAWYAKAAoCYoAAAFoACgAKAmYAAEBZQAoAAAJgwAAAAQAAAAAAAAAAgBd
AAAAKAgAAAAAXQAoAAAIAAABAAQAAAAAAAAABQFrAAAAKAlyAAABaAAoACgJmAABAWoAKAAoCYoA
AAFoACgAKAmYAAEBaQAoAAAJgwAAAAQAAAAAAAAAAgAeAAAAKAUCAAAAHgAoAAAFAgABAAQAAAAA
AAAAAgBAAAAAKAQoAAAAQAAoAAAEKAABAAQAAAAAAAAAAgBvAAAAKAJWAAAAbwAoAAACVgABAAA=
"""
_CAPTURA_FUENTE_NEGRITA_B64 = """
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
EIsQuhDaEQMRPRFMEX8RmhGoEckR3xINEi8SPxJPEl8SlxKjEq8SuxLHEtMTDhM4E0QTUBNcE2gT
dBOAE4wTmBOkE9sT5xPzE/8UCxQXFCMURRSPFJsUpxSzFL8UyxTzFTwVSBVUFWAVbBV4FYQV4xXv
FfsWBxYTFh8WKxY3FkMWTxaWFqIWrha6FsYW0hbeFvoXQxdPF1sXZxdzF38XsBe8F8gX1BfgF+wX
+BgEGBAYHBgoGDQYQBhMGFgYZBhwGHwYhBi+GMoY1hjiGO4Y+hkGGRIZHhkqGTYZQhlOGVoZZhly
GX4ZihmWGaIZrhnWGggaFBogGiwaOBpEGlAaWxpmGnIagBqMGpgapBqwGrwayBrkGvAa/BsIGxQb
IBssGzgbRBtiG3wbiBuTG58bqxu3G8Mbzxv/HDgcRBxQHFwcaBx0HIAcuR0NHRkdJB0wHTwdSB1T
HV8dah12HYEdjR2YHaQdsB28Hccd0x3fHf0eLB44HkQeUB5cHmgedB6AHowemB6kHrAevB7IHtQe
4B7sHvgfBB8PHxsfJx8zHz4fWh9wH3gfih+eH7gf3h/+IC0gQyBRIFogaCB2IIQgkiCkILUg0iDv
IRghNiFDIV8hrSHSIeAh8yICIjciRiJYImsihSKTIqoAAQAAAAJeuH0iZLBfDzz1AB8IAAAAAADg
+tE5AAAAAOD60Tn3cvyuD80JZwABAAgAAgAAAAAAAATNAGYCyQAAA6YBHwQrAMMGtACLBZEAoAgE
AEIG+gB7AnMAwwOoALADqACkBC8AKQa0ANkDCgBtA1IAbwMKANEC7AAABZEAYgWRAOcFkQCiBZEA
iQWRAFwFkQCeBZEAfwWRAIkFkQB9BZEAagMzAOUDMwCBBrQA2Qa0ANkGtADZBKQAjQgAAIcGMQAK
BhkAvAXfAGYGpAC8BXcAvAV3ALwGkQBmBrIAvAL6ALwC+v+NBjMAvAUZALwH9gC8BrIAvAbNAGYF
3QC8Bs0AZgYpALwFwwCTBXUACgZ/ALwGMQAKCNMAPQYrACcFy//sBc0AXAOoALAC7AAAA6gAiwa0
AM8EAAAABAAAXgVmAFgFugCsBL4AWAW6AFwFbQBYA3sAJwW6AFwFsgCsAr4ArAK+/7wFUgCsAr4A
rAhWAKoFsgCsBX8AWAW6AKwFugBcA/IArATDAGoD0wAbBbIAoAU3AB8HZABIBSkAHwU3ABkEqABc
BbIBAALsAQQFsgEABrQA2QLJAAADpgEfBZEArgWRAH0FFwBKBZEAGQLsAQQEAAAOBAAAxQgAARsE
gwCeBSsAnga0ANkDUgBvCAABGwQAAMUEAACyBrQA2QOBAG0DgQBaBAABbQXjAK4FFwCBAwoA0QQA
AQYDgQB7BIMAdQUrAMEISABkCEgAZAhIAGgEpACNBjEACgYxAAoGMQAKBjEACgYxAAoGMQAKCK4A
AAXfAGYFdwC8BXcAvAV3ALwFdwC8AvoAFgL6ALwC+gADAvoAQQa0ACEGsgC8Bs0AZgbNAGYGzQBm
Bs0AZgbNAGYGtAEABs0ALQZ/ALwGfwC8Bn8AvAZ/ALwFy//sBecAvAXBAKwFZgBYBWYAWAVmAFgF
ZgBYBWYAWAVmAFgIYgBYBL4AWAVtAFgFbQBYBW0AWAVtAFgCvv/VAr4ArAK+/+UCvgAjBX8AWAWy
AKwFfwBYBX8AWAV/AFgFfwBYBX8AWAa0ANkFfwBOBbIAoAWyAKAFsgCgBbIAoAU3ABkFugCsBTcA
GQYxAAoFZgBYBjEACgVmAFgGMQAKBWYAWAXfAGYEvgBYBd8AZgS+AFgF3wBmBL4AWAXfAGYEvgBY
BqQAvAW6AFwGtAAhBboAXAV3ALwFbQBYBXcAvAVtAFgFdwC8BW0AWAV3ALwFbQBYBXcAvAVtAFgG
kQBmBboAXAaRAGYFugBcBpEAZgW6AFwGkQBmBboAXAayALwFsv/tB8oAvAZSAKYC+gAgAr4AAwL6
AEECvgAkAvoALAK+AA8C+gC8Ar4ArAL6ALwCvgCsBfQAvAV8AKwC+v+NAr7/vAYzALwFUgCsBVIA
rAUZALwCvgCsBRkAvAK+AJEFGQC8A9YArAUZALwEdACsBSP/pAL4/9sGsgC8BbIArAayALwFsgCs
BrIAvAWyAKwH3QBpBrIArAWyAKwGzQBmBX8AWAbNAGYFfwBYBs0AZgV/AFgJVgBmCMEAWAYpALwD
8gCsBikAvAPyAJEGKQC8A/IArAXDAJMEwwBqBcMAkwTDAGoFwwCTBMMAagXDAJMEwwBqBXUACgPT
ABsFdQAKA9MAGwV1AAoD0wAbBn8AvAWyAKAGfwC8BbIAoAZ/ALwFsgCgBn8AvAWyAKAGfwC8BbIA
oAZ/ALwFsgCgCNMAPQdkAEgFy//sBTcAGQXL/+wFzQBcBKgAXAXNAFwEqABcBc0AXASoAFwDewAn
Ar7/vAMKAIEEAACHBAAAhwQAALAEAADjBAABVgQAAKQEAADBAAD9SwAA/IUAAP3UAAD9YgQAAG4I
AABuAwoA0wMKAIEFQgDTBUIAvAUdAScIAACiAVb+aAOBADgFkf/ZCAABJwAA/W38xf1t/KT8svyH
/If8sP13/KAAAAABAAAHbf4dAAAQIfdy+TIPzQABAAAAAAAAAAAAAAAAAAABWgABBJUCvAAFAAAF
MwWZAAABHgUzBZkAAAPXAGYCEgAAAgsIAwMGBAICBIAAAAcAAAAKAAAAAAAAAABQZkVkACAAICEi
BhT+FAGaB20B4wAAAJMAAAAAAAAAAAACAAAAAwAAABQAAwABAAAAFAAEAGAAAAAUABAAAwAEAH4B
fyAUIBkgHSAiICYgrCEi//8AAAAgAKAgEyAYIBwgIiAmIKwhIv///+H/wOE64TfhNeEx4S7gq+A2
AAEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAcAWgADAAEECQAAATAAAAADAAEECQABABYBMAADAAEE
CQACAAgBRgADAAEECQADACABTgADAAEECQAEACABTgADAAEECQAFABgBbgADAAEECQAGAB4BhgBD
AG8AcAB5AHIAaQBnAGgAdAAgACgAYwApACAAMgAwADAAMwAgAGIAeQAgAEIAaQB0AHMAdAByAGUA
YQBtACwAIABJAG4AYwAuACAAQQBsAGwAIABSAGkAZwBoAHQAcwAgAFIAZQBzAGUAcgB2AGUAZAAu
AAoAQwBvAHAAeQByAGkAZwBoAHQAIAAoAGMAKQAgADIAMAAwADYAIABiAHkAIABUAGEAdgBtAGoA
bwBuAGcAIABCAGEAaAAuACAAQQBsAGwAIABSAGkAZwBoAHQAcwAgAFIAZQBzAGUAcgB2AGUAZAAu
AAoARABlAGoAYQBWAHUAIABjAGgAYQBuAGcAZQBzACAAYQByAGUAIABpAG4AIABwAHUAYgBsAGkA
YwAgAGQAbwBtAGEAaQBuAAoARABlAGoAYQBWAHUAIABTAGEAbgBzAEIAbwBsAGQARABlAGoAYQBW
AHUAIABTAGEAbgBzACAAQgBvAGwAZABWAGUAcgBzAGkAbwBuACAAMgAuADMANwBEAGUAagBhAFYA
dQBTAGEAbgBzAC0AQgBvAGwAZAAAAAMAAAAAAAD/2ABaAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAC
AAgAAv//AAMAAQAAAAwAAAAAAAAAAgADAAEBPwABAU0BVAABAVcBWAABAAAAAQAAAAoAHAAeAAFE
RkxUAAgABAAAAAD//wAAAAAAAAABAAAACgCSAJQAFERGTFQAemFyYWIAhGFybW4AhGJyYWkAhGNh
bnMAhGNoZXIAhGN5cmwAhGdlb3IAhGdyZWsAhGhhbmkAhGhlYnIAhGthbmEAhGxhbyAAhGxhdG4A
hG1hdGgAhG5rbyAAhG9nYW0AhHJ1bnIAhHRmbmcAhHRoYWkAhAAEAAAAAP//AAAAAAAAAAAAAAAA
"""
_CAPTURA_CACHE_FUENTES = {}


def _captura_fuente(tamano, negrita=False):
    """Devuelve (con caché) la fuente de -ver-img en el tamaño pedido --
    ver el comentario de _CAPTURA_FUENTE_REGULAR_B64 de arriba sobre por
    qué está incrustada en vez de usar la de Pillow por defecto."""
    clave = (tamano, negrita)
    fuente = _CAPTURA_CACHE_FUENTES.get(clave)
    if fuente is None:
        from PIL import ImageFont
        b64 = _CAPTURA_FUENTE_NEGRITA_B64 if negrita else _CAPTURA_FUENTE_REGULAR_B64
        fuente = ImageFont.truetype(io.BytesIO(base64.b64decode(b64)), tamano)
        _CAPTURA_CACHE_FUENTES[clave] = fuente
    return fuente


class _HTMLaFlujoCaptura(HTMLParser):
    """Como _HTMLaTexto, pero en vez de aplanar todo a texto, conserva la
    ESTRUCTURA que hace falta para dibujar -ver-img: una lista de
    'bloques', cada uno {"tipo": "parrafo", "runs": [(texto, negrita,
    enlace), ...]} o {"tipo": "imagen", "src": "..."} -- en el ORDEN en
    que aparecen, para que una imagen incrustada a mitad del correo salga
    dibujada a mitad del correo, no al principio ni al final. Mismo
    tratamiento de enlaces que _HTMLaTexto (añade la URL entre paréntesis
    tras el texto visible, salvo que ya la contenga; mismos esquemas
    útiles -- _ESQUEMAS_ENLACE_UTILES)."""

    _BLOQUE = ("p", "div", "tr", "li", "br", "h1", "h2", "h3", "h4", "h5", "h6", "table", "blockquote")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.bloques = []
        self._runs_actuales = []
        self._negrita = 0
        self._omitir = False
        self._href_actual = None
        self._inicio_texto_ancla = None

    def _cerrar_parrafo(self):
        if any(r[0].strip() for r in self._runs_actuales):
            self.bloques.append({"tipo": "parrafo", "runs": self._runs_actuales})
        self._runs_actuales = []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag in ("script", "style"):
            self._omitir = True
        elif tag in self._BLOQUE:
            self._cerrar_parrafo()
        elif tag in ("b", "strong"):
            self._negrita += 1
        elif tag == "img":
            self._cerrar_parrafo()
            src = (d.get("src") or "").strip()
            if src:
                self.bloques.append({"tipo": "imagen", "src": src})
        elif tag == "a" and self._href_actual is None:
            href = (d.get("href") or "").strip()
            if href.lower().startswith(_ESQUEMAS_ENLACE_UTILES):
                self._href_actual = href
                self._inicio_texto_ancla = len(self._runs_actuales)

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._omitir = False
        elif tag in self._BLOQUE:
            self._cerrar_parrafo()
        elif tag in ("b", "strong"):
            self._negrita = max(0, self._negrita - 1)
        elif tag == "a" and self._href_actual is not None:
            texto_visible = "".join(r[0] for r in self._runs_actuales[self._inicio_texto_ancla:])
            if self._href_actual not in texto_visible:
                self._runs_actuales.append((f" ({self._href_actual})", bool(self._negrita), True))
            for i in range(self._inicio_texto_ancla, len(self._runs_actuales)):
                texto, neg, _ = self._runs_actuales[i]
                self._runs_actuales[i] = (texto, neg, True)
            self._href_actual = None
            self._inicio_texto_ancla = None

    def handle_data(self, data):
        if not self._omitir and data:
            self._runs_actuales.append((data, bool(self._negrita), self._href_actual is not None))

    def fin(self):
        self._cerrar_parrafo()
        return self.bloques


def _captura_flujo_desde_html(html_contenido):
    parser = _HTMLaFlujoCaptura()
    try:
        parser.feed(html_contenido)
    except Exception:
        pass
    return parser.fin()


def _captura_runs_desde_texto_con_urls(texto):
    """Para texto plano (sin HTML): reutiliza _pdf_detectar_urls (misma
    detección de URLs sueltas que ya usa el PDF) para que un enlace https
    pelado en un correo de puro texto también salga resaltado como
    enlace, no solo cuando viene marcado con <a> en HTML."""
    runs = []
    pos = 0
    for inicio, fin, _url in _pdf_detectar_urls(texto):
        if inicio > pos:
            runs.append((texto[pos:inicio], False, False))
        runs.append((texto[inicio:fin], False, True))
        pos = fin
    if pos < len(texto):
        runs.append((texto[pos:], False, False))
    return runs or [(texto, False, False)]


def _captura_flujo_desde_texto_plano(texto):
    """Para correos sin parte HTML: cada párrafo (separado por una o más
    líneas en blanco) es un bloque de texto sin negrita, con las URLs
    sueltas coloreadas como enlace (ver _captura_runs_desde_texto_con_urls)."""
    bloques = []
    for parrafo in re.split(r"\n\s*\n", texto.strip()):
        parrafo = parrafo.strip()
        if parrafo:
            bloques.append({"tipo": "parrafo", "runs": _captura_runs_desde_texto_con_urls(parrafo)})
    return bloques


def _captura_tokens_de_parrafo(runs):
    """runs (texto, negrita, enlace) -> tokens (palabra, negrita, enlace,
    pegar), partiendo por espacios. 'pegar'=True significa "sin espacio
    respecto al token anterior": pasa cuando dos runs consecutivos se
    tocan sin ningún espacio en blanco de por medio -- típicamente una
    frase en negrita seguida AL VUELO de puntuación en texto normal, p.ej.
    '<b>reserva</b>.' -- sin esto, se vería 'reserva .' con un espacio de
    más que no estaba en el correo original."""
    tokens = []
    pegar_siguiente = False  # el run anterior (no vacío) ¿terminaba SIN espacio en blanco?
    for texto, negrita, enlace in runs:
        if texto == "":
            continue
        sin_espacio_inicial = not texto[0].isspace()
        sin_espacio_final = not texto[-1].isspace()
        palabras = texto.split()
        for i, palabra in enumerate(palabras):
            pegar = bool(tokens) and i == 0 and pegar_siguiente and sin_espacio_inicial
            tokens.append((palabra, negrita, enlace, pegar))
        pegar_siguiente = bool(palabras) and sin_espacio_final
    return tokens


def _captura_envolver_tokens(draw, tokens, fuente_n, fuente_b, ancho_max):
    """Devuelve una lista de líneas; cada línea es una lista de (palabra,
    negrita, enlace, x) -- ajuste de línea por ANCHURA DE PÍXEL de verdad
    (draw.textlength), no por número de caracteres (_pdf_envolver_texto sí
    puede hacerlo por caracteres porque en un PDF el texto lo dibuja el
    propio lector; aquí se dibuja píxel a píxel, así que hace falta medir)."""
    lineas = []
    linea_actual = []
    x = 0
    espacio_n = draw.textlength(" ", font=fuente_n)
    for palabra, negrita, enlace, pegar in tokens:
        fnt = fuente_b if negrita else fuente_n
        ancho_palabra = draw.textlength(palabra, font=fnt)
        espacio_requerido = 0 if pegar else espacio_n
        necesita = (espacio_requerido if linea_actual else 0) + ancho_palabra
        if linea_actual and x + necesita > ancho_max:
            lineas.append(linea_actual)
            linea_actual = []
            x = 0
        if linea_actual:
            x += espacio_requerido
        linea_actual.append((palabra, negrita, enlace, x))
        x += ancho_palabra
    if linea_actual:
        lineas.append(linea_actual)
    return lineas


def _html_crudo_del_mensaje(msg):
    """Devuelve el HTML ORIGINAL (sin convertir a texto) de la primera
    parte text/html del mensaje, o None si no tiene ninguna (correo de
    puro texto plano). A diferencia de texto_cuerpo() -- que ya convierte
    el HTML a texto plano para buscar/mostrar --, -ver-img necesita el
    HTML tal cual para poder reproducir su estructura (negrita, enlaces,
    imágenes en su sitio)."""
    for parte in msg.walk():
        if parte.is_multipart():
            continue
        if parte.get_content_type() == "text/html":
            try:
                contenido = parte.get_content()
            except Exception:
                continue
            if isinstance(contenido, str):
                return contenido
    return None


def _parte_por_content_id(msg, cid):
    """Busca la parte MIME cuya cabecera Content-ID sea 'cid' (con o sin
    los símbolos < > que normalmente la rodean) -- para resolver
    referencias cid:... de <img src="cid:..."> a la imagen incrustada de
    verdad. None si no se encuentra."""
    objetivo = cid.strip().strip("<>").lower()
    if not objetivo:
        return None
    for parte in msg.walk():
        if parte.is_multipart():
            continue
        content_id = str(parte.get("Content-ID", "") or "").strip().strip("<>").lower()
        if content_id and content_id == objetivo:
            return parte
    return None


def _resolver_imagen_para_captura(msg, src, con_remotas):
    """Convierte la 'src' de un <img> a sus bytes de verdad. Devuelve
    (bytes, None) si se consigue, o (None, motivo) si no -- para dibujar
    un aviso explicado en su lugar en vez de dejar un hueco sin más.

    - cid:xxx             imagen incrustada en el propio correo: siempre.
    - data:...;base64,xxx imagen incrustada directamente en el HTML: siempre.
    - http(s)://...       imagen en un servidor AJENO: SOLO si con_remotas
                          es True (-con-remotas) -- igual que "mostrar
                          imágenes" bloqueado por defecto en cualquier
                          cliente de correo, para no avisar sin querer a
                          quien mandó el correo de que lo has abierto, ni
                          contactar ningún servidor ajeno sin pedirlo tú
                          expresamente.
    - cualquier otra cosa: no soportado."""
    src = (src or "").strip()
    if not src:
        return None, "sin dirección de imagen"

    if src.lower().startswith("cid:"):
        parte = _parte_por_content_id(msg, src[4:])
        if parte is None:
            return None, "imagen incrustada no encontrada en el correo"
        try:
            datos = parte.get_payload(decode=True)
        except Exception:
            datos = None
        if not datos:
            return None, "no se pudo leer la imagen incrustada"
        return datos, None

    if src.lower().startswith("data:"):
        try:
            cabecera, datos_b64 = src.split(",", 1)
            if "base64" not in cabecera.lower():
                return None, "imagen 'data:' sin codificar en base64 (no soportado)"
            return base64.b64decode(datos_b64), None
        except Exception:
            return None, "no se pudo decodificar la imagen 'data:'"

    if src.lower().startswith(("http://", "https://")):
        if not con_remotas:
            return None, "imagen remota bloqueada (usa -con-remotas para descargarla)"
        try:
            peticion = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(peticion, timeout=_CAPTURA_TIMEOUT_REMOTA) as resp:
                datos = resp.read(_CAPTURA_LIMITE_BYTES_REMOTA + 1)
            if len(datos) > _CAPTURA_LIMITE_BYTES_REMOTA:
                return None, f"imagen remota demasiado grande (más de {_CAPTURA_LIMITE_BYTES_REMOTA // (1024 * 1024)} MB)"
            return datos, None
        except Exception as e:
            return None, f"no se pudo descargar la imagen remota ({e})"

    return None, "tipo de imagen no soportado"


def _captura_truncar_a_ancho(draw, texto, fuente, ancho_max):
    """Recorta 'texto' (si hace falta) para que quepa en ancho_max píxeles
    con 'fuente', añadiendo "…" al final -- usado para el aviso dentro del
    recuadro de "imagen no disponible" (una sola línea de alto fijo: el
    motivo del fallo, p.ej. un mensaje de excepción al descargar una
    imagen remota, puede ser arbitrariamente largo)."""
    if draw.textlength(texto, font=fuente) <= ancho_max:
        return texto
    sufijo = "…"
    recortado = texto
    while recortado and draw.textlength(recortado + sufijo, font=fuente) > ancho_max:
        recortado = recortado[:-1]
    return (recortado.rstrip() + sufijo) if recortado else sufijo


def _dibujar_captura(id_msg, de, para, asunto, fecha, bloques, resolver_imagen, alto_maximo):
    """Dos pasadas (como es habitual al maquetar sin saber de antemano
    cuánto ocupará el contenido): 1ª mide cada elemento (líneas de texto
    ya ajustadas al ancho, imágenes ya redimensionadas) sobre un lienzo de
    1x1 que no se llega a guardar, para saber la altura TOTAL antes de
    crear el lienzo definitivo del tamaño justo; 2ª dibuja de verdad sobre
    ese lienzo ya del tamaño correcto.

    alto_maximo: deja de añadir MÁS bloques en cuanto el contenido ya
    generado llega a esta altura, en vez de procesar/descargar/
    redimensionar el correo COMPLETO aunque solo se hayan pedido 1 o 2
    pantallas de él con -pantallas -- lo que sobre de más no hace falta:
    recortar_en_pantallas tampoco lo mostraría, y así tampoco se
    descargan (con -con-remotas) imágenes remotas que nunca se llegarían
    a ver."""
    from PIL import Image, ImageDraw

    fuente_n = _captura_fuente(TAMANO_FUENTE_CAPTURA)
    fuente_b = _captura_fuente(TAMANO_FUENTE_CAPTURA, negrita=True)
    fuente_cab = _captura_fuente(TAMANO_FUENTE_CAPTURA + 4)
    lienzo_medida = Image.new("RGB", (1, 1))
    draw_medida = ImageDraw.Draw(lienzo_medida)
    ancho_util = ANCHO_CAPTURA_PANTALLA - 2 * _CAPTURA_MARGEN
    alto_fila_cabecera = TAMANO_FUENTE_CAPTURA + 4 + _CAPTURA_ALTO_EXTRA_LINEA
    alto_fila_texto = TAMANO_FUENTE_CAPTURA + _CAPTURA_ALTO_EXTRA_LINEA

    elementos = []  # (tipo, y, contenido)
    y = _CAPTURA_MARGEN
    for etiqueta, valor in (("ID", id_msg), ("De", de), ("Para", para), ("Asunto", asunto), ("Fecha", fecha)):
        elementos.append(("cabecera", y, f"{etiqueta}: {valor}"))
        y += alto_fila_cabecera
    elementos.append(("linea", y, None))
    y += 16

    for bloque in bloques:
        if y >= alto_maximo:
            break
        if bloque["tipo"] == "parrafo":
            tokens = _captura_tokens_de_parrafo(bloque["runs"])
            if not tokens:
                continue
            lineas = _captura_envolver_tokens(draw_medida, tokens, fuente_n, fuente_b, ancho_util)
            for linea in lineas:
                elementos.append(("linea_texto", y, linea))
                y += alto_fila_texto
            y += _CAPTURA_ALTO_EXTRA_LINEA
        elif bloque["tipo"] == "imagen":
            datos, motivo = resolver_imagen(bloque["src"])
            if datos is None:
                elementos.append(("imagen_fallo", y, motivo or "no disponible"))
                y += 50
                continue
            try:
                img = Image.open(io.BytesIO(datos))
                img.load()
                if img.mode in ("RGBA", "LA", "P"):
                    fondo = Image.new("RGB", img.size, _CAPTURA_COLOR_FONDO)
                    img_rgba = img.convert("RGBA")
                    fondo.paste(img_rgba, mask=img_rgba.split()[-1])
                    img = fondo
                else:
                    img = img.convert("RGB")
            except Exception as e:
                elementos.append(("imagen_fallo", y, f"no se pudo abrir ({e})"))
                y += 50
                continue
            w, h = img.size
            if w > ancho_util:
                h = max(1, int(h * (ancho_util / w)))
                img = img.resize((ancho_util, h))
            elementos.append(("imagen", y, img))
            y += h + 14

    alto_total = y + _CAPTURA_MARGEN
    lienzo = Image.new("RGB", (ANCHO_CAPTURA_PANTALLA, alto_total), _CAPTURA_COLOR_FONDO)
    draw = ImageDraw.Draw(lienzo)
    for tipo, ey, contenido in elementos:
        if tipo == "cabecera":
            draw.text((_CAPTURA_MARGEN, ey), contenido, font=fuente_cab, fill=_CAPTURA_COLOR_TEXTO)
        elif tipo == "linea":
            draw.line(
                (_CAPTURA_MARGEN, ey + 6, ANCHO_CAPTURA_PANTALLA - _CAPTURA_MARGEN, ey + 6),
                fill=(200, 200, 200), width=2,
            )
        elif tipo == "linea_texto":
            for palabra, negrita, enlace, x in contenido:
                fnt = fuente_b if negrita else fuente_n
                color = _CAPTURA_COLOR_ENLACE if enlace else _CAPTURA_COLOR_TEXTO
                draw.text((_CAPTURA_MARGEN + x, ey), palabra, font=fnt, fill=color)
        elif tipo == "imagen":
            lienzo.paste(contenido, (_CAPTURA_MARGEN, ey))
        elif tipo == "imagen_fallo":
            draw.rectangle(
                (_CAPTURA_MARGEN, ey, ANCHO_CAPTURA_PANTALLA - _CAPTURA_MARGEN, ey + 36),
                outline=_CAPTURA_COLOR_GRIS,
            )
            ancho_caja_texto = (ANCHO_CAPTURA_PANTALLA - 2 * _CAPTURA_MARGEN) - 16
            texto_caja = _captura_truncar_a_ancho(
                draw, f"[imagen no disponible: {contenido}]", fuente_n, ancho_caja_texto
            )
            draw.text(
                (_CAPTURA_MARGEN + 8, ey + 8), texto_caja,
                font=fuente_n, fill=_CAPTURA_COLOR_GRIS,
            )
    return lienzo


def _recortar_en_pantallas(lienzo, n_pedidas):
    """Corta el lienzo completo en hasta n_pedidas imágenes de alto
    ALTO_CAPTURA_PANTALLA cada una (la 1ª es lo primero que se ve al abrir
    el correo; la 2ª continúa justo donde acababa la 1ª, como ir bajando
    la pantalla; etc.) -- menos de n_pedidas si el correo no da para
    tantas (la última pantalla parcial se recorta a lo que quede, en vez
    de dejar el resto en blanco)."""
    ancho, alto_total = lienzo.size
    pantallas = []
    for i in range(n_pedidas):
        y0 = i * ALTO_CAPTURA_PANTALLA
        if y0 >= alto_total:
            break
        y1 = min(y0 + ALTO_CAPTURA_PANTALLA, alto_total)
        pantallas.append(lienzo.crop((0, y0, ancho, y1)))
    return pantallas


def _renderizar_captura_mensaje(msg, con_remotas, n_pantallas):
    """Arma la imagen completa (sin recortar aún en pantallas) de un
    mensaje: cabecera ID/De/Para/Asunto/Fecha + el cuerpo -- con el HTML
    original si lo hay (negrita, enlaces e imágenes incrustadas en su
    sitio), o si no, el texto plano de siempre partido en párrafos (con
    los enlaces sueltos igualmente resaltados)."""
    html_crudo = _html_crudo_del_mensaje(msg)
    if html_crudo:
        bloques = _captura_flujo_desde_html(html_crudo)
    else:
        bloques = _captura_flujo_desde_texto_plano(texto_cuerpo_limpio(msg, recortar_urls=False))

    def resolver(src):
        return _resolver_imagen_para_captura(msg, src, con_remotas)

    de = _limpiar_direccion(msg.get("from", ""), 200)
    para = _limpiar_direccion(msg.get("to", ""), 200)
    asunto = _sanear_texto_pantalla(str(msg.get("subject", "") or "(sin asunto)"), una_linea=True)
    fecha = formatear_fecha(fecha_mensaje(msg))
    alto_maximo = n_pantallas * ALTO_CAPTURA_PANTALLA
    return _dibujar_captura(id_amigable(msg), de, para, asunto, fecha, bloques, resolver, alto_maximo)


def _validar_n_pantallas(valor_bruto):
    """Valida el valor de -pantallas (opcional; por defecto 1 si no se
    da): un entero mayor que 0. Avisa por stderr y devuelve None si no es
    válido -- sin sys.exit, para no cortar en seco otras acciones que se
    hayan pedido A LA VEZ en la misma orden (ver _ejecutar_una_accion_cli)."""
    if valor_bruto is None:
        return 1
    try:
        n = int(str(valor_bruto).strip())
    except ValueError:
        n = 0
    if n <= 0:
        _error(f"-pantallas necesita un número entero mayor que 0 (recibido: '{valor_bruto}').")
        return None
    return n


# Valores válidos de -si-existe (qué hacer si el fichero de destino ya
# existe, junto con -descargar/-eml/-ver-txt/-ver-pdf/-adjuntos -- ver
# _validar_modo_si_existe y _accion_si_existe):
#   "no"           si ya existe, no se toca (se deja tal cual).
#   "sobrescribir" si ya existe, se sobrescribe siempre.
#   "correo"       sobrescribe el .eml/.txt/.pdf si ya existe, pero deja
#                  tal cual los adjuntos que ya existan.
#   "adjuntos"     sobrescribe los adjuntos que ya existan, pero deja tal
#                  cual el .eml/.txt/.pdf si ya existe.
_VALORES_SI_EXISTE = ("no", "sobrescribir", "correo", "adjuntos")


def _validar_modo_si_existe(valor_bruto):
    """Valida el valor de -si-existe (opcional). Devuelve el modo
    normalizado -- "numerar" (el comportamiento de siempre: añadir _2,
    _3... en vez de sobrescribir o de omitir) si no se dio el flag. Avisa
    por stderr y devuelve None si el valor dado no es uno de
    _VALORES_SI_EXISTE -- sin sys.exit, mismo patrón que
    _validar_n_pantallas (para no cortar en seco otras acciones pedidas A
    LA VEZ en la misma orden)."""
    if valor_bruto is None:
        return "numerar"
    valor = str(valor_bruto).strip().lower()
    if valor not in _VALORES_SI_EXISTE:
        _error(f"-si-existe: valor no válido '{valor_bruto}' — usa uno de: {', '.join(_VALORES_SI_EXISTE)}.")
        return None
    return valor


def _guardar_capturas_pantalla(msg, carpeta_salida, carpeta_imap, n_pantallas, con_remotas):
    """-ver-img: genera y guarda hasta n_pantallas imágenes .png con el
    aspecto del correo al abrirlo -- <nombre_del_correo>_pantalla1.png,
    _pantalla2.png... (mismo <nombre_del_correo> que el .eml/.txt/.pdf de
    ese mensaje -- ver _nombre_base_mensaje). La 1ª es lo primero que se
    ve; cada una siguiente continúa justo donde acababa la anterior, como
    ir bajando con el dedo en la pantalla del móvil. Si el correo no da
    para tantas pantallas como se pidieron, genera menos y lo explica (no
    hay más contenido debajo). Devuelve la lista de rutas guardadas.

    con_remotas: ver _resolver_imagen_para_captura -- por defecto NO se
    descarga ninguna imagen remota (http/https), solo las incrustadas en
    el propio correo (cid:/data:), igual que "mostrar imágenes" bloqueado
    por defecto en cualquier cliente de correo."""
    try:
        from PIL import Image  # noqa: F401 -- solo para comprobar que Pillow está instalado
    except ImportError:
        _error(
            "-ver-img necesita la librería Pillow, que no parece estar instalada. "
            "Instálala con: pip install Pillow --break-system-packages "
            "(en Termux puede ir mejor: pkg install python-pillow)."
        )
        return []

    try:
        lienzo = _renderizar_captura_mensaje(msg, con_remotas, n_pantallas)
        pantallas = _recortar_en_pantallas(lienzo, n_pantallas)
    except Exception as e:
        _error(f"No se pudo generar la imagen de este mensaje: {e}")
        return []

    os.makedirs(carpeta_salida, exist_ok=True)
    base = _nombre_base_mensaje(msg, carpeta_imap)
    rutas = []
    for n, pantalla in enumerate(pantallas, 1):
        ruta = _ruta_sin_colision(os.path.join(carpeta_salida, f"{base}_pantalla{n}.png"))
        pantalla.save(ruta, format="PNG")
        print(f"Guardado: {ruta}")
        _registrar_fichero(msg, "img", ruta)
        rutas.append(ruta)

    faltantes = n_pantallas - len(pantallas)
    if faltantes > 0:
        palabra = "pantalla" if len(pantallas) == 1 else "pantallas"
        print(
            f"(este correo solo da para {len(pantallas)} {palabra}; no se "
            f"generaron {faltantes} más -- no hay más contenido debajo)"
        )
    return rutas


# ---------- -ver-pdf con imágenes incrustadas en su sitio ----------
# Pedido 2026-10-05/07 (verbatim): "cuando en un escrito se haya puesto por
# parte de quien lo envio, se haya puesto una imagen incrustada y yo genere
# un pdf con -ver-pdf no crees el pdf a partir del txt sino que sea como
# cuando generas una imgen que pones tanto el texto como la imagen del
# correo electronico porque ahora mismo estas, al generar un pdf, estas
# simplemente generandolo a partir del archivo de texto txt, olvidando las
# imagenes incrustadas en el correo electronico ademas pon, en la
# generacion del pdf, pon en el pdf todas las imagenes que se puedan ver en
# el correo, comparandolo con la version imagen, si 1 imagen esta en la
# version imagen, ponla en la version pdf pero en la misma ubicacion, para
# que se vea de la misma forma, modo y aspecto".
#
# Reutiliza EXACTAMENTE la misma extracción de contenido que -ver-img
# (_html_crudo_del_mensaje/_captura_flujo_desde_html/_captura_flujo_desde_
# texto_plano/_resolver_imagen_para_captura, todas más arriba en esta misma
# sección) para que una imagen que aparece en "la versión imagen" aparezca
# TAMBIÉN en el PDF, en la misma posición relativa al texto -- "misma
# forma, modo y aspecto" pedido.
#
# Diferencia a propósito frente a -ver-img: aquí el TEXTO sigue siendo
# texto de PDF de verdad (vectorial, seleccionable/copiable, no una
# captura), para conservar SIN REGRESIÓN los enlaces clicables de verdad
# que -ver-pdf ya tenía desde el pedido 2026-09-29 (#56) -- solo las
# imágenes se incrustan como tal: bytes RGB en crudo comprimidos con
# zlib/FlateDecode (el propio formato PDF lo soporta de forma nativa, sin
# pasar por ningún formato de fichero de imagen ni perder calidad por
# volver a comprimir como JPEG).
#
# Sin negrita en el texto: -ver-pdf nunca ha distinguido negrita (siempre
# Helvetica normal, igual que el .txt) y esto no lo cambia -- hubiera
# significado incrustar una 2ª fuente y cambiar de fuente por cada palabra
# sin que el pedido lo necesitara. Los enlaces SÍ se colorean igual que en
# -ver-img (_CAPTURA_COLOR_ENLACE), para acercar también el aspecto del
# texto al de "la versión imagen".

_PDF_IMG_HUECO = 14     # espacio tras una imagen incrustada, en puntos (mismo valor que -ver-img, en píxeles)
_PDF_PARRAFO_HUECO = 6  # espacio extra tras cada párrafo de texto, en puntos

# Anchos DE VERDAD de Helvetica (tabla AFM estándar de Adobe -- la misma
# métrica que usa cualquier lector de PDF para esta fuente base, sin
# incrustar nada), en milésimas de "em" -- clave: el byte WinAnsi/cp1252
# de cada carácter (lo que _pdf_escapar ya codifica). Hace falta esto (y
# no el ancho medio por carácter de _PDF_ANCHO_CARACTER, que SÍ basta
# para decidir solo saltos de línea con margen de sobra -- ver
# _pdf_envolver_texto) porque aquí cada palabra se posiciona A MANO (con
# Tm) para poder colorear/enlazar cada una por separado: con un ancho
# medio, una palabra con letras anchas (mayúsculas, "W", "M"...) se
# dibujaría más ancha de lo calculado y se solaparía con la siguiente
# (bug encontrado y arreglado durante el desarrollo de esta ronda,
# viendo el PDF ya generado). Cubre ASCII + el subconjunto de Latin-1/
# cp1252 de _CAPTURA_FUENTE_REGULAR_B64 (acentos/eñes/diéresis de
# español y otros idiomas europeos, símbolos de puntuación/moneda
# habituales); cualquier otro carácter usa _PDF_ANCHO_HELVETICA_DEFECTO.
_PDF_ANCHOS_HELVETICA = {
    32: 278, 33: 278, 34: 355, 35: 556, 36: 556, 37: 889, 38: 667, 39: 222, 40: 333, 41: 333,
    42: 389, 43: 584, 44: 278, 45: 333, 46: 278, 47: 278, 48: 556, 49: 556, 50: 556, 51: 556,
    52: 556, 53: 556, 54: 556, 55: 556, 56: 556, 57: 556, 58: 278, 59: 278, 60: 584, 61: 584,
    62: 584, 63: 556, 64: 1015, 65: 667, 66: 667, 67: 722, 68: 722, 69: 667, 70: 611, 71: 778,
    72: 722, 73: 278, 74: 500, 75: 667, 76: 556, 77: 833, 78: 722, 79: 778, 80: 667, 81: 778,
    82: 722, 83: 667, 84: 611, 85: 722, 86: 667, 87: 944, 88: 667, 89: 667, 90: 611, 91: 278,
    92: 278, 93: 278, 94: 469, 95: 556, 96: 222, 97: 556, 98: 556, 99: 500, 100: 556, 101: 556,
    102: 278, 103: 556, 104: 556, 105: 222, 106: 222, 107: 500, 108: 222, 109: 833, 110: 556,
    111: 556, 112: 556, 113: 556, 114: 333, 115: 500, 116: 278, 117: 556, 118: 500, 119: 722,
    120: 500, 121: 500, 122: 500, 123: 334, 124: 260, 125: 334, 126: 584, 128: 556, 130: 222,
    131: 556, 132: 333, 133: 1000, 134: 556, 135: 556, 136: 333, 137: 1000, 138: 667, 139: 333,
    140: 1000, 142: 611, 145: 222, 146: 222, 147: 333, 148: 333, 149: 350, 150: 556, 151: 1000,
    152: 333, 153: 1000, 154: 500, 155: 333, 156: 944, 158: 500, 159: 667, 161: 333, 162: 556,
    163: 556, 164: 556, 165: 556, 166: 260, 167: 556, 168: 333, 169: 737, 170: 370, 171: 556,
    172: 584, 173: 333, 174: 737, 175: 333, 176: 400, 177: 584, 178: 333, 179: 333, 180: 333,
    181: 556, 182: 537, 183: 278, 184: 333, 185: 333, 186: 365, 187: 556, 188: 834, 189: 834,
    190: 834, 191: 611, 192: 667, 193: 667, 194: 667, 195: 667, 196: 667, 197: 667, 198: 1000,
    199: 722, 200: 667, 201: 667, 202: 667, 203: 667, 204: 278, 205: 278, 206: 278, 207: 278,
    208: 722, 209: 722, 210: 778, 211: 778, 212: 778, 213: 778, 214: 778, 215: 584, 216: 778,
    217: 722, 218: 722, 219: 722, 220: 722, 221: 667, 222: 667, 223: 611, 224: 556, 225: 556,
    226: 556, 227: 556, 228: 556, 229: 556, 230: 889, 231: 500, 232: 556, 233: 556, 234: 556,
    235: 556, 236: 278, 237: 278, 238: 278, 239: 278, 240: 556, 241: 556, 242: 556, 243: 556,
    244: 556, 245: 556, 246: 556, 247: 584, 248: 611, 249: 556, 250: 556, 251: 556, 252: 556,
    253: 500, 254: 556, 255: 500,
}
_PDF_ANCHO_HELVETICA_DEFECTO = 556  # ancho medio para cualquier carácter fuera de la tabla


def _pdf_ancho_texto(texto, tam_fuente):
    """Ancho aproximado, en PUNTOS, de 'texto' dibujado en Helvetica a
    'tam_fuente' -- suma el ancho real de cada carácter (_PDF_ANCHOS_
    HELVETICA, en milésimas de em) tal como se codifica para dibujarlo
    (_pdf_escapar: cp1252). Usada tanto para decidir los saltos de línea
    como para posicionar cada palabra -- con la MISMA función para las
    dos cosas, nunca se desacuerdan entre sí."""
    try:
        datos = texto.encode("cp1252", errors="replace")
    except Exception:
        datos = texto.encode("ascii", errors="replace")
    milesimas = sum(_PDF_ANCHOS_HELVETICA.get(b, _PDF_ANCHO_HELVETICA_DEFECTO) for b in datos)
    return milesimas * tam_fuente / 1000


def _pdf_tokens_de_parrafo(runs):
    """runs == [(texto, negrita, enlace), ...] (de _captura_flujo_desde_
    html/_captura_flujo_desde_texto_plano/_captura_runs_desde_texto_con_
    urls) -> tokens [(palabra, url_o_None, pegar), ...] para maquetar como
    texto de PDF de verdad. Se ignora 'negrita' a propósito (ver el
    comentario de la sección). 'url': la dirección de verdad, detectada
    con _pdf_detectar_urls DENTRO del propio run cuando éste es un enlace
    -- un run-enlace contiene, él solo, la URL suelta o el "(URL)" añadido
    tras el texto visible de un <a> (mismo convenio que la conversión a
    texto plano de siempre), así que basta mirar el propio run para
    encontrarla, sin rastrear posiciones de carácter en todo el párrafo.
    'pegar'=True: sin espacio respecto al token anterior (misma idea que
    _captura_tokens_de_parrafo, para -ver-img)."""
    tokens = []
    pegar_siguiente = False
    for texto, _negrita, enlace in runs:
        if texto == "":
            continue
        url = None
        if enlace:
            encontradas = _pdf_detectar_urls(texto)
            if encontradas:
                url = encontradas[0][2]
        sin_espacio_inicial = not texto[0].isspace()
        sin_espacio_final = not texto[-1].isspace()
        palabras = texto.split()
        for i, palabra in enumerate(palabras):
            pegar = bool(tokens) and i == 0 and pegar_siguiente and sin_espacio_inicial
            tokens.append((palabra, url, pegar))
        pegar_siguiente = bool(palabras) and sin_espacio_final
    return tokens


def _pdf_envolver_tokens(tokens, ancho_max):
    """Como _captura_envolver_tokens (-ver-img), pero midiendo cada
    palabra con la tabla de anchos de verdad de Helvetica
    (_pdf_ancho_texto) en vez de los píxeles reales de una fuente que
    aquí no existe (Helvetica es una de las 14 fuentes base del propio
    formato PDF, sin incrustar). Nunca parte una palabra/URL por la
    mitad: si una sola no cupiera en el ancho, esa línea se saldría un
    poco del margen -- precio aceptado a cambio de que un enlace nunca
    quede partido a medias (mismo criterio que _pdf_envolver_texto)."""
    lineas = []
    linea_actual = []
    x = 0.0
    espacio = _pdf_ancho_texto(" ", _PDF_TAM_FUENTE)
    for palabra, url, pegar in tokens:
        ancho_palabra = _pdf_ancho_texto(palabra, _PDF_TAM_FUENTE)
        espacio_requerido = 0 if pegar else espacio
        necesita = (espacio_requerido if linea_actual else 0) + ancho_palabra
        if linea_actual and x + necesita > ancho_max:
            lineas.append(linea_actual)
            linea_actual = []
            x = 0.0
        if linea_actual:
            x += espacio_requerido
        linea_actual.append((palabra, url, x))
        x += ancho_palabra
    if linea_actual:
        lineas.append(linea_actual)
    return lineas


def _pdf_imagen_a_rgb(datos):
    """Abre 'datos' (bytes de cualquier formato que Pillow entienda) y los
    deja en modo RGB, aplanando la transparencia sobre fondo blanco --
    MISMO tratamiento exacto que ya hace _dibujar_captura (-ver-img) para
    que una imagen con transparencia se vea igual en los dos sitios."""
    from PIL import Image
    img = Image.open(io.BytesIO(datos))
    img.load()
    if img.mode in ("RGBA", "LA", "P"):
        fondo = Image.new("RGB", img.size, "white")
        img_rgba = img.convert("RGBA")
        fondo.paste(img_rgba, mask=img_rgba.split()[-1])
        return fondo
    return img.convert("RGB")


def _pdf_trocear_imagen_alto(img, alto_max):
    """Si 'img' (ya redimensionada al ancho que va a ocupar en el PDF) es
    más alta que una página entera, la trocea en imágenes de hasta
    alto_max cada una -- misma idea que _recortar_en_pantallas en
    -ver-img: reparte en varias páginas en vez de encoger una imagen muy
    alta (una captura larga, un banner vertical...) para que quepa entera
    en una sola."""
    ancho, alto_total = img.size
    if alto_total <= alto_max:
        return [img]
    trozos = []
    y0 = 0
    while y0 < alto_total:
        y1 = min(y0 + alto_max, alto_total)
        trozos.append(img.crop((0, y0, ancho, y1)))
        y0 = y1
    return trozos


def _pdf_imagen_xobject(registrar, img_rgb):
    """Registra (con 'registrar', ver _generar_pdf_con_imagenes) la imagen
    PIL 'img_rgb' (ya en modo RGB) como objeto /XObject /Image de PDF: los
    bytes RGB en crudo, comprimidos con zlib (/FlateDecode) -- el propio
    formato PDF soporta esto de forma nativa, sin pasar por ningún formato
    de fichero de imagen. Devuelve el número de objeto."""
    w, h = img_rgb.size
    crudo = zlib.compress(img_rgb.tobytes(), 6)
    contenido = (
        f"<< /Type /XObject /Subtype /Image /Width {w} /Height {h} "
        f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode "
        f"/Length {len(crudo)} >>\nstream\n"
    ).encode("ascii") + crudo + b"\nendstream"
    return registrar(contenido)


def _generar_pdf_con_imagenes(msg, modo_cabeceras, con_remotas):
    """-ver-pdf con imágenes incrustadas en su sitio -- ver el comentario
    de la sección, más arriba, para el pedido completo y las decisiones de
    diseño. Devuelve los bytes del PDF. Deja escapar la excepción que sea
    si algo falla (Pillow no instalado, típicamente) -- la llamadora
    (_guardar_texto_completo_pdf) decide qué hacer (cae al generador de
    solo texto)."""
    from PIL import Image  # noqa: F401 -- fuerza ImportError aquí si falta, antes de tocar nada más

    de = _limpiar_direccion(msg.get("from", ""), 200)
    para = _limpiar_direccion(msg.get("to", ""), 200)
    asunto = _sanear_texto_pantalla(str(msg.get("subject", "") or "(sin asunto)"), una_linea=True)
    fecha_txt = formatear_fecha(fecha_mensaje(msg))

    unidades = []  # [("parrafo", tokens), ("blanco", None), ("regla", None), ("imagen", PIL.Image)]

    def _anadir_parrafo(texto):
        if texto == "":
            unidades.append(("blanco", None))
        else:
            unidades.append(("parrafo", _pdf_tokens_de_parrafo([(texto, False, False)])))

    for etiqueta, valor in (("ID", id_amigable(msg)), ("De", de), ("Para", para), ("Asunto", asunto), ("Fecha", fecha_txt)):
        _anadir_parrafo(f"{etiqueta}: {valor}")
    unidades.append(("regla", None))

    if modo_cabeceras == "importantes":
        for linea in _lineas_cabeceras_importantes(msg):
            _anadir_parrafo(linea)
    elif modo_cabeceras == "todas":
        for linea in _lineas_todas_las_cabeceras(msg):
            _anadir_parrafo(linea)

    adjuntos = _lista_adjuntos(msg)
    if adjuntos:
        _anadir_parrafo(f"Adjuntos ({len(adjuntos)}):")
        for nombre, tamano in adjuntos:
            _anadir_parrafo(f"  - {nombre} ({_formatear_tamano(tamano)})")

    html_crudo = _html_crudo_del_mensaje(msg)
    if html_crudo:
        bloques_cuerpo = _captura_flujo_desde_html(html_crudo)
    else:
        bloques_cuerpo = _captura_flujo_desde_texto_plano(texto_cuerpo_limpio(msg, recortar_urls=False))
    if not bloques_cuerpo:
        _anadir_parrafo("(sin cuerpo de texto legible)")

    for bloque in bloques_cuerpo:
        if bloque["tipo"] == "parrafo":
            tokens = _pdf_tokens_de_parrafo(bloque["runs"])
            if tokens:
                unidades.append(("parrafo", tokens))
            continue
        datos, motivo = _resolver_imagen_para_captura(msg, bloque["src"], con_remotas)
        if datos is None:
            _anadir_parrafo(f"[imagen no disponible: {motivo or 'no disponible'}]")
            continue
        try:
            img = _pdf_imagen_a_rgb(datos)
        except Exception as e:
            _anadir_parrafo(f"[imagen no disponible: no se pudo abrir ({e})]")
            continue
        w, h = img.size
        if w > _PDF_ANCHO_UTIL:
            h = max(1, int(h * (_PDF_ANCHO_UTIL / w)))
            img = img.resize((int(_PDF_ANCHO_UTIL), h))
        unidades.append(("imagen", img))

    # ---- Ensamblado de bajo nivel del PDF: objetos, páginas, xref/trailer ----
    # (deliberadamente independiente de _generar_pdf_simple -- mismo
    # formato de salida, pero sin compartir código con esa función ya
    # probada, para no arriesgar ninguna regresión en ella.)
    objetos = []

    def registrar(contenido=b""):
        objetos.append(contenido)
        return len(objetos)

    num_catalogo = registrar()
    num_pages = registrar()
    num_fuente = registrar(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")

    alto_contenido_pagina = _PDF_ALTO_PAGINA - 2 * _PDF_MARGEN
    y_tope = _PDF_ALTO_PAGINA - _PDF_MARGEN
    r_enlace, g_enlace, b_enlace = (c / 255 for c in _CAPTURA_COLOR_ENLACE)

    paginas = []
    pagina_actual = {"ops": [], "annots": [], "imagenes": {}}
    y = y_tope

    def nueva_pagina():
        nonlocal pagina_actual, y
        paginas.append(pagina_actual)
        pagina_actual = {"ops": [], "annots": [], "imagenes": {}}
        y = y_tope

    def asegurar_espacio(alto_necesario):
        if y - alto_necesario < _PDF_MARGEN:
            nueva_pagina()

    for tipo, contenido in unidades:
        if tipo == "blanco":
            asegurar_espacio(_PDF_ALTURA_LINEA)
            y -= _PDF_ALTURA_LINEA
            continue

        if tipo == "regla":
            asegurar_espacio(16)
            y_regla = y - 6
            op = (
                f"0.78 0.78 0.78 RG 1.5 w {_PDF_MARGEN} {y_regla:.2f} m "
                f"{_PDF_ANCHO_PAGINA - _PDF_MARGEN} {y_regla:.2f} l S"
            )
            pagina_actual["ops"].append(op.encode("ascii"))
            y -= 16
            continue

        if tipo == "imagen":
            img = contenido
            w, h = img.size
            trozos = _pdf_trocear_imagen_alto(img, int(alto_contenido_pagina)) if h > alto_contenido_pagina else [img]
            for i, trozo in enumerate(trozos):
                tw, th = trozo.size
                asegurar_espacio(th)
                num_img = _pdf_imagen_xobject(registrar, trozo)
                nombre_xobj = f"Im{num_img}"
                pagina_actual["imagenes"][nombre_xobj] = num_img
                y_base = y - th
                op = f"q {tw} 0 0 {th} {_PDF_MARGEN} {y_base:.2f} cm /{nombre_xobj} Do Q"
                pagina_actual["ops"].append(op.encode("ascii"))
                y = y_base
                if i < len(trozos) - 1:
                    nueva_pagina()
            y -= _PDF_IMG_HUECO
            continue

        # "parrafo"
        for linea in _pdf_envolver_tokens(contenido, _PDF_ANCHO_UTIL):
            asegurar_espacio(_PDF_ALTURA_LINEA)
            for palabra, url, x in linea:
                color = f"{r_enlace:.3f} {g_enlace:.3f} {b_enlace:.3f} rg" if url else "0 0 0 rg"
                op = (
                    f"BT /F1 {_PDF_TAM_FUENTE} Tf {color} 1 0 0 1 "
                    f"{_PDF_MARGEN + x:.2f} {y:.2f} Tm "
                ).encode("ascii") + b"(" + _pdf_escapar(palabra) + b") Tj ET"
                pagina_actual["ops"].append(op)
                if url:
                    x1 = _PDF_MARGEN + x
                    x2 = _PDF_MARGEN + x + _pdf_ancho_texto(palabra, _PDF_TAM_FUENTE)
                    y1 = y - 0.2 * _PDF_TAM_FUENTE
                    y2 = y + 0.8 * _PDF_TAM_FUENTE
                    num_enlace = registrar(
                        (
                            f"<< /Type /Annot /Subtype /Link "
                            f"/Rect [{x1:.2f} {y1:.2f} {x2:.2f} {y2:.2f}] "
                            f"/Border [0 0 0] "
                            f"/A << /Type /Action /S /URI /URI ("
                        ).encode("ascii") + _pdf_escapar(url) + b") >> >>"
                    )
                    pagina_actual["annots"].append(num_enlace)
            y -= _PDF_ALTURA_LINEA
        y -= _PDF_PARRAFO_HUECO

    paginas.append(pagina_actual)

    num_paginas_obj = []
    for pag in paginas:
        recursos_fuente = f"/Font << /F1 {num_fuente} 0 R >>"
        if pag["imagenes"]:
            xobjs = " ".join(f"/{nombre} {num} 0 R" for nombre, num in pag["imagenes"].items())
            recursos = f"<< {recursos_fuente} /XObject << {xobjs} >> >>"
        else:
            recursos = f"<< {recursos_fuente} >>"
        stream = b"\n".join(pag["ops"])
        num_contenido = registrar(
            f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"\nendstream"
        )
        num_pagina = registrar()
        anotaciones_txt = ""
        if pag["annots"]:
            anotaciones_txt = " /Annots [" + " ".join(f"{n} 0 R" for n in pag["annots"]) + "]"
        objetos[num_pagina - 1] = (
            f"<< /Type /Page /Parent {num_pages} 0 R /Resources {recursos} "
            f"/MediaBox [0 0 {_PDF_ANCHO_PAGINA} {_PDF_ALTO_PAGINA}] "
            f"/Contents {num_contenido} 0 R{anotaciones_txt} >>"
        ).encode("ascii")
        num_paginas_obj.append(num_pagina)

    hijos = " ".join(f"{n} 0 R" for n in num_paginas_obj)
    objetos[num_pages - 1] = f"<< /Type /Pages /Kids [{hijos}] /Count {len(num_paginas_obj)} >>".encode("ascii")
    objetos[num_catalogo - 1] = f"<< /Type /Catalog /Pages {num_pages} 0 R >>".encode("ascii")

    salida = bytearray()
    salida += b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    offsets = [0] * (len(objetos) + 1)
    for i, c in enumerate(objetos, start=1):
        offsets[i] = len(salida)
        salida += f"{i} 0 obj\n".encode("ascii") + c + b"\nendobj\n"
    inicio_xref = len(salida)
    salida += f"xref\n0 {len(objetos) + 1}\n".encode("ascii")
    salida += b"0000000000 65535 f \n"
    for i in range(1, len(objetos) + 1):
        salida += f"{offsets[i]:010d} 00000 n \n".encode("ascii")
    salida += (
        f"trailer\n<< /Size {len(objetos) + 1} /Root {num_catalogo} 0 R >>\n"
        f"startxref\n{inicio_xref}\n%%EOF"
    ).encode("ascii")
    return bytes(salida)


def _imprimir_item(idx, fecha, msg, prefijo, carpeta, largo_de, campos_coincidentes=None, texto_low=None, presupuesto_preview=None):
    if _ESTADO_LISTADO["modo"] == "id":
        print(f" {idx}. id:{id_amigable(msg)}")
        return
    etiqueta_campo, valor_campo = _campo_a_mostrar(msg, carpeta, campos_coincidentes)
    prefijo_txt = f"{prefijo[:30]} " if prefijo else ""
    cabecera_fija = f" {idx}. id:{id_amigable(msg)} {formatear_fecha(fecha)} {prefijo_txt}{etiqueta_campo}:"
    separador = " — "
    # Reparte el ancho que quede (tras la parte fija de ESTA línea en
    # concreto: id/fecha/cuenta-carpeta/campo) entre dirección y asunto,
    # apuntando a ANCHO_TERMINAL en total — así ninguna línea se pasa por
    # unos pocos caracteres solo porque el prefijo era más largo de lo
    # habitual, y las líneas sin prefijo aprovechan el hueco de sobra.
    espacio_restante = max(24, ANCHO_TERMINAL - len(cabecera_fija) - len(separador))
    largo_direccion = min(largo_de, max(10, espacio_restante // 2))
    largo_asunto = max(10, espacio_restante - largo_direccion)
    direccion = _direccion_compacta(valor_campo, largo_direccion)
    asunto = _sanear_texto_pantalla(str(msg.get("subject", "") or "(sin asunto)"), una_linea=True)[:largo_asunto]
    print(f"{cabecera_fija}{direccion}{separador}{asunto}")
    if _ESTADO_LISTADO["modo"] == "titulo":
        return
    texto_preview = formatear_preview(
        vista_previa(msg, campos_coincidentes, texto_low), presupuesto_preview
    )
    if texto_preview:
        print(f" {texto_preview}")


def _exportar_csv(items):
    """Exporta la lista (metadatos, no el cuerpo) a un CSV reutilizable en
    correos_eml/ (o en el directorio actual, con -aqui). Devuelve la ruta
    del fichero creado."""
    carpeta_base = _carpeta_base_salida()
    os.makedirs(carpeta_base, exist_ok=True)
    marca = datetime.now().strftime("%Y%m%d-%H%M%S")
    ruta = os.path.join(carpeta_base, f"lista_{marca}.csv")
    with open(ruta, "w", newline="", encoding="utf-8") as f:
        escritor = csv.writer(f)
        escritor.writerow(["id", "fecha", "carpeta", "de", "para", "asunto"])
        for _cs, carpeta_imap, _imap, _msg_id, _crudo, msg in items:
            escritor.writerow([
                id_amigable(msg),
                formatear_fecha(fecha_mensaje(msg)),
                carpeta_imap,
                _limpiar_direccion(msg.get("from", ""), 300),
                _limpiar_direccion(msg.get("to", ""), 300),
                str(msg.get("subject", "") or ""),
            ])
    return ruta


def _ejecutar_eliminacion(items, indices):
    """Núcleo de eliminación (ya sin pedir selección ni confirmación —
    eso lo hace quien llama, según sea modo interactivo o comando
    directo). Marca los mensajes como eliminados en el servidor y los
    expulsa. IRREVERSIBLE. Devuelve cuántos se eliminaron."""
    grupos = {}  # (id(imap), carpeta_imap) -> {"imap":.., "carpeta":.., "ids": [...]}
    for idx in sorted(indices):
        _cs, carpeta_imap, imap, msg_id, _crudo, _msg = items[idx - 1]
        clave = (id(imap), carpeta_imap)
        if clave not in grupos:
            grupos[clave] = {"imap": imap, "carpeta": carpeta_imap, "ids": []}
        grupos[clave]["ids"].append(msg_id)

    eliminados = 0
    for grupo in grupos.values():
        imap, carpeta_imap, msg_ids = grupo["imap"], grupo["carpeta"], grupo["ids"]
        status, _ = _seleccionar_carpeta(imap, carpeta_imap, readonly=False)  # readonly=False: hace falta para poder eliminar
        if status != "OK":
            _error(f"No se pudo abrir '{carpeta_imap}' para eliminar.")
            continue
        for msg_id in msg_ids:
            try:
                status, _ = imap.store(msg_id, "+FLAGS", "\\Deleted")
                if status == "OK":
                    eliminados += 1
                else:
                    _error(f"No se pudo marcar para eliminar el mensaje {msg_id.decode()} en '{carpeta_imap}'.")
            except Exception as e:
                _error(f"Error eliminando el mensaje {msg_id.decode()} en '{carpeta_imap}': {e}")
        try:
            imap.expunge()
        except Exception as e:
            _error(f"Aviso: no se pudo confirmar el borrado (expunge) en '{carpeta_imap}': {e}")

    print(f"\nEliminados {eliminados} de {len(indices)} mensajes.")
    return eliminados


def _eliminar_mensajes(items):
    """Pide cuáles eliminar, muestra qué se va a borrar, pide una
    confirmación explícita (escribir ELIMINAR) y solo entonces elimina de
    verdad (ver _ejecutar_eliminacion). IRREVERSIBLE."""
    sel = input(f"Números a ELIMINAR, de 1 a {len(items)} (ej: 1,3,5-8 o 'todos'): ")
    indices = parsear_seleccion(sel, len(items))
    if not indices:
        print("Nada seleccionado, no se ha eliminado nada.")
        return

    print(f"\nSe van a ELIMINAR {len(indices)} mensajes:")
    for idx in sorted(indices):
        _cs, carpeta_imap, _imap, _msg_id, _crudo, msg = items[idx - 1]
        etiqueta_campo, valor_campo = _campo_a_mostrar(msg, carpeta_imap, None)
        direccion = _direccion_compacta(valor_campo, 40)
        asunto = _sanear_texto_pantalla(str(msg.get("subject", "") or "(sin asunto)"), una_linea=True)[:40]
        print(f"  {idx}. {carpeta_imap} {etiqueta_campo}:{direccion} — {asunto}")

    confirmacion = input(
        "\nEsto NO se puede deshacer. Escribe ELIMINAR (en mayúsculas) para confirmar: "
    ).strip()
    if confirmacion != "ELIMINAR":
        print("Cancelado, no se ha eliminado nada.")
        return

    _ejecutar_eliminacion(items, indices)


def _cuenta_de_carpeta_salida(carpeta_salida):
    """carpeta_salida es siempre SALIDA/<cuenta> — recupera el nombre y la
    configuración de esa cuenta. Devuelve (nombre, cfg) o (nombre, None) si
    ya no está en CUENTAS."""
    nombre_cuenta = getattr(carpeta_salida, "cuenta", None) or os.path.basename(carpeta_salida.rstrip(os.sep))
    return nombre_cuenta, CUENTAS.get(nombre_cuenta)


class _ConexionesSesion(dict):
    """dict {cuenta: (imap, cfg)} de una sesión interactiva con VARIAS
    pasadas (modo_todas_las_cuentas). Recuerda en .caidas {cuenta: cfg} las
    cuentas que NO se pudieron conectar o reconectar, para volver a
    intentarlo en la siguiente pasada en vez de darlas por perdidas para
    toda la sesión."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.caidas = {}


def _refrescar_conexiones(conexiones):
    """Comprueba cada conexión de 'conexiones' (modo todas las cuentas) y
    reconecta las que el servidor haya cerrado por inactividad. Actualiza
    el diccionario EN SITIO (misma referencia), así que todo lo que lo
    tenga guardado ve la conexión nueva sin más.

    Ronda Z (muchas cuentas, 2ª pasada): las reconexiones se hacen con
    reintentos y espaciadas (PAUSA_ENTRE_RECONEXIONES); una cuenta que no
    se puede reconectar se deja fuera de ESTA pasada pero, si 'conexiones'
    es una _ConexionesSesion, se recuerda (.caidas) y se vuelve a intentar
    en la siguiente, en vez de borrarla para siempre."""
    caidas = getattr(conexiones, "caidas", None)
    cambio_de_cuentas = False
    if caidas:
        for nombre_cuenta, cfg in list(caidas.items()):
            nuevo_imap = _conectar_con_reintentos(cfg, nombre_cuenta)
            if nuevo_imap is not None:
                conexiones[nombre_cuenta] = (nuevo_imap, cfg)
                del caidas[nombre_cuenta]
                cambio_de_cuentas = True
                print(f"'{nombre_cuenta}' vuelve a estar conectada.")
                _ESTADO_CONEXION["dormir"](PAUSA_ENTRE_RECONEXIONES)
    for nombre_cuenta in list(conexiones.keys()):
        imap, cfg = conexiones[nombre_cuenta]
        nuevo_imap = _con_conexion_viva(imap, cfg, nombre_cuenta)
        if nuevo_imap is None:
            if caidas is not None:
                caidas[nombre_cuenta] = cfg
                print(f"'{nombre_cuenta}' se omite en esta pasada (no se pudo reconectar); se volverá a intentar en la siguiente.")
            else:
                print(f"'{nombre_cuenta}' se omite el resto de esta operación (no se pudo reconectar).")
            del conexiones[nombre_cuenta]
        elif nuevo_imap is not imap:
            conexiones[nombre_cuenta] = (nuevo_imap, cfg)
            _ESTADO_CONEXION["dormir"](PAUSA_ENTRE_RECONEXIONES)
    if cambio_de_cuentas:
        # que las cuentas recuperadas vuelvan a su sitio (orden de CUENTAS)
        orden = [k for k in CUENTAS if k in conexiones] + [k for k in conexiones if k not in CUENTAS]
        pares = [(k, conexiones[k]) for k in orden]
        conexiones.clear()
        conexiones.update(pares)
    return conexiones


def _componer_respuesta():
    """Pide el texto de la respuesta línea a línea. Termina con una línea
    que sea solo un punto '.'. Devuelve el texto -- que puede ser la
    cadena vacía "" si se escribe '.' como primerísima línea, sin haber
    escrito nada más (para responder a propósito con el cuerpo en
    blanco) -- o None si se cancela (Enter en blanco antes de haber
    escrito ninguna línea; eso sigue cancelando igual que siempre)."""
    print("\nEscribe tu respuesta. Termina con una línea que tenga solo un punto '.'")
    print("(Enter en blanco al principio cancela; escribe solo '.' para dejarla sin cuerpo)")
    lineas = []
    while True:
        linea = input()
        if linea == ".":
            break
        if not linea and not lineas:
            return None
        lineas.append(linea)
    return "\n".join(lineas)


def _carpeta_enviados_candidatas(cfg):
    """Nombres de carpeta candidatos para 'Enviados', en el orden en que
    se prueban: primero el que el usuario haya configurado explícitamente
    (cfg['sent_folder'] en CUENTAS -- la forma FIABLE, porque el nombre
    real varía mucho de un proveedor a otro, e incluso con el IDIOMA de
    la cuenta en Gmail), y si no hay ninguno configurado, una lista de
    nombres habituales de los proveedores que soporta este programa
    (Yahoo, GMX, Gmail, Hotmail/Outlook) más variantes conocidas. Sin
    duplicados, conservando el orden."""
    candidatos = []
    if cfg.get("sent_folder"):
        candidatos.append(cfg["sent_folder"])
    candidatos += [
        "Sent", "Sent Items", "Sent Messages", "INBOX.Sent",
        "[Gmail]/Sent Mail", "[Google Mail]/Sent Mail",
        "[Gmail]/Correo enviado", "Gesendet", "Enviados", "Correo enviado",
    ]
    vistos, resultado = set(), []
    for c in candidatos:
        if c not in vistos:
            vistos.add(c)
            resultado.append(c)
    return resultado


def _guardar_copia_en_enviados(imap, cfg, nombre_cuenta, mensaje):
    """Tras un envío SMTP correcto, intenta guardar una copia de
    'mensaje' (EmailMessage ya construido, con toda cabecera puesta,
    adjuntos incluidos) en la carpeta de Enviados de la cuenta, vía IMAP
    APPEND -- muchos proveedores NO copian el enviado ellos solos al
    lado del servidor. Prueba, en orden, _carpeta_enviados_candidatas(cfg)
    y se queda con la PRIMERA que acepte el APPEND (mismo entrecomillado
    que _seleccionar_carpeta, para carpetas con espacios o corchetes tipo
    "[Gmail]/Sent Mail"). Se marca (\\Seen), como hace cualquier cliente
    de correo con su propia copia en Enviados.

    'Best effort': si ninguna candidata funciona, avisa por stderr con
    los nombres probados y cómo fijar 'sent_folder' en CUENTAS, pero NO
    hace fallar nada -- el correo ya se envió correctamente por SMTP,
    esto es solo la copia local. Devuelve el nombre de la carpeta usada,
    o None si no se pudo guardar en ninguna."""
    candidatas = _carpeta_enviados_candidatas(cfg)
    datos_mensaje = mensaje.as_bytes()
    for candidata in candidatas:
        try:
            estado, _resp = imap.append(_entrecomillar_imap(candidata), r"(\Seen)", None, datos_mensaje)
            if estado == "OK":
                return candidata
        except Exception:
            continue
    _error(
        f"Aviso: el correo se envió, pero no se pudo guardar una copia en Enviados para "
        f"'{nombre_cuenta}' (se probó: {', '.join(candidatas)}). Si tu proveedor usa otro "
        "nombre de carpeta, añade \"sent_folder\": \"<nombre exacto>\" a esa cuenta en "
        "CUENTAS (Manual 1, sección 3) — para ver el nombre exacto, mira la lista de "
        "carpetas de esa cuenta (p.ej. -folders all en cualquier búsqueda por comando "
        "directo, o la opción \"Elegir carpeta(s)\" del menú interactivo)."
    )
    return None


def _con_conexion_para_guardar_enviados(imap, cfg, nombre_cuenta):
    """Devuelve (imap_a_usar, hay_que_cerrarla) para poder guardar la
    copia en Enviados justo después de enviar: si YA había una conexión
    abierta ('imap' no es None -- caso de -responder, que siempre parte
    de un mensaje ya encontrado por IMAP), la reutiliza -- comprobando y
    reconectando sola si hacía falta, vía _con_conexion_viva -- y no hay
    que cerrarla luego (sigue siendo de quien llamó a _enviar_correo). Si
    no había ninguna (-escribir, que por diseño no conecta por IMAP si no
    hace falta -- enviar es solo SMTP), abre una conexión nueva SOLO para
    esto, y esta sí hay que cerrarla luego. (None, False) si no se pudo
    conseguir ninguna conexión utilizable."""
    if imap is not None:
        imap_vivo = _con_conexion_viva(imap, cfg, nombre_cuenta)
        return (imap_vivo, False) if imap_vivo is not None else (None, False)
    nueva = conectar(cfg, nombre_cuenta)
    return (nueva, True) if nueva is not None else (None, False)


def _enviar_correo(cfg, nombre_cuenta, destinatario, asunto, cuerpo, in_reply_to=None, interactivo=True, cc=None, bcc=None, adjuntos=None, imap=None, smtp=None, guardar_enviados=True):
    """Muestra una vista previa y, si interactivo=True, pide confirmación
    explícita (ENVIAR) antes de mandar. Si interactivo=False, envía
    directamente sin preguntar (modo comando directo). Devuelve True si se
    envió, False si no (cancelado, sin SMTP configurado, sin poder leer
    algún adjunto, o error).

    destinatario: cadena ya lista para el campo "To" (una o varias
    direcciones separadas por ", "). cc/bcc (opcionales): igual, para
    "Cc"/"Bcc" — usados tanto por -escribir (sección 5.11 del Manual 2)
    como por -responder (sección 5.8, Ronda N: -responder-todos/-cc/-c/
    -bcc). adjuntos (opcional): lista de RUTAS de fichero — en CUALQUIER
    carpeta, no solo la actual — a adjuntar tal cual; el tipo (imagen,
    pdf, etc.) se adivina por la extensión, y si no se reconoce se manda
    como binario genérico. No se pide confirmación aparte por adjunto: se
    listan en la vista previa, antes de la confirmación general de envío.

    Sobre 'bcc': se añade como cabecera "Bcc" normal al mensaje ("nuevo",
    más abajo) y NO se hace nada más especial con ella a propósito —
    smtplib.SMTP.send_message() ya se encarga de las dos cosas que
    importan, sin ayuda de este programa: (1) calcula los destinatarios
    DE SOBRE (envelope/RCPT TO) a partir de To+Cc+Bcc, así que SÍ reciben
    el correo; y (2) antes de transmitir el mensaje de verdad, trabaja
    sobre una COPIA en la que borra la cabecera Bcc — así que ningún
    destinatario (ni en To ni en Cc) ve jamás quién iba en copia oculta.
    La copia que se guarda luego en Enviados, en cambio, es el objeto
    "nuevo" ORIGINAL (send_message nunca lo modifica a él, solo a su
    copia interna) — así que esa copia SÍ conserva la cabecera Bcc, igual
    que cualquier cliente de correo te deja ver tu propia lista de BCC en
    tu carpeta de Enviados, aunque los destinatarios nunca la vean.

    imap (opcional): conexión YA ABIERTA a la cuenta, si quien llama ya
    tiene una a mano (-responder, que siempre parte de un mensaje ya
    encontrado por IMAP) — se reutiliza (comprobando/reconectando sola
    si hacía falta) para guardar la copia en Enviados tras el envío, sin
    abrir una conexión aparte. Sin ella (-escribir, que por diseño no
    conecta por IMAP si no hace falta), se abre una conexión nueva solo
    para ese guardado, y se cierra después. En ambos casos, guardar la
    copia es 'best effort': si falla o no se encuentra la carpeta, solo
    avisa por stderr — no cambia el resultado del envío en sí. Se puede
    desactivar por cuenta con "save_sent": False en CUENTAS (algunos
    proveedores, sobre todo Gmail, ya guardan ellos solos una copia en
    Enviados al enviar por su propio SMTP autenticado — con ese ajuste
    en False se evita duplicarla).

    smtp (opcional, Ronda O): conexión SMTP YA ABIERTA Y AUTENTICADA
    (ver _abrir_smtp), si quien llama ya tiene una a mano — típicamente
    -escribir con -lista/-lista-fichero, que reutiliza una misma
    conexión para enviar un correo independiente a cada destinatario de
    la lista, en vez de conectar y volver a pedir la contraseña en
    cada uno. Con ella puesta, no se pide contraseña ni se abre/cierra
    ninguna conexión SMTP aquí dentro — se usa tal cual y se deja
    abierta (es responsabilidad de quien la abrió cerrarla cuando
    termine toda la tanda). Sin ella (como hasta ahora, -escribir de un
    solo destinatario o -responder), el comportamiento es exactamente
    el de siempre: se resuelve la contraseña y se abre una conexión
    SMTP nueva solo para este envío, que se cierra sola al terminar.

    guardar_enviados (opcional, Ronda O; True por defecto -- con eso el
    comportamiento es EXACTAMENTE el de siempre, decidido solo por
    "save_sent" en CUENTAS): en False, NUNCA intenta guardar copia en
    Enviados, pase lo que pase con "save_sent" -- pensado para cuando
    quien llama (-escribir -lista/-lista-fichero) ya intentó UNA vez
    conectar por IMAP para toda la tanda y falló: sin esto, cada envío
    de la lista volvería a intentar conectar por su cuenta (y a pedir la
    contraseña otra vez si no está guardada) para acabar fallando otra
    vez, en vez de aceptar de una vez que no hay copia posible y seguir
    solo enviando.

    El propio mensaje ("nuevo") recibe su cabecera Date: (fecha y hora
    REAL de envío, en España peninsular) justo antes de enviarlo por
    SMTP — así tanto el correo recibido por el destinatario como la
    copia en Enviados (que es ese mismo "nuevo", reutilizado tal cual
    para el IMAP APPEND) quedan con fecha correcta."""
    smtp_server = cfg.get("smtp_server")
    smtp_port = cfg.get("smtp_port", 587)
    if not smtp_server:
        _error(f"No hay servidor SMTP configurado para '{nombre_cuenta}' (falta 'smtp_server' en CUENTAS).")
        return False

    print("\n" + "=" * 60)
    print(f"De: {cfg['user']}")
    print(f"Para: {destinatario}")
    if cc:
        print(f"CC: {cc}")
    if bcc:
        print(f"BCC: {bcc}")
    print(f"Asunto: {asunto}")
    if adjuntos:
        print(f"Adjuntos ({len(adjuntos)}): {', '.join(os.path.basename(a) for a in adjuntos)}")
    print("-" * 60)
    print(cuerpo)
    print("=" * 60)
    if interactivo:
        confirmacion = input("\n¿Enviar este correo de verdad? Escribe ENVIAR para confirmar: ").strip()
        if confirmacion != "ENVIAR":
            print("Cancelado, no se ha enviado nada.")
            return False

    nuevo = EmailMessage()
    nuevo["From"] = cfg["user"]
    nuevo["To"] = destinatario
    if cc:
        nuevo["Cc"] = cc
    if bcc:
        nuevo["Bcc"] = bcc
    nuevo["Subject"] = asunto
    if in_reply_to:
        nuevo["In-Reply-To"] = in_reply_to
        nuevo["References"] = in_reply_to
    nuevo.set_content(cuerpo)

    for ruta in (adjuntos or []):
        try:
            with open(ruta, "rb") as f:
                datos = f.read()
        except OSError as e:
            _error(f"No se pudo leer el adjunto '{ruta}': {e}")
            return False
        tipo_adivinado, _codificacion = mimetypes.guess_type(ruta)
        tipo_principal, tipo_secundario = (tipo_adivinado.split("/", 1) if tipo_adivinado else ("application", "octet-stream"))
        nuevo.add_attachment(
            datos, maintype=tipo_principal, subtype=tipo_secundario,
            filename=os.path.basename(ruta),
        )

    if smtp is None:
        password = cfg.get("password") or getpass.getpass(f"Contraseña para enviar desde {nombre_cuenta}: ")
    # Cabecera Date: puesta AQUÍ (justo antes de conectar y enviar, después
    # del posible getpass.getpass() interactivo de arriba, que puede tardar
    # lo que tarde la persona en escribir la contraseña) para que sea lo más
    # fiel posible a la fecha y hora REAL de envío -- sin esto, el mensaje
    # no llevaba Date: ninguna (ni el que se envía por SMTP, ni por tanto la
    # copia idéntica que se guarda luego en Enviados vía _guardar_copia_en_
    # enviados, que reutiliza este mismo "nuevo"), y al releer esa copia más
    # tarde (fecha_mensaje() lee la cabecera Date: del mensaje, no hay
    # ninguna fecha "de servidor" de donde sacarla) se mostraba "(sin
    # fecha)". Se calcula en hora de España PENINSULAR de forma robusta
    # (_a_hora_espana_peninsular, igual que el resto del programa) en vez
    # de confiar en la zona horaria del sistema (formatdate/localtime=True):
    # así la fecha guardada es correcta aunque el dispositivo tenga mal
    # puesta su zona horaria.
    nuevo["Date"] = format_datetime(_a_hora_espana_peninsular(datetime.now(timezone.utc)))
    try:
        # Sin to_addrs explícito, send_message() saca los destinatarios de
        # sobre (envelope) de las cabeceras To/Cc del propio mensaje —
        # incluye tanto 'destinatario' como 'cc' automáticamente.
        if smtp is not None:
            # Conexión ya abierta y autenticada por quien llama (ver
            # parámetro 'smtp' más arriba) -- se usa tal cual, sin abrir
            # ni cerrar nada aquí.
            smtp.send_message(nuevo)
        elif smtp_port == 465:
            with smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=30) as servidor:
                servidor.login(cfg["user"], password)
                servidor.send_message(nuevo)
        else:
            with smtplib.SMTP(smtp_server, smtp_port, timeout=30) as servidor:
                servidor.starttls()
                servidor.login(cfg["user"], password)
                servidor.send_message(nuevo)
        print("Correo enviado.")
    except Exception as e:
        _error(f"No se pudo enviar: {e}")
        return False

    if guardar_enviados and cfg.get("save_sent", True):
        imap_guardado, hay_que_cerrarla = _con_conexion_para_guardar_enviados(imap, cfg, nombre_cuenta)
        if imap_guardado is not None:
            carpeta_usada = _guardar_copia_en_enviados(imap_guardado, cfg, nombre_cuenta, nuevo)
            if carpeta_usada:
                print(f"Copia guardada en Enviados ({carpeta_usada}).")
            if hay_que_cerrarla:
                _cerrar_imap(imap_guardado)
        else:
            _error(
                f"Aviso: el correo se envió, pero no se pudo conectar por IMAP a "
                f"'{nombre_cuenta}' para guardar una copia en Enviados."
            )
    return True


def _abrir_smtp(cfg, nombre_cuenta):
    """Abre y autentica una conexión SMTP para 'cfg' -- la MISMA conexión
    que _enviar_correo abriría ella sola por dentro si no se le pasa nada
    en su parámetro 'smtp' (mismo puerto 465/587, mismo starttls, mismo
    login). Pensada para reutilizar UNA sola conexión en varios envíos
    seguidos (Ronda O: -escribir -lista/-lista-fichero) en vez de abrir
    una por destinatario -- así solo se pide la contraseña una vez (si no
    está guardada en CUENTAS) y no se machaca al servidor SMTP con
    conexiones repetidas.

    Devuelve la conexión ya autenticada, o None si no se pudo (servidor
    sin configurar, o fallo de red/autenticación) -- en ambos casos ya se
    avisa por stderr vía _error(), igual que hace _enviar_correo.

    Quien la abra es quien debe cerrarla al terminar (servidor.quit();
    como con cualquier smtplib.SMTP, un segundo quit() de más daría
    error, así que solo se llama una vez, al final de toda la tanda)."""
    smtp_server = cfg.get("smtp_server")
    smtp_port = cfg.get("smtp_port", 587)
    if not smtp_server:
        _error(f"No hay servidor SMTP configurado para '{nombre_cuenta}' (falta 'smtp_server' en CUENTAS).")
        return None
    password = cfg.get("password") or getpass.getpass(f"Contraseña para enviar desde {nombre_cuenta}: ")
    try:
        if smtp_port == 465:
            servidor = smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=30)
        else:
            servidor = smtplib.SMTP(smtp_server, smtp_port, timeout=30)
            servidor.starttls()
        servidor.login(cfg["user"], password)
        return servidor
    except Exception as e:
        _error(f"No se pudo conectar/autenticar por SMTP con '{nombre_cuenta}': {e}")
        return None


# Dirección de eGarante (servicio de certificación/sellado de tiempo de
# correos, habitual en España como prueba ante reclamaciones formales):
# copiándola en CC, eGarante responde con una copia certificada del envío.
# -c (junto con -escribir) la añade sola, sin tener que escribirla.
_DIRECCION_EGARANTE = "mailsigned@egarante.com"


def _lista_direcciones(valor):
    """'a@x.com, b@y.com' -> ['a@x.com', 'b@y.com']. [] si valor es None o
    vacío. Solo separa y quita espacios — el formato se valida aparte
    (ver _validar_direcciones), para poder acumular direcciones de varias
    fuentes (-p, -cc, -c) antes de comprobarlas todas juntas."""
    if not valor:
        return []
    return [d.strip() for d in valor.split(",") if d.strip()]


def _validar_direcciones(direcciones, nombre_flag):
    """sys.exit si alguna dirección no tiene ni pinta de dirección de
    correo (comprobación ligera: solo que contenga '@' — no es un
    validador RFC 5322 completo, solo para pillar pronto una coma de más
    o una dirección mal pegada antes de intentar enviar nada)."""
    for d in direcciones:
        if "@" not in d:
            sys.exit(f"'{d}' (en {nombre_flag}) no parece una dirección de correo válida (falta '@').")


def _direcciones_de_cabecera(msg, nombre_cabecera):
    """Lista de direcciones (addr_spec, tal cual) de una cabecera De/Para/
    CC/BCC del propio mensaje -- [] si no existe, está vacía, o no se
    puede interpretar (algunos mensajes reales traen direcciones mal
    formadas; esto nunca debe reventar por eso, solo dar [] en ese caso)."""
    valor = msg.get(nombre_cabecera)
    if valor is None:
        return []
    try:
        return [a.addr_spec for a in valor.addresses if a.addr_spec]
    except Exception:
        return []


def _datos_respuesta(msg_original, modo_todos=False, asunto_manual=None,
                      direccion_propia=None, cc_extra=None, bcc_extra=None, usar_reply_to=True):
    """Calcula (destinatario, cc, bcc, asunto_respuesta, in_reply_to) a
    partir del mensaje original -- o (None, None, None, None, None) si
    falta algo imprescindible (se explica por stderr). Las tres primeras
    ya vienen como cadenas listas para _enviar_correo (direcciones unidas
    con ", ", o None si no hay ninguna) -- cc/bcc nunca duplican una
    dirección ya puesta en otro campo, ni incluyen direccion_propia (para
    no incluirte a ti mismo en tu propia respuesta).

    modo_todos=False (por defecto -- "responder solo al remitente", el
    comportamiento de siempre): destinatario es SOLO la dirección De: del
    mensaje original.
    modo_todos=True (-responder-todos -- "responder a todos"): destinatario
    es el De: original MÁS los Para: originales; cc son los CC: originales.
    Los BCC: originales (en la práctica, solo presentes si el mensaje es
    una copia de un envío anterior hecho por ESTE MISMO programa -- un
    correo de verdad, tal como LLEGA a un destinatario, nunca trae ese
    dato: por diseño del propio protocolo, el servidor se lo quita a
    todo el mundo menos a quien lo escribió) se devuelven en 'bcc'.

    cc_extra/bcc_extra (opcionales): direcciones ADICIONALES a incluir
    (de -cc/-c/-bcc, o de los mismos flags del menú interactivo) -- se
    combinan con lo anterior, nunca lo sustituyen.

    asunto_manual (opcional): si se da (incluso ""), se usa TAL CUAL en
    vez de calcular "Re: <asunto original>" -- "" es "en blanco a
    propósito", igual que ya se trata en -escribir.

    usar_reply_to (Ronda AB, pedido 2026-11-04; por defecto True): si el
    mensaje trae cabecera Reply-To ("Responder a") con alguna dirección
    válida distinta de la propia, la respuesta va a ESA(S) dirección(es) en
    lugar de al De: (lo que hace cualquier cliente de correo y manda el RFC
    5322). Si Reply-To falta, está vacío, no se puede interpretar o es solo
    la dirección propia, se responde al De: como siempre. False (-sin-reply-to):
    se ignora Reply-To y se responde al De:."""
    remitentes = _direcciones_de_cabecera(msg_original, "from")
    principales = remitentes[:1]
    if usar_reply_to:
        propias = (direccion_propia or "").strip().lower()
        reply_to = [d for d in _direcciones_de_cabecera(msg_original, "reply-to") if "@" in d and d.strip().lower() != propias]
        if reply_to:
            principales = reply_to
            remitentes = remitentes or reply_to
    if not remitentes:
        _error("No se pudo determinar a quién responder (sin dirección De: válida en el mensaje).")
        return None, None, None, None, None

    propia = (direccion_propia or "").strip().lower()
    vistas = {propia} if propia else set()

    def _anadir(lista_destino, direcciones):
        for d in direcciones:
            clave = d.strip().lower()
            if clave and clave not in vistas:
                vistas.add(clave)
                lista_destino.append(d)

    destinatarios, cc_final, bcc_final = [], [], []
    _anadir(destinatarios, principales)
    if modo_todos:
        _anadir(destinatarios, _direcciones_de_cabecera(msg_original, "to"))
        _anadir(cc_final, _direcciones_de_cabecera(msg_original, "cc"))
        _anadir(bcc_final, _direcciones_de_cabecera(msg_original, "bcc"))
    _anadir(cc_final, cc_extra or [])
    _anadir(bcc_final, bcc_extra or [])

    if not destinatarios:
        # Solo puede pasar cuando el remitente original ERAS TÚ MISMO
        # (p.ej. respondiendo a tu propia copia en Enviados) -- en
        # modo_todos, además, sin ningún otro Para; en modo normal
        # (responder solo al remitente), directamente no hay a quién
        # responder, porque ese "remitente" eres tú. Nota de la Ronda N:
        # ANTES de esta ronda no se excluía nunca la dirección propia, así
        # que este caso exacto (responder, en modo normal, a un mensaje
        # cuyo De: es tu propia cuenta) habría acabado "respondiéndote a
        # ti mismo" en silencio -- ahora se avisa con claridad en vez de
        # hacer eso.
        _error(
            "No se pudo determinar ningún destinatario para la respuesta "
            "(¿respondes a un mensaje tuyo sin otros destinatarios?)."
        )
        return None, None, None, None, None

    asunto_original = str(msg_original.get("subject", "") or "")
    asunto_respuesta = (
        asunto_manual if asunto_manual is not None else
        (asunto_original if asunto_original.strip().lower().startswith("re:") else f"Re: {asunto_original}")
    )
    message_id_original = msg_original.get("message-id")
    in_reply_to = str(message_id_original) if message_id_original else None
    return (
        ", ".join(destinatarios),
        ", ".join(cc_final) or None,
        ", ".join(bcc_final) or None,
        asunto_respuesta,
        in_reply_to,
    )


def _avisar_si_reply_to(msg_original, destinatario, usar_reply_to):
    """Si la respuesta va al Reply-To ("Responder a") y no al De:, lo dice."""
    de = ", ".join(_direcciones_de_cabecera(msg_original, "from"))
    if usar_reply_to and str(msg_original.get("reply-to") or "").strip() and de and destinatario.split(", ")[0].lower() != de.split(", ")[0].lower():
        print(f"Aviso: el mensaje tiene 'Responder a' ({msg_original.get('reply-to')}); se responde a esa dirección, no al De ({de}). "
              f"Para responder al De usa -sin-reply-to.", file=sys.stderr)


def _responder_mensaje(carpeta_salida, msg_original, imap=None):
    """Responde a msg_original de forma interactiva. Por defecto, el
    flujo de siempre: solo texto, solo al remitente. Las opciones nuevas
    de la Ronda N (responder a todos, asunto propio, cuerpo desde un
    fichero .txt, adjuntos, CC/BCC y -c/eGarante) son todas OPCIONALES,
    tras una única pregunta inicial -- así quien solo quiere responder
    rápido, como siempre, no ve ni un prompt de más. 'imap' (opcional):
    la conexión ya abierta de donde viene msg_original — se pasa a
    _enviar_correo para guardar la copia en Enviados sin abrir una
    conexión aparte (ver _enviar_correo)."""
    nombre_cuenta, cfg = _cuenta_de_carpeta_salida(carpeta_salida)
    if cfg is None:
        print(f"No se encontró la configuración de la cuenta '{nombre_cuenta}' para responder.")
        return

    modo_todos, asunto_manual, ruta_cuerpo = False, None, ""
    cc_extra, bcc_extra, adjuntos = [], [], []
    usar_reply_to = True
    avanzado = input(
        "¿Opciones avanzadas (responder a todos, asunto propio, cuerpo "
        "desde .txt, adjuntar ficheros, CC/BCC)? s/N: "
    ).strip().lower() == "s"
    if avanzado:
        modo_todos = input(
            "¿Responder solo al remitente, o a todos (Para/CC/BCC originales)? "
            "1=Solo remitente, 2=A todos (Enter=1): "
        ).strip() == "2"
        if str(msg_original.get("reply-to") or "").strip():
            usar_reply_to = input(
                f"El mensaje trae 'Responder a': {msg_original.get('reply-to')}. "
                "¿Responder a esa dirección en vez de al remitente? S/n: "
            ).strip().lower() != "n"
        asunto_manual = input('Asunto (Enter = "Re: <el original>"): ').strip() or None
        ruta_cuerpo = input(
            "¿Leer el cuerpo de un fichero .txt? Ruta completa (Enter = escribirlo aquí): "
        ).strip()
        rutas_adj = input("¿Adjuntar algún fichero? Rutas separadas por comas (Enter = ninguno): ").strip()
        adjuntos = [r.strip() for r in rutas_adj.split(",") if r.strip()]
        cc_extra = _lista_direcciones(
            input("¿Añadir alguna dirección en CC? (Enter = ninguna): ").strip()
        )
        if input(
            "¿Copia certificada de eGarante en CC (mailsigned@egarante.com)? s/N: "
        ).strip().lower() == "s" and _DIRECCION_EGARANTE not in cc_extra:
            cc_extra.append(_DIRECCION_EGARANTE)
        bcc_extra = _lista_direcciones(
            input("¿Añadir alguna dirección en BCC (copia oculta)? (Enter = ninguna): ").strip()
        )

    destinatario, cc, bcc, asunto_respuesta, in_reply_to = _datos_respuesta(
        msg_original, modo_todos=modo_todos, asunto_manual=asunto_manual,
        direccion_propia=cfg.get("user"), cc_extra=cc_extra, bcc_extra=bcc_extra,
        usar_reply_to=usar_reply_to,
    )
    if destinatario is None:
        return
    _avisar_si_reply_to(msg_original, destinatario, usar_reply_to)

    if ruta_cuerpo:
        try:
            with open(ruta_cuerpo, "r", encoding="utf-8") as f:
                cuerpo = f.read()
        except OSError as e:
            print(f"No se pudo leer '{ruta_cuerpo}': {e}. No se ha respondido a nada.")
            return
    else:
        cuerpo = _componer_respuesta()
        if cuerpo is None:
            print("Cancelado, no se escribió nada.")
            return

    _enviar_correo(
        cfg, nombre_cuenta, destinatario, asunto_respuesta, cuerpo, in_reply_to,
        cc=cc, bcc=bcc, adjuntos=(adjuntos or None), imap=imap,
    )


def _responder_mensaje_cli(carpeta_salida, msg_original, texto_respuesta, imap=None,
                            modo_todos=False, asunto_manual=None,
                            cc_extra=None, bcc_extra=None, adjuntos=None, usar_reply_to=True):
    """Como _responder_mensaje, pero para modo comando directo: el texto ya
    viene dado (de -cuerpo/-texto o de stdin) y se envía sin pedir
    confirmación. modo_todos/asunto_manual/cc_extra/bcc_extra: ver
    _datos_respuesta. adjuntos: ver _enviar_correo. 'imap' (opcional): ver
    _responder_mensaje / _enviar_correo."""
    nombre_cuenta, cfg = _cuenta_de_carpeta_salida(carpeta_salida)
    if cfg is None:
        _error(f"No se encontró la configuración de la cuenta '{nombre_cuenta}' para responder.")
        return False
    destinatario, cc, bcc, asunto_respuesta, in_reply_to = _datos_respuesta(
        msg_original, modo_todos=modo_todos, asunto_manual=asunto_manual,
        direccion_propia=cfg.get("user"), cc_extra=cc_extra, bcc_extra=bcc_extra,
        usar_reply_to=usar_reply_to,
    )
    if destinatario is None:
        return False
    _avisar_si_reply_to(msg_original, destinatario, usar_reply_to)
    return _enviar_correo(
        cfg, nombre_cuenta, destinatario, asunto_respuesta, texto_respuesta,
        in_reply_to, interactivo=False, cc=cc, bcc=bcc, adjuntos=adjuntos, imap=imap,
    )


def menu_descargar_o_ver(items, multi, contexto=None):
    """items: [(carpeta_salida, carpeta_imap, imap, msg_id, crudo, msg), ...],
    ya mostrados (con vista previa) antes de llamar a esto; crudo no es None.
    Deja elegir: descargar algunos como .eml, ver el texto completo de uno,
    exportar la lista a CSV, eliminar (¡irreversible!), responder a uno, o
    continuar sin hacer nada más con esta lista.

    contexto (opcional): dict que permite lanzar una nueva búsqueda o ver
    los recientes de nuevo SIN volver al menú anterior. Formas válidas:
      {"tipo": "cuenta", "imap":.., "cfg":.., "nombre_cuenta":.., "carpeta_salida":..}
      {"tipo": "todas", "conexiones":..}

    Devuelve True si se saltó a "ver recientes de nuevo" o "buscar de
    nuevo" (quien llama debería no seguir con su propia paginación, ya se
    ha pasado a otra cosa); False si solo se continuó normalmente.
    """
    if not items:
        return False
    while True:
        print("\n¿Qué quieres hacer?")
        print("  1. Descargar como .eml (elige cuáles)")
        print("  2. Ver el texto completo de uno")
        print("  3. Exportar esta lista a CSV")
        print("  4. Eliminar (¡irreversible!)")
        print("  5. Responder a uno")
        opciones_extra = "  6. Continuar (nada más con esta lista)"
        if contexto:
            print("  6. Ver los más recientes de nuevo")
            print("  7. Buscar de nuevo")
            print("  8. Continuar (nada más con esta lista)")
        else:
            print(opciones_extra)
        opcion = input("Opción: ").strip()

        if opcion == "1":
            sel = input(f"Números a descargar, de 1 a {len(items)} (ej: 1,3,5-8 o 'todos'): ")
            indices = parsear_seleccion(sel, len(items))
            for idx in sorted(indices):
                carpeta_salida, carpeta_imap, _imap, _msg_id, crudo, msg = items[idx - 1]
                os.makedirs(carpeta_salida, exist_ok=True)
                guardar_crudo(crudo, carpeta_salida, msg, carpeta_imap)
        elif opcion == "2":
            num = input(f"¿Cuál? (1 a {len(items)}): ").strip()
            try:
                idx = int(num)
            except ValueError:
                idx = -1
            if 1 <= idx <= len(items):
                como = input(
                    "¿Cómo? 1=Pantalla, 2=Guardar en .txt, 3=Guardar en .pdf, "
                    "4=Guardar como imagen (Enter = pantalla): "
                ).strip()
                if como == "2":
                    carpeta_salida, carpeta_imap, _imap, _msg_id, _crudo, msg = items[idx - 1]
                    _guardar_texto_completo_txt(msg, carpeta_salida, carpeta_imap, multi)
                elif como == "3":
                    carpeta_salida, carpeta_imap, _imap, _msg_id, _crudo, msg = items[idx - 1]
                    _guardar_texto_completo_pdf(msg, carpeta_salida, carpeta_imap, multi)
                elif como == "4":
                    # -ver-img, versión interactiva -- misma función de
                    # fondo que el comando directo (_guardar_capturas_
                    # pantalla), preguntando aquí lo que allí se da como
                    # flags (-pantallas/-con-remotas). Mismo "no válido"
                    # que el resto de este menú ante un número suelto.
                    carpeta_salida, carpeta_imap, _imap, _msg_id, _crudo, msg = items[idx - 1]
                    texto_n = input("¿Cuántas pantallas? (1, 2 o 3 — Enter = 1): ").strip()
                    try:
                        n_pantallas = int(texto_n) if texto_n else 1
                    except ValueError:
                        n_pantallas = -1
                    if n_pantallas <= 0:
                        print("Número de pantallas no válido; no se ha generado nada.")
                    else:
                        remotas = input(
                            "¿Incluir también imágenes remotas (descarga desde internet)? s/N: "
                        ).strip().lower() == "s"
                        _guardar_capturas_pantalla(msg, carpeta_salida, carpeta_imap, n_pantallas, remotas)
                else:
                    while True:
                        _cs, _ci, _imap, _msg_id, _crudo, msg = items[idx - 1]
                        _mostrar_texto_completo(msg)
                        if idx >= len(items):
                            break
                        resp = input("\nEnter = ver el siguiente, cualquier otra cosa = volver al menú: ")
                        if resp != "":
                            break
                        idx += 1
            else:
                print("Número no válido.")
        elif opcion == "3":
            ruta = _exportar_csv(items)
            print(f"Guardado: {ruta}")
        elif opcion == "4":
            _eliminar_mensajes(items)
        elif opcion == "5":
            num = input(f"¿Responder a cuál? (1 a {len(items)}): ").strip()
            try:
                idx = int(num)
            except ValueError:
                idx = -1
            if 1 <= idx <= len(items):
                carpeta_salida, _ci, imap, _msg_id, _crudo, msg = items[idx - 1]
                _responder_mensaje(carpeta_salida, msg, imap)
            else:
                print("Número no válido.")
        elif contexto and opcion == "6":
            _ver_recientes_de_nuevo(contexto)
            return True
        elif contexto and opcion == "7":
            _buscar_de_nuevo(contexto)
            return True
        elif (contexto and opcion in ("8", "")) or (not contexto and opcion in ("6", "")):
            return False
        else:
            print("Opción no válida.")


def _ver_recientes_de_nuevo(contexto):
    if contexto["tipo"] == "cuenta":
        nuevo_imap = elegir_de_recientes(contexto["imap"], contexto["carpeta_salida"], contexto["cfg"])
        if nuevo_imap is not None:
            contexto["imap"] = nuevo_imap
    else:
        recientes_todas_cuentas(contexto["conexiones"])


def _buscar_de_nuevo(contexto):
    if contexto["tipo"] == "cuenta":
        nuevo_imap = buscar_y_descargar(contexto["imap"], contexto["cfg"], contexto["nombre_cuenta"])
        if nuevo_imap is not None:
            contexto["imap"] = nuevo_imap
    else:
        buscar_todas_las_cuentas(contexto["conexiones"])


# ---------- Selección manual de mensajes recientes (una cuenta) ----------

def parsear_seleccion(texto, maximo):
    """'1,3,5-8' -> {1,3,5,6,7,8}. 'todos' -> {1..maximo}. Ignora entradas inválidas."""
    texto = texto.strip().lower()
    if not texto:
        return set()
    if texto in ("todos", "all", "*"):
        return set(range(1, maximo + 1))
    indices = set()
    for parte in texto.split(","):
        parte = parte.strip()
        if not parte:
            continue
        if "-" in parte:
            try:
                a, b = parte.split("-", 1)
                a, b = int(a), int(b)
                indices.update(range(min(a, b), max(a, b) + 1))
            except ValueError:
                print(f"Ignorando rango no válido: '{parte}'")
        else:
            try:
                indices.add(int(parte))
            except ValueError:
                print(f"Ignorando entrada no válida: '{parte}'")
    return {i for i in indices if 1 <= i <= maximo}


def elegir_de_recientes(imap, carpeta_salida, cfg):
    """Devuelve la conexión imap (la misma, o una nueva si hubo que
    reconectar) para que quien llama pueda seguir usándola después."""
    nombre_cuenta, _ = _cuenta_de_carpeta_salida(carpeta_salida)
    imap = _con_conexion_viva(imap, cfg, nombre_cuenta)
    if imap is None:
        return None
    contexto = {
        "tipo": "cuenta", "imap": imap, "cfg": cfg,
        "nombre_cuenta": nombre_cuenta, "carpeta_salida": carpeta_salida,
    }
    carpetas = elegir_carpetas(imap, cfg)
    if len(carpetas) > 1:
        _navegar_todas_las_carpetas(imap, carpeta_salida, carpetas, contexto)
        return contexto["imap"]
    carpeta = carpetas[0]
    status, _ = _seleccionar_carpeta(imap, carpeta, readonly=True)
    if status != "OK":
        print(f"No se pudo abrir '{carpeta}'.")
        return imap
    _navegar_carpeta(imap, carpeta_salida, carpeta, False, contexto)
    return contexto["imap"]


def _navegar_carpeta(imap, carpeta_salida, carpeta, multi, contexto=None):
    """Navega UNA carpeta, paginada, de más reciente a más antigua (orden del servidor)."""
    status, datos = imap.search(None, "ALL")
    if status != "OK":
        print("No se pudo listar la carpeta.")
        return
    ids_totales = datos[0].split()
    ids_totales.reverse()  # el más reciente primero

    if not ids_totales:
        print("No hay mensajes en esta carpeta.")
        return

    mostrados = 0
    tam_lote = MAX_INICIAL

    while mostrados < len(ids_totales):
        lote = ids_totales[mostrados: mostrados + tam_lote]
        print(f"\nMensajes {mostrados + 1}-{mostrados + len(lote)} de {len(ids_totales)}:")
        items = []
        for idx, msg_id in enumerate(lote, start=1):
            try:
                status, d = imap.fetch(msg_id, "(RFC822)")
                if status != "OK":
                    print(f"  {idx}. (no se pudo descargar)")
                    continue
                crudo = d[0][1]
                msg = email.message_from_bytes(crudo, policy=policy.default)
                _imprimir_item(idx, fecha_mensaje(msg), msg, None, carpeta, 33, presupuesto_preview=LINEAS_CONTEXTO * LARGO_LINEA_PREVIEW)
                items.append((carpeta_salida, carpeta, imap, msg_id, crudo, msg))
            except Exception as e:
                print(f"  {idx}. (no se pudo leer: {e})")

        mostrados += len(lote)
        salto_a_otra_accion = menu_descargar_o_ver(items, multi, contexto)
        if salto_a_otra_accion:
            return

        if mostrados >= len(ids_totales):
            print("No hay más mensajes en esta carpeta.")
            break

        resp = input("\n¿Ver más? Escribe 100, 250 o 500, o Enter para volver: ").strip()
        if resp in ("100", "250", "500"):
            tam_lote = int(resp)
        else:
            break


# --- -nuevos (pedido 2026-10-05/07): recuerda qué correos del INBOX de
# cada cuenta ya se han visto en una ejecución anterior (en días
# distintos, p.ej. desde Termux), para poder distinguir los nuevos desde
# la última vez y mostrarlos los primeros -- ver _marcar_nuevos_y_
# reordenar, más abajo, y el uso de -nuevos en comando_directo. Pedido
# verbatim (2026-10-05/07): "añade una funcionalidad: si busco correos
# desde termux, dada una cuenta de correo electrónico, revisa la carpeta
# inbox (únicamente por ahora esa carpeta en cada cuenta); si busco
# correos electrónicos nuevos en varios días distintos, deja registro de
# los resultados de tal manera que puedas saber si existen resultados
# nuevos y de esta manera ponerlos al principio." Aclarado con el usuario
# (3 preguntas, 2026-10-07), con estas respuestas: (1) flag nuevo dedicado
# -nuevos, NO un cambio de comportamiento automático de lo que ya existe;
# (2) se aplica solo a -descargar/-ver sobre el INBOX sin fechas (no a
# -buscar/-buscarfecha(s)/-id/-duplicados/etc., que tienen su propio
# sentido de "búsqueda", ni a -acc todas o -folders all/todas, que miran
# MÁS que solo el INBOX); (3) los nuevos se muestran reordenados al
# principio de la lista, SIN marca tipo "[NUEVO]" ni resumen aparte tipo
# "tienes 3 correos nuevos" -- se dieron las 3 alternativas como opciones
# y se eligió explícitamente la más simple de las tres. ---
def _ruta_registro_nuevos(nombre_cuenta):
    return _ruta_fichero_de_estado(CARPETA_REGISTRO_NUEVOS, "registro_nuevos", nombre_cuenta)


def _cargar_registro_nuevos(nombre_cuenta):
    """Devuelve el conjunto (set) de id_amigable ya vistos en el INBOX de
    esta cuenta en una ejecución anterior con -nuevos, o un conjunto vacío
    si todavía no hay ninguno registrado -- o si el fichero existente no
    se puede leer (se avisa y se empieza de cero: un registro dañado nunca
    debe impedir ver el correo en sí, como mucho hace que todo salga como
    "nuevo" otra vez, igual que la primera ejecución)."""
    ruta = _ruta_registro_nuevos(nombre_cuenta)
    if os.path.isfile(ruta):
        try:
            with open(ruta, "r", encoding="utf-8") as f:
                datos = json.load(f)
            return set(datos.get("vistos", []))
        except Exception as e:
            _error(
                f"Aviso: no se pudo leer el registro de correos nuevos de '{nombre_cuenta}' "
                f"({ruta}): {e} — por esta vez, todo el INBOX actual cuenta como nuevo."
            )
    return set()


def _guardar_registro_nuevos(nombre_cuenta, vistos):
    """Guarda 'vistos' (set de id_amigable) en disco de forma ATÓMICA
    (fichero temporal + os.replace), igual que _guardar_checkpoint_busqueda
    (-reanudable) -- para que una interrupción a mitad de guardar no deje
    el registro corrupto."""
    ruta = _ruta_registro_nuevos(nombre_cuenta)
    datos = {
        "version": 1,
        "actualizado": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "vistos": sorted(vistos),
    }
    _asegurar_carpeta_de(ruta)
    ruta_tmp = ruta + ".tmp"
    with open(ruta_tmp, "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False, indent=2)
    os.replace(ruta_tmp, ruta)


def _marcar_nuevos_y_reordenar(items):
    """items: lista de (carpeta_salida, carpeta, imap, msg_id, crudo, msg),
    ya con el mensaje COMPLETO descargado (RFC822) -- esta función no hace
    ninguna llamada IMAP, solo mira lo que ya se trajo. Separa los mensajes
    en "nuevos" (su id_amigable no estaba en el registro de su cuenta) y
    "ya vistos", conservando en cada grupo el mismo orden relativo que
    traían (más reciente primero), y antepone los nuevos -- sin marca ni
    resumen (ver nota de diseño justo arriba). La cuenta de cada item se
    recupera de carpeta_salida (_cuenta_de_carpeta_salida), así que
    funciona igual con 1 cuenta o con varias mezcladas (p.ej. -acc gmx
    yahoo -nuevos). Actualiza y guarda el registro de CADA cuenta
    implicada con TODOS los id_amigable vistos en este paso (nuevos y ya
    vistos), para que ninguno vuelva a contar como nuevo la próxima vez.
    Devuelve (items_reordenados, cuántos_nuevos)."""
    registros = {}   # nombre_cuenta -> set (se carga una sola vez por cuenta, aunque salga en varios items)
    registros_previos = {}   # nombre_cuenta -> cuántos vistos había al empezar (para el diagnóstico)
    nuevos, vistos_items = [], []
    for item in items:
        carpeta_salida = item[0]
        msg = item[5]
        nombre_cuenta, _cfg = _cuenta_de_carpeta_salida(carpeta_salida)
        if nombre_cuenta not in registros:
            registros[nombre_cuenta] = _cargar_registro_nuevos(nombre_cuenta)
            registros_previos[nombre_cuenta] = len(registros[nombre_cuenta])
        vistos_de_cuenta = registros[nombre_cuenta]
        h = id_amigable(msg)
        (nuevos if h not in vistos_de_cuenta else vistos_items).append(item)
        vistos_de_cuenta.add(h)

    for nombre_cuenta, vistos_de_cuenta in registros.items():
        _guardar_registro_nuevos(nombre_cuenta, vistos_de_cuenta)
    # Diagnóstico (Ronda AD): una línea por cuenta con DÓNDE está el registro y qué pasó.
    for nombre_cuenta in registros:
        total = sum(1 for it in items if _cuenta_de_carpeta_salida(it[0])[0] == nombre_cuenta)
        nuevos_c = sum(1 for it in nuevos if _cuenta_de_carpeta_salida(it[0])[0] == nombre_cuenta)
        print(f"[-nuevos] {nombre_cuenta}: {nuevos_c} nuevos de {total}; registro: {_ruta_registro_nuevos(nombre_cuenta)} "
              f"({'ya existía' if registros_previos.get(nombre_cuenta) else 'NO existía: primera vez desde esta ubicación'}, "
              f"{registros_previos.get(nombre_cuenta, 0)} vistos antes)", file=sys.stderr)

    return nuevos + vistos_items, len(nuevos)


def _reimprimir_tras_reordenar_nuevos(items, solo_nuevos=False, cuantos_nuevos=None):
    """Vuelve a numerar e imprimir 'items' (ya reordenados por
    _marcar_nuevos_y_reordenar) -- se usa en vez de dejar que
    _navegar_carpeta_n/_recientes_todas_cuentas_n impriman su propio
    listado (ver su parámetro imprimir_listado_final=False): así los
    números que ve el usuario en pantalla corresponden de verdad al orden
    YA reordenado (nuevos primero), no al orden original por fecha."""
    if solo_nuevos:
        if items:
            print(f"\n{len(items)} correo(s) NUEVO(S) desde la última vez:")
    else:
        extra = f" -- los {cuantos_nuevos} primeros son nuevos; con -solo-nuevos verías solo esos" if cuantos_nuevos else ""
        print(f"\nÚltimos {len(items)} mensaje(s) de INBOX (nuevos primero{extra}):")
    for idx, (carpeta_salida, carpeta, _imap, _msg_id, _crudo, msg) in enumerate(items, 1):
        nombre_cuenta, _cfg = _cuenta_de_carpeta_salida(carpeta_salida)
        etiqueta = f"{nombre_cuenta}/{carpeta}"
        _imprimir_item(idx, fecha_mensaje(msg), msg, etiqueta, carpeta, 33, presupuesto_preview=LINEAS_CONTEXTO * LARGO_LINEA_PREVIEW)


def _navegar_carpeta_n(imap, carpeta_salida, carpeta, n, contexto=None, interactivo=True, imprimir_listado_final=True):
    """Como _navegar_carpeta, pero de una sola pasada y sin preguntar: muestra
    como mucho los 'n' más recientes de la carpeta. Para el modo -acc/-last.
    Si interactivo=False, no entra en el menú de acciones: devuelve
    (items, multi) para que quien llama decida qué hacer con ellos.
    imprimir_listado_final=False (-nuevos, pedido 2026-10-05/07): omite el
    encabezado y la impresión de cada mensaje (los errores de lectura/
    descarga SÍ se siguen avisando) -- para cuando quien llama va a
    reordenar 'items' (_marcar_nuevos_y_reordenar) y volver a imprimirlos
    YA reordenados (_reimprimir_tras_reordenar_nuevos), y el listado de
    aquí saldría con el orden (y la numeración) equivocados si se
    imprimiera dos veces."""
    status, datos = imap.search(None, "ALL")
    if status != "OK":
        _error("No se pudo listar la carpeta.")
        return ([], False) if not interactivo else None
    ids_totales = datos[0].split()
    ids_totales.reverse()
    if not ids_totales:
        print("No hay mensajes en esta carpeta.")
        return ([], False) if not interactivo else None

    lote = ids_totales[:n]
    if imprimir_listado_final:
        print(f"\nÚltimos {len(lote)} mensajes de '{carpeta}':")
    items = []
    for idx, msg_id in enumerate(lote, start=1):
        try:
            status, d = imap.fetch(msg_id, "(RFC822)")
            if status != "OK":
                print(f"  {idx}. (no se pudo descargar)")
                continue
            crudo = d[0][1]
            msg = email.message_from_bytes(crudo, policy=policy.default)
            if imprimir_listado_final:
                _imprimir_item(idx, fecha_mensaje(msg), msg, None, carpeta, 33, presupuesto_preview=LINEAS_CONTEXTO * LARGO_LINEA_PREVIEW)
            items.append((carpeta_salida, carpeta, imap, msg_id, crudo, msg))
        except Exception as e:
            print(f"  {idx}. (no se pudo leer: {e})")

    if not interactivo:
        return items, False
    menu_descargar_o_ver(items, multi=False, contexto=contexto)


def _navegar_todas_las_carpetas_n(imap, carpeta_salida, carpetas, profundidad, contexto=None, interactivo=True):
    """Una pasada (sin preguntar por más profundidad): recoge hasta
    'profundidad' mensajes de cada carpeta dada, los junta ordenados por
    fecha real (más reciente primero) y los muestra de una vez — sin pausar
    ni preguntar carpeta por carpeta.
    Si interactivo=True (por defecto): devuelve True si se debe seguir
    ofreciendo ampliar (hubo mensajes y no se saltó a otra acción).
    Si interactivo=False: no entra en el menú de acciones, devuelve
    (items, multi) para que quien llama decida qué hacer con ellos."""
    print(f"\nRecogiendo mensajes de {len(carpetas)} carpetas (hasta {profundidad} por carpeta)...")
    candidatos = []  # (fecha, carpeta, msg_id)
    for carpeta in carpetas:
        status, _ = _seleccionar_carpeta(imap, carpeta, readonly=True)
        if status != "OK":
            continue
        status, datos = imap.search(None, "ALL")
        if status != "OK":
            continue
        ids = datos[0].split()
        ids.reverse()
        for msg_id in ids[:profundidad]:
            try:
                # Se incluye MESSAGE-ID aunque aquí solo se use la fecha
                # para ordenar candidatos (no el id) — por coherencia con
                # el mismo fetch de _ejecutar_busqueda (ver nota ahí) y
                # para que cualquier uso futuro de este 'msg' no vuelva a
                # caer en el mismo fallo de id_amigable().
                status, d = imap.fetch(msg_id, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])")
                if status != "OK":
                    continue
                msg = email.message_from_bytes(d[0][1], policy=policy.default)
                candidatos.append((fecha_mensaje(msg), carpeta, msg_id))
            except Exception:
                continue

    if not candidatos:
        print("No hay mensajes en estas carpetas.")
        return ([], True) if not interactivo else False

    candidatos.sort(key=lambda c: c[0], reverse=True)

    print(f"\nPreparando vista previa de {len(candidatos)} mensajes...")
    items = []
    for _fecha, carpeta, msg_id in candidatos:
        status, _ = _seleccionar_carpeta(imap, carpeta, readonly=True)
        if status != "OK":
            continue
        status, d = imap.fetch(msg_id, "(RFC822)")
        if status != "OK":
            continue
        crudo = d[0][1]
        msg = email.message_from_bytes(crudo, policy=policy.default)
        items.append((carpeta_salida, carpeta, imap, msg_id, crudo, msg))

    print(f"\n{len(items)} mensajes de {len(carpetas)} carpetas, por fecha (más reciente primero):")
    for idx, (_cs, carpeta, _imap, _msg_id, _crudo, msg) in enumerate(items, 1):
        _imprimir_item(idx, fecha_mensaje(msg), msg, carpeta, carpeta, 27, presupuesto_preview=LINEAS_CONTEXTO * LARGO_LINEA_PREVIEW)

    if not interactivo:
        return items, True
    salto_a_otra_accion = menu_descargar_o_ver(items, multi=True, contexto=contexto)
    return not salto_a_otra_accion


def _navegar_todas_las_carpetas(imap, carpeta_salida, carpetas, contexto=None):
    """Modo interactivo: repite _navegar_todas_las_carpetas_n, dejando
    ampliar la profundidad (100/250/500) entre vueltas."""
    profundidad = MAX_INICIAL
    while True:
        seguir_aqui = _navegar_todas_las_carpetas_n(imap, carpeta_salida, carpetas, profundidad, contexto)
        if not seguir_aqui:
            return
        resp = input(
            "\n¿Ampliar cuántos se miran por carpeta? Escribe 100, 250 o 500, o Enter para terminar: "
        ).strip()
        if resp in ("100", "250", "500") and int(resp) > profundidad:
            profundidad = int(resp)
        else:
            break


# ---------- Carpetas (INBOX / una concreta / todas) ----------

def decodificar_utf7_imap(nombre):
    """Decodifica un nombre de carpeta en UTF-7 modificado (RFC 3501), p.ej.
    para carpetas con acentos/ñ creadas desde el webmail."""
    if isinstance(nombre, bytes):
        nombre = nombre.decode("ascii", errors="replace")
    resultado = []
    i, n = 0, len(nombre)
    while i < n:
        c = nombre[i]
        if c == "&":
            j = nombre.find("-", i + 1)
            if j == -1:
                j = n
            tramo = nombre[i + 1:j]
            if tramo == "":
                resultado.append("&")
            else:
                b64 = tramo.replace(",", "/")
                b64 += "=" * (-len(b64) % 4)
                try:
                    resultado.append(base64.b64decode(b64).decode("utf-16-be"))
                except Exception:
                    resultado.append(nombre[i:j + 1])
            i = j + 1
        else:
            resultado.append(c)
            i += 1
    return "".join(resultado)


_PATRON_LIST = re.compile(r'^\(([^)]*)\)\s+("(?:[^"\\]|\\.)*"|\S+)\s+("(?:[^"\\]|\\.)*"|.+)$')


def _parsear_linea_list(linea):
    """Parsea una línea de respuesta IMAP LIST -> (nombre_crudo,
    seleccionable, atributos_crudos) o None. atributos_crudos: la cadena
    completa entre paréntesis, tal cual la dio el servidor (ej.
    "\\HasNoChildren \\Sent") -- ver listar_carpetas_con_atributos
    (pedido 2026-10-05/07, -listar-carpetas); listar_carpetas (de
    siempre) sigue ignorándolos, igual que antes."""
    if isinstance(linea, bytes):
        linea = linea.decode("utf-8", errors="replace")
    m = _PATRON_LIST.match(linea.strip())
    if not m:
        return None
    flags, _delim, nombre_crudo = m.groups()
    nombre_crudo = nombre_crudo.strip()
    if nombre_crudo.startswith('"') and nombre_crudo.endswith('"'):
        nombre_crudo = nombre_crudo[1:-1]
    return nombre_crudo, ("\\Noselect" not in flags), flags


def _entrecomillar_imap(texto):
    """Prepara un nombre de carpeta como cadena IMAP entre comillas, con
    las comillas y barras invertidas internas escapadas. imaplib NO
    entrecomilla los argumentos por sí solo, así que un nombre de carpeta
    con espacios (muy habitual en Gmail: "[Gmail]/Todos los mensajes",
    "[Gmail]/All Mail"...) rompía el comando IMAP al mandarse sin
    comillas — el servidor lo veía como varias palabras sueltas en vez de
    un único nombre, y respondía 'Could not parse command'."""
    escapado = texto.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escapado}"'


def _seleccionar_carpeta(imap, carpeta, readonly=True):
    """Como imap.select(carpeta, readonly), pero entrecomillando el
    nombre de la carpeta correctamente (ver _entrecomillar_imap) — usar
    siempre esta función en vez de imap.select() directo."""
    resultado = imap.select(_entrecomillar_imap(carpeta), readonly=readonly)
    # Ronda AC: se recuerda en la propia conexión qué carpeta tiene abierta, para poder
    # reabrirla si hay que reconectar a mitad de trabajo (ver _con_conexion_viva).
    try:
        imap._carpeta_seleccionada = (carpeta, readonly) if resultado and resultado[0] == "OK" else None
    except Exception:
        pass
    return resultado


def listar_carpetas(imap):
    """Devuelve [(nombre_crudo, nombre_visible, seleccionable), ...] de todo el buzón."""
    status, datos = imap.list()
    resultado = []
    if status != "OK":
        return resultado
    for linea in datos:
        if not linea:
            continue
        analizado = _parsear_linea_list(linea)
        if analizado is None:
            continue
        nombre_crudo, seleccionable, _flags = analizado
        resultado.append((nombre_crudo, decodificar_utf7_imap(nombre_crudo), seleccionable))
    return resultado


def listar_carpetas_con_atributos(imap):
    """Como listar_carpetas, pero con un 4º elemento por carpeta: los
    atributos crudos de su línea LIST (ej. "\\HasNoChildren \\Sent") --
    ver _atributos_especiales/_es_cuenta_gmail/_clasificar_carpeta_gmail,
    pedido 2026-10-05/07 para -listar-carpetas. Devuelve [(nombre_crudo,
    nombre_visible, seleccionable, atributos_crudos), ...]."""
    status, datos = imap.list()
    resultado = []
    if status != "OK":
        return resultado
    for linea in datos:
        if not linea:
            continue
        analizado = _parsear_linea_list(linea)
        if analizado is None:
            continue
        nombre_crudo, seleccionable, flags = analizado
        resultado.append((nombre_crudo, decodificar_utf7_imap(nombre_crudo), seleccionable, flags))
    return resultado


# Atributos de una línea IMAP LIST puramente ESTRUCTURALES (cómo se puede
# usar la carpeta: si tiene hijas, si está marcada como "con cosas
# nuevas"...) que no dicen nada sobre QUÉ ES la carpeta -- para distinguir
# carpetas reales de etiquetas de Gmail solo importan los demás (SPECIAL-USE,
# RFC 6154: \All, \Archive, \Drafts, \Flagged, \Junk, \Sent, \Trash -- Gmail
# añade además \Important).
_ATRIBUTOS_LIST_ESTRUCTURALES = {"\\noselect", "\\haschildren", "\\hasnochildren", "\\marked", "\\unmarked"}


def _atributos_especiales(flags):
    """Atributos SPECIAL-USE presentes en 'flags' (la cadena cruda entre
    paréntesis de una línea LIST), sin los puramente estructurales (ver
    _ATRIBUTOS_LIST_ESTRUCTURALES)."""
    return [a for a in flags.split() if a.lower() not in _ATRIBUTOS_LIST_ESTRUCTURALES]


def _es_cuenta_gmail(cfg):
    """True si el servidor configurado para esta cuenta es de Gmail/Google
    Mail -- la única señal fiable (no depende del idioma de la cuenta, a
    diferencia de mirar si algún nombre de carpeta contiene "gmail")."""
    servidor = str(cfg.get("server", "") or "").lower()
    return "gmail.com" in servidor or "googlemail.com" in servidor


def _clasificar_carpeta_gmail(nombre_crudo, especiales):
    """Solo tiene sentido llamarla en una cuenta Gmail (_es_cuenta_gmail):
    "carpeta" si es INBOX o trae algún atributo SPECIAL-USE (Enviados,
    Papelera, Spam, Borradores, Destacados, Todos los mensajes --
    estructurales de verdad), "etiqueta" en cualquier otro caso. Gmail
    expone CADA etiqueta -- de sistema, como "Updates"/"Social"/
    "Promotions", o creada por el propio usuario -- como si fuera una
    carpeta IMAP más, sin ningún atributo que la distinga de una carpeta
    real: esa es justamente la confusión que pidió aclarar el usuario."""
    if nombre_crudo.strip().upper() == "INBOX" or especiales:
        return "carpeta"
    return "etiqueta"


def _imprimir_carpetas_cuenta(nombre_cuenta, imap, cfg):
    """Para -listar-carpetas: imprime todas las carpetas de 'nombre_cuenta'.
    En cuentas Gmail, marca cada una como carpeta de verdad o etiqueta
    (ver _clasificar_carpeta_gmail) -- en el resto de proveedores (Yahoo,
    GMX, Hotmail/Outlook) no existe ese concepto, así que solo se listan,
    con cualquier atributo especial que el servidor sí mande como dato
    adicional (nunca como "etiqueta": ahí todas son carpetas de verdad)."""
    carpetas = listar_carpetas_con_atributos(imap)
    print(f"\n{nombre_cuenta}: {len(carpetas)} carpeta(s)")
    if not carpetas:
        print("  (no se pudo obtener la lista de carpetas)")
        return
    es_gmail = _es_cuenta_gmail(cfg)
    hay_etiquetas = False
    for nombre_crudo, nombre_visible, seleccionable, flags in carpetas:
        especiales = _atributos_especiales(flags)
        nombres_especiales = [a.lstrip("\\") for a in especiales]
        nota_sel = "  [no seleccionable]" if not seleccionable else ""
        nota_especiales = f"  ({', '.join(nombres_especiales)})" if especiales else ""
        if es_gmail:
            tipo = _clasificar_carpeta_gmail(nombre_crudo, especiales)
            if tipo == "etiqueta":
                hay_etiquetas = True
            marca = "carpeta " if tipo == "carpeta" else "ETIQUETA"
            print(f"  [{marca}] {nombre_visible}{nota_especiales}{nota_sel}")
        else:
            print(f"  {nombre_visible}{nota_especiales}{nota_sel}")
    if es_gmail and hay_etiquetas:
        print(
            "  (Gmail: ETIQUETA = una etiqueta tuya o de sistema -- Updates/Social/Promociones, "
            "o creada por ti -- que el servidor expone como si fuera una carpeta más, pero no lo "
            "es de verdad; carpeta = estructural (INBOX, o con atributo especial del servidor: "
            "Enviados, Papelera, Spam, Borradores, Destacados, Todos los mensajes).)"
        )


def elegir_carpetas(imap, cfg):
    """Pregunta qué carpeta(s) usar y devuelve una lista de nombres IMAP (crudos)."""
    print("\n¿Qué carpeta(s) quieres usar?")
    print("  1. Solo la carpeta principal (INBOX)")
    print("  2. Elegir una carpeta concreta de la lista")
    print("  3. TODAS las carpetas (Enviados, Spam, Borradores, etc.)")
    while True:
        opcion = input("Opción: ").strip()
        if opcion in ("1", "2", "3"):
            break
        print("Escribe 1, 2 o 3.")

    if opcion == "1":
        return [cfg["folder"]]

    carpetas = listar_carpetas(imap)
    seleccionables = [(crudo, visible) for crudo, visible, sel in carpetas if sel]
    if not seleccionables:
        print("No se pudo obtener la lista de carpetas; se usará solo INBOX.")
        return [cfg["folder"]]

    if opcion == "3":
        if any("gmail" in c.lower() for c, _ in seleccionables):
            print(
                "Aviso (Gmail): si aparece '[Gmail]/All Mail' o similar, va a duplicar "
                "mensajes que ya están en INBOX/Enviados/etc. — es una vista de todo lo demás."
            )
        return [c for c, _ in seleccionables]

    print("\nCarpetas disponibles:")
    for idx, (_, visible) in enumerate(seleccionables, 1):
        print(f"  {idx}. {visible}")
    while True:
        sel = input("Número de carpeta: ").strip()
        try:
            idx = int(sel)
            if 1 <= idx <= len(seleccionables):
                return [seleccionables[idx - 1][0]]
        except ValueError:
            pass
        print("Número no válido.")


def _prefijo_carpeta(carpeta):
    """Convierte un nombre de carpeta IMAP en un prefijo seguro para nombres de fichero."""
    return re.sub(r"[^A-Za-z0-9_-]+", "_", carpeta).strip("_")[:20]


# ---------- Búsqueda por campo y descarga masiva ----------

CAMPOS_DISPONIBLES = {
    "1": ("to", "Para (To)"),
    "2": ("from", "De (From)"),
    "3": ("cc", "CC"),
    "4": ("bcc", "BCC"),
    "5": ("subject", "Asunto"),
    "6": ("body", "Cuerpo del mensaje"),
    "7": ("id", "ID del mensaje (para volver a encontrarlo más adelante)"),
}


def pedir_campos():
    print("\n¿En qué campos buscar? (combina varios, ej: 1,2,3,6, o solo 6)")
    for clave, (_, etiqueta) in CAMPOS_DISPONIBLES.items():
        print(f"  {clave}. {etiqueta}")
    while True:
        resp = input("Campos: ").strip()
        claves = [c.strip() for c in resp.split(",") if c.strip()]
        if claves and all(c in CAMPOS_DISPONIBLES for c in claves):
            return [CAMPOS_DISPONIBLES[c][0] for c in claves]
        print("Entrada no válida, vuelve a intentarlo (ej: 1,2,3,6).")


def pedir_texto_y_modo(campos):
    """Pide el texto a buscar y, si aplica, el modo exacto/incluye para direcciones.
    Devuelve (texto_low, modo_exacto) o (None, None) si se cancela."""
    if "bcc" in campos:
        print(
            "Nota: el campo BCC casi nunca aparece en mensajes RECIBIDOS (solo lo ve "
            "quien lo envía) — esta búsqueda encontrará poco salvo en una carpeta de Enviados."
        )

    texto_busqueda = input("Texto a buscar: ").strip()
    if not texto_busqueda:
        print("Búsqueda vacía, cancelado.")
        return None, None
    texto_low = texto_busqueda.lower()

    modo_exacto = False
    if {"to", "from", "cc", "bcc"} & set(campos):
        print("\nPara los campos de dirección (To/From/CC/BCC):")
        print("  1. Coincidencia EXACTA: la dirección completa debe ser igual al texto")
        print("     (ej. buscar 'bbva' así NO encuentra nada, hace falta el email entero)")
        print("  2. INCLUYE el texto (ej. 'bbva' sí encuentra")
        print("     'respuestareclamaciones.sac@bbva.com') — normalmente es esta")
        eleccion = input("Elige 1 o 2 (Enter = 2, incluye): ").strip()
        modo_exacto = eleccion == "1"

    return texto_low, modo_exacto


def _formatear_duracion(segundos):
    """'30 segundos', o si son 60+, con la equivalencia en minutos:
    '125 segundos (más de 2 minutos)' / '120 segundos (2 minutos)'."""
    segundos = int(segundos)
    if segundos < 60:
        return f"{segundos} segundos"
    minutos, resto = divmod(segundos, 60)
    palabra = "minuto" if minutos == 1 else "minutos"
    if resto == 0:
        return f"{segundos} segundos ({minutos} {palabra})"
    return f"{segundos} segundos (más de {minutos} {palabra})"


class _StdinNoDisponible(Exception):
    """La entrada estándar no es un flujo interactivo utilizable: está en
    EOF (cerrada, redirigida desde /dev/null, desconectada del terminal
    por cómo se lanzó el proceso — p.ej. a través de un script .sh que
    corta o redirige la entrada, o en segundo plano). Leer de un flujo en
    EOF no espera nunca y devuelve una línea vacía al instante; sin esta
    distinción, eso se confundía con 'el usuario pulsó Enter sin escribir
    nada', y la búsqueda parecía ignorar silenciosamente cualquier cosa
    que el usuario escribiera (bug reportado 2026-09-18)."""


def _input_con_limite(prompt, limite_segundos):
    """Como input(), pero si no hay respuesta en 'limite_segundos', devuelve
    None en vez de quedarse esperando. Si el terminal no lo permite (poco
    habitual), recurre a input() normal, sin límite. Si la entrada
    estándar está en EOF (no es un terminal interactivo de verdad),
    lanza _StdinNoDisponible en vez de devolver una respuesta vacía —
    ver _StdinNoDisponible."""
    print(prompt, end="", flush=True)
    try:
        listos, _, _ = select.select([sys.stdin], [], [], limite_segundos)
        if not listos:
            return None
        linea = sys.stdin.readline()
        if linea == "":
            # readline() devuelve "" SOLO en EOF real — una línea de
            # verdad, aunque el usuario no escriba nada más que Enter,
            # siempre trae como mínimo el '\n'.
            raise _StdinNoDisponible()
        return linea.rstrip("\n")
    except _StdinNoDisponible:
        raise
    except Exception:
        return input()


_PATRON_MULTIPLICADOR = re.compile(r"^(-?\d+)\s*x$", re.IGNORECASE)

# Una vez detectado que la entrada estándar no es utilizable (EOF), se
# recuerda para el resto de la ejecución: no tiene sentido seguir
# intentando leer de ella en cada aviso — solo añadiría ruido, aunque no
# se quede colgada (ver _StdinNoDisponible). Es una lista de 1 elemento
# en vez de una variable module-level normal para poder modificarla desde
# _preguntar_seguir_buscando sin declarar 'global'.
_ESTADO_ENTRADA = {"sin_entrada": False}


def _preguntar_seguir_buscando(intervalo_actual, interactivo=True):
    """Muestra el intervalo de aviso actual y pregunta si seguir buscando.
    Devuelve (seguir: bool, nuevo_intervalo: int). Escribir 'Nx' (ej. '2x',
    '4x') multiplica el intervalo (más espaciado); '-Nx' (ej. '-2x', '-4x')
    lo DIVIDE (más frecuente). Si el intervalo (nuevo o no) llega a 5+
    minutos, el próximo aviso solo espera LIMITE_ESPERA_RESPUESTA segundos
    una respuesta y sigue sola si no llega; si baja de 5 minutos (p.ej. tras
    un '-Nx'), el siguiente aviso vuelve a esperar normal, sin ese límite.
    Si interactivo=False (modo comando directo): la espera es SIEMPRE
    breve (LIMITE_ESPERA_RESPUESTA segundos), no solo a partir de los 5
    minutos — así nunca se queda colgada esperando indefinidamente en un
    script desatendido, pero deja una ventana breve por si hay alguien
    delante que quiera pararla o cambiar el intervalo.
    Si la entrada estándar resulta no ser utilizable (EOF — ver
    _StdinNoDisponible), lo avisa UNA vez y, de ahí en adelante (para el
    resto de esta ejecución), deja de intentar preguntar del todo.
    Si la entrada estándar NO es una terminal interactiva de verdad
    (sys.stdin.isatty() == False: viene de una tubería, de un fichero
    redirigido, o de /dev/null), NUNCA se intenta leer de ella — ni
    siquiera con límite de tiempo, y esta comprobación va ANTES de
    intentar nada (no solo como reacción a un EOF). Motivo (bug
    reportado 2026-09-25): si la entrada es una TUBERÍA con datos de
    verdad esperando — ej. "ls *.txt | while read -r f; do python3
    correo.py ... -ver-pdf X; done" —, select() la ve "lista para
    leer" porque hay bytes esperando, pero esos bytes son la SIGUIENTE
    línea del 'while read' de quien llamó a este programa, no una
    respuesta a este aviso. Leerla de todas formas se la "robaría" a
    ese bucle — el script parece terminar antes de tiempo, sin ningún
    error visible, porque 'read' se queda sin líneas que en realidad
    seguían ahí, solo que ya consumidas por este programa."""
    hora_actual = datetime.now().strftime("%H:%M")

    if _ESTADO_ENTRADA["sin_entrada"]:
        print(f"  [{hora_actual}] Aviso configurado a {_formatear_duracion(intervalo_actual)} (sigue sola: sin entrada disponible).")
        return True, intervalo_actual

    if not sys.stdin.isatty():
        _ESTADO_ENTRADA["sin_entrada"] = True
        print(
            f"\n  [{hora_actual}] Aviso configurado a {_formatear_duracion(intervalo_actual)}.\n"
            "  La entrada estándar no es una terminal interactiva (viene de una tubería, de un "
            "fichero redirigido, o de /dev/null) — no se va a intentar leer una respuesta de ahí: "
            "si fuera una tubería de verdad (p.ej. de un 'while read' que lanza este programa en "
            "cada vuelta), leer de esa misma entrada le robaría líneas a ese bucle. La búsqueda "
            "sigue sola sin volver a preguntar; lánzala directamente desde una terminal "
            "interactiva si quieres poder detenerla o cambiar el intervalo de aviso."
        )
        return True, intervalo_actual

    print(f"  [{hora_actual}] Aviso configurado a {_formatear_duracion(intervalo_actual)}.")
    prompt = "  ¿Seguir buscando? [S/n, o 'Nx'/'-Nx' para espaciar más/menos los avisos, ej. 2x o -2x]: "

    try:
        if not interactivo or intervalo_actual >= 300:
            resp = _input_con_limite(prompt, LIMITE_ESPERA_RESPUESTA)
            if resp is None:
                print(f"  (sin respuesta en {LIMITE_ESPERA_RESPUESTA}s, sigue buscando sola)")
                return True, intervalo_actual
        else:
            resp = input(prompt)
    except (_StdinNoDisponible, EOFError):
        _ESTADO_ENTRADA["sin_entrada"] = True
        print(
            "\n  Aviso: la entrada estándar no está disponible para responder a este aviso "
            "(¿se ha lanzado el programa sin terminal interactivo — p.ej. desde un script que "
            "redirige o corta la entrada, o en segundo plano? prueba 'python3 -c \"import sys; "
            "print(sys.stdin.isatty())\"' con el mismo lanzador para confirmarlo). La búsqueda "
            "sigue sola sin volver a preguntar; lánzala directamente desde una terminal "
            "interactiva si quieres poder detenerla o cambiar el intervalo de aviso."
        )
        return True, intervalo_actual

    resp = (resp or "").strip().lower()
    if resp in ("n", "no"):
        return False, intervalo_actual
    m = _PATRON_MULTIPLICADOR.match(resp)
    if m:
        n = int(m.group(1))
        if n == 0:
            print("  '0x' no es válido, se mantiene el intervalo.")
            return True, intervalo_actual
        if n > 0:
            nuevo_intervalo = intervalo_actual * n
        else:
            nuevo_intervalo = max(1, round(intervalo_actual / abs(n)))
        print(f"  Avisos ahora cada {_formatear_duracion(nuevo_intervalo)}.")
        return True, nuevo_intervalo
    return True, intervalo_actual


def _ejecutar_busqueda(plan, campos, texto_low, modo_exacto, necesita_cuerpo, contexto=None, interactivo=True, conexiones=None, ids_multiples=None, fecha_desde=None, fecha_hasta=None, con_adjuntos=None, tamano_ronda=None, tamano_ronda_inbox=None, modo_desatentido=False, intervalo_inicial=None, tiempo_maximo=None):
    """plan: [(cuenta, carpeta, imap, ids), ...]. Analiza TODO el plan como una
    sola pasada (sin diferenciar ni pausar por carpeta), avisando cada cierto
    tiempo si tarda, y muestra TODOS los resultados juntos al final, en una
    única lista ordenada por fecha real (más reciente primero), con vista
    previa, antes de ofrecer descargar o ver el texto completo de alguno.
    contexto (opcional): ver menu_descargar_o_ver — permite "buscar de
    nuevo" / "ver recientes de nuevo" sin volver al menú anterior.
    conexiones (opcional): dict cuenta -> (imap, cfg) — si se da, antes de
    cada ronda se comprueba que la conexión de esa cuenta sigue viva y se
    reconecta sola si el servidor la cerró por inactividad (una búsqueda
    larga, con pausas por el aviso de "¿seguir buscando?", puede tardar
    más que el tiempo de inactividad que tolera el servidor). Se actualiza
    en sitio (el diccionario del que llama ve la conexión nueva también).
    tamano_ronda_inbox (opcional): tamaño de ronda SOLO para las carpetas
    INBOX del plan — si no se da, usa el mismo tamano_ronda que el resto.
    Pensado para búsquedas por ID (ver comando_directo): con muchas
    carpetas de por medio, repartir el esfuerzo a partes iguales entre
    todas (el reparto normal de "ronda") deja a INBOX con solo 1/N del
    esfuerzo por vuelta, así que un mensaje que no esté entre los poquísimos
    más recientes puede tardar muchísimo en alcanzarse aunque INBOX vaya
    primero (_priorizar_inbox) — con un tamano_ronda_inbox bastante mayor
    que el del resto, INBOX (la carpeta con más probabilidad, con mucha
    diferencia, de tener un mensaje del que ya se conoce el ID) avanza
    mucho más rápido por vuelta, sin dejar de repartir el resto de
    carpetas entre sí a su ritmo normal (más barato, para el caso de que
    el mensaje esté en alguna de ellas en vez de en INBOX).
    campos=None (en vez de una lista): no se filtra por texto — todos los
    mensajes pasan esta parte (útil para -buscarfecha/-buscaradjunto* sin
    -buscar/-buscarcuerpo). fecha_desde/fecha_hasta (datetime con zona
    horaria, opcionales): solo cuentan los mensajes en ese rango.
    con_adjuntos (True/False/None): si no es None, filtra por si el
    mensaje tiene o no archivos adjuntos (fuerza a descargar el mensaje
    completo para poder comprobarlo).
    Si interactivo=False: nunca pregunta nada (ni para confirmar la
    búsqueda en el cuerpo, ni para seguir buscando si tarda — avisa del
    progreso igualmente, pero sin pausar nunca), y no entra en el menú de
    acciones: devuelve (items, multi) para que quien llama decida.
    modo_desatentido (-ds, opcional): a diferencia de interactivo=False
    (que SÍ sigue avisando y esperando muy brevemente una respuesta, por
    si hay alguien delante — ver _preguntar_seguir_buscando), aquí NUNCA
    se pregunta nada en absoluto, ni siquiera esa espera breve: en vez del
    aviso normal (con la hora, el recuento y la pregunta), se muestra un
    único progreso que se autosobreescribe en la misma línea (correos
    vistos / encontrados, sin fecha ni hora), y se omiten además las
    líneas decorativas de inicio ("Analizando N mensajes...") y de cada
    fuente nueva — solo quedan los avisos de fallo de verdad (conexión
    perdida, carpeta que no se pudo abrir...), cada uno en su propia línea.
    intervalo_inicial (segundos, opcional): sustituye a
    INTERVALO_AVISO_INICIAL como intervalo de partida — cada cuánto se
    refresca el aviso/progreso (con modo_desatentido) o se pregunta "¿seguir
    buscando?" (sin él).
    tiempo_maximo (segundos, opcional): tiempo MÁXIMO total para esta
    búsqueda — al alcanzarlo, se detiene sola con lo encontrado hasta ese
    momento (avisando por qué), sin esperar al siguiente aviso programado."""
    multi = len(plan) > 1
    total = sum(len(ids) for _, _, _, ids in plan)
    if total == 0:
        print("No hay mensajes que analizar.")
        return ([], multi) if not interactivo else None

    if necesita_cuerpo and interactivo:
        resp = input(
            f"\nBuscar en el CUERPO de {total} mensajes puede tardar bastante "
            "(hay que descargar cada uno entero). ¿Continuar? [s/N]: "
        ).strip().lower()
        if resp != "s":
            print("Cancelado.")
            return

    # OJO: MESSAGE-ID tiene que estar en esta lista sí o sí. id_amigable()
    # se calcula a partir de la cabecera Message-ID real; si este fetch no
    # la trae, id_amigable() cae a su respaldo (from|date|subject), que da
    # un id DISTINTO al que se calculó (y se le mostró al usuario) cuando
    # el mensaje se descargó completo en otro momento — así, buscar por
    # "id:XXXX" con el id real y correcto nunca coincidía con nada, por
    # más que ese mensaje estuviera en la bandeja de entrada (bug
    # reportado 2026-09-18: "-descargar id:X" no encontraba un correo
    # reciente de la carpeta INBOX de gmx).
    campos_cabecera = "(BODY.PEEK[HEADER.FIELDS (TO FROM CC BCC SUBJECT DATE MESSAGE-ID)])"

    if not modo_desatentido:
        print(f"Analizando {total} mensajes de {len(plan)} carpeta(s)...")
        print("(Los resultados se muestran todos juntos al final, ordenados por fecha, con vista previa.)")
    coincidencias = []  # (fecha, cuenta, carpeta, imap, msg_id, crudo_o_None, msg, campos_ok)
    inicio = time.monotonic()
    intervalo_actual = intervalo_inicial if intervalo_inicial is not None else INTERVALO_AVISO_INICIAL
    proximo_aviso = intervalo_actual
    # Estado de la línea de progreso de -ds: True mientras la última cosa
    # impresa fue esa línea SIN salto final (se autosobreescribe con \r) —
    # así, si hay que meter un aviso de verdad (fallo de conexión, etc.) de
    # por medio, _avisar() sabe que primero tiene que bajar de línea para
    # no mezclarlo con ella.
    estado_linea = {"ds_abierta": False}

    def _avisar(texto, usar_stderr=False):
        if estado_linea["ds_abierta"]:
            print()
            estado_linea["ds_abierta"] = False
        _error(texto) if usar_stderr else print(texto)
    i = 0
    detener = False
    cuentas_perdidas = set()  # cuentas cuya reconexión falló — se omiten el resto de rondas

    # Se avanza en RONDAS: de cada fuente (cuenta+carpeta) se miran como
    # mucho tamano_ronda mensajes (los más recientes que le falten) antes
    # de pasar a la siguiente, repitiendo la vuelta a todas hasta
    # agotarlas. Así una cuenta con mucho correo nunca retrasa llegar a
    # las demás: un mensaje de hoy en cualquier cuenta se analiza dentro
    # de la primera ronda, no solo tras terminar con las cuentas
    # anteriores. Por defecto usa TAMANO_RONDA_BUSQUEDA, pero se puede
    # pedir una ronda más pequeña (ej. búsqueda por ID: ver comando_directo).
    tamano_ronda = tamano_ronda or TAMANO_RONDA_BUSQUEDA
    tamano_ronda_inbox = tamano_ronda_inbox or tamano_ronda
    cursores = [0] * len(plan)
    activas = set(range(len(plan)))

    while activas and not detener:
        for idx in sorted(activas):
            if detener:
                break
            cuenta, carpeta, imap_plan, ids = plan[idx]
            inicio_c = cursores[idx]
            if inicio_c >= len(ids):
                activas.discard(idx)
                continue
            if cuenta in cuentas_perdidas:
                activas.discard(idx)
                continue
            ronda_de_esta_fuente = tamano_ronda_inbox if carpeta.strip().upper() == "INBOX" else tamano_ronda
            fin_c = min(inicio_c + ronda_de_esta_fuente, len(ids))

            imap = _imap_vivo_para(conexiones, cuenta, imap_plan, estado_linea=(estado_linea if modo_desatentido else None))
            if imap is None:
                _avisar(f"  No se pudo mantener la conexión con '{cuenta}'; se omite el resto de sus carpetas.", usar_stderr=True)
                cuentas_perdidas.add(cuenta)
                activas.discard(idx)
                continue

            if multi and inicio_c == 0 and not modo_desatentido:
                print(f"  -- Analizando {cuenta}/{carpeta} ({len(ids)} mensajes) --")
            status, _ = _seleccionar_carpeta(imap, carpeta, readonly=True)
            if status != "OK":
                _avisar(f"  Aviso: no se pudo abrir '{cuenta}/{carpeta}', se omite.")
                activas.discard(idx)
                continue

            for pos in range(inicio_c, fin_c):
                msg_id = ids[pos]
                i += 1
                origen_perdido = False
                try:
                    msg = None
                    crudo = None
                    if necesita_cuerpo:
                        status, d = imap.fetch(msg_id, "(RFC822)")
                        if status == "OK":
                            crudo = d[0][1]
                            msg = email.message_from_bytes(crudo, policy=policy.default)
                        else:
                            _avisar(f"  Aviso: no se pudo descargar el mensaje {msg_id.decode()} en {cuenta}/{carpeta}.")
                    else:
                        status, d = imap.fetch(msg_id, campos_cabecera)
                        if status == "OK":
                            msg = email.message_from_bytes(d[0][1], policy=policy.default)
                        else:
                            _avisar(f"  Aviso: no se pudo leer la cabecera del mensaje {msg_id.decode()} en {cuenta}/{carpeta}.")

                    if msg is not None:
                        coincide = True
                        campos_ok = set()
                        if campos is not None:
                            campos_ok = campos_que_coinciden(msg, campos, texto_low, modo_exacto, ids_multiples)
                            coincide = bool(campos_ok)
                        if coincide and fecha_desde is not None and fecha_mensaje(msg) < fecha_desde:
                            coincide = False
                        if coincide and fecha_hasta is not None and fecha_mensaje(msg) > fecha_hasta:
                            coincide = False
                        if coincide and con_adjuntos is not None and _tiene_adjuntos(msg) != con_adjuntos:
                            coincide = False

                        if coincide:
                            coincidencias.append((fecha_mensaje(msg), cuenta, carpeta, imap, msg_id, crudo, msg, campos_ok))
                            # Si se buscan IDs concretos (ej. -ver "id:X,id:Y"),
                            # no hace falta seguir escaneando el resto de la
                            # cuenta una vez se han encontrado TODOS los
                            # pedidos — son únicos, así que continuar no
                            # serviría de nada y solo tardaría.
                            if ids_multiples is not None:
                                ids_encontrados = {
                                    p for c in coincidencias for p in ids_multiples
                                    if id_amigable(c[6]).startswith(p)
                                }
                                if len(ids_encontrados) >= len(set(ids_multiples)):
                                    detener = True
                except Exception as e:
                    _avisar(f"  Aviso: fallo leyendo el mensaje {msg_id.decode()} en {cuenta}/{carpeta}: {e}")
                    # La conexión puede haber muerto a mitad de la ronda (no
                    # solo entre rondas) — se comprueba y, si hace falta, se
                    # reconecta para las carpetas/mensajes que falten.
                    imap_recuperado = _imap_vivo_para(conexiones, cuenta, imap, estado_linea=(estado_linea if modo_desatentido else None))
                    if imap_recuperado is None:
                        cuentas_perdidas.add(cuenta)
                        origen_perdido = True
                    else:
                        imap = imap_recuperado

                # Si tarda, se avisa con un recuento (y en qué cuenta/carpeta va),
                # y se pregunta si seguir — en modo no interactivo, la espera es
                # siempre breve (nunca se queda colgada), pero da una ventana
                # real para poder detener la búsqueda si hace falta. Esto se
                # comprueba SIEMPRE, coincida o no coincida el mensaje (antes,
                # un mensaje que no coincidía saltaba directamente esta
                # comprobación, así que en la práctica el aviso solo salía
                # cuando aparecía una coincidencia — pudiendo tardar mucho más
                # de lo configurado si había pocas o ninguna).
                transcurrido = time.monotonic() - inicio
                if tiempo_maximo is not None and transcurrido >= tiempo_maximo:
                    _avisar(
                        f"  Tiempo máximo de búsqueda alcanzado ({_formatear_duracion(tiempo_maximo)}) — "
                        f"se detiene tras analizar {i}/{total} mensajes ({len(coincidencias)} encontrados hasta ahora)."
                    )
                    detener = True
                elif transcurrido >= proximo_aviso:
                    if modo_desatentido:
                        print(
                            f"\r  Progreso: {i}/{total} correos vistos, {len(coincidencias)} encontrados...\033[K",
                            end="", flush=True,
                        )
                        estado_linea["ds_abierta"] = True
                    else:
                        print(
                            f"  ({int(transcurrido)}s transcurridos — {i}/{total} analizados, "
                            f"{len(coincidencias)} encontrados hasta ahora — ahora en {cuenta}/{carpeta})"
                        )
                        seguir, intervalo_actual = _preguntar_seguir_buscando(intervalo_actual, interactivo=interactivo)
                        if not seguir:
                            print("  Búsqueda detenida por el usuario.")
                            detener = True
                    proximo_aviso = transcurrido + intervalo_actual

                if detener or origen_perdido:
                    break

            cursores[idx] = fin_c
            if cursores[idx] >= len(ids):
                activas.discard(idx)

    if not coincidencias:
        print(f"\n0 mensajes coinciden (analizados {i} de {total}).")
        return ([], multi) if not interactivo else None

    # Asegurar contenido completo (hace falta para vista previa y para "ver")
    print(f"\nPreparando vista previa de {len(coincidencias)} mensajes encontrados...")
    ultima_carpeta = {}
    resueltos = []  # (fecha, cuenta, carpeta, imap, msg_id, crudo, msg, campos_ok)
    for fecha, cuenta, carpeta, imap, msg_id, crudo, msg, campos_ok in coincidencias:
        if crudo is None:
            imap = _imap_vivo_para(conexiones, cuenta, imap, estado_linea=(estado_linea if modo_desatentido else None))
            if imap is None:
                print(f"No se pudo recuperar la conexión con '{cuenta}' para completar este mensaje.")
                continue
            # Se mira la carpeta que tiene abierta ESA conexión (no un dict por cuenta: si se
            # había reconectado, el dict mentía y se hacía FETCH sin carpeta abierta).
            if getattr(imap, "_carpeta_seleccionada", None) != (carpeta, True):
                status, _ = _seleccionar_carpeta(imap, carpeta, readonly=True)
                if status != "OK":
                    print(f"No se pudo reabrir '{carpeta}' en '{cuenta}'.")
                    continue
                ultima_carpeta[cuenta] = carpeta
            status, d = imap.fetch(msg_id, "(RFC822)")
            if status != "OK":
                print(f"No se pudo recuperar un mensaje de '{cuenta}/{carpeta}'.")
                continue
            crudo = d[0][1]
            msg = email.message_from_bytes(crudo, policy=policy.default)
        resueltos.append((fecha, cuenta, carpeta, imap, msg_id, crudo, msg, campos_ok))

    resueltos.sort(key=lambda c: c[0], reverse=True)  # más reciente primero, cruzando carpetas/cuentas

    print(f"\n{len(resueltos)} mensajes coinciden (analizados {i} de {total}), por fecha:")
    items = []  # para menu_descargar_o_ver
    for idx, (fecha, cuenta, carpeta, imap_conn, msg_id, crudo, msg, campos_ok) in enumerate(resueltos, 1):
        prefijo = f"{cuenta}/{carpeta}" if multi else None
        # Vista previa ACOTADA (antes sin límite: si el término no estaba en
        # el cuerpo -- p. ej. se buscó por asunto/remitente -- se mostraba
        # el cuerpo ENTERO de cada correo). Con el término en el cuerpo se
        # conserva el contexto de LINEAS_CONTEXTO líneas a cada lado, con un
        # tope proporcional; en el resto, LINEAS_CONTEXTO líneas de texto.
        lineas_preview = (2 * LINEAS_CONTEXTO + 1) if (campos_ok and "body" in campos_ok) else LINEAS_CONTEXTO
        _imprimir_item(idx, fecha, msg, prefijo, carpeta, 27, campos_ok, texto_low,
                       presupuesto_preview=lineas_preview * LARGO_LINEA_PREVIEW)
        items.append((_carpeta_salida_para_cuenta(cuenta), carpeta, imap_conn, msg_id, crudo, msg))

    if not interactivo:
        return items, multi
    menu_descargar_o_ver(items, multi, contexto)


def buscar_y_descargar(imap, cfg, nombre_cuenta):
    """Búsqueda dentro de UNA cuenta (una o varias de sus carpetas).
    Devuelve la conexión imap (la misma, o una nueva si hubo que reconectar)."""
    imap = _con_conexion_viva(imap, cfg, nombre_cuenta)
    if imap is None:
        return None
    carpetas_a_buscar = elegir_carpetas(imap, cfg)
    campos = pedir_campos()
    texto_low, modo_exacto = pedir_texto_y_modo(campos)
    if texto_low is None:
        return imap
    necesita_cuerpo = "body" in campos

    plan = []
    for carpeta in carpetas_a_buscar:
        status, _ = _seleccionar_carpeta(imap, carpeta, readonly=True)
        if status != "OK":
            print(f"Aviso: no se pudo abrir '{carpeta}', se omite.")
            continue
        status, datos = imap.search(None, "ALL")
        if status != "OK":
            print(f"Aviso: no se pudo listar '{carpeta}', se omite.")
            continue
        ids = datos[0].split()
        ids.reverse()  # más reciente primero
        plan.append((nombre_cuenta, carpeta, imap, ids))

    carpeta_salida = _carpeta_salida_para_cuenta(nombre_cuenta)
    contexto = {
        "tipo": "cuenta", "imap": imap, "cfg": cfg,
        "nombre_cuenta": nombre_cuenta, "carpeta_salida": carpeta_salida,
    }
    conexiones_para_reconectar = {nombre_cuenta: (imap, cfg)}
    _ejecutar_busqueda(plan, campos, texto_low, modo_exacto, necesita_cuerpo, contexto, conexiones=conexiones_para_reconectar)
    imap_final, _cfg_final = conexiones_para_reconectar[nombre_cuenta]
    contexto["imap"] = imap_final
    return imap_final


# ---------- Modo "todas las cuentas" ----------

def recientes_todas_cuentas(conexiones):
    """Ver los N mensajes más recientes combinando todas las cuentas y carpetas conectadas."""
    print("\n¿Cuántos mensajes recientes en total? (ej: 10, 20, 50, o cualquier cantidad que quieras)")
    while True:
        n_txt = input("Cantidad: ").strip()
        try:
            n = int(n_txt)
            if n > 0:
                break
        except ValueError:
            pass
        print("Escribe un número entero mayor que 0.")
    _recientes_todas_cuentas_n(conexiones, n)


def _recientes_todas_cuentas_n(conexiones, n, interactivo=True, folders_arg="all", imprimir_listado_final=True):
    """Igual que recientes_todas_cuentas, pero con 'n' ya dado (sin preguntar)
    — la usa tanto el menú interactivo como el modo de comandos directos.
    folders_arg: 'all'/'todas' mira todas las carpetas de cada cuenta (lo
    habitual con -acc todas); cualquier otro valor usa solo la carpeta
    configurada de cada cuenta (cfg['folder'], típicamente INBOX) — para
    cuando se piden varias cuentas nombradas a la vez, no 'todas'.
    Si interactivo=False, no entra en el menú de acciones: devuelve
    (items, multi) para que quien llama decida qué hacer con ellos.
    imprimir_listado_final=False (-nuevos, pedido 2026-10-05/07): omite
    solo el listado FINAL (encabezado "Los N mensajes más recientes..." +
    cada mensaje) -- los avisos de progreso de más arriba (candidatos,
    carpetas revisadas, preparando vista previa) se siguen mostrando igual,
    son solo progreso y no numeran nada. Ver la misma nota, más completa,
    en _navegar_carpeta_n."""
    _refrescar_conexiones(conexiones)
    if not conexiones:
        _error("No queda ninguna cuenta conectada.")
        return ([], True) if not interactivo else None
    print("\nRecogiendo candidatos de cada cuenta y carpeta (puede tardar un poco)...")
    candidatos = []  # (fecha, cuenta, carpeta, msg_id)
    cuentas_perdidas = set()
    for nombre_cuenta, (imap, cfg) in conexiones.items():
        # Antes de tocar esta cuenta para nada — incluido listar_carpetas(),
        # que si no, revienta sin capturar el fallo (imaplib.IMAP4.abort al
        # hacer LIST) en vez de reconectar sola — se comprueba que la
        # conexión sigue viva. Entre que se procesan cuentas anteriores (con
        # -acc todas, sobre todo) puede pasar tiempo de sobra para que el
        # servidor haya cerrado esta por inactividad.
        imap = _imap_vivo_para(conexiones, nombre_cuenta, imap)
        if imap is None:
            _error(f"  No se pudo mantener la conexión con '{nombre_cuenta}'; se omite esta cuenta.")
            cuentas_perdidas.add(nombre_cuenta)
            continue
        if folders_arg in ("all", "todas"):
            carpetas = listar_carpetas(imap)
            seleccionables = [c for c, _v, sel in carpetas if sel]
        else:
            seleccionables = [cfg["folder"]]
        for carpeta in seleccionables:
            # Recorrer todas las carpetas de todas las cuentas (sobre todo
            # con -acc todas, que siempre las mira todas) puede tardar lo
            # bastante como para que una conexión se cierre por inactividad
            # a mitad — se comprueba y reconecta sola antes de CADA select(),
            # no solo una vez al principio de toda la función.
            imap = _imap_vivo_para(conexiones, nombre_cuenta, imap)
            if imap is None:
                _error(f"  No se pudo mantener la conexión con '{nombre_cuenta}'; se omite el resto de sus carpetas.")
                cuentas_perdidas.add(nombre_cuenta)
                break
            status, _ = _seleccionar_carpeta(imap, carpeta, readonly=True)
            if status != "OK":
                continue
            status, datos = imap.search(None, "ALL")
            if status != "OK":
                continue
            ids = datos[0].split()
            ids.reverse()  # más reciente primero, según orden del servidor
            for msg_id in ids[:n]:  # con n candidatos de cada carpeta basta (ver nota 6)
                try:
                    # Se incluye MESSAGE-ID aunque aquí solo se use la fecha
                    # para ordenar candidatos (no el id) — por coherencia con
                    # el mismo fetch de _ejecutar_busqueda (ver nota ahí) y
                    # para que cualquier uso futuro de este 'msg' no vuelva a
                    # caer en el mismo fallo de id_amigable().
                    status, d = imap.fetch(msg_id, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])")
                    if status != "OK":
                        continue
                    msg = email.message_from_bytes(d[0][1], policy=policy.default)
                    candidatos.append((fecha_mensaje(msg), nombre_cuenta, carpeta, msg_id))
                except Exception:
                    continue
        if nombre_cuenta not in cuentas_perdidas:
            print(f"  {nombre_cuenta}: {len(seleccionables)} carpetas revisadas.")

    if not candidatos:
        print("No se encontraron mensajes.")
        return ([], True) if not interactivo else None

    candidatos.sort(key=lambda c: c[0], reverse=True)
    top = candidatos[:n]

    print(f"\nPreparando vista previa de los {len(top)} elegidos...")
    items = []       # (carpeta_salida, carpeta, imap, msg_id, crudo, msg)
    etiquetas = []   # "[cuenta/carpeta]" en paralelo a items, para mostrar
    for _fecha, cuenta, carpeta, msg_id in top:
        imap, _cfg = conexiones[cuenta]
        imap = _imap_vivo_para(conexiones, cuenta, imap)
        if imap is None:
            print(f"No se pudo recuperar la conexión con '{cuenta}' para completar este mensaje.")
            continue
        status, _ = _seleccionar_carpeta(imap, carpeta, readonly=True)
        if status != "OK":
            continue
        status, d = imap.fetch(msg_id, "(RFC822)")
        if status != "OK":
            continue
        crudo = d[0][1]
        msg = email.message_from_bytes(crudo, policy=policy.default)
        items.append((_carpeta_salida_para_cuenta(cuenta), carpeta, imap, msg_id, crudo, msg))
        etiquetas.append(f"{cuenta}/{carpeta}")

    if imprimir_listado_final:
        print(f"\nLos {len(items)} mensajes más recientes de todas las cuentas:")
        for idx, ((_cs, carpeta, _imap, _mid, _crudo, msg), etiqueta) in enumerate(zip(items, etiquetas), 1):
            _imprimir_item(idx, fecha_mensaje(msg), msg, etiqueta, carpeta, 25, presupuesto_preview=LINEAS_CONTEXTO * LARGO_LINEA_PREVIEW)

    if not interactivo:
        return items, True
    contexto = {"tipo": "todas", "conexiones": conexiones}
    menu_descargar_o_ver(items, multi=True, contexto=contexto)


def buscar_todas_las_cuentas(conexiones):
    """Búsqueda en TODAS las carpetas de TODAS las cuentas conectadas, de una vez."""
    print("\nSe buscará en TODAS las carpetas de TODAS las cuentas conectadas, en una sola pasada.")
    campos = pedir_campos()
    texto_low, modo_exacto = pedir_texto_y_modo(campos)
    if texto_low is None:
        return
    necesita_cuerpo = "body" in campos

    _refrescar_conexiones(conexiones)
    if not conexiones:
        print("No queda ninguna cuenta conectada.")
        return

    print("\nListando carpetas de cada cuenta...")
    plan = []
    for nombre_cuenta, (imap, _cfg) in conexiones.items():
        # Mismo motivo que en _recientes_todas_cuentas_n: entre cuenta y
        # cuenta (o entre carpeta y carpeta, con cuentas que tienen muchas)
        # puede pasar tiempo de sobra para que el servidor cierre esta
        # conexión por inactividad — se comprueba y reconecta sola antes de
        # listar_carpetas() y antes de cada select()/search(), no solo una
        # vez al principio de toda la función (_refrescar_conexiones, más
        # arriba, solo protege el arranque).
        imap = _imap_vivo_para(conexiones, nombre_cuenta, imap)
        if imap is None:
            _error(f"  No se pudo mantener la conexión con '{nombre_cuenta}'; se omite esta cuenta.")
            continue
        carpetas = listar_carpetas(imap)
        seleccionables = [c for c, _v, sel in carpetas if sel]
        for carpeta in seleccionables:
            imap = _imap_vivo_para(conexiones, nombre_cuenta, imap)
            if imap is None:
                _error(f"  No se pudo mantener la conexión con '{nombre_cuenta}'; se omite el resto de sus carpetas.")
                break
            status, _ = _seleccionar_carpeta(imap, carpeta, readonly=True)
            if status != "OK":
                continue
            status, datos = imap.search(None, "ALL")
            if status != "OK":
                continue
            ids = datos[0].split()
            ids.reverse()
            plan.append((nombre_cuenta, carpeta, imap, ids))

    contexto = {"tipo": "todas", "conexiones": conexiones}
    _ejecutar_busqueda(plan, campos, texto_low, modo_exacto, necesita_cuerpo, contexto, conexiones=conexiones)


def _error(mensaje):
    """Imprime al canal de errores (stderr), no al de salida normal (stdout)
    — para que un script que use este programa pueda distinguir avisos de
    fallos de verdad."""
    print(mensaje, file=sys.stderr)


def _cerrar_imap(imap):
    try:
        imap.logout()
    except Exception:
        pass


def modo_todas_las_cuentas():
    """Conecta a todas las cuentas y ejecuta su menú. Devuelve 'salir' o el
    nombre de una cuenta concreta a la que cambiar."""
    print(f"Cuentas configuradas: {', '.join(CUENTAS.keys())}\n")
    conexiones = _ConexionesSesion()
    for nombre_cuenta, cfg in CUENTAS.items():
        print(f"Conectando a {nombre_cuenta} ({cfg['server']})...")
        imap = _conectar_con_reintentos(cfg, nombre_cuenta)
        if imap is None:
            print(f"  -> se omite '{nombre_cuenta}' (se volverá a intentar en la siguiente pasada).")
            conexiones.caidas[nombre_cuenta] = cfg
            continue
        conexiones[nombre_cuenta] = (imap, cfg)

    if not conexiones:
        print("No se pudo conectar a ninguna cuenta.")
        return "salir"

    print(f"\nConectado a: {', '.join(conexiones)}")
    if conexiones.caidas:
        print(f"Sin conexión por ahora: {', '.join(conexiones.caidas)}")
    resultado = "salir"

    while True:
        print("\n¿Qué quieres hacer con todas las cuentas conectadas?")
        print("  1. Ver los últimos N mensajes combinando todas (todas las carpetas)")
        print("  2. Buscar por remitente/destinatario/asunto/cuerpo en todas")
        print("  3. Salir")
        opciones_cambio = {}
        siguiente = 4
        for nombre_cuenta in CUENTAS:
            print(f"  {siguiente}. Cambiar a {nombre_cuenta}")
            opciones_cambio[str(siguiente)] = nombre_cuenta
            siguiente += 1

        opcion = input("Opción: ").strip()

        if opcion == "1":
            recientes_todas_cuentas(conexiones)
        elif opcion == "2":
            buscar_todas_las_cuentas(conexiones)
        elif opcion == "3":
            break
        elif opcion in opciones_cambio:
            resultado = opciones_cambio[opcion]
            break
        else:
            print("Opción no válida.")

    for imap, _cfg in conexiones.values():
        _cerrar_imap(imap)
    return resultado


def sesion_una_cuenta(nombre_cuenta):
    """Conecta a una cuenta y ejecuta su menú. Devuelve 'salir', 'todas', o
    el nombre de otra cuenta configurada a la que cambiar."""
    cfg = CUENTAS[nombre_cuenta]
    carpeta_salida = _carpeta_salida_para_cuenta(nombre_cuenta)
    os.makedirs(carpeta_salida, exist_ok=True)

    print(f"Conectando a {nombre_cuenta} ({cfg['server']})...")
    imap = conectar(cfg, nombre_cuenta)
    if imap is None:
        print(f"No se pudo conectar a '{nombre_cuenta}'.")
        return "salir"
    status, _ = _seleccionar_carpeta(imap, cfg["folder"], readonly=True)
    if status != "OK":
        print(f"No se pudo abrir la carpeta '{cfg['folder']}'. Revisa el nombre exacto.")
        return "salir"

    otras_cuentas = [c for c in CUENTAS if c != nombre_cuenta]

    while True:
        print(f"\n¿Qué quieres hacer? (cuenta actual: {nombre_cuenta})")
        print("  1. Elegir mensajes de los más recientes")
        print("  2. Buscar por remitente/destinatario/asunto/cuerpo")
        print("  3. Salir")
        opciones_cambio = {}
        siguiente = 4
        for otra in otras_cuentas:
            print(f"  {siguiente}. Cambiar a {otra}")
            opciones_cambio[str(siguiente)] = otra
            siguiente += 1
        print(f"  {siguiente}. Cambiar a todas las cuentas")
        opcion_todas = str(siguiente)

        opcion = input("Opción: ").strip()

        if opcion == "1":
            resultado_imap = elegir_de_recientes(imap, carpeta_salida, cfg)
            if resultado_imap is None:
                return "salir"
            imap = resultado_imap
        elif opcion == "2":
            resultado_imap = buscar_y_descargar(imap, cfg, nombre_cuenta)
            if resultado_imap is None:
                return "salir"
            imap = resultado_imap
        elif opcion == "3":
            _cerrar_imap(imap)
            return "salir"
        elif opcion in opciones_cambio:
            _cerrar_imap(imap)
            return opciones_cambio[opcion]
        elif opcion == opcion_todas:
            _cerrar_imap(imap)
            return "todas"
        else:
            print("Opción no válida.")


# ---------- Comandos directos (sin menús): -acc / -last / -folders / -buscar / -campos / -id ----------

_ALIAS_CAMPOS_CLI = {
    "to": "to", "para": "to", "destinatario": "to",
    "from": "from", "de": "from", "remitente": "from",
    "cc": "cc", "bcc": "bcc",
    "subject": "subject", "asunto": "subject",
    "body": "body", "cuerpo": "body",
    "id": "id",
}


def _desescapar_texto_cli(texto):
    """Interpreta \\n \\t \\r \\\\ literales (tal como llegan por la línea de
    comandos) como salto de línea, tabulador, retorno de carro y barra
    invertida de verdad — sin tocar el resto del texto (a propósito no usa
    el códec 'unicode_escape' de Python, que puede corromper acentos/UTF-8)."""
    equivalencias = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\"}
    resultado = []
    i = 0
    while i < len(texto):
        if texto[i] == "\\" and i + 1 < len(texto) and texto[i + 1] in equivalencias:
            resultado.append(equivalencias[texto[i + 1]])
            i += 2
        else:
            resultado.append(texto[i])
            i += 1
    return "".join(resultado)


def _parsear_campos_cli(valor):
    """'to,from,asunto' -> ['to','from','subject']. Ignora nombres no reconocidos."""
    resultado = []
    for parte in valor.split(","):
        clave = _ALIAS_CAMPOS_CLI.get(parte.strip().lower())
        if clave and clave not in resultado:
            resultado.append(clave)
    return resultado


_PATRON_DURACION_CLI = re.compile(r"^(\d+)\s*([sm]?)$", re.IGNORECASE)


def _segundos_desde_texto(valor, nombre_flag):
    """'35s' -> 35, '4m' -> 240 (segundos). Sin sufijo, se asume segundos
    ('35' -> 35). sys.exit con un mensaje claro si el formato no es válido
    o el número no es mayor que 0 — usado por -intervalo y -max."""
    texto = (valor or "").strip().lower()
    m = _PATRON_DURACION_CLI.match(texto)
    if not m:
        sys.exit(
            f"{nombre_flag} necesita un número seguido de 's' (segundos) o 'm' (minutos), "
            f"ej. 35s o 4m — no se entiende '{valor}'."
        )
    cantidad = int(m.group(1))
    if cantidad <= 0:
        sys.exit(f"{nombre_flag} tiene que ser mayor que 0 (recibido: '{valor}').")
    return cantidad * 60 if m.group(2) == "m" else cantidad


_FLAGS_CON_VALOR = {
    "-acc": "acc", "-last": "last", "-folders": "folders",
    "-buscar": "buscar", "-campos": "campos", "-id": "id",
    "-texto": "texto",
    "-buscarfecha": "buscarfecha", "-buscarcuerpo": "buscarcuerpo",
    # Para -escribir (correo nuevo desde cero — sección 5.11 del Manual 2):
    "-p": "para", "-s": "asunto", "-cc": "cc",
    "-cuerpo": "cuerpo", "-adjuntar": "adjuntar",
    # -bcc (Ronda N, junto con -escribir Y -responder): copia OCULTA
    # (BCC) — ver _enviar_correo para cómo se asegura que de verdad
    # quede oculta a los demás destinatarios.
    "-bcc": "bcc",
    # -lista/-lista-fichero (Ronda O, junto con -escribir): alternativas a
    # -p para mandar el MISMO correo (mismo asunto, cuerpo y adjuntos) a
    # una lista de direcciones, una por una -- un correo independiente por
    # destinatario, sin que ninguno vea a los demás (ver rama "escribir"
    # en _ejecutar_una_accion_cli). -lista: direcciones separadas por
    # comas, igual que -p. -lista-fichero: ruta a un .txt con una
    # dirección por línea (líneas en blanco y las que empiecen por "#" se
    # ignoran, para poder comentar la lista).
    "-lista": "lista", "-lista-fichero": "lista_fichero",
    # Control de la búsqueda desatendida (-ds — sección 5.13 del Manual 2):
    # cada cuánto se refresca el aviso/progreso, y tiempo máximo total.
    "-intervalo": "intervalo", "-max": "max_busqueda",
    # -subir-eml: ruta a un fichero .eml YA EXISTENTE a subir a la carpeta
    # INBOX de una cuenta (sección correspondiente del Manual 2, más abajo
    # en el docstring de comando_directo).
    "-subir-eml": "subir_eml",
    # -ver-img: cuántas "pantallas" generar (1, 2, 3...) -- ver _validar_n_pantallas.
    "-pantallas": "pantallas",
    # -sep (Ronda AA): separador del CSV de -tabla (por defecto ;).
    "-sep": "sep",
    # -si-existe: qué hacer si el fichero de destino ya existe, junto con
    # -descargar/-eml/-ver-txt/-ver-pdf/-adjuntos -- ver _validar_modo_si_existe.
    "-si-existe": "si_existe",
    # -orden (Ronda U, solo junto con -reanudable): "recientes" (de
    # siempre) o "antiguos" -- por qué punta de fechadesde/fechahasta se
    # empieza -- ver _validar_orden_busqueda.
    "-orden": "orden",
}
# Acciones cuyo valor (la selección) es OPCIONAL: si no se da (porque es
# el último argumento, o el siguiente ya es otro flag), se asume "todos"
# — así "-last 10 -ver" funciona igual que "-last 10 -ver todos".
_FLAGS_ACCION_VALOR_OPCIONAL = {
    "-descargar": "descargar", "-eml": "descargar",  # -eml es un alias de -descargar
    "-eliminar": "eliminar",
    "-ver": "ver", "-ver-txt": "ver_txt", "-ver-pdf": "ver_pdf", "-ver-img": "ver_img",
    "-adjuntos": "adjuntos",
    "-responder": "responder",
}
_FLAGS_SIN_VALOR = {
    "-csv": "csv", "-ver-lista": "ver_lista",
    # Ronda Z (pedido 2026-11-03): toda la información de los correos en JSON por stdout.
    "-json": "json",
    # Ronda X (pedido 2026-10-19): listar con menos detalle que -ver-lista.
    "-ver-titulos": "ver_titulos", "-ver-titulo": "ver_titulos",
    "-ver-ids": "ver_ids", "-ver-id": "ver_ids",
    "-buscaradjuntosi": "buscaradjuntosi", "-buscaradjuntono": "buscaradjuntono",
    "-cabeceras": "cabeceras", "-cabeceras-todas": "cabeceras_todas",
    # -escribir: acción aparte (correo nuevo, no busca ni selecciona nada
    # existente). -c: añade sola la dirección de eGarante a CC (ver
    # _DIRECCION_EGARANTE) — no confundir con -cc, que es para CUALQUIER
    # dirección de CC; se pueden combinar (-c Y -cc a la vez).
    "-escribir": "escribir", "-c": "cc_egarante",
    # -aqui: guarda .eml/.txt/.pdf y adjuntos en el directorio ACTUAL (desde
    # donde se llama al programa) en vez de en SALIDA/<cuenta> — ver
    # _ESTADO_SALIDA y _carpeta_salida_para_cuenta.
    "-aqui": "aqui",
    # -ds ("desatendido"): búsqueda sin preguntar NUNCA nada, con un único
    # aviso de progreso que se autosobreescribe — ver _ejecutar_busqueda.
    "-ds": "ds",
    # -duplicados: informa de correos EXACTAMENTE iguales (mismo remitente,
    # destinatarios, asunto, cuerpo y adjuntos) dentro de la misma cuenta y
    # carpeta -- sin selección propia, se aplica a TODOS los reunidos (ver
    # ACCIONES_CLI y _reportar_duplicados_exactos).
    "-duplicados": "duplicados",
    # -con-remotas (junto con -ver-img/-ver-pdf, Ronda T): permite además
    # descargar imágenes remotas (http/https) del correo, no solo las
    # incrustadas -- por defecto NO, igual que "mostrar imágenes"
    # bloqueado de entrada en cualquier cliente de correo (ver
    # _resolver_imagen_para_captura).
    "-con-remotas": "con_remotas",
    # -responder-todos (Ronda N, junto con -responder): en vez de
    # responder SOLO al remitente (de toda la vida, y lo que se sigue
    # haciendo por defecto sin este flag), responde también a los Para/CC
    # originales -- y, si el mensaje los trae (algo raro salvo en tus
    # propias copias en Enviados), a los BCC originales. Ver _datos_respuesta.
    "-responder-todos": "responder_todos",
    # -sin-reply-to (Ronda AB): -responder ignora la cabecera "Responder a" (Reply-To) y responde al De:.
    "-sin-reply-to": "sin_reply_to",
    # -listar-carpetas (Ronda S, pedido 2026-10-05/07): lista las carpetas
    # de la cuenta (o de cada cuenta, con -acc todas), marcando en cuentas
    # Gmail cuáles son carpetas de verdad y cuáles son etiquetas expuestas
    # como si fueran carpetas -- ver _imprimir_carpetas_cuenta. Acción
    # aparte: no se combina con ninguna otra (no selecciona ni actúa
    # sobre ningún mensaje).
    "-listar-carpetas": "listar_carpetas",
    # -reanudable (Ronda U, pedido 2026-10-05/07): búsqueda larga (-buscar/
    # -buscarcuerpo/-buscarfecha(s)/-buscaradjunto*, con -descargar como
    # única acción admitida) que descarga cada correo encontrado AL
    # MOMENTO y recuerda el progreso (qué cuentas/carpetas ya se
    # completaron, y qué correos ya se descargaron de las que quedaron a
    # medias) para poder pararla (Ctrl+C, o -max) y repetir la MISMA
    # orden más tarde sin perder ni repetir nada -- ver
    # _ejecutar_busqueda_reanudable.
    "-reanudable": "reanudable",
    # -nuevos (pedido 2026-10-05/07): recuerda qué correos del INBOX de
    # cada cuenta ya se vieron en una ejecución anterior, y reordena la
    # lista (-descargar/-ver, sin fechas ni otros filtros de búsqueda, sin
    # -acc todas ni -folders all/todas) para que los que son nuevos desde
    # la última vez salgan primero -- ver _marcar_nuevos_y_reordenar.
    "-nuevos": "nuevos",
    "-nuevas": "nuevos",   # Ronda AE: errata frecuente de -nuevos (sin esto se tomaba como nombre de cuenta)
    # -solo-nuevos (Ronda AE, 2026-11-05): como -nuevos, pero MUESTRA/aplica la acción SOLO a los nuevos
    # (-nuevos a secas muestra todos los últimos y solo pone los nuevos primero).
    "-solo-nuevos": "solo_nuevos", "-solo-nuevas": "solo_nuevos",
    # Ronda AA (pedido 2026-11-03): imágenes incrustadas del cuerpo (no adjuntas), junto con
    # -ver-txt/-adjuntos: -imagenes las guarda; -ocr lee su texto; -tabla las convierte en
    # CSV (separador -sep); -describir pide una descripción a un modelo de visión local.
    "-imagenes": "imagenes", "-ocr": "ocr", "-tabla": "tabla", "-describir": "describir",
}
_FLAGS_DOS_VALORES = {"-buscarfechas": "buscarfechas"}

# Nombres de las acciones exclusivas entre sí (la primera que aparezca en
# args manda) — todas menos "csv", "ver_lista", "ver_titulos", "ver_ids" y "duplicados" necesitan una
# selección (ver ACCIONES_CLI).
ACCIONES_CLI = ("descargar", "eliminar", "csv", "ver", "ver_txt", "ver_pdf", "ver_img", "adjuntos", "responder", "ver_lista", "ver_titulos", "ver_ids", "duplicados", "json")

_PATRON_POSICION = re.compile(r"^\d{1,4}(-\d{1,4})?$")  # selector de POSICIÓN: número corto, o rango N-M (nunca un id: los ids son cadenas hex de 12 caracteres)


def _parece_flag(tok):
    """True si 'tok' es uno de los parámetros reconocidos — para saber si
    el siguiente argumento es un VALOR de verdad o ya es otro flag (y por
    tanto el actual se queda sin valor explícito)."""
    return tok in _FLAGS_CON_VALOR or tok in _FLAGS_ACCION_VALOR_OPCIONAL or tok in _FLAGS_SIN_VALOR or tok in _FLAGS_DOS_VALORES


def _es_selector_por_id(valor):
    """True si 'valor' es una lista de IDs — con el prefijo "id:" (ej.
    "id:a3f9e21b4c78") o SIN él (ej. "a3f9e21b4c78,f8e2d1c093ab", el
    formato recomendado: más corto de escribir) — en vez de un selector
    de posición ('1,3,5-8' o 'todos'). Basta con que UN trozo (separado
    por comas) no tenga forma de número de posición para que TODO el
    selector se trate como lista de ids."""
    valor = (valor or "").strip()
    if not valor or valor.lower() == "todos":
        return False
    trozos = [t.strip() for t in valor.split(",") if t.strip()]
    if not trozos:
        return False
    for t in trozos:
        if t.lower().startswith("id:"):
            return True
        if not _PATRON_POSICION.match(t):
            return True
    return False


def _ids_desde_selector(valor):
    """Extrae la lista de prefijos de ID (en minúsculas) de un selector —
    tanto si cada trozo lleva el prefijo "id:" (ej. "id:X,id:Y") como si
    son ids sueltos separados por comas (ej. "X,Y", la forma recomendada,
    sin necesidad de escribir "id:" delante de cada uno)."""
    ids = []
    for trozo in (valor or "").split(","):
        trozo = trozo.strip()
        if not trozo:
            continue
        if trozo.lower().startswith("id:"):
            trozo = trozo[3:].strip()
        if trozo:
            ids.append(trozo.lower())
    return ids


def _parsear_argumentos_directos(argv):
    """Si argv trae -acc, devuelve un dict con las claves presentes. Si no
    hay -acc, devuelve None (modo interactivo normal)."""
    args = {}
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "-acc":
            # -acc admite una o varias cuentas seguidas (ej. "-acc gmx yahoo"):
            # se consumen todos los tokens siguientes que no sean ya otro flag.
            nombres = []
            j = i + 1
            while j < len(argv) and not _parece_flag(argv[j]):
                nombres.append(argv[j])
                j += 1
            if nombres:
                args["acc"] = nombres
            i = j
        elif tok in _FLAGS_DOS_VALORES and i + 2 < len(argv):
            args[_FLAGS_DOS_VALORES[tok]] = (argv[i + 1], argv[i + 2])
            i += 3
        elif tok in _FLAGS_ACCION_VALOR_OPCIONAL:
            if i + 1 < len(argv) and not _parece_flag(argv[i + 1]):
                args[_FLAGS_ACCION_VALOR_OPCIONAL[tok]] = argv[i + 1]
                i += 2
            else:
                args[_FLAGS_ACCION_VALOR_OPCIONAL[tok]] = "todos"  # sin valor explícito: se asume "todos"
                i += 1
        elif tok in _FLAGS_CON_VALOR and i + 1 < len(argv):
            args[_FLAGS_CON_VALOR[tok]] = argv[i + 1]
            i += 2
        elif tok in _FLAGS_SIN_VALOR:
            args[_FLAGS_SIN_VALOR[tok]] = "todos"  # sin selección; se guarda como valor neutro
            i += 1
        else:
            i += 1
    if "solo_nuevos" in args:
        args.setdefault("nuevos", "todos")
    return args if "acc" in args else None


def _conectar_segun_cli(lista_cuentas):
    """Conecta según -acc: una lista de 1 o más nombres de cuenta, o
    ['todas']. Devuelve (conexiones: {cuenta: (imap, cfg)}, es_todas:
    bool) — es_todas es True SOLO si se pidió literalmente 'todas' (no
    simplemente varias cuentas nombradas a la vez)."""
    if len(lista_cuentas) == 1 and lista_cuentas[0].lower() == "todas":
        conexiones = {}
        for nombre_cuenta, cfg in CUENTAS.items():
            imap = _conectar_con_reintentos(cfg, nombre_cuenta)
            if imap is not None:
                conexiones[nombre_cuenta] = (imap, cfg)
        if not conexiones:
            sys.exit("No se pudo conectar a ninguna cuenta.")
        return conexiones, True

    desconocidas = [a for a in lista_cuentas if a not in CUENTAS]
    if desconocidas:
        sospechosos = [a for a in desconocidas if a.startswith("-")]
        if sospechosos:
            import difflib
            conocidos = sorted(_FLAGS_CON_VALOR) + sorted(_FLAGS_SIN_VALOR) + sorted(_FLAGS_ACCION_VALOR_OPCIONAL)
            sug = [f"{a} -> ¿{difflib.get_close_matches(a, conocidos, 1, 0.6)[0]}?" for a in sospechosos if difflib.get_close_matches(a, conocidos, 1, 0.6)]
            sys.exit(f"{', '.join(repr(a) for a in sospechosos)} no es un parámetro conocido (se tomó como nombre de cuenta)."
                     + (f" Quizá quisiste decir: {'; '.join(sug)}." if sug else ""))
        sys.exit(
            f"Cuenta(s) {', '.join(repr(a) for a in desconocidas)} no está(n) en CUENTAS. "
            f"Opciones: {', '.join(CUENTAS)}, o 'todas'."
        )
    conexiones = {}
    for acc in lista_cuentas:
        cfg = CUENTAS[acc]
        imap = _conectar_con_reintentos(cfg, acc)
        if imap is not None:
            conexiones[acc] = (imap, cfg)
    if not conexiones:
        sys.exit(f"No se pudo conectar a ninguna de las cuentas pedidas ({', '.join(lista_cuentas)}).")
    return conexiones, False


_MESES_IMAP = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _fecha_imap(fecha):
    """Formatea un date/datetime como "DD-Mon-YYYY" -- el formato de fecha
    que exige SEARCH SINCE/BEFORE de IMAP (RFC 3501). A mano, con una
    tabla propia de meses en inglés, en vez de fecha.strftime("%b"): %b
    depende del locale activo del sistema (en español daría "ene",
    "oct"... que el servidor no reconocería como mes válido)."""
    return f"{fecha.day:02d}-{_MESES_IMAP[fecha.month - 1]}-{fecha.year:04d}"


def _criterio_search_segun_fechas(fecha_desde, fecha_hasta, usar_fecha_envio=False):
    """Criterios para imap.search() -- ALL (de siempre) si no hay
    fecha_desde/fecha_hasta; si hay alguna, acota por SINCE/BEFORE (se
    puede dar solo una de las dos). Pensado 2026-10-07 tras reportarse
    que -buscarfecha(s) con -acc todas/-folders all podía tardar 30-60
    minutos SIN terminar de analizar un solo día: antes, el "ALL" de
    siempre traía el ID de TODO el correo histórico de cada carpeta (años
    de mensajes), y _ejecutar_busqueda los iba mirando uno a uno para
    descartar casi todos por fecha -- ahora el propio servidor, que para
    esto es rapidísimo, deja fuera de entrada el grueso de lo que no
    tiene ninguna posibilidad de encajar.

    Un día de margen de más por cada lado (SINCE un día antes de
    fecha_desde, BEFORE dos días después de fecha_hasta -- BEFORE ya es
    exclusivo, así que "+2" deja un día entero de margen tras el límite)
    absorbe cualquier diferencia entre cómo compara el SERVIDOR (contra
    INTERNALDATE, en SU propia zona horaria, que no tiene por qué
    coincidir con la de España) y la fecha exacta que de verdad importa
    (la de la cabecera Date:, en hora de España) -- esto es SOLO para no
    tener que traer y mirar mensajes sin ninguna posibilidad real de
    encajar; el filtro exacto de después, en _ejecutar_busqueda (sin
    cambios), sigue siendo el que decide de verdad, mensaje a mensaje, así
    que no hay ningún riesgo de dejar fuera algo que antes sí contaba."""
    desde_clave, hasta_clave = ("SENTSINCE", "SENTBEFORE") if usar_fecha_envio else ("SINCE", "BEFORE")
    criterio = []
    if fecha_desde is not None:
        criterio += [desde_clave, _fecha_imap(fecha_desde.date() - timedelta(days=1))]
    if fecha_hasta is not None:
        criterio += [hasta_clave, _fecha_imap(fecha_hasta.date() + timedelta(days=2))]
    return tuple(criterio) if criterio else ("ALL",)


def _buscar_ids_por_fechas(imap, fecha_desde, fecha_hasta):
    """imap.search() acotado por fechas -- devuelve (status, datos) como
    imap.search(). Pedido 2026-10-09: una búsqueda de UN solo día en el
    INBOX de gmx decía estar mirando ~1.600 mensajes. Causa probable:
    SINCE/BEFORE comparan con INTERNALDATE (el día en que el mensaje
    LLEGÓ a ESE buzón), no con la cabecera Date: (el día en que se
    ENVIÓ) -- y el filtro exacto de _ejecutar_busqueda mira la cabecera
    Date:. Si muchos mensajes se movieron/importaron/copiaron en bloque a
    ese buzón el mismo día, todos comparten el mismo INTERNALDATE aunque
    se hubieran enviado años antes (o después). Por eso aquí se prueba
    PRIMERO SENTSINCE/SENTBEFORE (RFC 3501: comparan con la cabecera
    Date:, la misma fecha que el filtro exacto), con el mismo margen de
    un día; si el servidor no los entiende (respuesta distinta de OK, o
    error), se vuelve a SINCE/BEFORE de siempre. Sin fechas, "ALL"."""
    if fecha_desde is None and fecha_hasta is None:
        return imap.search(None, "ALL")
    try:
        status, datos = imap.search(None, *_criterio_search_segun_fechas(fecha_desde, fecha_hasta, usar_fecha_envio=True))
        if status == "OK":
            return status, datos
    except Exception:
        pass
    return imap.search(None, *_criterio_search_segun_fechas(fecha_desde, fecha_hasta))


def _plan_segun_folders(conexiones, es_todas, folders_arg, modo_desatentido=False, fecha_desde=None, fecha_hasta=None):
    """Construye el plan [(cuenta, carpeta, imap, ids), ...] para
    _ejecutar_busqueda, según -folders (inbox o all). Con -acc todas,
    siempre mira todas las carpetas de cada cuenta (se avisa si se pidió inbox).
    modo_desatentido (-ds, opcional): si alguna cuenta necesita reconectar
    AQUÍ (antes de que _ejecutar_busqueda llegue siquiera a empezar a
    escanear), el aviso queda en una sola línea que se autosobreescribe,
    igual que dentro de _ejecutar_busqueda -- ver _con_conexion_viva. Se
    usa un estado de línea propio (no el de _ejecutar_busqueda, que a
    esta altura todavía no existe) y se cierra con un salto de línea al
    final de esta función, para no dejarlo abierto de cara a lo que se
    imprima después.
    fecha_desde/fecha_hasta (datetime con zona horaria, opcionales --
    pedido 2026-10-07): si se dan, el propio servidor deja fuera de
    entrada (SEARCH SINCE/BEFORE) el correo que con total seguridad queda
    fuera de rango, en vez de traer el ID de TODO el histórico de cada
    carpeta -- ver _criterio_search_segun_fechas. El filtro EXACTO sigue
    siendo el de siempre, dentro de _ejecutar_busqueda; esto solo evita
    tener que mirar mensaje a mensaje lo que no tiene ninguna
    posibilidad."""
    todas_carpetas = es_todas or folders_arg in ("all", "todas")
    if es_todas and folders_arg not in ("all", "todas"):
        print("Aviso: con -acc todas, -folders se ignora (siempre mira todas las carpetas de cada cuenta).")

    estado_linea = {"ds_abierta": False} if modo_desatentido else None

    plan = []
    for nombre_cuenta, (imap, cfg) in conexiones.items():
        # Mismo motivo que en _recientes_todas_cuentas_n y
        # buscar_todas_las_cuentas: con varias cuentas (sobre todo -acc
        # todas) el tiempo entre cuenta y cuenta puede bastar para que el
        # servidor cierre esta conexión por inactividad — se comprueba y
        # reconecta sola antes de listar_carpetas() y antes de cada
        # select()/search().
        imap = _imap_vivo_para(conexiones, nombre_cuenta, imap, estado_linea=estado_linea)
        if imap is None:
            _error(f"  No se pudo mantener la conexión con '{nombre_cuenta}'; se omite esta cuenta.")
            continue
        if todas_carpetas:
            carpetas = [c for c, _v, sel in listar_carpetas(imap) if sel]
            if not carpetas:
                carpetas = [cfg["folder"]]
        else:
            carpetas = [cfg["folder"]]
        for carpeta in carpetas:
            imap = _imap_vivo_para(conexiones, nombre_cuenta, imap, estado_linea=estado_linea)
            if imap is None:
                _error(f"  No se pudo mantener la conexión con '{nombre_cuenta}'; se omite el resto de sus carpetas.")
                break
            status, _ = _seleccionar_carpeta(imap, carpeta, readonly=True)
            if status != "OK":
                continue
            status, datos = _buscar_ids_por_fechas(imap, fecha_desde, fecha_hasta)
            if status != "OK":
                continue
            ids = datos[0].split()
            ids.reverse()
            plan.append((nombre_cuenta, carpeta, imap, ids))
    if estado_linea is not None and estado_linea["ds_abierta"]:
        print()
    return plan


def _priorizar_inbox(plan):
    """Reordena el plan para que las carpetas INBOX de cada cuenta vayan
    primero. Pensado para la búsqueda directa por ID (sección 4 del
    Manual 2): al buscar un mensaje concreto (normalmente reciente), es
    donde es más probable encontrarlo, así que evita perder tiempo en
    decenas de otras carpetas (Enviados, Spam, archivadas...) antes de
    llegar a ella — el resto del orden entre sí no cambia."""
    return sorted(plan, key=lambda entrada: 0 if entrada[1].strip().upper() == "INBOX" else 1)


# =====================================================================
# -reanudable: búsqueda larga que se puede parar y continuar más tarde
# (Ronda U, pedido 2026-10-05/07) -- ver _ejecutar_busqueda_reanudable
# para el diseño completo; nota 69 del changelog al final del fichero.
# =====================================================================

_VALORES_ORDEN_BUSQUEDA = ("recientes", "antiguos")


def _validar_orden_busqueda(valor_bruto):
    """Valida el valor de -orden (opcional; solo junto con -reanudable):
    "recientes" (de siempre -- el mensaje más nuevo primero dentro de cada
    carpeta) o "antiguos" (el más antiguo primero -- para poder elegir por
    qué punta de fechadesde/fechahasta se empieza, pedido junto con
    -reanudable). Por defecto "recientes" si no se da -orden. Mismo
    patrón que _validar_modo_si_existe/_validar_n_pantallas: avisa por
    stderr y devuelve None si no es válido, sin sys.exit (para que quien
    llama decida cómo cortar)."""
    if valor_bruto is None:
        return "recientes"
    valor = str(valor_bruto).strip().lower()
    if valor not in _VALORES_ORDEN_BUSQUEDA:
        _error(f"-orden: valor no válido '{valor_bruto}' — usa uno de: {', '.join(_VALORES_ORDEN_BUSQUEDA)}.")
        return None
    return valor


def _clave_busqueda_reanudable(acc_arg, folders_arg, campos, texto_low, fecha_desde, fecha_hasta, con_adjuntos, orden):
    """Firma ESTABLE de una búsqueda -reanudable, para encontrar su
    progreso guardado de una vuelta a otra. Deliberadamente NO incluye
    -ds/-intervalo/-max/-si-existe (no cambian QUÉ se busca, solo cómo se
    informa o se nombran los ficheros al guardarlos) ni el ORDEN en que se
    escribieron los parámetros en la línea de comandos (irrelevante en
    todo el resto del programa -- sección 2 del Manual 2) -- así,
    cambiar cualquiera de esos entre una vuelta y la siguiente sigue
    encontrando el MISMO progreso.
    'acc_arg' se usa tal cual lo dio el usuario (ej. ["todas"], o la lista
    de cuentas nombradas) en vez de la lista de conexiones ya resueltas:
    con -acc todas, añadir una cuenta nueva a CUENTAS entre una vuelta y
    la siguiente simplemente le suma trabajo pendiente a esta misma
    búsqueda, en vez de crear una distinta.
    Devuelve (clave: hash de 16 caracteres para el nombre de fichero,
    firma: texto legible para mostrar y guardar dentro del propio
    checkpoint)."""
    partes = [
        "acc=" + ",".join(sorted(str(a).strip().lower() for a in acc_arg)),
        "folders=" + folders_arg,
        "campos=" + (",".join(sorted(campos)) if campos else ""),
        "texto=" + (texto_low or ""),
        "desde=" + (fecha_desde.isoformat() if fecha_desde is not None else ""),
        "hasta=" + (fecha_hasta.isoformat() if fecha_hasta is not None else ""),
        "adjuntos=" + str(con_adjuntos),
        "orden=" + orden,
    ]
    firma = " | ".join(partes)
    clave = hashlib.sha256(firma.encode("utf-8", errors="replace")).hexdigest()[:16]
    return clave, firma


def _ruta_checkpoint_busqueda(clave_hash):
    return _ruta_fichero_de_estado(CARPETA_PROGRESO_BUSQUEDAS, "progreso_busqueda", clave_hash)


def _cargar_checkpoint_busqueda(clave_hash, firma):
    """Carga el progreso guardado de una búsqueda -reanudable anterior con
    esta misma firma, o devuelve uno nuevo y vacío si no hay ninguno
    todavía -- o si el fichero existente no se puede leer (se avisa y se
    empieza de cero: un fichero de progreso dañado nunca debe impedir que
    la búsqueda en sí se pueda hacer)."""
    ruta = _ruta_checkpoint_busqueda(clave_hash)
    ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if os.path.isfile(ruta):
        try:
            with open(ruta, "r", encoding="utf-8") as f:
                datos = json.load(f)
            carpetas = {}
            for entrada in datos.get("carpetas", []):
                clave_cf = (entrada.get("cuenta", ""), entrada.get("carpeta", ""))
                carpetas[clave_cf] = {
                    "completa": bool(entrada.get("completa", False)),
                    "descargados": set(entrada.get("descargados", [])),
                }
            return {
                "ruta": ruta, "firma": firma,
                "creado": datos.get("creado", ahora),
                "carpetas": carpetas, "nuevo": False,
            }
        except Exception as e:
            _error(
                f"Aviso: no se pudo leer el progreso guardado de una búsqueda -reanudable "
                f"anterior ({ruta}): {e} — se empieza desde cero."
            )
    return {"ruta": ruta, "firma": firma, "creado": ahora, "carpetas": {}, "nuevo": True}


def _guardar_checkpoint_busqueda(checkpoint):
    """Guarda 'checkpoint' en disco de forma ATÓMICA (escribe en un
    fichero temporal y lo renombra encima del definitivo -- os.replace) --
    así, si el proceso se interrumpe (Ctrl+C, cierre de Termux, corte de
    luz...) justo durante el propio guardado, el progreso anterior se
    queda intacto en vez de a medio escribir (en vez de perderlo)."""
    datos = {
        "version": 1,
        "firma": checkpoint["firma"],
        "creado": checkpoint["creado"],
        "actualizado": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "carpetas": [
            {
                "cuenta": cuenta, "carpeta": carpeta,
                "completa": info["completa"], "descargados": sorted(info["descargados"]),
            }
            for (cuenta, carpeta), info in checkpoint["carpetas"].items()
        ],
    }
    _asegurar_carpeta_de(checkpoint["ruta"])
    ruta_tmp = checkpoint["ruta"] + ".tmp"
    with open(ruta_tmp, "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False, indent=2)
    os.replace(ruta_tmp, checkpoint["ruta"])


def _ejecutar_busqueda_reanudable(
    conexiones, es_todas, folders_arg, campos, texto_low,
    fecha_desde, fecha_hasta, con_adjuntos, orden, modo_si_existe, checkpoint,
    modo_desatentido=False, intervalo_inicial=None, tiempo_maximo=None,
):
    """-reanudable (pedido 2026-10-05/07, Ronda U, verbatim): "crea una
    funcionalidad para que al hacer busquedas complejas en largos periodos
    de tiempo, en muchas cuentas y carpetas, que puedas ir descargando
    cada correo encontrado, descargarlo inmediatamente, y ademas, que el
    usuario pueda parar en un momento y horas o dias mas tarde recomenzar
    en el punto en que lo dejo, suponiendo que los correos de las cuentas
    y carpetas ya analizadas, que no se volveran a analizar, sino mirando
    correos mas antiguos, si comenzas por el dia mas antiguo de la serie
    de fechadesde y fechahasta, y que se pueda indicar que comienze, de
    entre las 2 fechas, que comienza en la fecha mas antigua o en la fecha
    mas actual".

    A diferencia de _ejecutar_busqueda (que reúne TODO antes de dejar
    hacer nada con ello), aquí cada mensaje que coincide se DESCARGA al
    momento (vía guardar_crudo, igual que -descargar de toda la vida), y
    'checkpoint' (ver _cargar_checkpoint_busqueda) recuerda, por cada
    cuenta+carpeta: si ya se terminó de analizar ENTERA ("completa") y,
    de la que no, qué correos (por su id_amigable -- estable, no cambia
    nunca, a diferencia del número de secuencia IMAP que usa el resto del
    programa y que SÍ puede cambiar entre una conexión y otra si entra o
    sale algún mensaje) ya se descargaron -- así, repetir la MISMA orden
    más tarde (horas o días después) nunca repite trabajo ya hecho ni
    descarga nada dos veces, aunque se pare a la mitad de una carpeta
    grande. El checkpoint se guarda en disco tras CADA descarga y tras
    completar CADA carpeta (nunca solo al final), para que un Ctrl+C en
    cualquier momento deje aprovechable todo lo ya hecho hasta ese
    instante.

    A propósito INDEPENDIENTE de _ejecutar_busqueda (no comparte su
    reparto "por rondas" entre carpetas -- ver TAMANO_RONDA_BUSQUEDA):
    aquí se analiza una carpeta ENTERA antes de pasar a la siguiente, lo
    que encaja 1:1 con "esta cuenta/carpeta ya está completa" del
    checkpoint sin ambigüedad ninguna. El reparto por rondas de
    _ejecutar_busqueda existe para otro problema distinto (que una cuenta
    con mucho correo no retrase llegar a las demás dentro de UNA sola
    pasada); aquí, pensado para tardar horas o días EN TOTAL repartidos en
    varias ejecuciones, no hace falta, y complicaría bastante corresponder
    el progreso con el checkpoint.

    orden: "recientes" (de siempre: el id más reciente primero dentro de
    cada carpeta) o "antiguos" (el más antiguo primero -- para poder
    empezar por la fecha más vieja de fechadesde/fechahasta en vez de por
    la más nueva, tal como se pidió).

    modo_desatentido/intervalo_inicial/tiempo_maximo: mismo significado y
    mismo mecanismo (-ds/-intervalo/-max) que en _ejecutar_busqueda --
    reutilizados aquí tal cual para que una búsqueda -reanudable de verdad
    larga se pueda dejar corriendo sola igual que cualquier otra.

    Si se interrumpe con Ctrl+C, lo avisa con claridad y termina limpio
    (sin traceback) -- el progreso hasta ese instante ya está guardado,
    así que basta repetir la misma orden más tarde para continuar."""
    necesita_cuerpo = (campos is not None and "body" in campos) or con_adjuntos is not None
    todas_carpetas = es_todas or folders_arg in ("all", "todas")

    estado_linea = {"ds_abierta": False}

    def _avisar(texto, usar_stderr=False):
        if estado_linea["ds_abierta"]:
            print()
            estado_linea["ds_abierta"] = False
        _error(texto) if usar_stderr else print(texto)

    # --- Construcción del plan: igual en espíritu que _plan_segun_folders,
    # pero saltándose ENTERAS las cuentas+carpetas que el checkpoint ya
    # marca completas (ni siquiera se les pide el search), y respetando
    # 'orden' en vez del "más reciente primero" fijo de siempre. ---
    plan = []
    carpetas_completas_de_antes = 0
    for nombre_cuenta, (imap, cfg) in conexiones.items():
        imap = _imap_vivo_para(conexiones, nombre_cuenta, imap, estado_linea=(estado_linea if modo_desatentido else None))
        if imap is None:
            _avisar(f"  No se pudo mantener la conexión con '{nombre_cuenta}'; se omite esta cuenta.", usar_stderr=True)
            continue
        if todas_carpetas:
            carpetas = [c for c, _v, sel in listar_carpetas(imap) if sel]
            if not carpetas:
                carpetas = [cfg["folder"]]
        else:
            carpetas = [cfg["folder"]]
        for carpeta in carpetas:
            info_previa = checkpoint["carpetas"].get((nombre_cuenta, carpeta))
            if info_previa is not None and info_previa["completa"]:
                carpetas_completas_de_antes += 1
                continue
            imap = _imap_vivo_para(conexiones, nombre_cuenta, imap, estado_linea=(estado_linea if modo_desatentido else None))
            if imap is None:
                _avisar(f"  No se pudo mantener la conexión con '{nombre_cuenta}'; se omite el resto de sus carpetas.", usar_stderr=True)
                break
            status, _ = _seleccionar_carpeta(imap, carpeta, readonly=True)
            if status != "OK":
                continue
            status, datos = _buscar_ids_por_fechas(imap, fecha_desde, fecha_hasta)
            if status != "OK":
                continue
            ids = datos[0].split()
            if orden != "antiguos":
                ids.reverse()  # "recientes" (de siempre): el más nuevo primero
            if not ids:
                # Sin ningún candidato: no hay nada que hacer aquí nunca
                # (ni ahora ni en un futuro "reanudar" de esta misma
                # búsqueda, porque el rango de fechas no cambia), así que
                # se marca completa directamente.
                checkpoint["carpetas"][(nombre_cuenta, carpeta)] = {"completa": True, "descargados": set()}
            else:
                plan.append((nombre_cuenta, carpeta, imap, ids))
    if estado_linea["ds_abierta"]:
        print()
    _guardar_checkpoint_busqueda(checkpoint)  # deja constancia en disco ya desde ahora, antes de analizar nada

    total = sum(len(ids) for _c, _f, _i, ids in plan)
    ya_descargados_de_antes = sum(len(info["descargados"]) for info in checkpoint["carpetas"].values())

    if checkpoint["nuevo"]:
        print(f"\nBúsqueda -reanudable nueva (progreso: {checkpoint['ruta']}).")
    else:
        print(
            f"\nReanudando búsqueda -reanudable guardada el {checkpoint['creado']}: "
            f"{carpetas_completas_de_antes} carpeta(s) ya completa(s) antes de ahora, "
            f"{ya_descargados_de_antes} correo(s) ya descargados en total hasta el momento."
        )
    if not plan:
        print("No queda ninguna cuenta/carpeta pendiente -- esta búsqueda ya estaba completa del todo.")
        return 0, 0

    if not modo_desatentido:
        print(f"Analizando hasta {total} mensaje(s) candidato(s) de {len(plan)} carpeta(s) pendiente(s) (orden: {orden})...")
        print("Cada correo que coincide se descarga AL MOMENTO; se puede parar (Ctrl+C) y repetir esta misma orden más tarde para continuar.")

    campos_cabecera = "(BODY.PEEK[HEADER.FIELDS (TO FROM CC BCC SUBJECT DATE MESSAGE-ID)])"
    inicio = time.monotonic()
    intervalo_actual = intervalo_inicial if intervalo_inicial is not None else INTERVALO_AVISO_INICIAL
    proximo_aviso = intervalo_actual
    i = 0
    descargados_ahora = 0
    detener = False
    cuentas_perdidas = set()

    try:
        for cuenta, carpeta, imap, ids in plan:
            if detener:
                break
            if cuenta in cuentas_perdidas:
                continue
            info = checkpoint["carpetas"].setdefault((cuenta, carpeta), {"completa": False, "descargados": set()})
            carpeta_salida = _carpeta_salida_para_cuenta(cuenta)

            imap = _imap_vivo_para(conexiones, cuenta, imap, estado_linea=(estado_linea if modo_desatentido else None))
            if imap is None:
                _avisar(f"  No se pudo mantener la conexión con '{cuenta}'; se omite el resto de sus carpetas pendientes (siguen pendientes para la próxima vez).", usar_stderr=True)
                cuentas_perdidas.add(cuenta)
                continue
            if not modo_desatentido:
                print(f"  -- {cuenta}/{carpeta}: {len(ids)} candidato(s) por revisar ({len(info['descargados'])} de esta misma carpeta ya descargados antes) --")
            status, _ = _seleccionar_carpeta(imap, carpeta, readonly=True)
            if status != "OK":
                _avisar(f"  Aviso: no se pudo reabrir '{cuenta}/{carpeta}', se omite (sigue pendiente para la próxima vez).")
                continue

            terminada_sin_cortes = True
            for msg_id in ids:
                i += 1
                origen_perdido = False
                try:
                    if necesita_cuerpo:
                        status, d = imap.fetch(msg_id, "(RFC822)")
                        if status != "OK":
                            _avisar(f"  Aviso: no se pudo descargar el mensaje {msg_id.decode()} en {cuenta}/{carpeta}.")
                            crudo, msg = None, None
                        else:
                            crudo = d[0][1]
                            msg = email.message_from_bytes(crudo, policy=policy.default)
                    else:
                        status, d = imap.fetch(msg_id, campos_cabecera)
                        if status != "OK":
                            _avisar(f"  Aviso: no se pudo leer la cabecera del mensaje {msg_id.decode()} en {cuenta}/{carpeta}.")
                            crudo, msg = None, None
                        else:
                            crudo = None
                            msg = email.message_from_bytes(d[0][1], policy=policy.default)
                except Exception as e:
                    _avisar(f"  Aviso: fallo leyendo el mensaje {msg_id.decode()} en {cuenta}/{carpeta}: {e}")
                    crudo, msg = None, None
                    imap_recuperado = _imap_vivo_para(conexiones, cuenta, imap, estado_linea=(estado_linea if modo_desatentido else None))
                    if imap_recuperado is None:
                        cuentas_perdidas.add(cuenta)
                        terminada_sin_cortes = False
                        origen_perdido = True
                    else:
                        imap = imap_recuperado

                if msg is not None:
                    id_msg = id_amigable(msg)
                    if id_msg not in info["descargados"]:
                        coincide = True
                        if campos is not None:
                            coincide = bool(campos_que_coinciden(msg, campos, texto_low, False))
                        if coincide and fecha_desde is not None and fecha_mensaje(msg) < fecha_desde:
                            coincide = False
                        if coincide and fecha_hasta is not None and fecha_mensaje(msg) > fecha_hasta:
                            coincide = False
                        if coincide and con_adjuntos is not None and _tiene_adjuntos(msg) != con_adjuntos:
                            coincide = False

                        if coincide and crudo is None:
                            # Coincide por cabecera/fecha, pero solo se leyó
                            # la cabecera (necesita_cuerpo era False): hace
                            # falta el mensaje COMPLETO para poder guardarlo
                            # -- mismo segundo fetch que hace _ejecutar_
                            # busqueda para esto mismo, solo que aquí sucede
                            # al momento, no al final.
                            status, d = imap.fetch(msg_id, "(RFC822)")
                            if status != "OK":
                                _avisar(f"  Aviso: no se pudo descargar el mensaje {msg_id.decode()} en {cuenta}/{carpeta} para guardarlo.")
                                coincide = False
                            else:
                                crudo = d[0][1]
                                msg = email.message_from_bytes(crudo, policy=policy.default)

                        if coincide:
                            os.makedirs(carpeta_salida, exist_ok=True)
                            guardar_crudo(crudo, carpeta_salida, msg, carpeta, modo_si_existe=modo_si_existe)
                            info["descargados"].add(id_msg)
                            descargados_ahora += 1
                            _guardar_checkpoint_busqueda(checkpoint)

                transcurrido = time.monotonic() - inicio
                if tiempo_maximo is not None and transcurrido >= tiempo_maximo:
                    _avisar(
                        f"  Tiempo máximo de búsqueda alcanzado ({_formatear_duracion(tiempo_maximo)}) — "
                        f"se detiene tras analizar {i}/{total} candidato(s) ({descargados_ahora} descargado(s) en esta pasada)."
                    )
                    detener = True
                    terminada_sin_cortes = False
                elif transcurrido >= proximo_aviso:
                    if modo_desatentido:
                        print(
                            f"\r  Progreso: {i}/{total} candidatos analizados, {descargados_ahora} descargados en esta pasada...\033[K",
                            end="", flush=True,
                        )
                        estado_linea["ds_abierta"] = True
                    else:
                        print(
                            f"  ({int(transcurrido)}s transcurridos — {i}/{total} analizados, "
                            f"{descargados_ahora} descargados en esta pasada — ahora en {cuenta}/{carpeta})"
                        )
                        seguir, intervalo_actual = _preguntar_seguir_buscando(intervalo_actual, interactivo=False)
                        if not seguir:
                            print("  Búsqueda detenida -- repite exactamente la misma orden más tarde para continuar donde se dejó.")
                            detener = True
                            terminada_sin_cortes = False
                    proximo_aviso = transcurrido + intervalo_actual

                if detener or origen_perdido:
                    break

            if terminada_sin_cortes:
                info["completa"] = True
                _guardar_checkpoint_busqueda(checkpoint)
    except KeyboardInterrupt:
        if estado_linea["ds_abierta"]:
            print()
        print(
            "\n  Búsqueda -reanudable interrumpida (Ctrl+C). El progreso ya está guardado: "
            f"{descargados_ahora} correo(s) descargado(s) en esta pasada (de {i} analizados). "
            "Repite exactamente la misma orden más tarde para continuar donde se dejó."
        )
        return i, descargados_ahora

    if estado_linea["ds_abierta"]:
        print()
    print(f"\nBúsqueda -reanudable: {i}/{total} candidato(s) analizados, {descargados_ahora} correo(s) descargado(s) en esta pasada.")
    if detener:
        print("No se terminó del todo -- repite exactamente la misma orden más tarde para continuar donde se dejó.")
    return i, descargados_ahora


def _nombre_cuenta_aproximado(carpeta_salida):
    """Nombre de cuenta a partir de su carpeta de salida (SALIDA/<cuenta>,
    ver _carpeta_salida_para_cuenta) -- solo para MOSTRAR en el informe de
    -duplicados cuando hay varias cuentas a la vez. Con -aqui (que usa el
    directorio actual para TODAS las cuentas por igual) esta aproximación
    deja de distinguirlas -- no pasa nada: -aqui no guarda ningún fichero
    para -duplicados (es un informe, no una descarga), así que no afecta a
    nada más que a este detalle de visualización."""
    return os.path.basename(os.path.normpath(carpeta_salida)) or carpeta_salida


def _clave_duplicado_exacto(item):
    """Clave que agrupa dos mensajes como "el mismo correo, exactamente
    igual" para -duplicados: misma cuenta, misma carpeta IMAP, mismo
    remitente, mismos destinatarios, mismo asunto, mismo cuerpo y mismos
    adjuntos (nombre y tamaño). Dos mensajes con la MISMA clave se
    consideran duplicados exactos entre sí, con muy alta probabilidad de
    ser realmente el mismo correo subido o llegado dos veces -- sin
    necesidad de comparar cada adjunto byte a byte.

    Cuenta y carpeta forman PARTE de la clave a propósito: el mismo correo
    presente en dos carpetas distintas (p.ej. INBOX y una carpeta donde lo
    archivaste tú) no es el tipo de "duplicado accidental" que se quiere
    detectar aquí, así que nunca se compara entre carpetas ni entre
    cuentas distintas, solo dentro de la misma."""
    carpeta_salida, carpeta_imap, _imap, _mid, _crudo, msg = item
    cuenta_aprox = _nombre_cuenta_aproximado(carpeta_salida)
    de = tuple(sorted(direcciones_de_campo(msg, "from")))
    para = tuple(sorted(direcciones_de_campo(msg, "to")))
    asunto = str(msg.get("subject", "") or "").strip()
    cuerpo = texto_cuerpo_limpio(msg, recortar_urls=False).strip()
    adjuntos = tuple(sorted(_lista_adjuntos(msg)))
    return (cuenta_aprox, carpeta_imap, de, para, asunto, cuerpo, adjuntos)


def _reportar_duplicados_exactos(items, multi):
    """Agrupa 'items' (TODOS los mensajes ya reunidos por -duplicados, no
    una selección) por _clave_duplicado_exacto, e informa de cada grupo de
    2 o más copias -- con la POSICIÓN de cada una en ESTA MISMA lista (tal
    como se acaba de imprimir más arriba, sección 5.0 del Manual 2).

    Se usa la POSICIÓN, no el id propio del programa (id_amigable/-id):
    id_amigable() se deriva del Message-ID (no del cuerpo/adjuntos), así
    que dos copias EXACTAMENTE iguales para esta comparación SOLO
    comparten el mismo id cuando también comparten Message-ID -- el caso
    habitual cuando es el mismo fichero .eml subido más de una vez sin
    tocarlo (-subir-eml sube los bytes tal cual, sin tocar cabeceras).
    Cuando coincide, "-eliminar id:XXXX" borraría TODAS las copias del
    grupo a la vez, no una sola -- justo lo contrario de "elegir
    eliminar 1, cualquiera de ellas". La POSICIÓN, en cambio, selecciona
    SIEMPRE una única copia, compartan o no el mismo id."""
    grupos = {}
    for idx, item in enumerate(items, 1):
        grupos.setdefault(_clave_duplicado_exacto(item), []).append(idx)

    duplicados = [(clave, posiciones) for clave, posiciones in grupos.items() if len(posiciones) >= 2]
    if not duplicados:
        print(f"\nSin duplicados exactos entre los {len(items)} mensaje(s) analizados.")
        return

    copias_de_mas = sum(len(posiciones) - 1 for _clave, posiciones in duplicados)
    print(
        f"\n{len(duplicados)} correo(s) con copias EXACTAMENTE iguales entre los "
        f"{len(items)} analizados ({copias_de_mas} copia(s) de más en total):"
    )
    for clave, posiciones in duplicados:
        cuenta_aprox, carpeta_imap, _de, _para, asunto, _cuerpo, _adjuntos = clave
        asunto_mostrado = asunto or "(sin asunto)"
        ubicacion = f"{cuenta_aprox}/{carpeta_imap}" if multi else carpeta_imap
        print(f"\n  \"{asunto_mostrado}\" -- {len(posiciones)} copias idénticas en {ubicacion}:")
        for pos in posiciones:
            msg = items[pos - 1][5]
            print(f"    posición {pos}: id:{id_amigable(msg)}  {formatear_fecha(fecha_mensaje(msg))}")

    print(
        "\nPara quitar UNA copia de un duplicado (la que tú elijas, dejando la otra),\n"
        "repite EXACTAMENTE la misma orden que has usado para -duplicados, añadiendo\n"
        "-eliminar y la POSICIÓN a quitar -- no el id: dos copias EXACTAMENTE iguales\n"
        "pueden compartir el mismo id (lo habitual si es el mismo fichero subido más\n"
        "de una vez sin tocarlo), y entonces -eliminar id:XXXX las borraría TODAS a\n"
        "la vez, no solo una -- la POSICIÓN, en cambio, selecciona SIEMPRE una única\n"
        "copia. Por ejemplo, para quitar la copia en la posición 7:\n"
        "    <la misma orden de -duplicados> -eliminar 7\n"
        "No añadas ni borres correos de esa carpeta entre ver este informe y eliminar\n"
        "por posición, para que los números sigan correspondiendo a los mismos mensajes."
    )


# ---------- -json (Ronda Z, pedido 2026-11-03): toda la información de los
# correos, en JSON, por stdout ----------
# Pedido verbatim: "crea una nueva opcion para que envie por stdout en
# formato json, toda la informacion de los correos. cada campo del correo
# con la informacion completa en su campo json, de, para, cc, bcc, asunto,
# cuerpo en formato txt, listado de adjuntos, y la localizacion donde los
# has descargado, localizacion y nombre archivo pdf, localizacion y nomb
# arch txt, etc, local y nomb archi eml y sus adjuntos, local y nomb archi
# img de ver-img."
#
# Diseño: -json es una ACCIÓN más (sin valor, como -ver-lista) que se
# ejecuta SIEMPRE LA ÚLTIMA, así que puede incluir dónde han ido a parar los
# ficheros que hayan generado las demás acciones pedidas en la misma orden
# (-descargar, -ver-txt, -ver-pdf, -ver-img, -adjuntos). Para que stdout
# contenga SOLO el JSON (y se pueda canalizar a jq, a otro programa...), todo
# lo demás que el programa imprime normalmente (progreso, "Guardado: ...",
# listas...) se manda a stderr mientras dura la orden -- ver comando_directo.
_JSON_ESTADO = {"activo": False, "ficheros": {}, "eliminados": set(), "documento": None}


def _json_iniciar():
    _JSON_ESTADO.update({"activo": True, "ficheros": {}, "eliminados": set(), "documento": None})


def _registrar_fichero(msg, tipo, ruta, estado="guardado", nombre_original=None):
    """Anota (solo si -json está activo) un fichero generado -- o que ya
    existía y se dejó tal cual (estado "ya_existia", por -si-existe) -- para
    el mensaje 'msg'. tipo: "eml", "txt", "pdf", "img" o "adjuntos"."""
    if not _JSON_ESTADO["activo"]:
        return
    absoluta = os.path.abspath(ruta)
    _JSON_ESTADO["ficheros"].setdefault(id(msg), {}).setdefault(tipo, []).append({
        "ruta": absoluta,
        "carpeta": os.path.dirname(absoluta),
        "nombre": os.path.basename(absoluta),
        "estado": estado,
        "_original": nombre_original,
    })


def _fichero_json(entrada):
    return {k: v for k, v in entrada.items() if not k.startswith("_")}


def _direcciones_json(msg, campo):
    valor = msg.get(campo)
    if valor is None:
        return []
    try:
        return [{"nombre": a.display_name or "", "correo": a.addr_spec or ""} for a in valor.addresses]
    except AttributeError:
        return [{"nombre": n, "correo": c} for n, c in getaddresses([str(valor)])]


def _adjuntos_json(msg, registrados):
    """Lista de adjuntos de verdad del mensaje (nombre, tipo, tamaño) y, si
    -adjuntos los guardó (o ya estaban), dónde."""
    pendientes = list(registrados)
    salida = []
    for parte in _partes_adjuntas(msg):
        nombre = parte.get_filename() or "adjunto"
        try:
            contenido = parte.get_payload(decode=True)
        except Exception:
            contenido = None
        entrada = {
            "nombre": nombre,
            "tipo": parte.get_content_type(),
            "tamano_bytes": len(contenido) if contenido else 0,
            "descargado": None,
        }
        for i, reg in enumerate(pendientes):
            if reg.get("_original") == nombre:
                entrada["descargado"] = _fichero_json(pendientes.pop(i))
                break
        salida.append(entrada)
    return salida


def _datos_mensaje_json(posicion, item):
    carpeta_salida, carpeta_imap, _imap, _msg_id, crudo, msg = item
    cuenta, _cfg = _cuenta_de_carpeta_salida(carpeta_salida)
    fecha = fecha_mensaje(msg)
    registrados = _JSON_ESTADO["ficheros"].get(id(msg), {})
    adjuntos = _adjuntos_json(msg, registrados.get("adjuntos", []))

    def _uno(tipo):
        lista = registrados.get(tipo, [])
        return _fichero_json(lista[0]) if lista else None

    return {
        "posicion": posicion,
        "id": id_amigable(msg),
        "cuenta": cuenta,
        "carpeta": carpeta_imap,
        "message_id": str(msg.get("message-id") or ""),
        "fecha": fecha.isoformat() if fecha > _FECHA_MUY_ANTIGUA else None,
        "fecha_texto": formatear_fecha(fecha),
        "de": str(msg.get("from") or ""),
        "para": str(msg.get("to") or ""),
        "cc": str(msg.get("cc") or ""),
        "bcc": str(msg.get("bcc") or ""),
        "responder_a": str(msg.get("reply-to") or ""),
        "de_direcciones": _direcciones_json(msg, "from"),
        "para_direcciones": _direcciones_json(msg, "to"),
        "cc_direcciones": _direcciones_json(msg, "cc"),
        "bcc_direcciones": _direcciones_json(msg, "bcc"),
        "asunto": str(msg.get("subject") or ""),
        "cuerpo_txt": texto_cuerpo_limpio(msg, recortar_urls=False),
        "tamano_bytes": len(crudo) if crudo else None,
        "tiene_adjuntos": bool(adjuntos),
        "adjuntos": adjuntos,
        "ficheros": {
            "eml": _uno("eml"),
            "txt": _uno("txt"),
            "pdf": _uno("pdf"),
            "img": [_fichero_json(r) for r in registrados.get("img", [])],
            "imagenes_incrustadas": [_fichero_json(r) for r in registrados.get("imagenes", [])],
            "ocr": [_fichero_json(r) for r in registrados.get("ocr", [])],
            "tablas_csv": [_fichero_json(r) for r in registrados.get("tablas", [])],
            "descripciones": [_fichero_json(r) for r in registrados.get("descripciones", [])],
        },
        "eliminado": id(msg) in _JSON_ESTADO["eliminados"],
    }


def _documento_json(items):
    return {
        "version": 1,
        "generado": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "total": len(items),
        "mensajes": [_datos_mensaje_json(i, item) for i, item in enumerate(items, 1)],
    }


def _ejecutar_una_accion_cli(items, multi, accion, args):
    """Ejecuta (sin preguntar NADA) UNA acción pedida por línea de comandos
    sobre 'items'."""
    if not items:
        _error("No hay mensajes sobre los que aplicar la acción.")
        return

    # -cabeceras-todas manda si se dan los dos flags a la vez (más
    # información es la opción segura ante una combinación contradictoria).
    if args.get("cabeceras_todas"):
        modo_cabeceras = "todas"
    elif args.get("cabeceras"):
        modo_cabeceras = "importantes"
    else:
        modo_cabeceras = None

    if accion == "csv":
        ruta = _exportar_csv(items)
        print(f"Guardado: {ruta}")
        return

    if accion == "duplicados":
        _reportar_duplicados_exactos(items, multi)
        return

    if accion == "json":
        _JSON_ESTADO["documento"] = _documento_json(items)
        return

    if accion in ("ver_titulos", "ver_ids"):
        # Igual que ver_lista: la lista ya se mostró al reunirlos, con el
        # nivel de detalle que fijó comando_directo en _ESTADO_LISTADO.
        return

    if accion == "ver_lista":
        # No hay nada más que hacer: la lista (título + vista previa de
        # cada mensaje) ya se mostró al reunirlos. Esta acción existe
        # solo para que, al pedirla explícitamente, NO se entre al menú
        # interactivo (a diferencia de no indicar ninguna acción con
        # -last) ni se muestre el texto completo de cada uno (a
        # diferencia de -ver) — se lista y se vuelve, sin más.
        return

    valor_seleccion = args[accion]
    if _es_selector_por_id(valor_seleccion):
        ids_pedidos = _ids_desde_selector(valor_seleccion)
        indices = {
            idx for idx, item in enumerate(items, 1)
            if any(id_amigable(item[5]).startswith(p) for p in ids_pedidos)
        }
    else:
        indices = parsear_seleccion(valor_seleccion, len(items))
    if not indices:
        _error(f"La selección '{valor_seleccion}' no corresponde a ningún mensaje de la lista (1 a {len(items)}).")
        return

    if accion == "descargar":
        # -si-existe: validado AQUÍ (no al principio de la función, junto a
        # modo_cabeceras) a propósito -- igual que -pantallas en ver_img más
        # abajo, para que un valor inválido solo corte ESTA acción y no
        # otras sin relación (-csv, -ver, -eliminar...) pedidas a la vez.
        modo_si_existe = _validar_modo_si_existe(args.get("si_existe"))
        if modo_si_existe is None:
            return
        for idx in sorted(indices):
            carpeta_salida, carpeta_imap, _imap, _msg_id, crudo, msg = items[idx - 1]
            os.makedirs(carpeta_salida, exist_ok=True)
            guardar_crudo(crudo, carpeta_salida, msg, carpeta_imap, modo_si_existe=modo_si_existe)

    elif accion == "eliminar":
        eliminados = _ejecutar_eliminacion(items, indices)
        if _JSON_ESTADO["activo"] and eliminados == len(indices):
            _JSON_ESTADO["eliminados"].update(id(items[idx - 1][5]) for idx in indices)

    elif accion == "ver":
        for idx in sorted(indices):
            _cs, _ci, _imap, _mid, _crudo, msg = items[idx - 1]
            _mostrar_texto_completo(msg, modo_cabeceras)

    elif accion == "ver_txt":
        modo_si_existe = _validar_modo_si_existe(args.get("si_existe"))
        if modo_si_existe is None:
            return
        for idx in sorted(indices):
            carpeta_salida, carpeta_imap, _imap, _mid, _crudo, msg = items[idx - 1]
            extra = ""
            if _quiere_imagenes(args):
                extra = _procesar_imagenes_incrustadas(msg, carpeta_salida, carpeta_imap, args, modo_si_existe)
            _guardar_texto_completo_txt(msg, carpeta_salida, carpeta_imap, modo_cabeceras=modo_cabeceras, modo_si_existe=modo_si_existe, texto_extra=extra)

    elif accion == "ver_pdf":
        modo_si_existe = _validar_modo_si_existe(args.get("si_existe"))
        if modo_si_existe is None:
            return
        con_remotas = bool(args.get("con_remotas"))
        for idx in sorted(indices):
            carpeta_salida, carpeta_imap, _imap, _mid, _crudo, msg = items[idx - 1]
            _guardar_texto_completo_pdf(
                msg, carpeta_salida, carpeta_imap, modo_cabeceras=modo_cabeceras,
                modo_si_existe=modo_si_existe, con_remotas=con_remotas,
            )

    elif accion == "ver_img":
        n_pantallas = _validar_n_pantallas(args.get("pantallas"))
        if n_pantallas is None:
            return
        con_remotas = bool(args.get("con_remotas"))
        for idx in sorted(indices):
            carpeta_salida, carpeta_imap, _imap, _mid, _crudo, msg = items[idx - 1]
            _guardar_capturas_pantalla(msg, carpeta_salida, carpeta_imap, n_pantallas, con_remotas)

    elif accion == "adjuntos":
        modo_si_existe = _validar_modo_si_existe(args.get("si_existe"))
        if modo_si_existe is None:
            return
        for idx in sorted(indices):
            carpeta_salida, carpeta_imap, _imap, _mid, _crudo, msg = items[idx - 1]
            _guardados, hubo_adjuntos = _guardar_adjuntos(msg, carpeta_salida, carpeta_imap, modo_si_existe=modo_si_existe)
            if not hubo_adjuntos:
                print(f"(el mensaje {idx} no tiene adjuntos)")
            if _quiere_imagenes(args):
                _procesar_imagenes_incrustadas(msg, carpeta_salida, carpeta_imap, args, modo_si_existe)

    elif accion == "responder":
        modo_todos = "responder_todos" in args
        asunto_manual = args.get("asunto")  # None si no se dio -s (ver _datos_respuesta)

        # CC/BCC manuales + -c (eGarante): misma validación que ya usa
        # -escribir, reutilizada tal cual — se COMBINAN con lo que aporte
        # -responder-todos para cada mensaje (_datos_respuesta), nunca lo
        # sustituyen.
        cc_extra = _lista_direcciones(args.get("cc"))
        _validar_direcciones(cc_extra, "-cc")
        if args.get("cc_egarante") and _DIRECCION_EGARANTE not in cc_extra:
            cc_extra.append(_DIRECCION_EGARANTE)
        bcc_extra = _lista_direcciones(args.get("bcc"))
        _validar_direcciones(bcc_extra, "-bcc")

        # Adjuntos (opcional): mismas rutas completas que -escribir.
        rutas_adjuntos = [r.strip() for r in (args.get("adjuntar") or "").split(",") if r.strip()]
        for ruta in rutas_adjuntos:
            if not os.path.isfile(ruta):
                _error(f"No se encontró el fichero a adjuntar '{ruta}' (-adjuntar). No se ha respondido a nada.")
                return

        # Cuerpo: -cuerpo (fichero) > -texto (inline) > entrada estándar —
        # mismo orden y mismos avisos de "vacío a propósito" que ya usa
        # -escribir para -cuerpo (sección 5.11), pero con _error()+return
        # en vez de sys.exit: -responder es una acción más dentro de un
        # posible comando con VARIAS acciones a la vez (sección 5.9), y un
        # fallo aquí no debe abortar las demás. OJO: -texto "" (o stdin,
        # si no se da ni -cuerpo ni -texto) se deja EXACTAMENTE como
        # estaba — sin ningún aviso de "vacío" — porque así quedó decidido
        # a propósito en la Ronda G (test_ronda_g_cuerpo_asunto_vacio.py);
        # el aviso de -cuerpo vacío es nuevo solo porque -cuerpo en sí es
        # nuevo aquí, no había ningún comportamiento previo que preservar.
        if "cuerpo" in args:
            ruta_cuerpo = args["cuerpo"]
            try:
                with open(ruta_cuerpo, "r", encoding="utf-8") as f:
                    texto_resp = f.read()
            except OSError as e:
                _error(f"No se pudo leer el fichero de texto '{ruta_cuerpo}' (-cuerpo). No se ha respondido a nada: {e}")
                return
            if not texto_resp.strip():
                _error("Aviso: el cuerpo de la respuesta ha quedado vacío — se envía sin texto en el cuerpo.")
        else:
            texto_resp = args.get("texto")
            if texto_resp is not None:
                texto_resp = _desescapar_texto_cli(texto_resp)
            if texto_resp is None:
                if sys.stdin.isatty():
                    _error(
                        "Para responder desde comando directo hace falta -cuerpo <fichero.txt>, "
                        "-texto \"...\" o pasar el texto por la entrada estándar (stdin)."
                    )
                    return
                texto_resp = sys.stdin.read().strip()
                if not texto_resp:
                    _error("La entrada estándar estaba vacía; no hay texto para la respuesta.")
                    return

        for idx in sorted(indices):
            carpeta_salida, _ci, imap, _mid, _crudo, msg = items[idx - 1]
            _responder_mensaje_cli(
                carpeta_salida, msg, texto_resp, imap,
                modo_todos=modo_todos, asunto_manual=asunto_manual,
                cc_extra=cc_extra, bcc_extra=bcc_extra, adjuntos=(rutas_adjuntos or None),
                usar_reply_to=not args.get("sin_reply_to"),
            )


# Orden en el que se ejecutan si se piden varias acciones a la vez — las
# que guardan/muestran van primero, "eliminar" siempre la última (para no
# perder la posibilidad de descargar/ver/guardar adjuntos de un mensaje
# que la propia orden acaba de borrar).
_ORDEN_ACCIONES_MULTIPLES = (
    "duplicados", "descargar", "ver", "ver_lista", "ver_titulos", "ver_ids", "ver_txt", "ver_pdf", "ver_img",
    "adjuntos", "csv", "responder", "eliminar", "json",
)


def _ejecutar_accion_cli(items, multi, acciones, args):
    """Ejecuta (sin preguntar NADA) una o varias acciones pedidas por línea
    de comandos sobre 'items', en un orden sensato (ver
    _ORDEN_ACCIONES_MULTIPLES). 'acciones' puede ser un solo nombre de
    acción (compatibilidad) o una lista de varias, ej. ["descargar",
    "ver_txt"] para "-descargar -ver-txt" a la vez. Vacía o None: no hace
    nada más (ya se listó)."""
    if not acciones:
        return
    if isinstance(acciones, str):
        acciones = [acciones]
    pedidas = set(acciones)
    for accion in _ORDEN_ACCIONES_MULTIPLES:
        if accion in pedidas:
            _ejecutar_una_accion_cli(items, multi, accion, args)


def _comando_directo_interno(args):
    """Modo no interactivo: NUNCA pregunta nada, y siempre vuelve al
    terminal al terminar — incluso si la acción no se pudo completar del
    todo (se explica el motivo). El texto normal va a stdout; los errores
    y avisos de fallo van a stderr, para que se puedan distinguir en un
    script.

    Reunir mensajes (elige una forma; se pueden combinar -buscar/
    -buscarcuerpo con -buscarfecha(s) y -buscaradjunto* entre sí):
      Últimos N:   -acc <cuenta[s]|todas> -last N [-folders inbox|all]
      Búsqueda:    -acc <cuenta[s]|todas> -buscar TEXTO [-campos LISTA] [-folders inbox|all]
                   -acc <cuenta[s]|todas> -buscarcuerpo TEXTO [-folders inbox|all]
                   -acc <cuenta[s]|todas> -id VALOR [-folders inbox|all]
      Por fecha:   -acc <cuenta[s]|todas> -buscarfecha AAAA-MM-DD
                   -acc <cuenta[s]|todas> -buscarfechas AAAA-MM-DD[-HH[-MM]] AAAA-MM-DD[-HH[-MM]]
      Por adjunto: -acc <cuenta[s]|todas> -buscaradjuntosi   (o -buscaradjuntono)
      Por ID(s), SIN reunir nada antes:
                   -acc <cuenta[s]|todas> -ver "id:XXXX" / "id:XXXX,id:YYYY"
                   (también con -descargar/-eml, -eliminar, -ver-txt, -ver-pdf,
                   -adjuntos, -responder — y se pueden combinar varias a la
                   vez sobre los mismos ids, ver más abajo)

    -acc admite una o varias cuentas nombradas seguidas de espacio (ej.
    "-acc gmx yahoo"), o la palabra especial "todas". Con varias cuentas
    nombradas (a diferencia de "todas"), -folders se sigue respetando
    (por defecto, solo la carpeta principal de cada una).

    Acción sobre el resultado (opcional):
      -descargar <selección>   .eml de los elegidos (ej: 1,3,5-8, 'todos', o "id:X,id:Y")
      -eml <selección>         alias de -descargar
      -eliminar <selección>    IRREVERSIBLE y SIN CONFIRMAR — ten cuidado
      -csv                     exporta toda la lista a un CSV
      -ver <selección>         texto completo por pantalla (stdout)
      -ver-lista               solo la lista (título + vista previa), sin
                               texto completo de cada uno y sin menú
      -ver-titulos             solo el título de cada uno (id + origen/destino
                               + asunto), sin vista previa ni menú
      -ver-ids                 solo los ids, uno por línea ("1. id:XXXX")
      -json                    TODA la información de cada mensaje (de, para, cc,
                               bcc, asunto, cuerpo en texto, adjuntos y dónde se
                               guardó cada fichero generado: .eml, .txt, .pdf,
                               imágenes de -ver-img, adjuntos) en JSON por stdout;
                               todo lo demás va a stderr. Se ejecuta la última.
      -ver-txt <selección>     texto completo guardado en un .txt por mensaje
                               Con -imagenes / -ocr / -tabla [-sep X] / -describir
                               (también con -adjuntos) se tratan las imágenes
                               INCRUSTADAS en el cuerpo: guardarlas, leer su
                               texto, convertir una tabla en CSV o describirlas.
      -ver-pdf <selección>     texto completo guardado en un .pdf por mensaje,
                               con las imágenes incrustadas del correo en su
                               sitio (como -ver-img; admite -con-remotas) y
                               los enlaces clicables de siempre -- si Pillow
                               no está instalado, se genera igual pero solo
                               con el texto, avisando de por qué.
      -ver-img <selección>     el correo convertido en imagen(es) .png, tal
                               como se vería al abrirlo -- para cuando
                               -ver-txt/-ver-pdf no muestran nada útil
                               porque el correo es en realidad una sola
                               imagen grande; ver sección propia más abajo
                               (-pantallas/-con-remotas).
      -adjuntos <selección>    guarda los adjuntos de verdad de los elegidos
                               (no imágenes de firma incrustadas); si un
                               mensaje no tiene, lo indica y sigue.
      -responder <selección>   responde y envía; ver sección propia más
                               abajo (-responder-todos/-s/-cuerpo/-texto/
                               -cc/-c/-bcc/-adjuntar).
      Si reúnes con -buscarfecha(s)/-buscaradjunto*/-buscarcuerpo y no
      indicas ninguna acción, se usa -ver sobre TODOS los resultados.
      Con -last sin acción, se entra al menú interactivo de siempre.

      Varias acciones A LA VEZ, sobre los MISMOS mensajes, sin repetir la
      selección: se puede dar la selección (ids o posiciones) a UNA sola
      acción y añadir las demás como flags sueltos, sin valor — se aplican
      TODAS al mismo resultado (no hace falta volver a escribir los ids).
      Ejemplo — PDF con cabeceras, .eml y .txt de los mismos 2 ids, en una
      sola búsqueda:
        python3 correo.py -acc gmx -ver-pdf id:XXXX,id:YYYY -cabeceras -descargar -ver-txt
      Ejemplo — bajar el .eml Y sus adjuntos a la vez (sin -adjuntos id:...):
        python3 correo.py -acc gmx -descargar id:XXXX -adjuntos
      (Se ejecutan en el orden de _ORDEN_ACCIONES_MULTIPLES; -cabeceras/
      -cabeceras-todas se aplica igual a todas las acciones que muestran
      texto de la lista anterior.)

    Guardar en el directorio actual en vez de en SALIDA/<cuenta> (opcional,
    junto con -descargar/-eml/-ver-txt/-ver-pdf/-ver-img/-adjuntos/-csv):
      -aqui   los ficheros (mensajes Y adjuntos) se guardan directamente en
              el directorio desde donde se llama al programa, sin la
              subcarpeta SALIDA/<cuenta> de siempre. Con varias cuentas a
              la vez, todas comparten esa misma carpeta actual. No se
              puede combinar con -responder (que no guarda ningún fichero,
              y que identifica la cuenta a partir del nombre de la carpeta
              de salida — algo que -aqui le quita).

    Qué hacer si el fichero ya existe (opcional; junto con -descargar/-eml/
    -ver-txt/-ver-pdf/-adjuntos -- no afecta a -ver-img ni a -csv):
      -si-existe no             si ya existe, lo deja tal cual (no se guarda
                                nada nuevo para ese correo/adjunto).
      -si-existe sobrescribir   si ya existe, se sobrescribe sin avisar.
      -si-existe correo         sobrescribe el .eml/.txt/.pdf si ya existe,
                                pero deja tal cual los adjuntos que ya
                                existan.
      -si-existe adjuntos       sobrescribe los adjuntos que ya existan, pero
                                deja tal cual el .eml/.txt/.pdf si ya existe.
      Por defecto (sin -si-existe), el comportamiento de siempre: nunca se
      sobrescribe nada -- si el nombre ya existe, se añade _2, _3... antes de
      la extensión.
      Ejemplo -- repetir una descarga sin duplicar lo que ya se bajó:
        python3 correo.py -acc gmx1 -last 20 -descargar todos -si-existe no

    Cabeceras a mostrar (opcional, junto con -ver/-ver-txt/-ver-pdf):
      -cabeceras         añade Message-ID (el original, completo), Cc,
                          Reply-To, Return-Path y Authentication-Results
                          (o Received-SPF) — las que el mensaje tenga.
      -cabeceras-todas   añade TODAS las cabeceras del mensaje, tal como
                          llegaron (incluye las de -cabeceras y además
                          cada "Received" del recorrido). Si se dan los
                          dos a la vez, manda -cabeceras-todas.
      Por defecto (sin ninguno de los dos) se muestra solo ID/De/Para/
      Asunto/Fecha, como siempre.

    Búsqueda larga sin nadie delante (opcional; solo afecta a -buscar/-id/
    -buscarfecha(s)/-buscarcuerpo/-buscaradjunto* y a la forma "por ID sin
    reunir antes" — con -last a solas no hacen nada):
      -ds              NUNCA pregunta nada (ni siquiera la breve espera de
                       "¿aumentar el intervalo?" que -acc por sí solo ya
                       deja siempre muy corta) — en vez del aviso normal,
                       muestra un progreso (correos vistos/encontrados)
                       que se autosobreescribe en una sola línea.
      -intervalo 35s|4m  cada cuánto se refresca ese progreso (o, sin
                       -ds, cada cuánto se pregunta "¿seguir buscando?").
                       Admite segundos ('35s') o minutos ('4m'). Por
                       defecto, INTERVALO_AVISO_INICIAL (config, 30s).
      -max 15m         tiempo MÁXIMO total para esta búsqueda — pasado
                       ese tiempo se detiene sola con lo encontrado hasta
                       el momento, avisando por qué. Por defecto, sin
                       límite (sigue hasta agotar todo lo que haya).
      Ejemplo, pensado para dejarlo corriendo sin supervisión:
        python3 correo.py -acc gmx -buscarcuerpo "factura" -folders all \\
          -ds -intervalo 35s -max 15m -descargar

    Búsqueda larga que se puede parar y continuar más tarde (Ronda U,
    junto con -buscar/-buscarcuerpo/-buscarfecha(s)/-buscaradjunto* -- NO
    con -id ni -duplicados; solo admite -descargar como acción, o
    ninguna, que por defecto hace lo mismo):
      -reanudable      descarga cada correo que coincide AL MOMENTO (no
                       al final) y guarda el progreso (qué cuentas+
                       carpetas ya terminaron, y qué correos de las que
                       quedaron a medias ya se descargaron -- por su id
                       estable, nunca por su número de secuencia IMAP)
                       en CARPETA_PROGRESO_BUSQUEDAS. Repetir después
                       EXACTAMENTE la misma orden (sin ningún flag nuevo
                       para "reanudar") encuentra sola ese progreso y
                       continúa sin repetir nada -- ver
                       _ejecutar_busqueda_reanudable.
      -orden antiguos|recientes   solo junto con -reanudable. Dentro de
                       cada carpeta: "recientes" (de siempre) el más
                       nuevo primero; "antiguos" el más viejo primero
                       (para empezar por la punta más vieja del rango de
                       fechadesde/fechahasta).
      Ejemplo, para un rango de varios años en todas las cuentas/carpetas:
        python3 correo.py -acc todas -buscarcuerpo "seg-social" -folders all \\
          -buscarfechas 2020-01-01 2023-12-31 -reanudable -orden antiguos

    Correos nuevos del INBOX desde la última vez (Ronda V, junto con
    -descargar y/o -ver, SIN -buscar/-id/-buscarfecha(s)/-buscarcuerpo/
    -buscaradjunto*/-duplicados, sin -acc todas y sin -folders all/todas
    -- de momento solo mira el INBOX de cada cuenta):
      -nuevos          recuerda, por cuenta, qué correos del INBOX ya se
                       vieron en una ejecución anterior (por su id
                       estable, en CARPETA_REGISTRO_NUEVOS) y antepone en
                       la lista los que NO estaban -- sin ninguna marca ni
                       resumen, solo reordenados. Pensado para repetirlo
                       en días distintos (p.ej. desde Termux) y ver enseguida
                       qué ha llegado desde la última vez, aunque siga
                       siendo -last (20 por defecto) lo que decide CUÁNTOS
                       mensajes se miran cada vez. Una vez visto, un
                       correo no vuelve a contar como nuevo (aunque borres
                       los .eml descargados) salvo que borres a mano su
                       fichero en CARPETA_REGISTRO_NUEVOS -- ver
                       _marcar_nuevos_y_reordenar.
      Ejemplo, para mirar 2 cuentas a la vez cada mañana:
        python3 correo.py -acc gmx yahoo -nuevos -descargar

    Responder (detalle; Ronda N — los mismos flags de cuerpo/CC/BCC/
    adjuntos que -escribir, descritos ahí mismo más abajo, también
    funcionan aquí):
      -responder <selección>   responde y envía a cada mensaje elegido
                                (si eliges varios, se manda la MISMA
                                respuesta a todos, pero cada uno con SU
                                PROPIO destinatario/asunto calculado).
      -responder-todos          por defecto, responde SOLO al remitente
                                (De:) del mensaje original — con este
                                flag, responde A TODOS: el remitente más
                                los Para: originales van al nuevo Para:,
                                y el CC: original pasa al nuevo CC:. Si
                                el mensaje trae BCC: (algo que un correo
                                de verdad, recién llegado, nunca trae —
                                el propio protocolo se lo oculta a todo
                                el mundo menos a quien lo escribió; solo
                                ocurre si respondes a tu propia copia en
                                Enviados de un envío anterior con BCC),
                                también pasa al nuevo BCC:. Tu propia
                                dirección nunca se incluye a ti mismo.
      -s "Asunto"               por defecto, el asunto es "Re: <el
                                asunto original>" (o el original tal
                                cual, si ya empezaba por "Re:"). Con -s
                                se usa el que tú des, tal cual (incluso
                                en blanco con -s "").
      -cuerpo <fichero.txt>     cuerpo de la respuesta, leído de ese
                                fichero, en vez de -texto o la entrada
                                estándar — misma prioridad que -escribir
                                (-cuerpo > -texto > stdin).
      -cc/-c/-bcc/-adjuntar     ver la sección de -escribir, más abajo:
                                mismo significado exacto aquí. Se
                                COMBINAN con -responder-todos (nunca lo
                                sustituyen): por ejemplo, -responder-todos
                                -cc "jefe@x.com" añade a jefe@x.com AL
                                CC original, no en vez de él.
      Ejemplo — responder a todos, con copia certificada de eGarante y
      un adjunto, leyendo el cuerpo de un fichero:
        python3 correo.py -acc gmx1 -responder id:XXXX -responder-todos \\
          -cuerpo /sdcard/textos/respuesta.txt -c -adjuntar /sdcard/fotos/prueba.jpg

    Correo NUEVO, desde cero (no busca ni selecciona ningún mensaje
    existente — no se combina con lo anterior ni con -last/-buscar/etc.):
      -acc <cuenta>            UNA sola cuenta (no 'todas' ni varias):
                                desde la que se envía.
      -escribir                activa este modo.
      -p "a@x.com,b@y.com"     destinatarios (Para), separados por comas —
                                UN solo correo, con todos ellos juntos en
                                "Para" (se ven entre sí). No se combina
                                con -lista ni -lista-fichero.
      -lista "a@x.com,b@y.com" el MISMO correo (mismo asunto, cuerpo y
                                adjuntos) a cada dirección de la lista,
                                separadas por comas, pero mandando un
                                envío INDEPENDIENTE por destinatario: cada
                                uno recibe su propio correo con SOLO su
                                dirección en "Para", sin ver a los demás
                                de la lista. No se combina con -p ni con
                                -lista-fichero. Al final se imprime un
                                resumen (cuántos se enviaron bien y, si
                                falla alguno, cuáles).
      -lista-fichero <ruta.txt> igual que -lista, pero las direcciones se
                                leen de un fichero de texto, UNA por
                                línea. Las líneas en blanco y las que
                                empiecen por "#" se ignoran (para poder
                                comentar la lista). No se combina con -p
                                ni con -lista.
      -s "Asunto"              asunto (opcional; en blanco si se omite).
      -cuerpo <fichero.txt>    cuerpo del mensaje, leído de ese fichero.
                                Si no se da, admite -texto "..." (como en
                                -responder) o la entrada estándar (stdin).
      -cc "c@x.com,d@y.com"    copia (CC), separados por comas (opcional).
      -c                       añade a CC, sin más, la dirección de
                                eGarante (mailsigned@egarante.com) para
                                obtener una copia certificada del envío —
                                se puede combinar con -cc.
      -bcc "e@x.com,f@y.com"   copia OCULTA (BCC), separados por comas
                                (opcional) — ni los destinatarios de Para
                                ni los de CC ven esta cabecera; solo tú,
                                en tu propia copia de Enviados.
      -adjuntar <ruta1,ruta2>  ficheros a adjuntar, con su ruta completa
                                (pueden estar en cualquier carpeta, no
                                solo la actual), separados por comas.
      Ejemplo:
        python3 correo.py -acc gmx1 -escribir -p "jose@pp.es,rtve@rtve.es" \\
          -s "Reclamación" -cuerpo /sdcard/textos/reclamacion.txt -c \\
          -adjuntar /sdcard/Download/factura.pdf,/sdcard/fotos/prueba.jpg
      Ejemplo — el mismo correo, un envío independiente por destinatario,
      con la lista en un fichero:
        python3 correo.py -acc gmx1 -escribir -lista-fichero /sdcard/textos/lista.txt \\
          -s "Oferta" -cuerpo /sdcard/textos/oferta.txt -adjuntar /sdcard/Download/catalogo.pdf

    Subir un .eml YA EXISTENTE (en el ordenador/móvil) a la carpeta INBOX
    de una cuenta, vía IMAP (lo contrario de -descargar/-eml: aquí se
    parte de un fichero local, no se busca nada en el servidor — no se
    combina con lo anterior ni con -escribir):
      -acc <cuenta>            UNA sola cuenta (no 'todas' ni varias): a
                                la que se sube.
      -subir-eml <ruta.eml>    ruta completa al fichero .eml a subir.
      Se conserva la fecha original del mensaje (cabecera Date:) como
      fecha de recepción en el servidor (para que quede ordenado por esa
      fecha, no por "ahora"), y el mensaje queda SIN marcar como leído
      (igual que cualquier correo recién llegado). Si el fichero no
      tiene una Date: reconocible, el servidor le pone la fecha de
      subida.
      Ejemplo:
        python3 correo.py -acc gmx1 -subir-eml /sdcard/Download/correo.eml

    -duplicados               informa de correos EXACTAMENTE iguales (mismo
                              remitente, destinatarios, asunto, cuerpo y
                              adjuntos) dentro de la MISMA cuenta y carpeta
                              — nunca entre carpetas ni cuentas distintas.
                              No admite selección: se aplica a TODOS los
                              reunidos. Reúne él solo TODA la carpeta (por
                              defecto solo INBOX; -folders all para mirar
                              todas) — no hace falta -last ni ningún otro
                              filtro, aunque se pueden combinar (p.ej. con
                              -buscarfecha(s) para limitarlo a un rango de
                              fechas). No se combina con -escribir ni
                              -subir-eml.
      Cada grupo de copias idénticas se lista con su POSICIÓN (no su id:
      dos copias iguales tienen el MISMO id, así que -eliminar id:XXXX
      las borraría las dos a la vez). Para quitar solo una (la que tú
      elijas, dejando la otra), repite la MISMA orden añadiendo -eliminar
      y esa posición:
        python3 correo.py -acc gmx1 -duplicados
        python3 correo.py -acc gmx1 -duplicados -eliminar 7

    -ver-img <selección>      guarda el correo como 1 o más imágenes .png
                              (<nombre_del_correo>_pantalla1.png,
                              _pantalla2.png...), tal como se vería al
                              abrirlo -- pensado para cuando -ver-txt/
                              -ver-pdf no sirven de nada porque el correo
                              es en realidad UNA sola imagen grande (muy
                              habitual en boletines/publicidad): ahí el
                              texto no es texto de verdad, está DENTRO de
                              esa imagen, así que no hay nada que extraer.
                              Funciona igual con cualquier correo, no solo
                              ese caso (negrita, enlaces e imágenes
                              incrustadas en su sitio; es aproximado, no
                              una réplica exacta de ningún cliente de
                              correo). Necesita tener instalada la
                              librería Pillow (pip install Pillow
                              --break-system-packages; en Termux puede ir
                              mejor "pkg install python-pillow") -- único
                              caso en todo el programa que necesita algo
                              aparte; si no está instalada, se avisa con
                              el comando exacto a ejecutar, en vez de dar
                              un error críptico. (-ver-pdf, más arriba,
                              usa esta misma Pillow -- si está instalada
                              -- para incrustar estas mismas imágenes en
                              el PDF; a diferencia de -ver-img, sigue
                              funcionando sin ella, solo con el texto.)
      -pantallas N            cuántas imágenes generar, como ir bajando
                              con el dedo pantalla a pantalla en el móvil:
                              la 1ª es lo primero que se ve, la 2ª
                              continúa justo donde acababa la 1ª, etc. Por
                              defecto 1 (sin -pantallas). Si el correo no
                              da para tantas, se generan menos de las
                              pedidas y se explica (no hay más contenido
                              debajo).
      -con-remotas            además de las imágenes incrustadas en el
                              propio correo, descarga también las remotas
                              (http/https) que el correo solo enlaza. Por
                              defecto NO se descarga ninguna -- igual que
                              "mostrar imágenes" bloqueado de entrada en
                              cualquier cliente de correo (sin pedirlo tú
                              expresamente, no se contacta ningún
                              servidor ajeno).
      Ejemplo — 3 pantallas de un boletín que es todo una imagen, con
      imágenes remotas incluidas:
        python3 correo.py -acc gmx1 -ver-img id:XXXX -pantallas 3 -con-remotas
    """
    acc = args["acc"]
    if isinstance(acc, str):  # por si algún llamador pasa una cadena suelta en vez de una lista
        acc = [acc]
    folders_dado = args.get("folders")
    folders_arg = (folders_dado or "inbox").strip().lower()
    acciones = [a for a in ACCIONES_CLI if a in args]  # TODAS las presentes, no solo la primera — permite combinarlas (ej. -descargar -ver-txt)

    # Nivel de detalle del listado (-ver-ids gana a -ver-titulos).
    _ESTADO_LISTADO["modo"] = "id" if "ver_ids" in acciones else ("titulo" if "ver_titulos" in acciones else "completo")

    flags_de_reunion = (
        "last", "buscar", "id", "buscarfecha", "buscarfechas",
        "buscarcuerpo", "buscaradjuntosi", "buscaradjuntono",
    )
    hay_reunion_explicita = any(k in args for k in flags_de_reunion)

    # -aqui: todo lo que se guarde en esta llamada (mensajes Y adjuntos) va
    # al directorio ACTUAL, no a SALIDA/<cuenta> (ver _ESTADO_SALIDA). No
    # se combina con -responder: -responder identifica la cuenta a partir
    # del nombre de la carpeta de salida (SALIDA/<cuenta> — ver
    # _cuenta_de_carpeta_salida), y -aqui se lo quita; además -responder no
    # guarda ningún fichero, así que -aqui no le aporta nada de todos modos.
    if "aqui" in args:
        if "responder" in acciones:
            sys.exit(
                "-aqui no se puede combinar con -responder: -responder identifica la cuenta desde "
                "el nombre de la carpeta de salida (SALIDA/<cuenta>), y -aqui la cambia por el "
                "directorio actual — no hace falta de todos modos, -responder no guarda ningún fichero."
            )
        _ESTADO_SALIDA["aqui"] = True

    # -ds ("desatendido"), -intervalo y -max: control de una búsqueda larga
    # sin nadie delante (sección 5.13 del Manual 2) — solo importan para
    # las dos formas que de verdad pueden tardar y avisar periódicamente
    # (por ID sin reunir antes, y -buscar/-id/-buscarfecha(s)/-buscarcuerpo/
    # -buscaradjunto*: ver más abajo, donde se llama a _ejecutar_busqueda);
    # con -last a solas no hacen nada (esa forma no pasa por ahí).
    modo_desatentido = "ds" in args
    intervalo_cli = _segundos_desde_texto(args["intervalo"], "-intervalo") if "intervalo" in args else None
    tiempo_maximo_cli = _segundos_desde_texto(args["max_busqueda"], "-max") if "max_busqueda" in args else None

    # -orden (Ronda U) solo tiene sentido junto con -reanudable -- el resto
    # de formas de buscar no lo admiten (siempre "más reciente primero",
    # como de toda la vida). Se comprueba ANTES de conectar (igual que
    # -intervalo/-max de arriba): un valor que no va a usarse para nada
    # mejor que corte aquí, sin gastar una conexión real.
    if "orden" in args and "reanudable" not in args:
        sys.exit("-orden solo se puede usar junto con -reanudable.")

    # --- Correo nuevo desde cero (-escribir), sin relación con ningún
    # mensaje existente: se resuelve aparte y ANTES de conectar por IMAP
    # (no hace falta — enviar es solo SMTP), y no sigue con el resto de
    # esta función (sección 5.11 del Manual 2). ---
    if "escribir" in args:
        conflictivos = acciones + [f for f in flags_de_reunion if f in args]
        if "subir_eml" in args:
            conflictivos.append("subir_eml")
        if conflictivos:
            sys.exit(
                f"-escribir no se puede combinar con {', '.join('-' + c.replace('_', '-') for c in conflictivos)} "
                "— es una acción aparte, para redactar un correo NUEVO (no busca ni selecciona mensajes existentes)."
            )
        if len(acc) != 1 or acc[0].strip().lower() == "todas":
            sys.exit("-escribir necesita exactamente UNA cuenta con -acc (no 'todas' ni varias) — es la cuenta desde la que se envía.")
        nombre_cuenta = acc[0]
        if nombre_cuenta not in CUENTAS:
            sys.exit(f"Cuenta '{nombre_cuenta}' no está en CUENTAS. Opciones: {', '.join(CUENTAS)}.")
        cfg = CUENTAS[nombre_cuenta]

        # Destinatarios: -p (un correo, con TODOS en "Para") o -lista/
        # -lista-fichero (Ronda O: el MISMO correo -- mismo asunto, cuerpo
        # y adjuntos -- pero un envío INDEPENDIENTE por destinatario, sin
        # que ninguno vea a los demás). Las tres son mutuamente excluyentes
        # entre sí: dan formas distintas de rellenar "Para", y combinarlas
        # sería ambiguo sobre qué destinatarios van juntos en un mismo
        # correo y cuáles por separado.
        fuentes_destinatarios = [f for f in ("para", "lista", "lista_fichero") if f in args]
        if len(fuentes_destinatarios) > 1:
            sys.exit(
                "-escribir: usa solo UNO de -p / -lista / -lista-fichero para dar los destinatarios, no varios a la vez "
                "(-p manda un único correo con todos en \"Para\"; -lista y -lista-fichero mandan un correo independiente "
                "a cada uno)."
            )
        modo_lista = "lista" in args or "lista_fichero" in args
        if modo_lista:
            if "lista" in args:
                destinatarios_lista = _lista_direcciones(args["lista"])
                origen_lista = "-lista"
            else:
                ruta_lista = args["lista_fichero"]
                try:
                    with open(ruta_lista, "r", encoding="utf-8") as f:
                        lineas_lista = f.readlines()
                except OSError as e:
                    sys.exit(f"No se pudo leer el fichero de lista '{ruta_lista}' (-lista-fichero): {e}")
                destinatarios_lista = [
                    linea.strip() for linea in lineas_lista
                    if linea.strip() and not linea.strip().startswith("#")
                ]
                origen_lista = "-lista-fichero"
            if not destinatarios_lista:
                sys.exit(f"{origen_lista} no contiene ninguna dirección de correo.")
            _validar_direcciones(destinatarios_lista, origen_lista)
            # Quita duplicados (sin distinguir mayúsculas/minúsculas) conservando
            # el orden -- para no mandar el mismo correo dos veces a quien
            # aparezca repetido en la lista (fácil que pase en un fichero
            # grande mantenido a mano).
            vistos_lista = set()
            destinatarios_unicos = []
            for direccion in destinatarios_lista:
                clave = direccion.lower()
                if clave not in vistos_lista:
                    vistos_lista.add(clave)
                    destinatarios_unicos.append(direccion)
            if len(destinatarios_unicos) < len(destinatarios_lista):
                _error(f"Aviso: se han quitado {len(destinatarios_lista) - len(destinatarios_unicos)} dirección(es) duplicada(s) de la lista.")
            destinatarios_lista = destinatarios_unicos
        else:
            destinatarios = _lista_direcciones(args.get("para"))
            if not destinatarios:
                sys.exit('-escribir necesita al menos un destinatario: -p "direccion1,direccion2", -lista "..." o -lista-fichero <fichero.txt>.')
            _validar_direcciones(destinatarios, "-p")

        cc_direcciones = _lista_direcciones(args.get("cc"))
        _validar_direcciones(cc_direcciones, "-cc")
        if args.get("cc_egarante") and _DIRECCION_EGARANTE not in cc_direcciones:
            cc_direcciones.append(_DIRECCION_EGARANTE)
        bcc_direcciones = _lista_direcciones(args.get("bcc"))
        _validar_direcciones(bcc_direcciones, "-bcc")

        asunto = args.get("asunto", "")
        if not asunto:
            _error("Aviso: -escribir sin -s — se envía con el asunto en blanco.")

        # Cuerpo: -cuerpo (fichero, lo pedido) > -texto (inline, como en
        # -responder) > entrada estándar — el mismo orden de prioridad que
        # ya se documenta para -responder, con -cuerpo por delante.
        #
        # -cuerpo/-texto son formas EXPLÍCITAS de dar el cuerpo: si lo dado
        # resulta vacío (fichero vacío, o -texto ""), se entiende como
        # "vacío a propósito" — se avisa (por stderr) pero se envía igual,
        # en paralelo a como ya se trata el asunto vacío (-s omitido) más
        # arriba. La entrada estándar es distinta: no hay forma de saber
        # si "vacía" significa "cuerpo en blanco a propósito" o "me olvidé
        # de redirigir algo" — por eso, si no se dio ni -cuerpo ni -texto
        # y lo leído de stdin resulta vacío, se sigue bloqueando (mismo
        # criterio que -responder con la entrada estándar vacía).
        if "cuerpo" in args:
            ruta_cuerpo = args["cuerpo"]
            try:
                with open(ruta_cuerpo, "r", encoding="utf-8") as f:
                    cuerpo = f.read()
            except OSError as e:
                sys.exit(f"No se pudo leer el fichero de texto '{ruta_cuerpo}' (-cuerpo): {e}")
            if not cuerpo.strip():
                _error("Aviso: el cuerpo del correo ha quedado vacío — se envía sin texto en el cuerpo.")
        elif "texto" in args:
            cuerpo = _desescapar_texto_cli(args["texto"])
            if not cuerpo.strip():
                _error("Aviso: el cuerpo del correo ha quedado vacío — se envía sin texto en el cuerpo.")
        elif not sys.stdin.isatty():
            cuerpo = sys.stdin.read()
            if not cuerpo.strip():
                sys.exit("La entrada estándar estaba vacía; no hay texto para el cuerpo del correo.")
        else:
            sys.exit('-escribir necesita el cuerpo del mensaje: -cuerpo <fichero.txt>, -texto "..." o por la entrada estándar (stdin).')

        # Adjuntos: rutas de fichero completas, en cualquier carpeta (no
        # solo la actual) — separadas por comas, igual que -p/-cc.
        rutas_adjuntos = [r.strip() for r in (args.get("adjuntar") or "").split(",") if r.strip()]
        for ruta in rutas_adjuntos:
            if not os.path.isfile(ruta):
                sys.exit(f"No se encontró el fichero a adjuntar '{ruta}' (-adjuntar).")

        cc_final = ", ".join(cc_direcciones) if cc_direcciones else None
        bcc_final = ", ".join(bcc_direcciones) if bcc_direcciones else None
        adjuntos_final = rutas_adjuntos or None

        if not modo_lista:
            enviado = _enviar_correo(
                cfg, nombre_cuenta, ", ".join(destinatarios), asunto, cuerpo,
                interactivo=False,
                cc=cc_final, bcc=bcc_final, adjuntos=adjuntos_final,
            )
            sys.exit(0 if enviado else 1)

        # --- Modo -lista/-lista-fichero (Ronda O): mismo asunto, cuerpo y
        # adjuntos para TODOS, pero un envío INDEPENDIENTE por destinatario
        # (cada uno con su propia "Para", sin ver a los demás de la lista —
        # elegido explícitamente por el usuario entre las alternativas
        # posibles). Para no reconectar por SMTP/IMAP ni volver a pedir la
        # contraseña en cada envío, se abre UNA sola conexión de cada tipo
        # y se reutiliza en toda la tanda (ver _abrir_smtp y el parámetro
        # 'smtp' de _enviar_correo). ---
        print(f"Enviando el mismo correo, uno por uno, a {len(destinatarios_lista)} destinatario(s)...")
        smtp_abierto = _abrir_smtp(cfg, nombre_cuenta)
        if smtp_abierto is None:
            sys.exit(1)
        # Se decide UNA sola vez si se va a intentar guardar copia en
        # Enviados (y se intenta conectar por IMAP, como mucho, una sola
        # vez): si falla aquí, se avisa una única vez y se envía el resto
        # de la lista sin volver a intentarlo en cada correo (ver
        # 'guardar_enviados' en _enviar_correo) -- ni se reconecta sola, ni
        # vuelve a pedir la contraseña por cada destinatario.
        guardar_copia = cfg.get("save_sent", True)
        imap_abierto = None
        if guardar_copia:
            imap_abierto = conectar(cfg, nombre_cuenta)
            if imap_abierto is None:
                _error(
                    "Aviso: no se pudo conectar por IMAP para guardar copias en Enviados; "
                    "se seguirá enviando, pero sin guardar copia."
                )
                guardar_copia = False

        exitosos, fallidos = [], []
        total = len(destinatarios_lista)
        for i, destinatario_individual in enumerate(destinatarios_lista, start=1):
            print(f"\n[{i}/{total}] {destinatario_individual}")
            ok = _enviar_correo(
                cfg, nombre_cuenta, destinatario_individual, asunto, cuerpo,
                interactivo=False,
                cc=cc_final, bcc=bcc_final, adjuntos=adjuntos_final,
                imap=imap_abierto, smtp=smtp_abierto, guardar_enviados=guardar_copia,
            )
            (exitosos if ok else fallidos).append(destinatario_individual)
            if i < total and RETRASO_ENTRE_ENVIOS_LISTA > 0:
                time.sleep(RETRASO_ENTRE_ENVIOS_LISTA)

        try:
            smtp_abierto.quit()
        except Exception:
            pass
        if imap_abierto is not None:
            _cerrar_imap(imap_abierto)

        print("\n" + "=" * 60)
        print(f"Resumen: {len(exitosos)} de {total} correo(s) enviado(s) correctamente.")
        if fallidos:
            print(f"  FALLIDOS ({len(fallidos)}): {', '.join(fallidos)}")
        sys.exit(0 if not fallidos else 1)

    # --- Subir un .eml YA EXISTENTE (en el ordenador/móvil) a la carpeta
    # INBOX de una cuenta, vía IMAP APPEND (-subir-eml): lo contrario de
    # -descargar/-eml (que guarda un .eml a partir de un mensaje que YA
    # está en el servidor) — aquí se parte de un fichero local y se sube.
    # Se resuelve aparte, igual que -escribir, pero a diferencia de
    # aquella SÍ necesita IMAP desde el principio (es el propio mecanismo
    # de "subida": un APPEND a INBOX; -escribir en cambio solo necesita
    # SMTP, y abre IMAP aparte y solo si hace falta, para la copia en
    # Enviados). ---
    if "subir_eml" in args:
        conflictivos = acciones + [f for f in flags_de_reunion if f in args]
        if "escribir" in args:
            conflictivos.append("escribir")
        if conflictivos:
            sys.exit(
                f"-subir-eml no se puede combinar con {', '.join('-' + c.replace('_', '-') for c in conflictivos)} "
                "— es una acción aparte, para subir un .eml ya existente a la carpeta INBOX de una cuenta "
                "(no busca ni selecciona mensajes en el servidor)."
            )
        if len(acc) != 1 or acc[0].strip().lower() == "todas":
            sys.exit("-subir-eml necesita exactamente UNA cuenta con -acc (no 'todas' ni varias) — es la cuenta a la que se sube.")
        nombre_cuenta = acc[0]
        if nombre_cuenta not in CUENTAS:
            sys.exit(f"Cuenta '{nombre_cuenta}' no está en CUENTAS. Opciones: {', '.join(CUENTAS)}.")
        cfg = CUENTAS[nombre_cuenta]

        ruta_eml = args["subir_eml"]
        try:
            with open(ruta_eml, "rb") as f:
                crudo = f.read()
        except OSError as e:
            sys.exit(f"No se pudo leer el fichero '{ruta_eml}' (-subir-eml): {e}")
        if not crudo.strip():
            sys.exit(f"El fichero '{ruta_eml}' está vacío; no hay nada que subir.")

        # Se parsea SOLO para leer la fecha (Date:) y el asunto — el
        # APPEND sube 'crudo' tal cual viene del fichero, sin reconstruir
        # el mensaje a través de email/EmailMessage (que podría reordenar
        # cabeceras o alterar detalles del formato original): el objetivo
        # aquí es subir una copia FIEL del fichero, no recomponerlo.
        msg = email.message_from_bytes(crudo, policy=policy.default)
        fecha_dt = fecha_mensaje(msg)
        fecha_internaldate = None if fecha_dt <= _FECHA_MUY_ANTIGUA else fecha_dt
        asunto_msg = str(msg.get("subject", "") or "").strip() or "(sin asunto)"

        imap = conectar(cfg, nombre_cuenta)
        if imap is None:
            sys.exit(f"No se pudo conectar a '{nombre_cuenta}' para subir el correo.")
        try:
            # flags=None: el mensaje queda SIN marcar (nada de \Seen), es
            # decir, aparece como "no leído" en INBOX — igual que
            # cualquier correo recién llegado (a diferencia de la copia en
            # Enviados de -escribir/-responder, que sí se marca \Seen a
            # propósito). date_time=fecha_internaldate: conserva la fecha
            # ORIGINAL del mensaje (cabecera Date:) como INTERNALDATE en
            # el servidor, para que quede ordenado por esa fecha y no por
            # "ahora" — salvo que el mensaje no tenga una Date: reconocible,
            # en cuyo caso se deja que el servidor le ponga la fecha de
            # subida (None).
            estado, respuesta = imap.append(_entrecomillar_imap("INBOX"), None, fecha_internaldate, crudo)
        except Exception as e:
            _cerrar_imap(imap)
            sys.exit(f"No se pudo subir '{ruta_eml}' a la carpeta INBOX de '{nombre_cuenta}': {e}")
        _cerrar_imap(imap)
        if estado != "OK":
            sys.exit(f"El servidor rechazó la subida de '{ruta_eml}' a la carpeta INBOX de '{nombre_cuenta}': {respuesta}")
        print(f"Subido a la carpeta INBOX de '{nombre_cuenta}': {ruta_eml} (asunto: {asunto_msg}).")
        sys.exit(0)

    # Si alguna de las acciones pedidas trae un selector "id:X,id:Y" como
    # valor, y no hay ningún otro modo de reunir mensajes, se activa la
    # búsqueda directa por esos ids (sección 4 del Manual 2) — el resto de
    # acciones pedidas (si hay varias) se aplican después a lo encontrado.
    accion_con_ids = next(
        (a for a in acciones if a != "csv" and _es_selector_por_id(args.get(a, ""))), None
    )
    standalone_por_id = accion_con_ids is not None and not hay_reunion_explicita

    conexiones, es_todas = _conectar_segun_cli(acc)

    # --- Listar carpetas (-listar-carpetas), acción aparte: no selecciona
    # ni actúa sobre ningún mensaje, así que no se combina con nada más. ---
    if "listar_carpetas" in args:
        conflictivos = acciones + [f for f in flags_de_reunion if f in args]
        if conflictivos:
            for imap, _cfg in conexiones.values():
                _cerrar_imap(imap)
            sys.exit(
                f"-listar-carpetas no se puede combinar con {', '.join('-' + c.replace('_', '-') for c in conflictivos)} "
                "— es una acción aparte, solo para listar las carpetas de la cuenta (no busca ni selecciona mensajes)."
            )
        for nombre_cuenta, (imap, cfg) in conexiones.items():
            _imprimir_carpetas_cuenta(nombre_cuenta, imap, cfg)
        for imap, _cfg in conexiones.values():
            _cerrar_imap(imap)
        return

    # --- Selección directa por ID, sin reunir nada antes (ej: -ver "id:X,id:Y") ---
    if standalone_por_id:
        ids_buscados = _ids_desde_selector(args[accion_con_ids])
        if not ids_buscados:
            for imap, _cfg in conexiones.values():
                _cerrar_imap(imap)
            sys.exit(f"'{args[accion_con_ids]}' no tiene ningún id: válido (usa \"id:XXXX\" o \"id:XXXX,id:YYYY\").")
        # Buscando por ID concreto, sin -folders explícito, tiene más
        # sentido mirar TODAS las carpetas por defecto (no solo INBOX): si
        # ya conoces el ID es porque viste el mensaje antes, en CUALQUIER
        # carpeta (Enviados, archivo...), y limitarse a INBOX por defecto
        # aquí solía dar "no encontrado" sin más pista del porqué.
        folders_para_id = folders_arg if folders_dado else "all"
        plan = _plan_segun_folders(conexiones, es_todas, folders_para_id, modo_desatentido=modo_desatentido)
        # Prioriza INBOX (lo más probable para un mensaje reciente) y usa
        # una ronda pequeña para el resto de carpetas: así, aunque haya
        # muchas otras carpetas o el mensaje esté en una cuenta distinta a
        # la primera, no se pierden cientos de lecturas en carpetas que no
        # lo tienen antes de llegar a la que sí — antes esto podía tardar
        # minutos de verdad. INBOX, en cambio, usa una ronda MUCHO mayor
        # (ver tamano_ronda_inbox en _ejecutar_busqueda): con buzones
        # grandes y muchas carpetas (reportado: 31 carpetas, INBOX con
        # ~3000 mensajes), repartir a partes iguales entre todas dejaba a
        # INBOX con solo una pequeña fracción del esfuerzo por vuelta, así
        # que un mensaje que no estuviera entre los pocos más recientes
        # podía no encontrarse ni en 10 minutos, aun estando en INBOX.
        plan = _priorizar_inbox(plan)
        resultado = _ejecutar_busqueda(
            plan, ["id"], "", False, False, interactivo=False,
            conexiones=conexiones, ids_multiples=ids_buscados,
            tamano_ronda=10, tamano_ronda_inbox=500,
            modo_desatentido=modo_desatentido, intervalo_inicial=intervalo_cli, tiempo_maximo=tiempo_maximo_cli,
        )
        items, multi = resultado if resultado is not None else ([], es_todas or len(conexiones) > 1)
        if len(items) < len(set(ids_buscados)):
            _error(
                f"  Aviso: se pidieron {len(set(ids_buscados))} id(s) distintos y se encontraron "
                f"{len(items)}. Los que falten pueden estar en otra cuenta, o (si diste -folders "
                f"explícito) en una carpeta fuera de lo buscado."
            )
        # Todas las acciones pedidas se aplican a TODO lo encontrado —
        # aquí no hay una "lista" de la que seleccionar por posición, el
        # propio id ya fue la selección.
        args_todos = dict(args)
        for a in acciones:
            args_todos[a] = "todos"
        _ejecutar_accion_cli(items, multi, acciones, args_todos)
        for imap, _cfg in conexiones.values():
            _cerrar_imap(imap)
        return

    # --- Filtros de fecha / adjuntos / cuerpo (se pueden combinar entre sí) ---
    campos = None
    texto_low = ""
    fecha_desde = fecha_hasta = None
    con_adjuntos = None

    ids_multiples_busqueda = None
    if "id" in args:
        campos, texto_low = ["id"], args["id"].strip().lower()
        ids_multiples_busqueda = [texto_low]  # para poder detenerse en cuanto se encuentre (ver sección 4 del Manual 2)
    elif "buscar" in args:
        campos = _parsear_campos_cli(args.get("campos", "to,from,cc,bcc,subject,body"))
        if not campos:
            for imap, _cfg in conexiones.values():
                _cerrar_imap(imap)
            sys.exit(f"-campos '{args.get('campos')}' no reconoce ningún campo (usa to,from,cc,bcc,subject,body,id).")
        texto_low = args["buscar"].strip().lower()
    elif "buscarcuerpo" in args:
        campos, texto_low = ["body"], args["buscarcuerpo"].strip().lower()

    try:
        if "buscarfecha" in args:
            fecha_desde = _parsear_fecha_busqueda(args["buscarfecha"], fin_de_rango=False)
            fecha_hasta = _parsear_fecha_busqueda(args["buscarfecha"], fin_de_rango=True)
        elif "buscarfechas" in args:
            v1, v2 = args["buscarfechas"]
            fecha_desde = _parsear_fecha_busqueda(v1, fin_de_rango=False)
            fecha_hasta = _parsear_fecha_busqueda(v2, fin_de_rango=True)
    except ValueError as e:
        for imap, _cfg in conexiones.values():
            _cerrar_imap(imap)
        sys.exit(str(e))

    if "buscaradjuntosi" in args:
        con_adjuntos = True
    elif "buscaradjuntono" in args:
        con_adjuntos = False

    # "duplicados" entra también aquí (no en la rama de -last de más abajo)
    # aunque no traiga ningún filtro de texto/fecha/adjuntos propio: con
    # campos=None, _ejecutar_busqueda no descarta ningún mensaje por texto,
    # así que el efecto es "reunir TODO lo que haya en la(s) carpeta(s)
    # elegida(s)" — justo lo que -duplicados necesita para poder comparar
    # cada mensaje con todos los demás de su misma carpeta.
    modo_busqueda = campos is not None or fecha_desde is not None or con_adjuntos is not None or "duplicados" in args

    # -nuevos (pedido 2026-10-05/07): solo tiene sentido sobre la forma
    # "últimos N del INBOX" de más abajo (modo_busqueda=False) -- aclarado
    # con el usuario que NO se aplica a -buscar/-texto/-id/-buscarfecha(s)/
    # -buscarcuerpo/-buscaradjunto*/-duplicados, que tienen su propio
    # sentido de "qué es lo que se busca" y no el de "lo último del INBOX
    # que todavía no había visto". Se corta aquí, antes de entrar en
    # cualquiera de las dos ramas, para no dejar el flag dado por sentado
    # ignorado sin avisar.
    if "nuevos" in args and modo_busqueda:
        for imap, _cfg in conexiones.values():
            _cerrar_imap(imap)
        sys.exit(
            "-nuevos no se puede combinar con -buscar/-texto/-id/-buscarfecha(s)/-buscarcuerpo/"
            "-buscaradjuntosi/-buscaradjuntono/-duplicados -- -nuevos funciona sobre los últimos "
            "correos del INBOX (-last), sin ningún filtro de búsqueda."
        )

    if modo_busqueda:
        if "reanudable" in args:
            # -reanudable (Ronda U, pedido 2026-10-05/07): ver
            # _ejecutar_busqueda_reanudable para el diseño completo.
            # Validaciones propias -- mismo criterio que -listar-carpetas/
            # -aqui más arriba (sys.exit claro, cerrando conexiones, en vez
            # de seguir con algo a medias o ambiguo):
            if ids_multiples_busqueda is not None:
                for imap, _cfg in conexiones.values():
                    _cerrar_imap(imap)
                sys.exit(
                    "-reanudable no se puede combinar con -id: buscar un mensaje concreto ya "
                    "conocido no necesita progreso guardado entre una vez y la siguiente."
                )
            if "duplicados" in args:
                for imap, _cfg in conexiones.values():
                    _cerrar_imap(imap)
                sys.exit(
                    "-reanudable no se puede combinar con -duplicados: -duplicados necesita "
                    "comparar TODOS los mensajes reunidos entre sí, así que no puede ir "
                    "descargando y olvidando cada uno al momento."
                )
            acciones_no_permitidas = [a for a in acciones if a != "descargar"]
            if acciones_no_permitidas:
                for imap, _cfg in conexiones.values():
                    _cerrar_imap(imap)
                nombres = ", ".join("-" + a.replace("_", "-") for a in acciones_no_permitidas)
                sys.exit(
                    "-reanudable solo admite -descargar (o ninguna acción, que por defecto hace "
                    f"lo mismo) -- no se puede combinar con {nombres}."
                )
            orden = _validar_orden_busqueda(args.get("orden"))
            if orden is None:
                for imap, _cfg in conexiones.values():
                    _cerrar_imap(imap)
                return
            modo_si_existe = _validar_modo_si_existe(args.get("si_existe"))
            if modo_si_existe is None:
                for imap, _cfg in conexiones.values():
                    _cerrar_imap(imap)
                return

            clave_hash, firma = _clave_busqueda_reanudable(
                acc, folders_arg, campos, texto_low, fecha_desde, fecha_hasta, con_adjuntos, orden,
            )
            checkpoint = _cargar_checkpoint_busqueda(clave_hash, firma)
            _ejecutar_busqueda_reanudable(
                conexiones, es_todas, folders_arg, campos, texto_low,
                fecha_desde, fecha_hasta, con_adjuntos, orden, modo_si_existe, checkpoint,
                modo_desatentido=modo_desatentido, intervalo_inicial=intervalo_cli, tiempo_maximo=tiempo_maximo_cli,
            )
        else:
            folders_para_esta_busqueda = folders_arg
            if ids_multiples_busqueda is not None and not folders_dado:
                # Igual que en la forma standalone: buscando un ID concreto sin
                # -folders explícito, tiene más sentido mirar TODAS las
                # carpetas por defecto, no solo INBOX.
                folders_para_esta_busqueda = "all"
            plan = _plan_segun_folders(
                conexiones, es_todas, folders_para_esta_busqueda, modo_desatentido=modo_desatentido,
                fecha_desde=fecha_desde, fecha_hasta=fecha_hasta,
            )
            if ids_multiples_busqueda is not None:
                # Buscando por -id: igual que en la forma standalone (sección 4
                # del Manual 2), prioriza INBOX — un mensaje conocido por su ID
                # suele estar ahí, y así se evita perder tiempo en carpetas de
                # más antes de encontrarlo. INBOX usa además una ronda mucho
                # mayor que el resto (tamano_ronda_inbox) para poder
                # profundizar rápido en buzones grandes — ver la nota junto al
                # otro uso de _ejecutar_busqueda con ids_multiples, unas
                # líneas más arriba en este mismo fichero.
                plan = _priorizar_inbox(plan)
            # -duplicados también necesita el mensaje COMPLETO (cuerpo y
            # adjuntos, no solo cabeceras) para poder comparar correos entre sí.
            necesita_cuerpo = (campos is not None and "body" in campos) or con_adjuntos is not None or "duplicados" in args
            resultado = _ejecutar_busqueda(
                plan, campos, texto_low, False, necesita_cuerpo, interactivo=False, conexiones=conexiones,
                fecha_desde=fecha_desde, fecha_hasta=fecha_hasta, con_adjuntos=con_adjuntos,
                ids_multiples=ids_multiples_busqueda,
                tamano_ronda=(10 if ids_multiples_busqueda is not None else None),
                tamano_ronda_inbox=(500 if ids_multiples_busqueda is not None else None),
                modo_desatentido=modo_desatentido, intervalo_inicial=intervalo_cli, tiempo_maximo=tiempo_maximo_cli,
            )
            items, multi = resultado if resultado is not None else ([], es_todas or len(conexiones) > 1)
            # Se especificó QUÉ correo(s) buscar (por texto, id, fecha o
            # adjuntos) — si no se dice además QUÉ hacer con ellos, el verbo
            # por defecto es -ver (con su lista de adjuntos si los tiene),
            # sobre todos los resultados.
            if not acciones:
                acciones = ["ver"]
                args = {**args, "ver": "todos"}
            _ejecutar_accion_cli(items, multi, acciones, args)
    else:
        try:
            n = int(args.get("last", "20"))
        except ValueError:
            for imap, _cfg in conexiones.values():
                _cerrar_imap(imap)
            sys.exit(f"-last debe ser un número, no '{args.get('last')}'")
        if n <= 0:
            for imap, _cfg in conexiones.values():
                _cerrar_imap(imap)
            sys.exit("-last debe ser mayor que 0")

        # Aquí NO se ha dicho qué correo(s) buscar (ni -buscar, ni -id, ni
        # ninguno de los filtros) — solo "los últimos N". Si TAMPOCO se
        # pidió ninguna acción, se entra al menú interactivo de siempre
        # (¿qué quieres hacer?), en vez de solo listar y terminar: aquí no
        # hay nada "automatizable" que ejecutar sin más contexto.
        modo_interactivo_final = not acciones

        # -nuevos: resto de validaciones propias (la de modo_busqueda ya se
        # hizo más arriba, antes de entrar en esta rama) -- mismo criterio
        # que el resto de flags de esta sección (sys.exit claro, cerrando
        # conexiones, en vez de seguir con algo a medias o ambiguo). Ver
        # _marcar_nuevos_y_reordenar para el diseño completo.
        if "nuevos" in args:
            if modo_interactivo_final:
                for imap, _cfg in conexiones.values():
                    _cerrar_imap(imap)
                sys.exit(
                    "-nuevos necesita -descargar y/o -ver -- sin ninguna acción se entraría al "
                    "menú interactivo de siempre, y ahí no reordena nada."
                )
            acciones_no_permitidas_nuevos = [a for a in acciones if a not in ("descargar", "ver", "json")]
            if acciones_no_permitidas_nuevos:
                for imap, _cfg in conexiones.values():
                    _cerrar_imap(imap)
                nombres = ", ".join("-" + a.replace("_", "-") for a in acciones_no_permitidas_nuevos)
                sys.exit(f"-nuevos solo admite -descargar y/o -ver -- no se puede combinar con {nombres}.")
            if es_todas:
                for imap, _cfg in conexiones.values():
                    _cerrar_imap(imap)
                sys.exit(
                    "-nuevos no se puede combinar con -acc todas: 'todas' mira todas las carpetas "
                    "de cada cuenta, y -nuevos, de momento, solo funciona con el INBOX -- da las "
                    "cuentas por su nombre (-acc gmx yahoo...) en vez de 'todas'."
                )
            if folders_arg in ("all", "todas"):
                for imap, _cfg in conexiones.values():
                    _cerrar_imap(imap)
                sys.exit(
                    f"-nuevos no se puede combinar con -folders {folders_arg}: de momento solo "
                    "funciona con el INBOX (la carpeta por defecto de cada cuenta) -- no hace "
                    "falta dar -folders para eso, es lo que se mira si no se indica ninguna."
                )

        if es_todas or len(conexiones) > 1:
            # 'todas' SIEMPRE mira todas las carpetas de cada cuenta; con
            # varias cuentas nombradas a la vez (no 'todas'), se respeta
            # -folders igual que con una sola cuenta (por defecto, solo su
            # carpeta principal).
            if es_todas:
                if folders_arg not in ("all", "todas"):
                    print("Aviso: con -acc todas, -folders se ignora (siempre mira todas las carpetas de cada cuenta).")
                folders_para_recientes = "all"
            else:
                folders_para_recientes = folders_arg
            resultado = _recientes_todas_cuentas_n(
                conexiones, n, interactivo=modo_interactivo_final, folders_arg=folders_para_recientes,
                imprimir_listado_final=("nuevos" not in args),
            )
            items, multi = ([], True) if modo_interactivo_final else (resultado if resultado is not None else ([], True))
        else:
            nombre_cuenta_unica = next(iter(conexiones))
            imap, cfg = conexiones[nombre_cuenta_unica]
            carpeta_salida = _carpeta_salida_para_cuenta(nombre_cuenta_unica)
            os.makedirs(carpeta_salida, exist_ok=True)
            contexto_cuenta = {
                "tipo": "cuenta", "imap": imap, "cfg": cfg,
                "nombre_cuenta": nombre_cuenta_unica, "carpeta_salida": carpeta_salida,
            }
            if folders_arg in ("all", "todas"):
                # Por consistencia con el caso de varias cuentas (más arriba):
                # se comprueba la conexión antes de listar_carpetas() también
                # aquí, aunque con una sola cuenta recién conectada el riesgo
                # de que se haya cerrado por inactividad sea mucho menor.
                imap = _imap_vivo_para(conexiones, nombre_cuenta_unica, imap)
                if imap is None:
                    for i2, _c2 in conexiones.values():
                        _cerrar_imap(i2)
                    sys.exit(f"No se pudo mantener la conexión con '{nombre_cuenta_unica}'.")
                contexto_cuenta["imap"] = imap
                carpetas = [c for c, _v, sel in listar_carpetas(imap) if sel]
                if not carpetas:
                    carpetas = [cfg["folder"]]
                resultado = _navegar_todas_las_carpetas_n(imap, carpeta_salida, carpetas, n, contexto=contexto_cuenta, interactivo=modo_interactivo_final)
                items, multi = ([], True) if modo_interactivo_final else (resultado if resultado is not None else ([], True))
            else:
                status, _ = _seleccionar_carpeta(imap, cfg["folder"], readonly=True)
                if status != "OK":
                    for i2, _c2 in conexiones.values():
                        _cerrar_imap(i2)
                    sys.exit(f"No se pudo abrir la carpeta '{cfg['folder']}'.")
                resultado = _navegar_carpeta_n(
                    imap, carpeta_salida, cfg["folder"], n, contexto=contexto_cuenta, interactivo=modo_interactivo_final,
                    imprimir_listado_final=("nuevos" not in args),
                )
                items, multi = ([], False) if modo_interactivo_final else (resultado if resultado is not None else ([], False))

        if "nuevos" in args:
            # Reordenar DESPUÉS de que _recientes_todas_cuentas_n/
            # _navegar_carpeta_n ya terminaron de traer cada mensaje
            # COMPLETO (ver imprimir_listado_final=False, más arriba: ahí
            # se omitió su propio listado a propósito) y volver a
            # numerar/imprimir YA con los nuevos primero -- así lo que se
            # ve en pantalla concuerda con el orden que de verdad usa
            # _ejecutar_accion_cli a continuación (p.ej. -descargar todos,
            # o una selección por número).
            items, _cuantos_nuevos = _marcar_nuevos_y_reordenar(items)
            if "solo_nuevos" in args:
                items = items[:_cuantos_nuevos]
                if not items:
                    print("\nNo hay correos nuevos desde la última vez (en las cuentas pedidas).")
            _reimprimir_tras_reordenar_nuevos(items, solo_nuevos=("solo_nuevos" in args), cuantos_nuevos=_cuantos_nuevos)

        if not modo_interactivo_final:
            _ejecutar_accion_cli(items, multi, acciones, args)

    for imap, _cfg in conexiones.values():
        _cerrar_imap(imap)


# ---------- Programa principal ----------

def comando_directo(args):
    """Ver _comando_directo_interno. Con -json: durante toda la orden, lo que
    el programa imprime normalmente va a stderr, y al final se escribe en
    stdout SOLO el documento JSON (ver el bloque -json más arriba)."""
    if "json" not in args:
        return _comando_directo_interno(args)
    real = sys.stdout
    _json_iniciar()
    try:
        with contextlib.redirect_stdout(sys.stderr):
            _comando_directo_interno(args)
        documento = _JSON_ESTADO["documento"]
        if documento is None:
            documento = {"version": 1, "generado": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"), "total": 0, "mensajes": []}
            print("(-json: no hubo mensajes que describir.)", file=sys.stderr)
        real.write(json.dumps(documento, ensure_ascii=False, indent=2) + "\n")
        real.flush()
    finally:
        _JSON_ESTADO["activo"] = False


comando_directo.__doc__ = _comando_directo_interno.__doc__


def main():
    # Fuerza UTF-8 en la entrada/salida — en algunos entornos (Termux con la
    # configuración regional por defecto) sin esto, las tildes/ñ salen como
    # "?" en pantalla (p.ej. "Certificaci?n" en vez de "Certificación").
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    args_directos = _parsear_argumentos_directos(sys.argv[1:])
    if args_directos is not None:
        comando_directo(args_directos)
        print("Listo.", file=(sys.stderr if "json" in args_directos else sys.stdout))
        return

    if len(sys.argv) > 1 and sys.argv[1].strip().lower() == "todas":
        siguiente = "todas"
    else:
        siguiente = sys.argv[1] if len(sys.argv) > 1 else next(iter(CUENTAS))
        if siguiente not in CUENTAS:
            sys.exit(f"Cuenta '{siguiente}' no está en CUENTAS. Opciones: {', '.join(CUENTAS)}, o 'todas'.")

    while True:
        if siguiente == "todas":
            siguiente = modo_todas_las_cuentas()
        else:
            siguiente = sesion_una_cuenta(siguiente)
        if siguiente == "salir":
            break

    print("Listo.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelado por el usuario.")
        sys.exit(0)

# ============================== NOTAS ==============================
# 0) Contraseñas con comillas u otros caracteres raros:
#    Deja "password": "" en CUENTAS y el script la pide al arrancar (getpass).
#
# 1) Yahoo:
#    Contraseña de aplicación OBLIGATORIA — Yahoo ya no admite la contraseña
#    normal por IMAP. Se genera en Cuenta > Seguridad de la cuenta >
#    "Generar contraseña de aplicación", y SIEMPRE es solo letras/números,
#    sin símbolos. Si la tuya lleva una comilla u otro símbolo, es casi
#    seguro que es tu contraseña normal, no una de aplicación.
#
# 2) GMX:
#    - Por defecto POP3/IMAP está DESACTIVADO: actívalo primero en
#      Configuración > POP3/IMAP en la web de GMX.
#    - Servidor: imap.gmx.net (@gmx.net) o imap.gmx.com (@gmx.com).
#    - Con verificación en dos pasos, hace falta contraseña de aplicación;
#      si no, prueba primero con la normal.
#
# 3) Hotmail / Outlook.com / Live:
#    Microsoft retiró la autenticación básica (usuario+contraseña, incluidas
#    las contraseñas de aplicación) para IMAP en Outlook/Hotmail en abril de
#    2026. Es muy probable que este script, tal cual, ya NO funcione con
#    Hotmail: haría falta OAuth2. Avísame cuando llegues a esa cuenta.
#
# 4) Gmail:
#    Contraseña de aplicación en myaccount.google.com/apppasswords. Comprueba
#    que IMAP esté activado: Gmail > Configuración > Reenvío y POP/IMAP.
#
# 5) Fechas:
#    AAAA-MM-DD-HH-MM (sin segundos), según la cabecera Date del correo. Si
#    un correo no tiene fecha legible, sale "(sin fecha)" y va al final.
#
# 6) "Todas las carpetas" / "todas las cuentas" — cómo se junta y ordena:
#    Se cogen como candidatos los N mensajes más recientes (según el orden
#    del servidor) DE CADA carpeta, se juntan todos y se ordenan por la
#    fecha real del correo — nunca se pregunta ni se separa carpeta por
#    carpeta, sale todo en una sola lista.
#    En la búsqueda por término no hay límite N: se analizan TODOS los
#    mensajes de todas las carpetas elegidas, y el resultado se ordena por
#    fecha al final, no según se van encontrando.
#
# 7) Vista previa:
#    Bajo cada mensaje de la lista salen hasta LINEAS_CONTEXTO líneas del
#    cuerpo (sin líneas en blanco). En resultados de búsqueda: si el término
#    apareció en el CUERPO, son las líneas alrededor de donde aparece; si
#    apareció en to/from/cc/asunto, son las primeras líneas del cuerpo (vista
#    genérica, igual que en "recientes"). Cambia LINEAS_CONTEXTO (3 por
#    defecto) arriba del todo si quieres más contexto (p.ej. 5).
#
# 8) Descargar vs Ver:
#    Tras cualquier lista (recientes o búsqueda), el menú deja elegir entre
#    descargar como .eml (varios a la vez) o ver el texto completo de UNO
#    (sin descargar nada) — se puede alternar entre las dos cuantas veces
#    se quiera antes de continuar.
#
# 9) Rendimiento:
#    Buscar en To/From/CC/BCC/Asunto es rápido (cabeceras). Buscar en el
#    Cuerpo, o mostrar vista previa (que también necesita el cuerpo), es más
#    lento: hace falta descargar el mensaje completo. En búsquedas largas,
#    el aviso empieza a los INTERVALO_AVISO_INICIAL segundos (30 por
#    defecto) — ver nota 11.
#
# 10) Modo "todas las cuentas" y contraseñas:
#    Al conectar a todas, puede pedir una contraseña por cada cuenta con
#    "password" vacío en CUENTAS — normal, una por cuenta, al arrancar.
#
# 11) Aviso "¿seguir buscando?" configurable:
#    Muestra cada cuántos segundos pregunta (con la equivalencia en minutos
#    si son 60+). Responde "2x", "4x", etc. para MULTIPLICAR ese intervalo
#    (más espaciado), o "-2x", "-4x", etc. para DIVIDIRLO (más frecuente) —
#    compuesto en ambos casos: 2x y luego -2x vuelve al valor de antes.
#    Cuando el intervalo llega a 5 minutos (300s) o más, el aviso solo
#    espera LIMITE_ESPERA_RESPUESTA segundos (5 por defecto) una respuesta;
#    si no llega, sigue buscando sola — así una búsqueda muy larga no se
#    queda colgada esperando si no estás delante. Siempre muestra la hora
#    (HH:MM) de cada comprobación. Si con un "-Nx" el intervalo vuelve a
#    bajar de 5 minutos, el aviso siguiente vuelve a ser el normal (espera
#    tu respuesta sin límite de tiempo), sin necesidad de nada más.
#
# 12) ID de cada correo (para volver a encontrarlo más adelante):
#    Cada correo listado muestra un ID corto (12 caracteres) derivado de su
#    Message-ID — no cambia nunca para ese correo, así que hoy, en una
#    semana o en un mes puedes buscarlo con el campo 7 (ID) del menú de
#    búsqueda, o con -id en modo comando. Los .eml descargados también usan
#    este ID en el nombre del fichero (no el número de secuencia IMAP, que
#    sí puede cambiar).
#
# 13) Exportar a CSV:
#    Desde el menú de después de listar/buscar, la opción 3 guarda la lista
#    (id, fecha, carpeta, de, para, asunto — no el cuerpo) en
#    correos_eml/lista_<fecha>-<hora>.csv, para abrir en una hoja de cálculo.
#
# 14) Eliminar (opción 4 del menú):
#    Marca los mensajes elegidos con \Deleted y los expulsa (EXPUNGE) del
#    servidor — ES IRREVERSIBLE, no hay papelera. Pide confirmación
#    mostrando qué se va a borrar y exige escribir ELIMINAR en mayúsculas.
#
# 15) Responder (opción 5 del menú):
#    Compone una respuesta (línea a línea, termina con un "." solo; Enter en
#    blanco al principio cancela) y la envía por SMTP con el De/Para/Asunto/
#    In-Reply-To correctos. Necesita "smtp_server"/"smtp_port" en la cuenta
#    (ya vienen puestos para yahoo/gmx/gmail; hotmail probablemente falle
#    igual que con IMAP — ver nota 3). Pide confirmación mostrando el correo
#    completo y exige escribir ENVIAR en mayúsculas antes de mandarlo.
#
# 16) Reconexión automática:
#    Si una conexión lleva un rato sin usarse, el servidor puede cerrarla
#    sola (típico tras varios minutos de inactividad). El programa lo
#    detecta (con un NOOP) antes de cada acción nueva y reconecta solo,
#    avisando por pantalla — no hace falta reiniciar el programa.
#
# 17) Ver recientes de nuevo / Buscar de nuevo (opciones 6/7 del menú de
#    acciones): permiten lanzar otra búsqueda o ver otra tanda de
#    recientes directamente desde el menú de resultados, sin tener que
#    pasar antes por "Continuar" y volver al menú de la cuenta.
#
# 18) Orden de la búsqueda en varias cuentas/carpetas — por RONDAS:
#    En vez de agotar una cuenta entera antes de pasar a la siguiente, se
#    miran TAMANO_RONDA_BUSQUEDA mensajes (50 por defecto) de cada una por
#    turnos, repitiendo la vuelta hasta agotrarlas todas. Así una cuenta
#    con mucho correo acumulado (años) nunca retrasa llegar a las demás:
#    un mensaje de hoy en cualquier cuenta se analiza dentro de la primera
#    ronda, sea cual sea el tamaño de las otras cuentas.
#
# 19) Limpieza de texto añadida:
#    Se decodifican entidades HTML sueltas (&nbsp;, &amp;, etc.) que a
#    veces quedan sin convertir en partes "texto plano" que en realidad
#    venían de HTML, y el espacio de no separación (nbsp real) se
#    normaliza a un espacio normal — esto también hace que una "línea" que
#    solo tuviera ese tipo de espacio se trate como vacía y se descarte,
#    igual que una línea en blanco normal.
#
# 20) Modo comando directo — ahora totalmente sin preguntas:
#    Con -acc, el programa nunca pregunta nada (ni confirmaciones, ni el
#    aviso de "seguir buscando" — avisa del progreso igual, pero sin
#    pausar) y siempre vuelve al terminal al acabar. El texto normal va a
#    stdout; los avisos de fallo van a stderr (para poder distinguirlos
#    en un script). Además de -last/-folders/-buscar/-campos/-id, admite
#    una acción sobre el resultado: -descargar, -eliminar (¡sin
#    confirmar, cuidado!), -csv, -ver, -ver-txt, -ver-pdf, -responder
#    (con -texto "..." o por stdin). Sin ninguna acción, solo lista y
#    termina. Ver MANUAL_2_comandos_directos.txt para el detalle de cada
#    una con ejemplos.
#
# 21) Ver un correo completo — ahora con más opciones:
#    Desde el menú de acciones, "2. Ver el texto completo" pregunta cómo:
#    en pantalla (como antes, con "ver el siguiente"), guardado en un
#    .txt, o guardado en un .pdf (generado por el propio programa, sin
#    depender de ninguna librería externa — una fuente estándar, con
#    tildes/ñ/¿/¡ correctos, paginado automáticamente si el texto es
#    largo). En modo comando directo son las acciones -ver-txt/-ver-pdf.
#
# 22) Hora de los correos en España peninsular:
#    Las fechas mostradas se convierten a Europe/Madrid (NO Canarias),
#    con cambio de horario verano/invierno automático — antes se
#    mostraba la hora tal cual venía en la cabecera del correo (a veces
#    UTC u otra zona), lo que podía parecer "en el futuro".
#
# 23) Vista previa — presupuesto por caracteres, no por "líneas":
#    Muchos correos traen saltos de línea suaves cada 30-50 caracteres
#    (sin líneas en blanco de por medio). Contar "3 líneas" de eso daba
#    mucho menos texto del disponible. Ahora se acumula texto hasta un
#    presupuesto TOTAL de caracteres (LINEAS_CONTEXTO × LARGO_LINEA_PREVIEW),
#    sin importar cómo venía partido el original.
#
# 24) Línea de título — 1 sola línea, más compacta:
#    Sin corchetes en el id ni en cuenta/carpeta. El campo mostrado
#    (De/Para/Cc) se elige solo: si la búsqueda encontró el término en
#    from/to/cc, se muestra ESE campo; si no, Para en una carpeta de
#    Enviados (Sent/Enviados/Gesendet...) o De en cualquier otra
#    (recibido). El nombre se muestra sin espacios y el correo sin < >
#    (ej. "JuanJose jose@x.com" en vez de "Juan Jose <jose@x.com>").
#
# 25) Modo comando directo — selección por ID y filtros nuevos:
#    -descargar/-eml, -eliminar, -ver, -ver-txt, -ver-pdf, -responder
#    aceptan "id:XXXX" o "id:XXXX,id:YYYY" y funcionan SOLOS, sin -last/
#    -buscar antes (buscan esos ids directamente). Nuevos filtros para
#    reunir mensajes, combinables entre sí: -buscarfecha AAAA-MM-DD,
#    -buscarfechas INICIO FIN (hora/minuto opcional, hora de España
#    peninsular), -buscaradjuntosi/-buscaradjuntono, -buscarcuerpo TEXTO
#    (atajo de -buscar con solo el campo cuerpo). -texto para -responder
#    admite \n \t \r \\ literales.
#
# 26) Búsqueda por ID(s) — se detiene en cuanto los encuentra:
#    Si se buscan ids concretos (ids_multiples), la búsqueda para en
#    cuanto se han encontrado TODOS los pedidos, sin seguir escaneando el
#    resto de carpetas/cuentas — antes seguía hasta agotar el plan
#    entero, lo que en "-acc todas" (todas las carpetas de cada cuenta)
#    podía tardar minutos para nada.
#
# 27) Lista de adjuntos en "ver el texto completo":
#    Si el mensaje tiene archivos adjuntos de verdad (no imágenes de
#    firma incrustadas), se listan por nombre y tamaño antes del cuerpo.
#
# 28) ¿Qué pasa sin ninguna acción, según cómo se reunió? (comando directo):
#    Con -last (implícito o explícito) y sin acción: entra al menú
#    interactivo de siempre (como antes de que todo fuera no-interactivo).
#    Con -buscar/-id/-buscarcuerpo/-buscarfecha(s)/-buscaradjunto* y sin
#    acción: el verbo por defecto es -ver sobre todos los resultados (con
#    su lista de adjuntos si los tiene) — aquí SÍ se especificó qué
#    correo(s), así que no hace falta menú, solo mostrarlos.
#
# 29) Aviso "¿seguir buscando?" también en modo comando directo:
#    Ya no se salta sin más — se pregunta igual que en modo interactivo,
#    pero con espera SIEMPRE acotada (LIMITE_ESPERA_RESPUESTA segundos,
#    nunca indefinida): dentro de ese tiempo se puede parar la búsqueda
#    (responder 'n') o cambiar el intervalo (Nx/-Nx); sin respuesta a
#    tiempo, sigue sola. Así nunca se cuelga un script desatendido, pero
#    sí se puede intervenir si hay alguien delante.
#
# 30) Línea de título — ancho calculado, no fijo:
#    ANCHO_TERMINAL (100 por defecto) es el ancho objetivo de la línea de
#    título; el hueco para dirección/asunto se calcula descontando lo que
#    ya ocupan id/fecha/cuenta-carpeta/campo en ESA línea en concreto —
#    antes los topes eran fijos y generosos, y algunas líneas se pasaban
#    de ancho por unos pocos caracteres. El presupuesto de la vista previa
#    (LARGO_LINEA_PREVIEW) ahora es igual a ANCHO_TERMINAL, así que subir
#    o bajar ANCHO_TERMINAL ajusta ambas cosas a la vez según el ancho
#    real de tu pantalla/terminal.
#
# 31) Búsqueda por ID — ya no pierde tiempo en carpetas de más:
#    La parada temprana (nota 26) funcionaba, pero si INBOX no estaba
#    entre las primeras carpetas del plan, se leían de más cientos de
#    mensajes de OTRAS carpetas antes de llegar a ella — con red móvil
#    real, esto se notaba como varios minutos. Ahora, para la búsqueda
#    directa por ID (sección 4 del Manual 2), _priorizar_inbox() pone las
#    carpetas INBOX primero (lo más probable para un mensaje reciente), y
#    además se usa una ronda pequeña (10, no TAMANO_RONDA_BUSQUEDA) para
#    que, aunque el mensaje esté en otro sitio, no se pierdan más de un
#    puñado de lecturas por carpeta antes de seguir probando.
#
# 32) Acciones con selección opcional — ya se puede combinar -last con la acción:
#    -descargar/-eml, -eliminar, -ver, -ver-txt, -ver-pdf y -responder ya
#    no exigen un valor explícito: si no se da (porque es el último
#    argumento, o el siguiente ya es otro flag), se asume "todos". Esto
#    permite "-last 10 -ver" (o -ver-pdf, -ver-txt, -descargar...): reúne
#    los últimos 10, aplica la acción a todos, y vuelve — sin menú y sin
#    preguntar nada, porque aquí SÍ se ha dicho qué hacer. "-last 10" sin
#    ninguna acción sigue entrando al menú interactivo, como siempre.
#
# 33) Carpetas con espacios (Gmail) — arreglado un fallo real:
#    imaplib NO entrecomilla los nombres de carpeta por sí solo. Carpetas
#    con espacios — muy habituales en Gmail, ej. "[Gmail]/Todos los
#    mensajes" o "[Gmail]/All Mail" — se mandaban sin comillas al
#    servidor, que las rechazaba con "EXAMINE command error: BAD Could
#    not parse command" (reportado con 5 cuentas configuradas, 2 de
#    ellas Gmail). Ahora TODAS las llamadas a select() pasan por
#    _seleccionar_carpeta(), que entrecomilla y escapa el nombre
#    correctamente — para cualquier carpeta, no solo las de Gmail.
#
# 34) -ver-lista: solo la lista, sin menú ni texto completo:
#    Nueva acción para modo comando directo: muestra el título + vista
#    previa de cada mensaje (igual que al reunirlos) y vuelve al
#    terminal — sin entrar al menú interactivo (a diferencia de -last
#    sin ninguna acción) y sin mostrar el texto completo de cada uno (a
#    diferencia de -ver). No lleva selección (como -csv): siempre es
#    toda la lista reunida.
#
# 35) -acc admite varias cuentas nombradas a la vez:
#    "-acc gmx yahoo" (dos o más nombres seguidos de -acc, separados por
#    espacio) conecta y combina resultados de esas cuentas concretas —
#    distinto de "-acc todas" en que SÍ respeta -folders (por defecto,
#    solo la carpeta principal de cada una; con -folders all, todas las
#    carpetas de cada una). Funciona con -last, -buscar, -id, y el resto
#    de formas de reunir mensajes.
#
# 36) Aviso "¿seguir buscando?" — arreglado un fallo real, grave:
#    El aviso solo se comprobaba tras un mensaje que SÍ coincidía con la
#    búsqueda — cualquier mensaje que NO coincidía (la inmensa mayoría en
#    una búsqueda real, ej. un término poco común) saltaba esa
#    comprobación por completo mediante un 'continue'. Con pocas o
#    ninguna coincidencia, el aviso podía tardar muchísimo más de lo
#    configurado en aparecer (reportado: configurado a 30s, apareció con
#    6 minutos de diferencia). Ahora el bucle interno no usa 'continue':
#    se sigue un mensaje coincida o no, y la comprobación del aviso se
#    hace SIEMPRE tras cada uno, así que el intervalo configurado se
#    respeta de verdad, haya o no coincidencias.
#
# 37) Búsqueda por -id (flag separado) — ya se detiene al encontrarlo:
#    La parada temprana (nota 26/31) solo se aplicaba a la forma
#    standalone "-ver id:X", no al flag -id por separado (ej. "-acc gmx
#    yahoo -id XXXX -ver-txt") — con este último, la búsqueda seguía
#    escaneando el resto de mensajes de cada cuenta aunque ya hubiera
#    encontrado el correo, lo que en un INBOX con años de correo
#    acumulado se notaba como varios minutos. Ahora -id (el flag) también
#    para en cuanto lo encuentra, y prioriza INBOX + ronda pequeña igual
#    que la forma standalone.
#
# 38) "Últimos N" de varias/todas las cuentas — reconexión durante todo el recorrido:
#    _recientes_todas_cuentas_n() (usada por -last con "-acc todas" o
#    varias cuentas nombradas) solo comprobaba la conexión UNA VEZ, al
#    principio de toda la función — con 5 cuentas y "-acc todas" mirando
#    todas las carpetas de cada una, el recorrido entero puede tardar lo
#    bastante como para que una conexión se cierre por inactividad
#    mucho después de esa única comprobación, dando "IMAP4.abort: ...
#    Server logging out" sin capturar (reportado con -last 20 -ver-lista).
#    Ahora se comprueba y reconecta sola antes de CADA select() a lo
#    largo de toda la función (recogida de candidatos Y recuperación del
#    contenido completo de los elegidos), igual que ya hacía la búsqueda.
#
# 39) Búsqueda por ID — ya mira todas las carpetas por defecto:
#    Sin -folders explícito, buscar por ID (standalone "id:X" o el flag
#    -id) ahora mira TODAS las carpetas, no solo INBOX — antes, un correo
#    que no estuviera en INBOX (Enviados, Archivo...) nunca se
#    encontraba, y con muchos ids a la vez esto se notaba como una
#    búsqueda larguísima sin resultado (reportado: 20-25 ids, 30 minutos,
#    ninguno encontrado). Si SÍ das -folders explícito, se respeta tal
#    cual. Además, ahora avisa por stderr si no se encontraron todos los
#    ids pedidos.
#
# 40) Nuevo esquema de nombre de fichero (.eml/.txt/.pdf y adjuntos):
#    Ya no se usa el ID en el nombre. Los correos se guardan como
#    AAAA-MM-DD-HH-MM-SS_CARPETA_AsuntoCamelCase.ext (fecha del correo en
#    hora de España peninsular); los adjuntos, sueltos con -adjuntos
#    (nota 41) o dentro de otra acción, como
#    AAAA-MM-DD-HH-MM-SS_adj_NombreAdjuntoCamelCase.ext, con la fecha del
#    CORREO (no del adjunto), para que queden agrupados alfabéticamente.
#    "CamelCase" (_camel_case()): primera palabra en minúsculas, las
#    siguientes con la inicial en mayúscula, sin espacios. Protección
#    contra colisión de nombres (_ruta_sin_colision()): si el nombre ya
#    existe, añade _2, _3... antes de la extensión.
#
# 41) -adjuntos: descargar todos los adjuntos de los mensajes elegidos:
#    Guarda cada adjunto de verdad (no imágenes de firma incrustadas) de
#    los mensajes seleccionados, con el nombre de la nota 40. Si un
#    mensaje no tiene adjuntos, lo indica y sigue con el siguiente.
#
# 42) Varias acciones a la vez (ej. -descargar y -ver-txt juntas):
#    Ya se pueden combinar acciones en la misma orden — cada una con su
#    propia selección si hace falta (ej. "-descargar 1,3 -ver-txt 5").
#    Se ejecutan en un orden fijo y sensato
#    (_ORDEN_ACCIONES_MULTIPLES): las que guardan/muestran primero,
#    -eliminar SIEMPRE la última — así, si combinas -descargar (o
#    -ver-txt, -adjuntos...) con -eliminar sobre el mismo mensaje, te da
#    tiempo a guardarlo antes de que se borre.
#
# 43) Selección por ID sin necesidad del prefijo "id:":
#    Ya no hace falta escribir "id:X,id:Y" — basta con "X,Y" (los ids
#    sueltos, separados por comas). Se detecta solo (_es_selector_por_id):
#    cualquier trozo que no tenga forma de número de posición corto (o
#    rango N-M) se trata como id. El prefijo "id:" se sigue aceptando por
#    si se prefiere ser explícito. Funciona tanto en la forma standalone
#    (sin -last/-buscar antes) como dentro de una lista ya reunida (ej.
#    "-last 10 -descargar XXXXXXXXXXXX" selecciona por ese id concreto de
#    la lista, no por posición).
#
# 44) El ID completo, ahora también en el nombre de fichero:
#    Entre la carpeta y el asunto (para .eml/.txt/.pdf) y entre "adj" y
#    el nombre del adjunto (para adjuntos) va el ID completo de 12
#    caracteres — así, para volver a encontrar/seleccionar ese correo más
#    tarde, basta con copiar ese trozo del nombre del fichero (nota 43).
#
# 45) Búsqueda por ID — causa real de "no encuentra un correo que sí está":
#    Reportado: "-acc gmx yahoo -descargar id:X -ver-txt id:X" no
#    encontraba un correo de la carpeta INBOX de gmx, entre los 5 más
#    recientes. Causa: la búsqueda, para no tener que descargar cada
#    mensaje entero, pide al servidor solo un grupo de cabeceras (TO,
#    FROM, CC, BCC, SUBJECT, DATE) — y ese grupo NO incluía MESSAGE-ID.
#    id_amigable() (el id corto de 12 caracteres que se muestra y con el
#    que se busca) se calcula a partir de esa cabecera real; sin ella,
#    caía en un cálculo de respaldo (a partir de remitente+fecha+asunto)
#    pensado solo para el caso raro de un correo sin Message-ID — así que
#    CUALQUIER búsqueda por id (standalone "id:X", el flag -id, o un
#    "-descargar id:X" combinado con otra acción) comparaba contra un id
#    que nunca podía coincidir con el que el usuario había visto y
#    copiado, y acababa agotando el buzón entero sin encontrar nada,
#    por más que el correo estuviera justo ahí. Arreglado añadiendo
#    MESSAGE-ID al grupo de cabeceras pedido (las tres apariciones de
#    este patrón en el código) — verificado con un test que simula de
#    verdad la diferencia entre pedir el mensaje completo y pedir solo
#    cabeceras (el test anterior, con un simulador que siempre devolvía
#    el mensaje completo pasara lo que pasara, no habría pillado esto).
#
# 46) "¿Seguir buscando?" — respuestas que parecían ignorarse del todo:
#    Reportado: ni 'n' ni '2x' ni nada se tenían en cuenta, repetidas
#    veces en la misma búsqueda — y, pista clave del propio usuario:
#    lanzando el programa DIRECTO con "python3 correo.py" sí
#    funcionaba, pero a través de un script .sh (lanzado a su vez desde
#    la línea de comandos) no. Causa: cuando la entrada estándar del
#    proceso no es un terminal interactivo de verdad (p.ej. el .sh
#    redirige la salida a un log y de paso deja la pregunta sin verse en
#    pantalla, la redirige/corta con "< /dev/null", o lo lanza en según
#    plano), leer de ella no espera nada: devuelve "" (EOF) al instante,
#    indistinguible — tal y como estaba el código — de "el usuario pulsó
#    Enter sin escribir nada", que no coincide con 'n'/'no' ni con el
#    patrón Nx y por tanto simplemente sigue buscando sola, siempre, dé
#    igual lo que el usuario escriba en su terminal real (que no es la
#    entrada de este proceso). Ahora _input_con_limite() distingue de
#    verdad un EOF real (readline() devuelve "" sin más) de una espera
#    normal sin respuesta (devuelve None tras el tiempo límite) o de una
#    respuesta real así sea en blanco (readline() devuelve "\n"); ante un
#    EOF, se avisa UNA vez con un mensaje que explica la causa probable
#    (entrada no interactiva, redirigida o en segundo plano) y sugiere
#    comprobarlo con "python3 -c \"import sys; print(sys.stdin.isatty())\"",
#    y de ahí en adelante la búsqueda sigue sola sin volver a intentar
#    leer (evita ruido repetido; no había ningún riesgo real de
#    quedarse colgada, select() sobre un EOF también se resuelve al
#    instante, pero sí confundía al usuario). Las respuestas reales
#    (n, Nx, -Nx, texto no reconocido, Enter en blanco de verdad, o un
#    timeout genuino sin EOF) se comprobaron una a una y siguen
#    funcionando exactamente igual que antes.
#
# 47) "-ver-pdf id:X" (o cualquier búsqueda por ID) sin encontrar nada,
#    o tardando muchísimo, en una cuenta con muchas carpetas y un INBOX
#    grande:
#    Reportado con una cuenta real de 31 carpetas y 20689 mensajes en
#    total (INBOX con 2951, y una carpeta "alerts" de 10982) — un correo
#    de hace 1-2 semanas, presente en INBOX, no aparecía ni tras 10
#    minutos (420s, 2096 analizados, 0 encontrados, sin final a la
#    vista). Al principio parecía cosa de -ver-pdf en particular (según
#    lo reportado, -ver-txt/-descargar con el mismo id sí lo habían
#    encontrado antes), pero probado a fondo (mismo id, las tres
#    acciones a la vez en una sola orden) las tres se comportan
#    exactamente igual — no hay ninguna diferencia entre ellas en la
#    fase de búsqueda, así que lo más probable es que esas pruebas
#    anteriores encontraran el mensaje por pura suerte de turno (la
#    ronda es una carrera entre carpetas; en qué vuelta cae cada una
#    puede variar un poco de una ejecución a otra), no porque buscaran
#    de otra forma. Causa real: la búsqueda SÍ mira primero las carpetas
#    INBOX (nota 44), pero la ronda ("por rondas", para que una carpeta
#    enorme no acapare la búsqueda y deje sin analizar las pequeñas)
#    repartía el esfuerzo A PARTES IGUALES entre TODAS las carpetas
#    activas — con 31 carpetas, eso deja a INBOX con solo 1/31 del
#    esfuerzo por vuelta, igual que a "alerts" (10982 mensajes) o a
#    cualquier carpeta de 4. Con un INBOX de casi 3000 correos, un
#    mensaje que no esté entre los pocos más recientes podía tardar
#    muchísimo, o no terminar en una espera razonable. Arreglado con un
#    tamaño de ronda MAYOR específico para las carpetas INBOX
#    (tamano_ronda_inbox, parámetro nuevo de _ejecutar_busqueda,
#    separado del tamano_ronda del resto de carpetas, que sigue igual
#    que antes) — así INBOX avanza mucho más deprisa por vuelta sin
#    dejar de repartir el resto del esfuerzo entre las demás carpetas, y
#    sin que dos cuentas con INBOX grande se estorben entre sí (cada una
#    sigue recibiendo su ronda grande por turno, no una agotando a la
#    otra). Verificado con un test que reproduce la escala exacta del
#    caso real (mismos nombres y tamaños de carpeta, copiados de la
#    salida que pegó el usuario): un mensaje a 2000 de profundidad en un
#    INBOX de 2951 se encuentra tras analizar una fracción del buzón, en
#    vez de tener que agotarlo o rondar indefinidamente; también se
#    comprobó el caso límite (el mensaje más antiguo de todo INBOX) y
#    que un mensaje en una carpeta pequeña (4 mensajes) sigue
#    encontrándose al instante, sin regresión. No tiene relación con
#    -ver-pdf en particular: cualquier búsqueda por id (standalone, el
#    flag -id, o combinada con otras acciones) en una cuenta con muchas
#    carpetas se beneficia igual.
#
# 48) Nuevo: -cabeceras y -cabeceras-todas (junto con -ver/-ver-txt/-ver-pdf):
#    Hasta ahora, el texto completo (pantalla/.txt/.pdf) solo mostraba
#    ID/De/Para/Asunto/Fecha — nunca el Message-ID real (el "ID:" que se
#    muestra es un id corto de 12 caracteres propio de este programa,
#    derivado de él por hash — id_amigable() —, NO el Message-ID
#    original) ni ninguna otra cabecera. Pedido: dos opciones para verlas,
#    pensadas sobre todo para cuando hace falta que un tercero no dude de
#    la autenticidad de un correo. -cabeceras añade Message-ID (el
#    original completo), Cc, Reply-To, Return-Path y
#    Authentication-Results (o Received-SPF si el servidor usa esa forma
#    más vieja — el veredicto de SPF/DKIM/DMARC que dio el propio
#    servidor receptor), cada una solo si el mensaje realmente la trae.
#    -cabeceras-todas vuelca en cambio TODAS las cabeceras tal como
#    llegaron, en su orden original (incluye las anteriores y, además,
#    cada "Received" del recorrido — uno por servidor intermedio — y
#    cualquier otra cabecera propia del proveedor). Si se dan los dos a
#    la vez, gana -cabeceras-todas. Aviso incluido también en el propio
#    texto generado: un .txt/.pdf con cabeceras no es, por sí solo, algo
#    verificable por un tercero (es texto plano, cualquiera podría
#    escribirlo) — lo único con valor probatorio de verdad (la firma
#    DKIM, comprobable contra el DNS del dominio remitente) es el .eml
#    original, así que para ese caso conviene acompañar esto del .eml
#    (-descargar), no depender solo del .txt/.pdf. Implementado en
#    _texto_completo_formateado() (función única reutilizada por
#    pantalla/.txt/.pdf, así que los tres se benefician igual) mediante
#    un nuevo parámetro modo_cabeceras; verificado con tests que
#    comprueban tanto que el comportamiento por defecto no cambia (sin
#    ninguno de los dos flags) como que cada modo añade exactamente lo
#    que debe (ni de más ni de menos) en el .txt y en el .pdf.
#
# 49) "while read" en Termux que se cortaba a la 2ª vuelta, sin error:
#    Reportado: "ls *.txt | while read -r line; do txt=...; python3
#    correo.py -acc gmx -folders inbox -ver-pdf ${txt}; done" ejecutaba
#    la 1ª vuelta bien pero se salía del while en la 2ª, sin ningún
#    mensaje — y el propio bucle tenía sitio de sobra para más vueltas
#    (varios ficheros .txt disponibles). Causa: el modo comando directo
#    (interactivo=False) nunca pregunta nada al aplicar la acción, pero
#    la fase de BÚSQUEDA sí puede mostrar el aviso periódico "¿Seguir
#    buscando?" si tarda lo suficiente — y ese aviso, incluso en modo no
#    interactivo, intentaba leer una respuesta con límite de tiempo
#    (_input_con_limite → select() + readline()). Con una tubería de
#    verdad de por medio (como la de "ls *.txt |" en el bucle
#    reportado), select() ve datos disponibles — pero esos datos son la
#    SIGUIENTE línea que el propio 'while read' del script aún no había
#    consumido, no una respuesta a este aviso. Leerla de todas formas se
#    la robaba a ese bucle: 'read' se quedaba con una línea de menos de
#    las que en realidad había, y el bucle terminaba antes de tiempo sin
#    ningún error (desde el punto de vista de bash, 'read' simplemente
#    se quedó sin más líneas — indistinguible de haber llegado al final
#    de verdad). Esto es distinto de la nota 46 (que trataba un EOF real
#    — entrada cerrada o /dev/null): aquí la entrada SÍ tenía datos de
#    verdad, solo que no eran para este programa. Arreglado añadiendo
#    una comprobación de sys.stdin.isatty() AL PRINCIPIO de
#    _preguntar_seguir_buscando, antes de intentar nada: si la entrada
#    no es una terminal interactiva de verdad (tubería, fichero
#    redirigido, /dev/null), nunca se lee de ella, sea cual sea
#    'interactivo' — la búsqueda sigue sola sin más. El aviso ante un
#    EOF real (nota 46) se mantiene igual, como red de seguridad para
#    cualquier caso raro en que isatty() diera True pero la lectura
#    fallara de todas formas. Verificado con tests que comprueban, con
#    una tubería de verdad del sistema operativo (no solo un simulador):
#    que con isatty()=False nunca se lee nada (ni la primera vez ni en
#    avisos siguientes, con interactivo=True o False), y que con
#    isatty()=True de verdad no cambia nada — sigue esperando con
#    límite, respeta una respuesta a tiempo, y sigue sola si no llega
#    ninguna, exactamente como antes de este arreglo.
#
# 50) Nuevo: -escribir (redactar y enviar un correo NUEVO, desde cero):
#    Hasta ahora, enviar un correo solo era posible como RESPUESTA a un
#    mensaje ya existente (-responder, que necesita seleccionar uno de
#    una lista o por id). Pedido: poder redactar y enviar uno nuevo, sin
#    partir de nada. -escribir es una acción aparte (no se combina con
#    -last/-buscar/etc. ni con ninguna otra acción — sys.exit claro si se
#    intenta) que necesita -acc con UNA sola cuenta (no 'todas' ni varias
#    — es la cuenta desde la que se envía) y estos datos:
#      -p "a@x.com,b@y.com"   destinatarios (Para), separados por comas
#                              (_lista_direcciones) — se valida que cada
#                              uno tenga '@' (_validar_direcciones).
#      -s "Asunto"             opcional; si falta, avisa por stderr (no
#                              bloquea) y se envía con el asunto en blanco.
#      -cuerpo <fichero.txt>   el cuerpo, leído de ESE fichero — lo
#                              pedido explícitamente ("que el texto sea
#                              en formato txt, a partir de un archivo
#                              txt"). Si no se da, admite -texto "..."
#                              (como ya hacía -responder, con el mismo
#                              _desescapar_texto_cli) o, si stdin no es
#                              un terminal interactivo, la entrada
#                              estándar — mismo orden de prioridad que
#                              -responder, con -cuerpo por delante.
#      -cc "c@x.com,d@y.com"  copia (CC), opcional, separados por comas.
#      -c                     añade a CC, sin más, la dirección de
#                              eGarante (mailsigned@egarante.com,
#                              _DIRECCION_EGARANTE) — pedido explícito
#                              ("que exista un procedimiento específico y
#                              fácil"), para obtener una copia certificada
#                              del envío (prueba habitual en España ante
#                              reclamaciones formales). Se combina con
#                              -cc sin duplicar la dirección si ya
#                              estuviera.
#      -adjuntar <ruta1,ruta2> ficheros a adjuntar por su ruta COMPLETA —
#                              en cualquier carpeta, no solo la actual
#                              (pedido explícito) —, separados por comas;
#                              se comprueba que cada uno exista ANTES de
#                              intentar enviar nada. El tipo MIME se
#                              adivina por la extensión (mimetypes.
#                              guess_type); si no se reconoce, se manda
#                              como binario genérico.
#    Implementado ampliando _enviar_correo() con dos parámetros NUEVOS y
#    opcionales (cc=None, adjuntos=None, añadidos al final de la firma)
#    en vez de duplicar la función — así -responder (que no los usa)
#    sigue exactamente igual que antes; verificado con un test de
#    regresión dedicado que confirma que -responder compone y envía
#    igual que antes de este cambio (mismo To/Subject/In-Reply-To/
#    cuerpo, sin CC ni adjuntos). -escribir se resuelve al principio de
#    comando_directo(), ANTES de conectar por IMAP (no hace falta —
#    enviar es solo SMTP) — a diferencia de -responder, que sí necesita
#    IMAP para poder seleccionar el mensaje original. Verificado también
#    con tests propios: envío básico; varios destinatarios en -p con
#    comas; -cc y -c combinados sin duplicar eGarante; -c solo; adjuntos
#    con ruta en una carpeta distinta de la actual (comprobando nombre Y
#    contenido de cada adjunto en el mensaje MIME resultante); rechazo
#    claro (sys.exit, sin enviar nada) de: varias cuentas o 'todas',
#    combinarlo con otra acción o con una forma de reunir mensajes, sin
#    -p, una dirección sin '@', sin cuerpo disponible, y un adjunto que
#    no existe.
#
# 51) Nuevo: -aqui (guardar en el directorio actual); adjuntos con el
#    nombre del correo; y varias acciones sobre los mismos ids sin
#    repetirlos (esto último, YA funcionaba — solo hacía falta un
#    ejemplo claro):
#    a) -aqui: hasta ahora, -descargar/-eml/-ver-txt/-ver-pdf/-adjuntos/
#       -csv guardaban SIEMPRE en SALIDA/<cuenta>. Pedido: poder guardar
#       "en la misma carpeta donde está llamándose al programa" — tanto
#       los correos como los adjuntos. Con -aqui, todo el fichero que esa
#       orden guarde va al directorio ACTUAL (os.getcwd()) en vez de
#       SALIDA/<cuenta> — sin subcarpeta por cuenta ni siquiera con varias
#       cuentas a la vez (todas comparten esa misma carpeta actual).
#       Implementado con _ESTADO_SALIDA (mismo patrón dict-de-1-elemento
#       que _ESTADO_ENTRADA, para no necesitar 'global') y dos funciones,
#       _carpeta_base_salida() (para el CSV, que no es por-cuenta) y
#       _carpeta_salida_para_cuenta(nombre) (para todo lo demás) — se
#       sustituyeron TODOS los sitios que antes construían
#       os.path.join(SALIDA, cuenta) a mano (5 en total, incluidos los 2
#       más usados: dentro de _ejecutar_busqueda y de
#       _recientes_todas_cuentas_n, los que de verdad se ejercitan en
#       -ver-pdf/-descargar/etc. por id) para que pasen por esa función en
#       vez de construir la ruta ellos mismos — así -aqui se aplica
#       siempre, venga la búsqueda de donde venga. -aqui NO se puede
#       combinar con -responder (sys.exit claro): -responder no guarda
#       ningún fichero (no le aporta nada) y además identifica la cuenta a
#       partir del NOMBRE de la carpeta de salida
#       (_cuenta_de_carpeta_salida, que asume SALIDA/<cuenta>) — algo que
#       -aqui rompería en silencio si se permitiera.
#    b) Nuevo nombre de los adjuntos guardados: antes llevaban su PROPIA
#       fecha/hora y el ID por delante (AAAA-MM-DD-HH-MM-SS_adj_
#       NombreAdjuntoCamelCase.ext, ver nota 40). Pedido explícito: que el
#       adjunto lleve el mismo nombre que el .eml/.txt/.pdf de SU correo
#       (fecha del correo, carpeta, ID y asunto) y, justo después, "_adj_"
#       más el nombre del adjunto — así, mirando el nombre de un adjunto
#       suelto en una carpeta, se ve de un vistazo a qué correo pertenece,
#       y ordena alfabéticamente junto a él. _nombre_fichero_adjunto()
#       reescrita para anteponer _nombre_base_mensaje(msg, carpeta_imap)
#       (la misma función que arma el nombre del .eml/.txt/.pdf) en vez de
#       una fecha propia; _guardar_adjuntos() amplía su firma con
#       carpeta_imap para poder pasarlo.
#    c) Combinar varias acciones sobre los MISMOS ids sin repetirlos, y
#       "-descargar" bajando también los adjuntos a la vez: pedido como
#       si hiciera falta una sintaxis nueva (proponía algo como
#       "-ver-pdf-cabecerastodas" o "-ver-pdf-cabeceras,descargar,ver-txt
#       ids"), con la duda explícita de si esto ya se podía hacer sin
#       repetir la lista de ids para cada acción. Comprobado con tests
#       dedicados (test_ronda_d_aqui_multiaccion_adjuntos.py) que esto YA
#       funcionaba, sin cambiar nada: basta con dar la selección (ids o
#       posiciones) a UNA sola acción y añadir las demás como flags
#       sueltos, SIN valor — _FLAGS_ACCION_VALOR_OPCIONAL ya asume "todos"
#       cuando a una acción no le sigue un valor explícito, y
#       standalone_por_id ya sobrescribe (args_todos) la selección de
#       TODAS las acciones pedidas a "todos" antes de aplicarlas — es
#       decir, se aplican todas al mismo resultado encontrado por id, sin
#       ninguna selección repetida. Dos ejemplos ahora documentados en el
#       docstring de comando_directo() y en el Manual 2:
#         -ver-pdf id:X,id:Y -cabeceras -descargar -ver-txt   (3 acciones
#           + cabeceras, sobre los mismos 2 ids, una sola vez)
#         -descargar id:X -adjuntos    (el .eml Y sus adjuntos a la vez —
#           esto es, en la práctica, la "opción para que -descargar baje
#           también los adjuntos automáticamente" que se pedía)
#       No hizo falta ningún flag nuevo para esto — menos superficie que
#       pueda romperse, y compatible con todo lo demás sin más cambios.
#    Verificado con test_ronda_d_aqui_multiaccion_adjuntos.py (5 tests:
#    -aqui guardando correo+adjunto en el directorio actual con el nuevo
#    nombre; SIN -aqui, sigue yendo a SALIDA/<cuenta> como siempre —sin
#    regresión—; -aqui + -responder rechazado; -descargar+-adjuntos sin
#    repetir id; y -ver-pdf+-cabeceras+-descargar+-ver-txt sin repetir
#    id) y la batería completa existente (test_bug_ronda_inbox_grande,
#    test_bug_verpdf_1_cuenta, test_bug_while_pipe_roba_lineas,
#    test_escribir_correo_nuevo, test_opcion_cabeceras) — todos pasan.
#
# 52) Nuevo: -ds (búsqueda desatendida), -intervalo y -max:
#    Pedido: para dejar una búsqueda larga corriendo sin nadie delante,
#    que NO pregunte absolutamente nada (ni siquiera la breve espera de
#    "¿aumentar el intervalo?" que ya existía en modo comando directo —
#    ver nota 46/_preguntar_seguir_buscando), que en vez de eso muestre
#    solo el progreso en función de los correos vistos, en una única
#    línea que se autosobreescriba (no una debajo de otra), y que se
#    pueda configurar el intervalo entre avisos (en segundos o minutos) y
#    un tiempo máximo total para la búsqueda.
#    a) -ds: activa el modo desatendido. A diferencia de interactivo=False
#       (que ya existía y SÍ sigue avisando y esperando muy brevemente una
#       respuesta, por si hay alguien delante — LIMITE_ESPERA_RESPUESTA),
#       con -ds NUNCA se llama a _preguntar_seguir_buscando: cero lecturas
#       de stdin, cero preguntas. En su lugar, cada vez que toca avisar se
#       imprime "\r  Progreso: I/TOTAL correos vistos, N encontrados...
#       \033[K" (retorno de carro + borrar resto de línea, SIN salto de
#       línea) — el terminal ve una sola línea que se va actualizando en
#       el mismo sitio, no una nueva por cada aviso. También se omiten,
#       solo con -ds, las líneas puramente decorativas (el "Analizando N
#       mensajes de M carpeta(s)..." de inicio y la cabecera "--
#       Analizando cuenta/carpeta --" de cada fuente nueva) — los avisos
#       de fallo de verdad (conexión perdida, carpeta que no se pudo
#       abrir, mensaje que no se pudo leer) SÍ se siguen mostrando, cada
#       uno en su propia línea (se baja de línea primero si la de
#       progreso estaba a medio redibujar, para no mezclarlas — variable
#       local estado_linea/función _avisar dentro de _ejecutar_busqueda).
#    b) -intervalo <35s|4m>: sustituye a INTERVALO_AVISO_INICIAL (config,
#       30s) como intervalo de PARTIDA para esta búsqueda — cada cuánto
#       se refresca el progreso (-ds) o se pregunta "¿seguir buscando?"
#       (sin -ds). Nueva función _segundos_desde_texto(valor, nombre_flag)
#       interpreta el sufijo 's' (segundos) o 'm' (minutos) — sin sufijo,
#       se asume segundos —, con sys.exit claro si el formato no es válido
#       o el número no es mayor que 0.
#    c) -max <15m>: tiempo MÁXIMO total para la búsqueda (misma función
#       _segundos_desde_texto) — al alcanzarlo, se detiene con lo
#       encontrado hasta ese momento, avisando cuántos se analizaron de
#       cuántos había ("se detiene tras analizar I/TOTAL mensajes"). Se
#       comprueba en cada mensaje (no solo en cada aviso programado), así
#       que corta lo antes posible tras superarse, no en el siguiente
#       "tick" de -intervalo. Por defecto, sin límite (como hasta ahora).
#    Los tres son ortogonales entre sí y solo importan para las formas que
#    de verdad pueden tardar y avisar periódicamente: la búsqueda "por ID
#    sin reunir antes" (sección 4) y -buscar/-id/-buscarfecha(s)/
#    -buscarcuerpo/-buscaradjunto* (sección "modo_busqueda" de
#    comando_directo) — con -last a solas no hacen nada, porque esa forma
#    usa otras funciones (_recientes_todas_cuentas_n y similares) que
#    nunca han tenido este mecanismo de aviso/progreso. Implementado
#    ampliando _ejecutar_busqueda() con tres parámetros nuevos y opcionales
#    (modo_desatentido=False, intervalo_inicial=None, tiempo_maximo=None,
#    añadidos al final de la firma) en vez de duplicar la función — así
#    todo lo demás que la llama (el menú interactivo normal, sesión de una
#    cuenta, todas las cuentas) sigue exactamente igual que antes, sin
#    pasar estos parámetros nuevos.
#    Verificado con test_ds_intervalo_max.py (7 tests, con un reloj falso
#    — monkeypatch de time.monotonic, el único sitio del fichero que lo
#    usa — para probar los umbrales de tiempo sin depender de la
#    velocidad real de la máquina): parseo de _segundos_desde_texto
#    ('35s'/'4m'/sin sufijo/mayúsculas/valores inválidos); -ds nunca llama
#    a _preguntar_seguir_buscando (se envenena la función para que lance
#    una excepción si se llegara a llamar) y su salida lleva '\r'/'\033[K'
#    sin el aviso clásico ni las líneas decorativas; SIN -ds, todo sigue
#    exactamente igual que antes (regresión); -intervalo adelanta cuándo
#    llega el primer aviso/progreso; -max corta la búsqueda exactamente
#    cuando toca, con lo encontrado hasta ese momento; -max acepta
#    minutos y rechaza un formato inválido; y -ds también funciona en la
#    forma "por ID sin reunir antes". Batería completa existente
#    (test_bug_ronda_inbox_grande, test_bug_verpdf_1_cuenta,
#    test_bug_while_pipe_roba_lineas, test_escribir_correo_nuevo,
#    test_opcion_cabeceras, test_ronda_d_aqui_multiaccion_adjuntos) — sin
#    regresiones.
#
# 53) "-acc todas -last 200" reventaba con IMAP4.abort al listar carpetas:
#    Reportado 2026-09-28 con el traceback real: "imaplib.IMAP4.abort:
#    command: LIST => socket error: EOF" dentro de listar_carpetas() (su
#    imap.list() sin proteger), llamada desde _recientes_todas_cuentas_n()
#    -- justo la función de "-last" con "-acc todas" o varias cuentas a
#    la vez. Causa: la nota 38 ya había añadido reconexión automática
#    antes de CADA select() en esa función, pero se dejó fuera
#    precisamente listar_carpetas(), que se llama ANTES del primer
#    select() de cada cuenta -- con varias cuentas (sobre todo "todas",
#    que mira todas las carpetas de cada una), procesar las primeras
#    puede tardar lo bastante como para que el servidor cierre por
#    inactividad la conexión de una posterior, y esa llamada revienta sin
#    capturar el fallo, tirando abajo el programa entero y perdiendo
#    también los resultados ya recogidos de las cuentas anteriores. El
#    propio usuario diagnosticó correctamente la causa: "reordena el
#    codigo para que no suceda" -- la comprobación de conexión viva
#    (_imap_vivo_para) simplemente estaba colocada DESPUÉS del punto
#    donde hacía falta, no antes.
#    Revisando el mismo patrón se encontraron dos sitios más con la
#    misma vulnerabilidad estructural (llamar a listar_carpetas() en un
#    bucle multi-cuenta sin comprobar antes si la conexión sigue viva),
#    sin reportar todavía pero expuestos al mismo fallo en cuanto el
#    tiempo entre cuentas sea suficiente:
#      - buscar_todas_las_cuentas() (menú interactivo "buscar en TODAS
#        las cuentas"): no tenía NINGUNA comprobación de conexión, ni
#        siquiera la que sí tenía _recientes_todas_cuentas_n() (aunque
#        mal colocada).
#      - _plan_segun_folders() (usada por -buscar/-id/-buscarfecha(s)/
#        -buscarcuerpo/-buscaradjunto* y por la búsqueda "por ID sin
#        reunir antes" -- las dos llamadas a _ejecutar_busqueda de la
#        nota 52): tampoco tenía ninguna comprobación.
#    Las tres se corrigieron igual: _imap_vivo_para(conexiones,
#    nombre_cuenta, imap) se llama ahora ANTES de listar_carpetas() (y
#    se mantiene también, como ya hacía la 38, antes de cada
#    select()/search() dentro del bucle de carpetas de cada cuenta) --
#    si no se puede reconectar, se avisa por stderr y se pasa a la
#    siguiente cuenta en vez de reventar. Por consistencia se añadió la
#    misma comprobación, más barata de justificar pero igual de barata
#    de añadir, en la rama de comando_directo para UNA sola cuenta con
#    "-last -folders all" (el riesgo ahí es mucho menor -- la conexión
#    se acaba de usar segundos antes -- pero el patrón queda igual en
#    todos los sitios). Deliberadamente NO se tocó elegir_carpetas() (el
#    menú interactivo de una sola cuenta): su firma (imap, cfg) no lleva
#    nombre_cuenta ni conexiones, cambiarla arrastraría a otros sitios
#    que la llaman, y el riesgo real es bajo (conexión recién usada en
#    la misma sesión interactiva) -- desproporcionado para el problema
#    real reportado.
#    listar_carpetas() y _seleccionar_carpeta() en sí NO llevan try/except
#    propio -- se mantiene la convención ya existente en todo el fichero
#    (ver _ejecutar_busqueda) de comprobar/reconectar en el sitio que
#    orquesta el bucle por cuentas, no dentro del wrapper de bajo nivel.
#    Verificado con test_ronda_f_reconexion_listar_carpetas.py (3 tests,
#    con FakeIMAP que responde OK la primera vez a noop() -- para no
#    falsear el test dejando que la comprobación previa de arranque,
#    _refrescar_conexiones, arregle sola las cosas antes de que el bucle
#    llegue a probar nada -- y falla a partir de la segunda, simulando
#    que la cuenta del medio se queda inactiva DESPUÉS de esa
#    comprobación inicial, mientras se procesa la cuenta anterior, la
#    secuencia exacta del bug real): "-acc todas -last N" reconecta sola
#    y las tres cuentas aparecen en el resultado; buscar_todas_las_cuentas
#    y _plan_segun_folders igual. Confirmado además, aparte, que este
#    mismo test SÍ revienta con el traceback real (imaplib.IMAP4.abort:
#    command: LIST => socket error: EOF) si se ejecuta contra una copia
#    del fichero con este arreglo deshecho -- prueba de que el test
#    detecta de verdad el bug, no solo que pasa por casualidad. Batería
#    completa existente (test_bug_ronda_inbox_grande,
#    test_bug_verpdf_1_cuenta, test_bug_while_pipe_roba_lineas,
#    test_escribir_correo_nuevo, test_opcion_cabeceras,
#    test_ronda_d_aqui_multiaccion_adjuntos, test_ds_intervalo_max) — sin
#    regresiones.
#
# 54) -escribir y -responder ya admiten cuerpo vacío (y asunto vacío, que
#    ya funcionaba) a propósito:
#    Reportado 2026-09-28: "no me deja escribir sin nada en el cuerpo".
#    El asunto vacío en -escribir ya funcionaba desde antes (-s omitido:
#    solo avisa, no bloquea — nota anterior de -escribir). El CUERPO
#    vacío sí bloqueaba, de dos formas distintas:
#      a) -escribir por comando directo: si lo resuelto a partir de
#         -cuerpo/-texto/stdin quedaba vacío tras strip(), hacía
#         sys.exit("El cuerpo del correo está vacío.") sin excepción
#         posible.
#      b) -responder INTERACTIVO (menú, _componer_respuesta): no existía
#         ninguna forma de dejarlo vacío a propósito. Tanto pulsar Enter
#         en blanco nada más empezar como escribir "." como primerísima
#         línea (la única forma de "terminar" sin haber escrito nada)
#         devolvían None, que el llamador interpreta como "cancelado, no
#         se escribió nada" — no había manera de distinguir "cancelo" de
#         "quiero el cuerpo en blanco, va en serio".
#      (-responder por comando directo, con -texto "", en cambio, YA
#      funcionaba: _responder_mensaje_cli nunca tuvo ningún filtro de
#      cuerpo vacío. Se añadió un test de regresión para que se quede
#      así.)
#    Arreglado sin quitar ninguna protección real:
#      a) -escribir: -cuerpo (fichero) y -texto son formas EXPLÍCITAS de
#         dar el cuerpo — si lo dado resulta vacío, ahora se entiende
#         como "vacío a propósito": se avisa por stderr (mismo patrón que
#         el aviso ya existente de asunto vacío) y se envía igual. La
#         entrada estándar (stdin) es distinta: si no se dio ni -cuerpo
#         ni -texto y lo leído de ahí sale vacío, SIGUE bloqueando (mismo
#         criterio que ya tenía -responder con su propia entrada
#         estándar vacía) — no hay forma de saber si un stdin vacío es
#         "cuerpo en blanco a propósito" o "me olvidé de redirigir algo",
#         así que ahí se mantiene la protección. Sin ninguna fuente en
#         absoluto (ni -cuerpo, ni -texto, ni stdin no-tty) se sigue
#         exigiendo alguna, exactamente igual que antes — "vacío a
#         propósito" no es lo mismo que "no se dio nada".
#      b) _componer_respuesta(): escribir "." como primerísima línea
#         (sin haber escrito ninguna otra) ya NO se trata igual que
#         "Enter en blanco" — ahora devuelve "" (cadena vacía, cuerpo en
#         blanco confirmado) en vez de None. "Enter en blanco" al
#         principio sigue cancelando exactamente igual que siempre (esa
#         vía de escape no se ha tocado). El único llamador
#         (_responder_mensaje) ya comprobaba "is None" para cancelar, no
#         una comprobación de veracidad — así que "" fluye sin más
#         cambios hasta _enviar_correo() y se envía con normalidad
#         (EmailMessage.set_content("") no da ningún problema).
#    Verificado con test_ronda_g_cuerpo_asunto_vacio.py (11 tests):
#    _componer_respuesta() a nivel unitario ('.' primero → "";  Enter en
#    blanco primero → None, sin cambios; composición normal de varias
#    líneas, sin cambios; una línea en blanco EN MEDIO de la respuesta se
#    conserva, no cancela ni termina — regresión); -responder interactivo
#    de principio a fin con input() simulado (cuerpo vacío llega a
#    enviarse de verdad; cancelar con Enter en blanco sigue sin enviar
#    nada); -escribir por comando directo con -texto "" y con -cuerpo
#    apuntando a un fichero vacío (ambos avisan por stderr y envían);
#    asunto Y cuerpo vacíos A LA VEZ (nada impide que lo estén los dos);
#    sin ninguna fuente de cuerpo en absoluto, sigue bloqueando (sin
#    cambios); y -responder por comando directo con -texto "" (ya
#    funcionaba, confirmado sin regresión). Cuidado especial en el propio
#    test con la reutilización de _correr_comando_directo/
#    _preparar_smtp_falso (de test_escribir_correo_nuevo.py): hay que
#    reutilizar la MISMA instancia de módulo que esas funciones usan
#    (tesc.lc), no cargar una copia propia de correo.py, porque si no
#    fijar CUENTAS/smtplib ahí no tiene ningún efecto sobre lo que esas
#    funciones ven de verdad. Batería completa existente (las 8 anteriores)
#    — sin regresiones, incluida test_escribir_correo_nuevo.py, que sigue
#    exigiendo un cuerpo cuando de verdad no se da ninguna fuente.
#
# 55) Al enviar o responder, se guarda una copia en la carpeta de
#    Enviados de la cuenta que envía:
#    Pedido 2026-09-29: "al enviar o responder, podrias dejar una copia
#    en la carpeta enviados (sent) de la cuenta que envia?". Hasta ahora,
#    un correo enviado con -escribir o -responder (interactivo o por
#    comando directo) solo quedaba en el buzón del DESTINATARIO — nada
#    se guardaba en la cuenta propia, salvo que el proveedor lo hiciera
#    él solo, por su cuenta, al enviar por SMTP autenticado.
#
#    Tras un envío SMTP correcto, se intenta guardar una copia del
#    mensaje ya construido (con toda cabecera puesta, adjuntos incluidos)
#    en la carpeta de Enviados de la cuenta, vía IMAP APPEND, marcada
#    (\Seen) — como hace cualquier cliente de correo con su propia copia
#    en Enviados. El nombre real de esa carpeta varía MUCHO de un
#    proveedor a otro (y, en Gmail, incluso según el IDIOMA de la
#    cuenta), así que se prueban, en orden, hasta encontrar una que
#    acepte el APPEND:
#      1) "sent_folder" en CUENTAS, si esa cuenta lo tiene configurado —
#         la forma FIABLE, para cuando el nombre no está en la lista de
#         abajo, o para evitar tener que adivinar.
#      2) Una lista de nombres habituales de los proveedores que soporta
#         este programa, más variantes conocidas: "Sent", "Sent Items",
#         "Sent Messages", "INBOX.Sent", "[Gmail]/Sent Mail",
#         "[Google Mail]/Sent Mail", "[Gmail]/Correo enviado", "Gesendet",
#         "Enviados", "Correo enviado".
#    Se usa la PRIMERA que acepta el APPEND; si ninguna funciona, se
#    avisa por stderr (mencionando qué se probó y cómo fijar
#    "sent_folder") pero NO se hace fallar nada — el correo ya se envió
#    bien por SMTP, esto es solo la copia local ("best effort").
#
#    Conexión IMAP usada para guardar la copia:
#      - -responder (interactivo o por comando directo) SIEMPRE parte de
#        un mensaje ya encontrado por IMAP, así que ya hay una conexión
#        abierta a mano — se reutiliza (comprobando que sigue viva y
#        reconectando sola si hacía falta, mismo mecanismo que la Ronda
#        F), sin abrir ninguna otra. Como esa conexión sigue siendo de
#        quien llamó a _enviar_correo, no se cierra aquí.
#      - -escribir, por diseño, no toca IMAP para nada si no hace falta
#        (enviar es solo SMTP) — así que, si hace falta guardar la
#        copia, se abre una conexión IMAP NUEVA solo para eso, y esta sí
#        se cierra justo después.
#      - Si no se puede conseguir ninguna conexión utilizable (falla el
#        login, no hay red...), se avisa por stderr, pero el envío (que
#        ya se hizo) sigue devolviendo éxito.
#
#    Riesgo de DUPLICADOS: varios proveedores (sobre todo Gmail) ya
#    guardan ELLOS SOLOS una copia en Enviados al enviar por su propio
#    SMTP autenticado — guardarla también aquí duplicaría el correo en
#    esa carpeta. Se añade "save_sent": False (opcional, en CUENTAS,
#    junto a "sent_folder") para desactivar el guardado por cuenta sin
#    tocar nada más; por defecto (sin este ajuste, o con True) se guarda.
#
#    Nuevas funciones: _carpeta_enviados_candidatas(cfg) (arma la lista
#    de candidatas, sin duplicados); _guardar_copia_en_enviados(imap,
#    cfg, nombre_cuenta, mensaje) (prueba las candidatas en orden, hace
#    el APPEND, entrecomillando el nombre igual que _seleccionar_carpeta
#    — carpetas con espacios o corchetes tipo "[Gmail]/Sent Mail");
#    _con_conexion_para_guardar_enviados(imap, cfg, nombre_cuenta)
#    (decide si reutilizar la conexión dada o abrir/cerrar una nueva, ver
#    arriba). _enviar_correo() gana un parámetro opcional "imap=None"
#    (quien no lo tenga a mano, como antes, simplemente no lo pasa);
#    _responder_mensaje() y _responder_mensaje_cli() ganan el mismo
#    parámetro y lo reciben, a su vez, de los DOS sitios que ya tenían la
#    conexión a mano pero antes la descartaban (el menú interactivo y
#    _ejecutar_accion_cli) — ahora se la pasan a _enviar_correo en vez de
#    tirarla.
#
#    Verificado con test_ronda_h_copia_enviados.py (12 tests):
#    _carpeta_enviados_candidatas (sin "sent_folder" usa la lista
#    habitual; con "sent_folder" va siempre primero; sin duplicar si
#    coincide con uno habitual); _guardar_copia_en_enviados (prueba en
#    orden y para en la primera que acepta; si ninguna funciona avisa por
#    stderr sin lanzar; "sent_folder" configurado no prueba ninguna
#    otra); _enviar_correo de extremo a extremo (conexión ya abierta: la
#    reutiliza, sin conectar() ni cerrarla; sin conexión: abre una nueva
#    y la cierra; "save_sent": False: no toca la conexión para NADA — ver
#    _NoDebeUsarseIMAP, que lanza si se accede a cualquier atributo suyo;
#    "sent_folder" de extremo a extremo; sin poder conectar para guardar,
#    el envío sigue devolviendo éxito; conexión muerta reconecta sola,
#    mismo mecanismo que la Ronda F). Además, se corrigieron dos ficheros
#    de test YA EXISTENTES que -escribir dejó de poder ejecutar tal cual
#    (KeyError: 'port'): -escribir, al no tocar IMAP nunca antes de esta
#    ronda, se probaba con una cuenta de prueba sin ese dato — se le
#    añadió, junto con un conectar() de mentira para no intentar una
#    conexión de verdad (test_escribir_correo_nuevo.py,
#    test_ronda_g_cuerpo_asunto_vacio.py, que reutiliza esa misma cuenta
#    de prueba). El FakeIMAP compartido (test_bug_verpdf_1_cuenta.py)
#    ganó un método append() (antes no hacía falta), para que las
#    pruebas de -responder por comando directo que ya existían (que sí
#    pasan su propia conexión de mentira) no fallaran al intentar guardar
#    la copia. Batería completa (las 9 anteriores) — sin regresiones.
#
# 56) -ver-pdf: los enlaces quedan completos y clicables de verdad:
#    Pedido 2026-09-29: "al utilizar ver-pdf deja todos los links
#    completos y creados para pulsar sobre ellos y que se abran". Un
#    enlace podía perderse o quedar mutilado en TRES sitios distintos
#    antes de llegar al PDF final — arreglados los tres:
#
#    a) LA CAUSA MÁS DIRECTA, probablemente: texto_cuerpo_limpio /
#       _limpiar_cuerpo_texto recortaba TODO enlace http(s) de más de
#       LARGO_URL_MAX (20, el valor por defecto en
#       leerCorreoConfiguracion.py) caracteres a "primeros 20 + …" — es
#       decir, prácticamente cualquier URL real de verdad (un dominio
#       solo ya ronda esa longitud), y esto se aplicaba también a "ver
#       completo"/.txt/.pdf, no solo a la vista previa en listas para la
#       que tiene sentido. Un enlace cortado a medias ya no servía ni
#       para leerlo entero ni, en el PDF, para pulsarlo — daba igual lo
#       que se arreglara en (b)/(c) si el texto ya llegaba mutilado.
#       Arreglado separando los dos usos: texto_cuerpo_limpio() gana un
#       parámetro "recortar_urls=True" — por defecto, IGUAL que siempre
#       (vista_previa(), para listas al buscar/listar muchos mensajes,
#       sigue llamándola sin tocar nada, así que las listas siguen tan
#       compactas como antes); _texto_completo_formateado (compartida
#       por "ver completo" en pantalla, .txt Y .pdf) ahora pasa
#       recortar_urls=False, así que ahí los enlaces llegan siempre
#       completos. LARGO_URL_MAX en sí no cambia de valor — sigue siendo
#       la cuenta del usuario en leerCorreoConfiguracion.py — solo deja
#       de aplicarse fuera de la vista previa en listas.
#
#    b) _HTMLaTexto/html_a_texto: un correo HTML con un enlace tipo botón
#       ("Ver factura", "Confirmar", "Unsubscribe"...) perdía la URL de
#       destino POR COMPLETO al convertir a texto plano — el texto
#       visible no decía a dónde llevaba en ningún sitio (el conversor
#       solo se quedaba con el texto entre <a>...</a>, nunca con su
#       href). Arreglado: ahora, al cerrar un <a href="...">, se añade la
#       URL entre paréntesis justo después del texto visible del enlace
#       — "Ver factura (https://.../factura/123)" — salvo que ese texto
#       YA contenga la URL (para no duplicarla cuando el remitente ya
#       escribió la dirección tal cual como texto del enlace). Solo para
#       esquemas navegables de verdad: http://, https:// y mailto: (se
#       ignoran "#ancla", "javascript:", href vacío...). Como
#       texto_cuerpo() es compartida por TODO lo que lee el cuerpo del
#       mensaje, esto también mejora (de regalo, mismo mecanismo) la
#       vista previa en listas, "ver" interactivo y -ver-txt — no solo
#       -ver-pdf.
#
#    c) _generar_pdf_simple (el generador de PDF propio, sin
#       dependencias externas): aunque la URL SÍ llegara completa como
#       texto visible (tras (a) y (b)), se dibujaba como texto suelto —
#       no había ningún enlace de verdad ahí, no se podía pulsar. Ahora
#       cada URL/mailto: que aparezca en el texto (detectada por la
#       nueva _pdf_detectar_urls, que además recorta la puntuación de
#       cierre de la propia frase — un punto, una coma, un paréntesis o
#       corchete que no se abrió dentro de la URL — para no incluirla en
#       el destino del enlace, conservando a la vez el paréntesis de
#       cierre cuando sí pertenece a la URL, estilo Wikipedia) se marca
#       ADEMÁS con una anotación /Link (acción /URI) encima del propio
#       texto dibujado, en la posición donde cae — clicable de verdad en
#       cualquier lector de PDF, y se abre con el programa asociado al
#       enlace (navegador para http(s), gestor de correo para mailto:).
#       El rectángulo clicable se calcula con el mismo ancho de carácter
#       aproximado que ya se usaba para decidir dónde envolver las
#       líneas — Helvetica no es monoespaciada, así que es una posición
#       aproximada, no exacta al píxel, pero consistente con cómo ya se
#       maqueta el resto del texto (mismo criterio que el generador ya
#       usaba antes de esta ronda). Para que una URL nunca quede partida
#       a medias entre dos líneas (lo que le habría quitado el
#       rectángulo clicable a la mitad que faltase), _pdf_envolver_texto
#       deja de partir palabras largas (break_long_words=False) en
#       cualquier párrafo que contenga un enlace — en el caso raro de
#       una URL más larga que el ancho de página, esa línea concreta se
#       sale un poco del margen derecho, precio aceptado a cambio de un
#       enlace intacto; el resto de párrafos (sin ningún enlace) se
#       sigue partiendo exactamente igual que siempre. Sin ningún enlace
#       en el texto, no se añade ni la clave /Annots — el PDF no
#       engorda por nada.
#
#    Verificado con test_ronda_i_links_pdf.py (25 tests), sin ninguna
#    librería de PDF externa (a propósito, ni siquiera para LEER los PDF
#    generados en los tests — se mantiene tan libre de dependencias como
#    el propio programa): texto_cuerpo_limpio/_limpiar_cuerpo_texto (por
#    defecto sigue recortando como siempre — regresión de la vista
#    previa en listas; con recortar_urls=False conserva la URL completa;
#    "ver completo" de un correo de TEXTO PLANO normal con una URL larga
#    ya no la trocha, y llega clicable al PDF); _pdf_detectar_urls (caso
#    simple; recorta puntuación final de la frase; conserva un paréntesis
#    que sí pertenece a la URL —Wikipedia— pero recorta uno que la
#    envuelve entera; detecta mailto:; varias URLs en el mismo texto; sin
#    ningún enlace, lista vacía); _pdf_envolver_texto (una URL más larga
#    que el ancho de línea no se parte; sin URL, una palabra larga se
#    sigue partiendo igual que siempre); _generar_pdf_simple (un enlace
#    simple genera su anotación con la URI y un rectángulo válidos; sin
#    ningún enlace, ni anotaciones ni /Annots; varios enlaces, varias
#    anotaciones en orden; la puntuación de la frase no entra en la URI
#    pero el texto se sigue dibujando completo; en un PDF de varias
#    páginas, cada enlace aparece en SU página real, comprobado leyendo
#    /Kids del nodo /Pages — no por cercanía de texto en el fichero, que
#    no sirve aquí porque los objetos se numeran según se van
#    necesitando, no por página); html_a_texto/_HTMLaTexto (un enlace
#    tipo botón añade su URL entre paréntesis; si el texto ya es la URL
#    no se duplica; esquemas no navegables —#ancla, javascript:, href
#    vacío— no añaden nada; mailto: también se añade; varios enlaces,
#    cada uno con la suya); y un test de extremo a extremo (correo HTML
#    con un enlace "Ver factura" llega clicable de verdad al PDF final,
#    encadenando (a)+(b)+(c)). Batería completa (las 10 anteriores) —
#    sin regresiones.
#
# 57) Nuevo comando -subir-eml: sube un fichero .eml local a INBOX:
#    Pedido 2026-09-29: "podrias poner un comando para, dado un archivo
#    eml, que se pueda subir o encajar o anadir ese correo electronico a
#    la carpeta inbox de una cuenta determinada?". Es, en cierto modo, lo
#    CONTRARIO de -descargar/-eml: aquella guarda un .eml a partir de un
#    mensaje que YA está en el servidor; esta parte de un fichero .eml
#    que ya existe en el ordenador/móvil y lo sube, vía IMAP APPEND, a la
#    carpeta INBOX de una cuenta.
#
#    Uso (acción aparte, como -escribir — no busca ni selecciona ningún
#    mensaje existente, y no se combina con eso ni con -escribir):
#      python3 correo.py -acc <cuenta> -subir-eml <ruta.eml>
#    -acc exige EXACTAMENTE una cuenta (no "todas" ni varias) — es la
#    cuenta a la que se sube. La carpeta de destino es siempre INBOX (el
#    único nombre de carpeta verdaderamente estándar entre proveedores,
#    a diferencia del "Sent" de la Ronda H, que sí varía mucho — por eso
#    aquí no hace falta una lista de candidatas ni un "sent_folder").
#
#    Decisiones de diseño:
#      - El fichero se lee en BINARIO ("rb"), no en texto: un .eml no
#        tiene por qué ser UTF-8 puro (cabeceras codificadas, adjuntos en
#        base64...), y leerlo en texto podría reventar o corromper bytes.
#      - Se sube el contenido TAL CUAL viene del fichero (la variable
#        "crudo"), sin reconstruir el mensaje a través de
#        email.message_from_bytes()/EmailMessage: el objetivo es una
#        copia FIEL del original, y reserializar podría reordenar
#        cabeceras o normalizar el salto de línea. El mensaje SÍ se
#        parsea aparte (con email.message_from_bytes(), que es muy
#        tolerante y no lanza ni con contenido casi ilegible) pero solo
#        para LEER dos datos: la fecha (Date:) y el asunto — nunca para
#        reconstruir los bytes a subir.
#      - Fecha conservada: se usa fecha_mensaje(msg) (la cabecera Date:
#        original) como INTERNALDATE del mensaje ya subido — para que
#        aparezca ordenado por SU fecha real, no por la fecha de subida.
#        imaplib.Time2Internaldate() acepta un datetime "aware" tal cual
#        (fecha_mensaje() ya lo devuelve así); si el mensaje no tiene una
#        Date: reconocible (fecha_mensaje() devuelve el centinela
#        _FECHA_MUY_ANTIGUA), se pasa date_time=None y es el propio
#        servidor quien le pone la fecha de subida — mismo criterio de
#        "centinela" que ya usa formatear_fecha().
#      - Sin marcar \Seen (flags=None): el correo subido queda como NO
#        LEÍDO en INBOX, igual que cualquier correo recién llegado de
#        verdad — al contrario que la copia en Enviados de la Ronda H
#        (que sí se marca \Seen a propósito, porque ESA es una copia de
#        algo que la propia cuenta ya envió).
#      - Fichero vacío (0 bytes, o solo espacios): error claro, no se
#        intenta subir nada — un .eml sin contenido no tiene ni cabeceras
#        ni cuerpo, así que no hay nada que tenga sentido subir.
#      - La conexión IMAP se abre solo para esto (a diferencia de
#        -escribir, -subir-eml SÍ necesita IMAP desde el principio: es su
#        propio mecanismo de "subida") y se cierra siempre al terminar,
#        tanto si el APPEND sale bien como si el servidor lo rechaza o
#        lanza una excepción (p.ej. conexión cortada a media subida).
#      - Se rechaza combinarlo con cualquier otra acción, con una forma
#        de reunir mensajes (-last/-buscar/-id/etc.), o con -escribir —
#        en CUALQUIER orden de argv (ambas validaciones se comprueban
#        entre sí), con un mensaje que dice exactamente qué se combinó
#        mal.
#
#    Nueva entrada en _FLAGS_CON_VALOR ("-subir-eml": "subir_eml") y
#    nuevo bloque en comando_directo(), justo después del de -escribir
#    (mismo patrón: se resuelve aparte, con sys.exit() al final en vez de
#    seguir con el resto del flujo de comando_directo). No se añadió a
#    ACCIONES_CLI (igual que "escribir": es una acción aparte, no una que
#    actúe sobre una selección de mensajes ya reunidos).
#
#    Verificado con test_ronda_j_subir_eml.py (15 tests): sube a INBOX
#    sin marcar \Seen, con los bytes intactos, y cierra la conexión;
#    conserva la fecha original como INTERNALDATE; sin Date:, no fuerza
#    ninguna fecha; bytes crudos subidos tal cual incluso con saltos de
#    línea Unix y cabeceras no estándar (sin recomponer el mensaje);
#    asunto (o "(sin asunto)") en el mensaje de confirmación; fichero
#    inexistente, vacío, o con bytes que no son UTF-8 válido (se lee en
#    binario, no revienta); rechaza "todas"/varias cuentas; rechaza
#    combinarse con otra acción, con una forma de reunir, o con
#    -escribir, en cualquier orden; cuenta desconocida; fallo de
#    conexión; servidor que rechaza el APPEND (y aun así cierra la
#    conexión); excepción durante el APPEND (y aun así cierra la
#    conexión). Batería completa (las 11 anteriores) — sin regresiones.
#
# 58) Nuevo comando -duplicados: detecta copias EXACTAMENTE iguales de un
#    correo dentro de una misma carpeta, y permite borrar solo UNA:
#    Pedido 2026-09-29: "podrias anadir la posibilidad de revisar si al
#    subir emls, en una carpeta quedan 2 versiones exactamente iguales de
#    1 correo y permitirle elejir eliminar 1, cualquier de ellas?". Tras
#    preguntar qué debía contar como "exactamente igual" (la respuesta:
#    remitente, destinatarios, asunto, cuerpo y adjuntos, "hasta un cierto
#    punto en que... sabes con muy alta probabilidad que es el mismo
#    correo electronico" — sus palabras), y tras una corrección del propio
#    usuario a mitad de la implementación ("quiero, no una comprobacion
#    asociada a subir-eml, sino algo separado, aparte"), -duplicados
#    quedó como una acción TOTALMENTE independiente — ni se activa sola
#    tras -subir-eml, ni se combina con ella.
#
#    Uso (acción aparte que reúne ELLA SOLA toda la carpeta — no hace
#    falta -last ni ningún otro filtro, aunque si se añaden se respetan,
#    p.ej. para limitar por fecha):
#      python3 correo.py -acc gmx1 -duplicados
#      python3 correo.py -acc gmx1 -duplicados -eliminar 7
#    Por defecto solo mira INBOX (-folders all para revisar todas). Con
#    varias cuentas a la vez (-acc cuenta1,cuenta2 o -acc todas), cada
#    cuenta se analiza por separado.
#
#    Decisiones de diseño:
#      - Criterio de "exactamente igual" (_clave_duplicado_exacto): misma
#        CUENTA, misma CARPETA IMAP, mismo De, mismos Para (direcciones
#        normalizadas con direcciones_de_campo(), conservando repeticiones
#        — "a@x.com" y "a@x.com, a@x.com" NO son lo mismo), mismo Asunto,
#        mismo Cuerpo (texto_cuerpo_limpio(), igual que -buscarcuerpo) y
#        mismos Adjuntos (nombre+tamaño, vía _lista_adjuntos(), sin
#        importar el orden). El Cc NO entra en la comparación — el
#        usuario no lo mencionó al describir el criterio. Cuenta y
#        carpeta forman PARTE de la clave a propósito: el mismo correo en
#        dos carpetas (p.ej. INBOX y donde lo archivaste tú) o en dos
#        cuentas distintas no es el tipo de "duplicado accidental" que se
#        buscaba, así que nunca se comparan entre sí. La agrupación usa
#        un diccionario por clave (una pasada, O(n)) en vez de comparar
#        cada mensaje con todos los demás.
#      - POSICIÓN, no id, para elegir cuál borrar: id_amigable() se
#        deriva del Message-ID (no del cuerpo/adjuntos), así que dos
#        copias EXACTAMENTE iguales para -duplicados solo comparten el
#        mismo id_amigable() cuando también comparten Message-ID — el
#        caso habitual cuando es el mismo fichero .eml subido más de una
#        vez sin tocarlo (-subir-eml sube los bytes tal cual). Cuando
#        coincide, "-eliminar id:XXXX" borraría TODAS las copias del
#        grupo a la vez, no una sola — justo lo contrario de "elegir
#        eliminar 1, cualquiera de ellas". Por eso el informe de
#        -duplicados numera cada copia por su POSICIÓN en esa misma
#        lista (que SIEMPRE selecciona una única copia, compartan o no
#        el mismo id), y para borrar una sola hay que repetir la MISMA
#        orden añadiendo "-eliminar <posición>" (el propio informe lo
#        explica, y advierte contra usar "-eliminar id:").
#      - Reutiliza el motor de búsqueda ya existente (_ejecutar_busqueda)
#        en modo "reunir toda la carpeta": se añadió "duplicados" a la
#        condición modo_busqueda (junto a campos is not None / fecha_desde
#        is not None / con_adjuntos is not None) para que -duplicados por
#        sí solo dispare este modo sin necesitar -last, y a la condición
#        necesita_cuerpo, para traer el mensaje COMPLETO (no solo
#        cabeceras) de cada uno, ya que hace falta el cuerpo y los
#        adjuntos para comparar. campos sigue siendo None, así que
#        _ejecutar_busqueda no descarta ningún mensaje por texto: el
#        efecto es "traer todo lo que haya", que es justo lo que
#        -duplicados necesita.
#      - Sin selección propia: se añadió "duplicados" a ACCIONES_CLI (a
#        diferencia de "escribir"/"subir_eml", que son acciones aparte,
#        no en esa tupla) para que las comprobaciones de conflicto ya
#        existentes en -escribir y -subir-eml —que comparan contra
#        acciones + flags_de_reunion— rechacen automáticamente su
#        combinación con -duplicados, sin tocar el código de esas dos.
#        También se añadió al principio de _ORDEN_ACCIONES_MULTIPLES
#        (antes que "eliminar", que va siempre el último), por ser una
#        acción informativa que no borra ni cambia nada por sí sola.
#      - No borra nada por sí solo: -duplicados solo informa. Borrar una
#        copia sigue pasando por el mecanismo ya existente de -eliminar
#        (por posición, ver arriba), no por un mecanismo nuevo.
#
#    Verificado con test_ronda_k_duplicados.py (23 tests): la clave de
#    agrupación no depende del Message-ID; distingue por De/Para/Asunto/
#    Cuerpo/Adjuntos (cualquier diferencia rompe el duplicado); adjuntos
#    en distinto orden siguen siendo iguales; nunca cruza carpetas ni
#    cuentas; el Cc se ignora; no se pierde la repetición de un
#    destinatario; el informe agrupa 3+ copias en un solo grupo (no en
#    pares sueltos), agrupa varios duplicados distintos a la vez, indica
#    "Sin duplicados" con claridad cuando no los hay, recomienda borrar
#    por POSICIÓN y solo menciona "-eliminar id:" para advertir que
#    podría borrar todas las copias del grupo juntas, y muestra
#    "cuenta/carpeta" con varias cuentas o solo "carpeta" con una. Por
#    línea de comandos: solo INBOX por defecto; -folders all reúne
#    varias carpetas sin comparar
#    entre ellas; rechaza combinarse con -escribir y con -subir-eml;
#    funciona con varias cuentas a la vez sin confundir los duplicados de
#    una con los correos de otra. Batería completa (las 12 anteriores) —
#    sin regresiones.
#
# 59) Arreglo: la copia en Enviados (y el propio correo enviado) no
#    llevaba fecha:
#    Pedido 2026-09-30: "la copia en la carpeta enviados que dejas, no le
#    pones fecha, ponle la fecha y hora de envio".
#
#    Diagnóstico: _enviar_correo() (usada tanto por -escribir como por
#    -responder, en modo comando directo y en el menú interactivo) nunca
#    ponía una cabecera Date: en el mensaje que construye ("nuevo" —
#    EmailMessage()). Eso afectaba a DOS sitios a la vez, porque son el
#    MISMO objeto reutilizado:
#      - El correo que de verdad recibe el destinatario por SMTP (sin
#        Date:, fuera de la norma RFC 5322, aunque muchos servidores de
#        salida rellenan una si falta al retransmitir — no hay que
#        confiar en eso).
#      - La copia que se guarda después en Enviados vía IMAP APPEND
#        (Ronda H, _guardar_copia_en_enviados) — esta SIEMPRE se veía
#        afectada, sin depender de ningún servidor intermedio, porque
#        _guardar_copia_en_enviados hace mensaje.as_bytes() sobre ese
#        mismo "nuevo" tal cual. Al releer esa copia más tarde,
#        fecha_mensaje() (que lee la cabecera Date: del propio mensaje,
#        no hay ninguna fecha "de servidor" alternativa de donde
#        sacarla) no encontraba nada y devolvía el centinela
#        _FECHA_MUY_ANTIGUA, que formatear_fecha() muestra como "(sin
#        fecha)" — justo el síntoma reportado.
#
#    Arreglo: nuevo["Date"] se pone ahora justo antes de conectar y
#    enviar por SMTP — DESPUÉS del posible getpass.getpass() interactivo
#    (que puede tardar lo que tarde la persona en escribir la
#    contraseña) y de leer los adjuntos del disco, para que sea lo más
#    fiel posible al momento REAL de envío, tal como pidió el usuario.
#    Calculada con datetime.now(timezone.utc) convertido a España
#    PENINSULAR con el mismo helper que usa el resto del programa para
#    mostrar fechas (_a_hora_espana_peninsular) y formateada con
#    email.utils.format_datetime() -- NO con formatdate(localtime=True),
#    que se fiaría de la zona horaria del SISTEMA en vez de calcular
#    Europe/Madrid de forma explícita (con su propia gestión de horario
#    de verano/invierno, y reserva manual si no hay zoneinfo/tzdata
#    instalada -- igual que _a_hora_espana_peninsular ya hace en todo lo
#    demás). Así la fecha queda correcta aunque el dispositivo tenga mal
#    puesta su zona horaria.
#
#    Al ser el mismo objeto "nuevo" en todo el flujo, este único cambio
#    arregla las dos cosas a la vez: el correo que llega al destinatario
#    ahora SÍ lleva una Date: bien formada, y la copia en Enviados queda
#    con la fecha y hora reales del envío en vez de "(sin fecha)" — sin
#    tocar nada de _guardar_copia_en_enviados ni de _carpeta_enviados_
#    candidatas (Ronda H), que siguen exactamente igual.
#
#    Verificado con test_ronda_l_fecha_envio.py (8 tests): el mensaje
#    mandado por SMTP lleva Date:; también la lleva aunque "save_sent"
#    esté en False (el envío en sí no depende de si se guarda copia); la
#    copia guardada en Enviados lleva EXACTAMENTE la misma Date: que el
#    enviado por SMTP (mismo objeto); funciona igual con una conexión
#    IMAP ya abierta (-responder) que abriendo una nueva (-escribir);
#    fecha_mensaje()/formatear_fecha() sobre esa copia ya no devuelven/
#    muestran el centinela "(sin fecha)"; la fecha puesta es "ahora
#    mismo" (con tolerancia de unos segundos); y el offset de la
#    cabecera coincide con el que toca en España peninsular en el
#    momento de ejecutar el test (calculado con el mismo helper de
#    producción, para no depender de si se ejecuta en horario de verano
#    o de invierno). Batería completa (las 13 anteriores) — sin
#    regresiones.
#
#    Manual 1 (sección 10.5, "Responder a uno") actualizado con una
#    frase confirmando que tanto el correo enviado como la copia en
#    Enviados quedan con la fecha y hora reales del envío. Manual 2 no
#    necesitaba cambios: sus dos menciones de la copia en Enviados
#    (-responder y -escribir) ya remiten a esa misma sección del Manual
#    1 para el detalle, sin repetirlo.
#
# 60) Nuevo comando -ver-img: el correo "tal como se ve" convertido en
#    imagen(es):
#    Pedido 2026-10-01: "anade la posibilidad de obtener 1 imagen que
#    seria la copia en formato de imagen de lo que se ve al abrir el
#    correo, y sirve para cuando se pide un pdf o txt de un correo pero
#    no se ve nada pues todo el correo se trata de 1 imagen que no se ve
#    en pdf ni txt. pero que se pueda pedir de cualquier correo. que se
#    pueda pedir 1 imagen que seria lo primero que se ve, o 2 o 3
#    imagenes, que serian bajando pantalla a pantalla hacia abajo, para
#    que se pueda ver que esta abajo de la 1ra imagen."
#
#    El caso real que lo motivó: boletines/publicidad donde el correo
#    ENTERO es una sola imagen grande (el remitente no manda nada de
#    texto de verdad) — ahí -ver-txt y -ver-pdf (Rondas anteriores) no
#    sirven de nada, porque no hay texto que extraer: solo queda una
#    página en blanco o casi. -ver-img soluciona justo ese caso, pero
#    funciona con CUALQUIER correo, no solo ese: siempre reconstruye el
#    aspecto del mensaje (negrita, enlaces, imágenes incrustadas) en 1 o
#    más imágenes .png, tantas como "pantallas" se pidan.
#
#    Antes de construirlo se planteó al usuario la decisión técnica de
#    fondo: renderizar con un navegador de verdad (Playwright/Chromium —
#    reproducción más fiel al cliente de correo real, pero una
#    dependencia pesada con riesgo real de no instalarse ni funcionar
#    bien en Termux/Android) frente a dibujar el correo a mano con
#    Pillow (reproducción aproximada, pero una dependencia ligera, con
#    muchas más posibilidades de funcionar sin problemas en un móvil).
#    Se eligió la opción "hecho a mano" (Pillow), precisamente por ese
#    riesgo de compatibilidad con Termux — coherente con el espíritu de
#    todo el programa hasta ahora, que no depende de nada más que la
#    librería estándar de Python.
#
#    Uso:
#      python3 correo.py -acc gmx1 -ver-img id:XXXX
#      python3 correo.py -acc gmx1 -ver-img id:XXXX -pantallas 3 -con-remotas
#
#    Decisiones de diseño:
#      - Pillow pasa a ser la PRIMERA dependencia externa de todo el
#        programa (hasta ahora, cero: ni siquiera el PDF de -ver-pdf la
#        necesita — se genera a mano, sin librerías). Se importa de
#        forma PEREZOSA (el "from PIL import ..." vive dentro de las
#        funciones, nunca a nivel de módulo), así que el resto del
#        programa sigue funcionando exactamente igual, sin Pillow
#        instalada, mientras no se use -ver-img. Si falta, se avisa con
#        el comando exacto a instalar ("pip install Pillow
#        --break-system-packages"; en Termux puede ir mejor "pkg install
#        python-pillow") en vez de un Traceback críptico.
#      - Fuentes incrustadas en el propio fichero: la fuente por defecto
#        de Pillow (Aileron, embebida en la propia librería) no trae
#        tildes/ñ españolas completas — se detectó en pruebas que "í" y
#        "ó" salían como un cuadrado vacío. Se solucionó incrustando una
#        fuente completa (DejaVu Sans, normal y negrita DE VERDAD, no
#        negrita simulada por doble trazo) directamente en correo.py
#        como texto base64 -- recortada antes (con fontTools/pyftsubset)
#        a solo los caracteres que hacen falta (letras, tildes, ñ,
#        símbolos y signos de puntuación habituales), para no disparar
#        el tamaño del fichero. Así el programa SIGUE siendo un único
#        fichero autocontenido (más leerCorreoConfiguracion.py de
#        siempre): no hace falta instalar ni distribuir ningún fichero
#        de fuente aparte.
#      - Renderizado en dos pasadas: una de MEDIDA (sobre un lienzo
#        de prueba de 1x1, para calcular cuánto ocupa cada línea/párrafo
#        antes de saber la altura final) y otra de DIBUJO (sobre el
#        lienzo ya del tamaño correcto). Con un límite de altura
#        (n_pantallas × ALTO_CAPTURA_PANTALLA): en cuanto el contenido ya
#        acumulado llega a cubrir las pantallas pedidas, se deja de
#        procesar el resto del correo — así un boletín larguísimo, con
#        -pantallas 1, no pierde tiempo (ni ancho de banda, con
#        -con-remotas) de más en contenido que nunca se iba a mostrar.
#      - Imágenes remotas BLOQUEADAS por defecto: las incrustadas en el
#        propio correo (cid:/data:) se resuelven siempre, pero las que el
#        correo solo ENLAZA a un servidor ajeno (http/https) solo se
#        descargan con -con-remotas — igual que "mostrar imágenes"
#        bloqueado de entrada en cualquier cliente de correo. Con límite
#        de 15 MB y 10 segundos por imagen remota; si se supera
#        cualquiera de los dos, o falla la descarga, esa imagen en
#        concreto se sustituye por un recuadro "[imagen no disponible:
#        <motivo>]" (recortado con "…" si el motivo es muy largo — fallo
#        real visto durante las pruebas de integración, se desbordaba
#        del recuadro) y el resto del correo se genera con normalidad.
#      - Arreglo real encontrado en pruebas: un espacio de más entre
#        texto en negrita y la puntuación que viene pegada justo después
#        (ej. "<b>reserva del 15 de octubre</b>. Adjuntamos" salía como
#        "...octubre ." con un espacio antes del punto). El trozador de
#        palabras ahora recuerda si dos trozos de HTML consecutivos
#        tenían o no un espacio de verdad entre ellos, y solo añade
#        espacio cuando lo había en el original.
#      - Para correos sin HTML (solo texto plano), las URLs sueltas del
#        cuerpo se resaltan como enlace reutilizando el mismo detector ya
#        usado por -ver-pdf (_pdf_detectar_urls), sin duplicar esa
#        lógica.
#      - Nombre de fichero: mismo <nombre_del_correo> que el .eml/.txt/
#        .pdf de ese mensaje, seguido de "_pantalla1.png", "_pantalla2.png"...
#        (Manual 2, sección 6); con colisión de nombre, igual que el
#        resto del programa, añade _2/_3...
#      - Nuevos flags: "-ver-img <selección>" (acción, igual que -ver-pdf:
#        selección opcional, por defecto "todos"), "-pantallas N"
#        (opcional, por defecto 1 — validado con _validar_n_pantallas,
#        que usa _error()+return en vez de sys.exit, para no abortar de
#        golpe un comando con VARIAS acciones si -ver-img es solo una de
#        ellas) y "-con-remotas" (booleano, opcional, por defecto NO).
#        "ver_img" se añadió a ACCIONES_CLI y a _ORDEN_ACCIONES_MULTIPLES
#        (justo después de "ver_pdf").
#      - Paridad con el menú interactivo: la pregunta "¿Cómo?" de "Ver el
#        texto completo de uno" (opción 2 del menú de acciones) gana una
#        4ª opción, "4=Guardar como imagen", que pregunta a continuación
#        cuántas pantallas (Enter = 1) y si incluir imágenes remotas
#        (s/N) — incluso en el menú interactivo, igual que en el CLI,
#        imágenes remotas es "no" salvo que se pida expresamente.
#      - Tres constantes nuevas en leerCorreoConfiguracion.py (solo
#        AÑADIDAS al final, nunca se tocó nada existente):
#        ANCHO_CAPTURA_PANTALLA (1080), ALTO_CAPTURA_PANTALLA (1920) y
#        TAMANO_FUENTE_CAPTURA (30) — tamaño de las imágenes generadas y
#        de la letra dentro de ellas.
#      - Sin -cabeceras/-cabeceras-todas: a diferencia de -ver/-ver-txt/
#        -ver-pdf, -ver-img no admite más detalle de cabeceras — la
#        imagen siempre lleva el mismo encabezado fijo (ID/De/Para/
#        Asunto/Fecha).
#
#    Verificado con test_ronda_m_ver_img.py (25 tests): genera 1 pantalla
#    por defecto y varias con -pantallas, avisando cuando el correo da
#    para menos de las pedidas pero no cuando da justo para las pedidas;
#    nombre de fichero coherente con el resto de vistas; resolución de
#    imagen por cid: (encontrada y no encontrada), por data:, remota
#    (bloqueada por defecto, permitida con -con-remotas, rechazada por
#    tamaño, y con esquema no soportado); la puntuación pegada a negrita
#    ya no lleva espacio de más, mientras que el espacio normal entre
#    palabras se conserva; extracción de HTML/texto plano (con y sin
#    HTML, URLs sueltas resaltadas, acentos y símbolos españoles sin
#    reventar); validación de -pantallas con valores válidos e inválidos;
#    aviso correcto (con el comando a instalar) cuando falta Pillow; la
#    fuente incrustada cubre de verdad acentos y símbolos españoles, y la
#    negrita es NEGRITA DE VERDAD (más ancha de forma medible), no
#    simulada; y el menú interactivo genera la imagen con la opción 4,
#    sin reventar con un número de pantallas inválido. Batería completa
#    (las 14 anteriores) — sin regresiones.
#
#    Manual 1 (secciones 1, 2, 10.2, 13 y 15) y Manual 2 (ÍNDICE y
#    secciones 5.0, 5.9, 5.12, 6, 10 y la nueva 5.16) actualizados.
#
# 61) Responder: cuerpo desde fichero, adjuntos, responder a todos
#    (De/CC/BCC) y asunto propio; -c (eGarante) y copia en Enviados
#    también para -responder-todos/-bcc:
#    Pedido 2026-10-01: "anade responder con el cuerpo en un txt no solo
#    con texto en linea, que se puedan anadir adjuntos, y que solo
#    responda 1. al correo desde el cual llego el correo al cual se
#    responde, o 2. que pueda responder a todos ( de, cc, bcc ), ambas
#    opciones, e indicar un asunto automatico "Re: AsuntoOrigen" o que se
#    pueda poner otro adjunto si se quiere. que se pueda indicar -c para
#    que cc sea mailsigned y no olvidez dejar copia en carpeta enviados y
#    que tenga fecha, pues anteriormente dio error en ese sentido".
#
#    Hasta esta ronda, -responder (tanto interactivo como por comando
#    directo) solo sabía escribir el cuerpo en línea (-texto, o
#    tecleándolo en el menú), nunca admitía adjuntos, y SIEMPRE respondía
#    solo al remitente, con el asunto SIEMPRE calculado como "Re: <el
#    original>" — sin forma de cambiar nada de eso. "-c" (eGarante) y
#    "-cc" ya existían, pero solo para -escribir.
#
#    Diseño (resuelto sin tener que preguntar al usuario): la frase
#    "-c para que cc sea mailsigned" no era ningún error ni texto
#    cortado — "mailsigned@egarante.com" ya era, desde antes de esta
#    ronda, la dirección real de eGarante (servicio español de
#    certificación/sellado de tiempo de correos: en copia, responde con
#    una prueba certificada del envío, útil ante reclamaciones formales),
#    ya wireada a -escribir mediante "-c". Lo único nuevo aquí es que
#    -responder pueda usar ese mismo "-c" — no hizo falta ninguna
#    aclaración porque el propio código ya explicaba qué era "mailsigned".
#
#    Uso:
#      python3 correo.py -acc gmx1 -responder id:XXXX -cuerpo /ruta/respuesta.txt
#      python3 correo.py -acc gmx1 -responder id:XXXX -responder-todos -texto "Recibido" \
#        -s "Asunto propio" -c -adjuntar /ruta/factura.pdf -bcc "jefe@empresa.com"
#
#    Cambios:
#      - _datos_respuesta(msg_original, modo_todos, asunto_manual,
#        direccion_propia, cc_extra, bcc_extra): función nueva, usada
#        tanto por el menú interactivo como por el comando directo, que
#        calcula (destinatario, cc, bcc, asunto, in_reply_to) a partir
#        del mensaje original. Por defecto (modo_todos=False, EL
#        COMPORTAMIENTO DE SIEMPRE): destinatario es solo el remitente.
#        Con -responder-todos: destinatario es el remitente MÁS los Para:
#        originales, cc son los CC: originales, y si el mensaje trae BCC:
#        (en la práctica, solo tus propias copias en Enviados — un correo
#        real tal como LLEGA nunca lo trae, el servidor se lo quita a
#        todo el mundo menos a quien lo escribió) también pasa a bcc.
#        cc_extra/bcc_extra (de -cc/-c/-bcc) se COMBINAN con todo lo
#        anterior, nunca lo sustituyen, y nunca duplican una dirección ya
#        puesta en otro campo. Nueva exclusión (antes no existía
#        NINGUNA): tu propia dirección nunca se incluye a ti mismo, ni en
#        destinatario ni en cc ni en bcc — si eso deja la respuesta sin
#        NINGÚN destinatario (p.ej. respondes, en modo normal, a un
#        mensaje tuyo sin nadie más), ahora se avisa con claridad en vez
#        de enviarte el correo a ti mismo en silencio.
#      - Asunto automático "Re: <el original>" — salvo que el original ya
#        empiece por "Re:" (no se duplica), o se dé uno manual con -s
#        (incluso "" en blanco a propósito, igual que ya admitía
#        -escribir).
#      - -cuerpo <fichero.txt>: ahora también en -responder, con la MISMA
#        prioridad que -escribir (-cuerpo > -texto > entrada estándar) y
#        el mismo aviso de "cuerpo vacío a propósito" si el fichero está
#        vacío (sin bloquear, se envía igual — Ronda G). OJO: se dejó
#        EXACTAMENTE intacta la asimetría ya existente y probada de la
#        Ronda G: -texto ""/entrada estándar vacía en -responder sigue
#        SIN avisar (a diferencia de -escribir) — no se "corrigió" esa
#        inconsistencia por no haberse pedido, y arreglarla habría
#        contradicho una decisión de diseño ya tomada a propósito.
#      - -adjuntar: ahora también en -responder (misma validación y
#        mismo formato de rutas, separadas por comas, que ya usaba
#        -escribir).
#      - Nuevo flag -bcc "a@x.com,b@y.com" (copia OCULTA/BCC), añadido a
#        -escribir Y a -responder a la vez, por simetría con -cc: se
#        añade como cabecera Bcc normal al mensaje en _enviar_correo, sin
#        nada más especial a propósito — smtplib.SMTP.send_message() ya
#        se encarga, sin ayuda de este programa, de (1) incluir a los
#        destinatarios de BCC en el envío de verdad (RCPT TO, calculado
#        de To+Cc+Bcc) y (2) transmitir el mensaje sin la cabecera Bcc
#        (nunca la ven ni los de To ni los de CC) — verificado leyendo
#        el propio código fuente de smtplib, no solo asumido. La copia
#        que se guarda en Enviados, al ser el mismo objeto "nuevo" tal
#        cual (nunca el que transmite send_message, que trabaja sobre
#        una copia propia), SÍ conserva la cabecera Bcc — igual que en
#        cualquier cliente de correo, ves tu propia lista de BCC en tu
#        carpeta de Enviados, aunque los destinatarios nunca la vean.
#      - Nuevo flag -responder-todos (booleano): ver _datos_respuesta
#        arriba.
#      - Menú interactivo (_responder_mensaje): todas las opciones
#        anteriores son OPCIONALES, agrupadas tras una única pregunta
#        inicial ("¿Opciones avanzadas?") — quien solo quiere responder
#        rápido, como siempre, no ve ni una pregunta de más; quien
#        contesta que sí, encadena 6 preguntas (responder a todos/asunto
#        propio/cuerpo desde fichero/adjuntos/CC/eGarante/BCC) antes de
#        llegar al cuerpo (si no se dio fichero) y la confirmación de
#        envío de siempre.
#      - Validación de -cuerpo/-adjuntar en -responder por comando
#        directo usa _error()+return, NUNCA sys.exit() — mismo patrón de
#        seguridad multi-acción ya establecido en la Ronda M para
#        -ver-img: -responder puede combinarse con otras acciones
#        (-ver, -descargar...) en el mismo comando, y un fallo ahí no
#        debe impedir que las demás se ejecuten.
#      - _enviar_correo gana el parámetro bcc= (nuevo) — cc= y adjuntos=
#        ya existían desde que -escribir los introdujo — y ahora los usa
#        también -responder, compartiendo exactamente la misma función
#        (y por tanto el mismo guardado en Enviados con fecha de las
#        Rondas H/L) que -escribir.
#
#    Verificado con test_ronda_n_responder_avanzado.py (34 tests):
#    _datos_respuesta (responder solo al remitente por defecto frente a
#    -responder-todos reuniendo De+Para+CC+BCC originales sin incluirme a
#    mí mismo ni duplicar direcciones; asunto automático "Re:"/sin
#    duplicarlo/manual incluso vacío; aviso en vez de "respuesta a mí
#    mismo" en silencio; in_reply_to ausente sin Message-ID original);
#    _enviar_correo con bcc= (cabecera puesta, vista previa la muestra
#    solo si se da); -responder por comando directo con -cuerpo (prioridad
#    sobre -texto, aviso si el fichero está vacío, error claro y SIN
#    enviar si no existe, sin impedir otras acciones del mismo comando),
#    -adjuntar, -responder-todos (con y sin combinarse con -cc/-bcc
#    manuales, que se COMBINAN, nunca sustituyen), -s (asunto propio,
#    incluso vacío), -cc/-c (eGarante)/-bcc, direcciones inválidas
#    rechazadas, y sin ninguna fuente de cuerpo; el menú interactivo
#    completo (avanzado con responder a todos+CC/eGarante/BCC+asunto
#    propio, cuerpo desde fichero sin llamar a _componer_respuesta, y
#    fichero de cuerpo inexistente cancelando sin pedir la confirmación
#    de envío); -escribir -bcc (regresión); y, en línea con el aviso
#    explícito del propio pedido ("no olvidez dejar copia en carpeta
#    enviados y que tenga fecha, pues anteriormente dio error en ese
#    sentido"), -responder-todos combinado con -bcc y -adjuntar a la vez
#    sigue guardando bien la copia en Enviados (con CC/BCC/adjunto/fecha
#    correctos) reutilizando la conexión ya abierta. Batería completa
#    (las 15 anteriores, 16 ficheros en total) — sin regresiones.
#
#    Manual 1 (sección 10.5, "Responder a uno") y Manual 2 (sección 5.8,
#    más referencias cruzadas en 5.11 y el ÍNDICE) actualizados.
#
# 62) -escribir -lista / -lista-fichero: el mismo correo a una lista de
#    direcciones, un envío independiente por cada una:
#    Pedido 2026-10-02 (verbatim): "anade la posibilida de enviar le mismo
#    cuerpo con los mismos adjuntos a una serie de correos, que para sea
#    una lista o un archivo de texto y poder enviar el mismo correo con
#    el mismo asunto, mismo cuerpo y mismos adjuntos a esa lista de
#    direcciones de correo electronico".
#
#    Aclaración necesaria antes de programar nada (pregunta explícita al
#    usuario): "el mismo correo a una lista" es ambiguo en un punto de
#    diseño importante y difícil de deshacer una vez elegido — si los
#    destinatarios se ven entre sí o no. Se preguntó entre tres opciones
#    (un correo por destinatario / un solo correo con todos en BCC / un
#    solo correo con todos en Para, viéndose entre sí) y el usuario eligió
#    la primera: UN CORREO INDEPENDIENTE POR DESTINATARIO — cada uno
#    recibe su propio mensaje con SOLO su dirección en "Para", sin ver a
#    los demás de la lista, y con visibilidad de éxito/fallo por
#    dirección (frente a un único envío que o falla entero o no).
#
#    Problema de rendimiento/corrección anticipado ANTES de programar
#    nada (no es un bug encontrado después, sino una consecuencia directa
#    de cómo ya funcionaba _enviar_correo): llamarla sin más, una vez por
#    destinatario de la lista, habría reconectado por SMTP Y por IMAP en
#    cada vuelta — incluyendo, si la contraseña no está guardada en
#    CUENTAS, volver a pedirla por cada destinatario (inviable con una
#    lista de cierto tamaño), amén de machacar al servidor SMTP con
#    conexiones repetidas en vez de una sola sesión autenticada.
#
#    Uso:
#      python3 correo.py -acc gmx1 -escribir -lista "a@x.com,b@x.com" \\
#        -s "Oferta" -cuerpo /ruta/oferta.txt -adjuntar /ruta/catalogo.pdf
#      python3 correo.py -acc gmx1 -escribir -lista-fichero /ruta/lista.txt \\
#        -s "Oferta" -cuerpo /ruta/oferta.txt
#
#    Cambios:
#      - Dos flags nuevos para -escribir, alternativas a -p (mutuamente
#        excluyentes entre sí y con -p — dan formas distintas de rellenar
#        "Para", y combinarlas sería ambiguo sobre qué destinatarios van
#        juntos en un mismo correo y cuáles por separado):
#          -lista "a@x.com,b@x.com,..."   direcciones separadas por comas,
#            igual que -p, pero activando el modo de envío independiente.
#          -lista-fichero <ruta.txt>      igual, leyendo las direcciones
#            de un fichero de texto, UNA por línea — las líneas en blanco
#            y las que empiecen por "#" se ignoran, para poder comentar
#            la lista (p.ej. separar tandas, anotar de dónde sale cada
#            bloque de direcciones). Sin soporte de comentario DETRÁS de
#            una dirección en la misma línea — a propósito, por simpleza
#            y para no arriesgarse a interpretar mal una dirección real.
#        En ambos, direcciones duplicadas (sin distinguir mayúsculas de
#        minúsculas) se quitan solas, avisando cuántas se quitaron — fácil
#        que pase en una lista grande mantenida a mano.
#      - -s/-cuerpo/-texto/-cc/-c/-bcc/-adjuntar: EXACTAMENTE el mismo
#        significado y validación que ya tenían con -p — se aplican
#        idénticos a cada envío individual de la lista (mismo asunto,
#        cuerpo y adjuntos para todos, como pedía el enunciado).
#      - _enviar_correo gana dos parámetros nuevos, pensados para
#        reutilizar conexiones ya abiertas en vez de abrir una por envío
#        (mismo espíritu que el parámetro imap= ya existente, que
#        -responder usa desde antes para no reconectar sobre la conexión
#        con la que ya encontró el mensaje original):
#          - smtp= (conexión SMTP YA abierta y autenticada): con ella
#            puesta, no se pide contraseña ni se abre/cierra ninguna
#            conexión SMTP dentro de la función — se envía por la
#            conexión dada tal cual, y queda abierta (es cosa de quien la
#            abrió cerrarla al terminar toda la tanda). Sin ella (como
#            hasta ahora, en -escribir de un destinatario o -responder),
#            el comportamiento es EXACTAMENTE el de siempre.
#          - guardar_enviados= (True por defecto — con eso el
#            comportamiento es el de siempre, decidido solo por
#            "save_sent" en CUENTAS): en False, nunca intenta guardar
#            copia en Enviados pase lo que pase — para cuando quien llama
#            ya intentó UNA vez conectar por IMAP para toda la tanda y
#            falló, y no tiene sentido que cada envío de la lista lo
#            vuelva a intentar (y a avisar) por su cuenta.
#      - Nueva función _abrir_smtp(cfg, nombre_cuenta): abre y autentica
#        una conexión SMTP exactamente igual a como _enviar_correo la
#        abriría ella sola por dentro (mismo puerto 465/587, mismo
#        starttls, mismo login) — pero pensada para abrirse UNA sola vez y
#        reutilizarse en varios envíos seguidos.
#      - La rama de -escribir en comando_directo, en modo -lista/-lista-
#        fichero: resuelve la contraseña y abre UNA sola conexión SMTP
#        (_abrir_smtp) y, si la cuenta no tiene "save_sent": False, UNA
#        sola conexión IMAP (si falla, se avisa una vez y se sigue sin
#        copia en Enviados, en vez de abortar el envío) — y recorre la
#        lista llamando a _enviar_correo una vez por destinatario,
#        reutilizando ambas conexiones (imap=/smtp=/guardar_enviados=)
#        en cada vuelta. Una pausa configurable entre envíos
#        (RETRASO_ENTRE_ENVIOS_LISTA, nueva constante en
#        leerCorreoConfiguracion.py, 2 segundos por defecto, 0 para
#        desactivarla) evita disparar límites antiabuso del proveedor.
#        Un fallo en un destinatario concreto (dirección rechazada por el
#        servidor, etc.) NO aborta el resto de la lista — se cuenta como
#        fallido y se sigue con el siguiente, reutilizando la misma
#        conexión. Al terminar toda la tanda: se cierran las dos
#        conexiones, se imprime un resumen (cuántos de cuántos se
#        enviaron bien, y la lista de direcciones fallidas si las hay), y
#        el código de salida es 0 SOLO si todos los envíos tuvieron
#        éxito (1 si falló alguno).
#
#    Verificado con test_ronda_o_envio_lista.py (13 tests): -lista inline
#    y -lista-fichero (ignorando líneas en blanco/comentarios, quitando
#    duplicados con aviso, fichero inexistente o sin direcciones de
#    verdad); un correo independiente por destinatario (cada mensaje real
#    lleva SOLO su propia dirección en "Para", nunca las demás de la
#    lista); reutilización de UNA sola conexión SMTP (incluida la
#    contraseña pedida una sola vez para toda la tanda, no una por
#    destinatario) y, como mucho, UNA sola conexión IMAP para las copias
#    en Enviados — incluyendo los dos casos que NO deben ni intentarlo:
#    "save_sent": False, y el fallo de conexión IMAP (se avisa UNA vez,
#    no una por envío — el problema anticipado antes de programar nada);
#    -cc/-c/-bcc/-adjuntar idénticos en cada envío; -lista/-lista-fichero
#    mutuamente excluyentes entre sí y con -p; dirección inválida en la
#    lista rechaza TODO antes de conectar o enviar nada; fallo parcial de
#    un destinatario (no aborta los demás, resumen y código de salida
#    agregado correctos); y regresión de -p (sigue mandando un único
#    correo con todos los destinatarios juntos en "Para", sin cambios).
#    Batería completa (las 16 anteriores, 17 ficheros en total) — sin
#    regresiones.
#
#    Manual 1 (sección 10, correo nuevo) y Manual 2 (sección 5.11 y el
#    ÍNDICE) actualizados.
#
# 63) Renombrado: leerCorreo.py -> correo.py:
#    Pedido 2026-10-02 (verbatim): "el py llamalo correo.py no
#    leerCorreo.py".
#
#    Puro cambio de nombre, sin ningún cambio de comportamiento: el
#    programa principal pasa a llamarse correo.py (antes leerCorreo.py).
#    Se han actualizado TODOS los ejemplos "python3 ..." de este propio
#    fichero (incluidos los de entradas de changelog anteriores, para que
#    cualquier ejemplo que se copie, de la ronda que sea, funcione tal
#    cual) y los dos manuales. leerCorreoConfiguracion.py NO se renombra
#    (no se pidió) — solo se corrigen, dentro de ese fichero, dos líneas
#    de comentario que mencionaban "leerCorreo.py" por su nombre, sin
#    tocar CUENTAS ni ningún valor ajustable.
#
#    A partir de esta ronda, el programa se arranca así:
#      python3 correo.py <cuenta>
#      python3 correo.py -acc gmx1 -last 20
#
#    Verificado: la batería completa (los 17 ficheros de test) sigue
#    pasando sin regresiones — se actualizó en cada uno la ruta de
#    spec_from_file_location (que apuntaba a .../leerCorreo.py) para que
#    siga cargando el programa real tras el renombrado.
#
# 64) Nuevo: -si-existe (qué hacer si el fichero de destino ya existe):
#    Pedido 2026-10-02 (verbatim): "anade una opcion para que, al
#    descargar txt, pdf, eml o adjuntos, si el archivo ya existe, se
#    pueda indicar en linea de comandos 1. si existe no sobreescribir,
#    2. si existe sobreescribir, 3. si existe sobreescribir txt, pdf y
#    eml pero no adjuntos, 4 si existe sobreescrbir solo adjuntos".
#
#    Hasta ahora, _ruta_sin_colision() solo sabía hacer una cosa cuando
#    el nombre ya existía: añadir _2, _3... y guardar aparte, sin tocar
#    nunca el fichero existente. Eso es exactamente lo que hace falta la
#    mayoría de las veces (dos correos distintos que coinciden de
#    nombre), pero no cuando se repite A PROPÓSITO la misma orden sobre
#    el mismo correo (por ejemplo, tras perder o estropear el .eml ya
#    bajado, o para refrescar un adjunto que cambió) — hasta ahora eso
#    solo podía acabar en copias _2/_3 acumulándose, nunca en sustituir
#    lo ya guardado.
#
#    Solo en modo comando directo (-ver-img y -csv quedan fuera de
#    alcance — no se pidieron, y -csv ni siquiera guarda un fichero por
#    mensaje). Cuatro valores, tal como los pidió el usuario:
#      -si-existe no            si ya existe, no se toca (se deja tal
#                                cual lo que ya había).
#      -si-existe sobrescribir  si ya existe, se sobrescribe siempre,
#                                sin avisar antes.
#      -si-existe correo        sobrescribe el .eml/.txt/.pdf si ya
#                                existe, pero deja tal cual los adjuntos
#                                que ya existan.
#      -si-existe adjuntos      sobrescribe los adjuntos que ya existan,
#                                pero deja tal cual el .eml/.txt/.pdf si
#                                ya existe.
#    Sin el flag, el comportamiento es IDÉNTICO al de siempre: numerar
#    _2, _3... — ningún caso existente cambia de comportamiento.
#
#    Problema de corrección anticipado ANTES de programar nada (no es un
#    bug encontrado después): -adjuntos ya avisaba "(el mensaje N no
#    tiene adjuntos)" cuando la lista de rutas guardadas salía vacía —
#    pero con -si-existe no/correo, la lista también puede salir vacía
#    porque los adjuntos SÍ existían y se omitieron a propósito, no
#    porque el mensaje no tuviera ninguno. _guardar_adjuntos() cambia su
#    contrato de "devuelve una lista" a "devuelve (rutas, hubo_alguno)"
#    — hubo_alguno distingue ambos casos, y el aviso ahora solo se
#    imprime cuando de verdad no había ningún adjunto.
#
#    Cambios:
#      - _ruta_sin_colision(ruta, accion="numerar") gana el parámetro
#        'accion': "numerar" (de siempre), "sobrescribir" (devuelve la
#        ruta tal cual) u "omitir" (devuelve None — señal de "no
#        escribas nada").
#      - Nueva _accion_si_existe(modo_si_existe, categoria): traduce el
#        modo de -si-existe más la categoría del fichero ("correo" para
#        .eml/.txt/.pdf, "adjunto" para un adjunto) a la 'accion' que
#        entiende _ruta_sin_colision.
#      - Nueva _validar_modo_si_existe(valor_bruto) (mismo patrón sin
#        sys.exit que _validar_n_pantallas, para no cortar en seco otras
#        acciones pedidas A LA VEZ en el mismo comando que no tengan
#        relación con -si-existe).
#      - guardar_crudo/_guardar_texto_completo_txt/
#        _guardar_texto_completo_pdf/_guardar_adjuntos ganan el
#        parámetro modo_si_existe="numerar" (el valor por defecto deja
#        a CUALQUIER llamador que no lo pase, incluido el menú
#        interactivo, exactamente como estaba). Cuando el fichero ya
#        existe y el modo pide omitirlo, imprimen "Ya existe, no se
#        sobrescribe: <ruta>" y no escriben nada.
#      - -si-existe en _FLAGS_CON_VALOR, y _ejecutar_una_accion_cli
#        valida y usa modo_si_existe DENTRO de cada una de las 4 ramas
#        afectadas (descargar/ver_txt/ver_pdf/adjuntos) — a propósito
#        ahí y no al principio de la función (donde se resuelve
#        modo_cabeceras): así, un valor inválido corta solo esas 4
#        acciones, nunca otra sin relación (-csv, -ver, -eliminar...)
#        pedida a la vez en el mismo comando.
#
#    Verificado con test_ronda_p_si_existe.py (8 tests): sin -si-existe,
#    las 4 acciones siguen numerando (_2) exactamente como siempre; los
#    4 modos, combinando siempre las 4 acciones a la vez sobre el mismo
#    mensaje (eml+txt+pdf+adjunto); valor inválido avisa y no guarda
#    nada, y NO bloquea -csv (sin relación) pedido en el mismo comando;
#    y el aviso "(el mensaje N no tiene adjuntos)" solo salta cuando de
#    verdad no hay ninguno, nunca cuando los había pero se omitieron.
#    Batería completa (las 17 anteriores + esta, 18 ficheros en total) —
#    sin regresiones.
#
#    Manual 2 (nueva sección y el ÍNDICE) actualizado.
#
# 65) Reconexión bajo -ds: una sola línea que se autosobreescribe:
#    Pedido 2026-10-03 (verbatim): "cuando pongo -ds y debas reconectar
#    limita a 1 linea y pon solo reconectando NombreDeCuenta y borra las
#    reconexiones anteriores. solo 1 linea".
#
#    Hasta ahora, CADA reconexión automática (el servidor cierra una
#    conexión por inactividad típicamente tras varios minutos; ver nota
#    38) imprimía su propio aviso largo y permanente: "La conexión con
#    'X' se había cerrado (probable inactividad); reconectando...", una
#    línea entera por cada cuenta que hiciera falta reconectar. Bajo -ds
#    (pensado precisamente para no llenar la pantalla — nota 44 del
#    progreso de la búsqueda, que ya usaba una sola línea
#    autosobreescrita), un comando largo con "-acc todas" y varias
#    cuentas quedándose inactivas una tras otra podía acabar mostrando
#    varias de estas líneas casi idénticas seguidas.
#
#    Ahora, SOLO bajo -ds, el aviso de reconexión usa el MISMO mecanismo
#    que el de progreso (\r + borrado hasta fin de línea, sin salto de
#    línea): "Reconectando: NombreDeCuenta" — cada reconexión nueva
#    sobreescribe la anterior en el mismo sitio, en vez de apilarse.
#    Sin -ds, el aviso largo de siempre no cambia NI UNA PALABRA
#    (regresión explícitamente comprobada). El fallo DE VERDAD — "No se
#    pudo reconectar con 'X'." — sigue siendo SIEMPRE su propia línea
#    permanente, con -ds o sin él; si había quedado abierta una línea
#    autosobreescrita (de esta reconexión o de una anterior), se cierra
#    primero con un salto de línea antes de ese aviso de fallo.
#
#    Cambios:
#      - _con_conexion_viva(imap, cfg, nombre_cuenta, estado_linea=None)
#        gana el parámetro opcional 'estado_linea' — el mismo dict
#        {"ds_abierta": ...} que ya usa _ejecutar_busqueda para el
#        progreso de -ds. Sin él (el valor por defecto, usado siempre
#        que no hay -ds), el comportamiento es IDÉNTICO al de siempre.
#      - _imap_vivo_para(conexiones, cuenta, imap_de_reserva,
#        estado_linea=None) traspasa el mismo parámetro.
#      - Dentro de _ejecutar_busqueda, las 3 llamadas a _imap_vivo_para
#        (el chequeo por fuente dentro del bucle principal, la
#        recuperación tras una excepción a mitad de ronda, y el bucle
#        final que resuelve el contenido completo) pasan su propio
#        estado_linea SOLO cuando modo_desatentido es cierto — nunca el
#        dict a secas, que existe siempre pero activaría el modo de una
#        línea incluso sin -ds.
#      - _plan_segun_folders(conexiones, es_todas, folders_arg,
#        modo_desatentido=False) gana ese nuevo parámetro y su propio
#        estado_linea (no el de _ejecutar_busqueda, que a esta altura
#        todavía no existe — _plan_segun_folders se ejecuta antes),
#        cerrado con un salto de línea al final de la función si se
#        había dejado abierto, para no afectar a lo que se imprima
#        después. Sus dos llamadores en comando_directo (la forma
#        standalone por ID, y la de -buscar/-id/-buscarfecha(s)/
#        -buscarcuerpo/-buscaradjunto*) le pasan modo_desatentido.
#
#    Verificado con test_ronda_q_reconexion_una_linea.py (8 tests): una
#    sola reconexión bajo -ds (línea exacta, sin el aviso largo); dos
#    reconexiones seguidas bajo -ds (sin ningún salto de línea entre
#    medias — la prueba directa de que se sobreescriben, no se
#    acumulan); sin -ds, el aviso largo de siempre sin cambios, cada
#    reconexión en su propia línea; el cierre correcto de la línea
#    cuando la reconexión termina fallando; lo mismo a través de
#    _imap_vivo_para y de _plan_segun_folders (con dos cuentas rotas de
#    tres, para probar el caso real de varias reconexiones seguidas); y
#    un comando completo (-acc todas -ds -duplicados) de punta a punta.
#    Batería completa (las 18 anteriores + esta, 19 ficheros en total) —
#    sin regresiones.
#
#    Manual 1 (la pregunta frecuente sobre este aviso) y Manual 2
#    (sección 5.13 y la nota sobre reconexión de la sección 9)
#    actualizados; de paso, se corrige en el Manual 2 que ese aviso
#    sale por salida normal, no por stderr como decía (nunca llevó
#    file=sys.stderr).
#
# 66) Búsqueda por fecha: que el propio servidor descarte lo que no
#    puede encajar, en vez de traer TODO el historial de cada carpeta:
#    Reportado 2026-10-05/07 (verbatim): "cuando busco correos en 1 dia
#    determinado, veo que no es eficiente [...] por ejemplo busco
#    seg-social en todas las cuentas, y por lo tanto en todas las
#    carpetas, en 1 dia del ano 2022 [...] despues de 30 minutos o 1
#    hora buscando no terminas de buscar en 1 fecha, y no comprendo
#    porque es tan lento ademas se reconecta frecuentemente a yahoo".
#
#    Diagnóstico (verificable leyendo el código, sin necesidad de
#    preguntar nada): TODAS las llamadas a imap.search() de todo el
#    programa -- incluida la que arma el plan para -buscarfecha(s), en
#    _plan_segun_folders -- usaban sin más "ALL": piden al servidor el
#    ID de TODO el correo histórico de cada carpeta, sin importar la
#    fecha pedida, y dejan que _ejecutar_busqueda los vaya mirando uno a
#    uno (cabecera a cabecera) para descartar por fecha casi todos. Con
#    una cuenta de años de antigüedad y -acc todas (Yahoo incluida), eso
#    es traer y mirar miles de mensajes para quedarse, al final, con los
#    de un solo día -- de ahí la lentitud, y de ahí también las
#    reconexiones frecuentes (cuanto más tarda una cuenta, más fácil que
#    el servidor cierre por inactividad la conexión de otra que todavía
#    espera turno -- ver nota 65).
#
#    Arreglo: _plan_segun_folders gana fecha_desde/fecha_hasta
#    (opcionales, datetime con zona horaria -- lo mismo que ya recibía
#    _ejecutar_busqueda) y, si se dan, usa SEARCH SINCE/BEFORE (RFC
#    3501) en vez de "ALL", para que sea el PROPIO SERVIDOR quien deje
#    fuera, de entrada, el correo sin ninguna posibilidad de encajar.
#    Nunca se arriesga a dejar fuera algo que sí debería contar: SINCE
#    se pide con 1 día de margen ANTES de fecha_desde, y BEFORE con 1
#    día de margen DESPUÉS de fecha_hasta (BEFORE ya es exclusivo, así
#    que "+2 días" deja un día entero de margen tras el límite) --
#    cubre de sobra cualquier diferencia entre cómo compara el SERVIDOR
#    (contra INTERNALDATE, en SU zona horaria) y la fecha exacta que de
#    verdad importa (la cabecera Date:, en hora de España). El filtro
#    EXACTO de siempre, dentro de _ejecutar_busqueda, no cambia ni una
#    línea -- este arreglo es SOLO de rendimiento (menos mensajes que
#    traer y mirar), nunca de qué cuenta como encontrado.
#
#    Cambios:
#      - Nueva _fecha_imap(fecha): formatea "DD-Mon-YYYY" con una tabla
#        de meses en inglés propia (no fecha.strftime("%b"), que
#        depende del locale activo del sistema y podría dar "ene"/"oct"
#        en vez de "Jan"/"Oct", que el servidor no reconocería).
#      - Nueva _criterio_search_segun_fechas(fecha_desde, fecha_hasta):
#        ("ALL",) si no se da ninguna fecha (ninguna búsqueda SIN fecha
#        cambia de comportamiento); si se da alguna, SINCE/BEFORE con el
#        margen de 1 día explicado arriba (se puede pedir solo un lado).
#      - _plan_segun_folders(..., fecha_desde=None, fecha_hasta=None):
#        usa ese criterio en su imap.search(), en vez de "ALL" fijo.
#      - Su llamador dentro de comando_directo para -buscar/-id/
#        -buscarfecha(s)/-buscarcuerpo/-buscaradjunto* le pasa
#        fecha_desde/fecha_hasta (ya calculadas ahí mismo); el otro
#        llamador (la forma standalone por ID, sección 4, que no tiene
#        fecha que acotar) sigue sin pasarlas, exactamente como antes.
#
#    Verificado con test_ronda_r_busqueda_fecha_eficiente.py (7 tests):
#    _fecha_imap/_criterio_search_segun_fechas a solas (formato, los 4
#    casos de fechas); _plan_segun_folders con fecha de un solo día en
#    una carpeta de 54 mensajes de los que solo 3 caen dentro del margen
#    ensanchado (el resto, lejísimos, quedan fuera ya en el "servidor");
#    la MISMA carpeta sin fecha sigue pidiendo "ALL" entero (regresión);
#    y de punta a punta por línea de comandos, -buscarfecha sigue
#    devolviendo EXACTAMENTE el día pedido (ni los días de al lado, que
#    sí entran en el margen ensanchado pero no en el filtro exacto de
#    después) con un candidato de servidor ya mucho más estrecho.
#    Batería completa (las 19 anteriores + esta, 20 ficheros en total) —
#    sin regresiones.
#
#    Manual 2 (nota de rendimiento añadida en la sección 3.8) -- Manual
#    1 no necesitaba ningún cambio (nada de lo que ya decía era
#    incorrecto; esto no cambia ninguna sintaxis ni ningún resultado,
#    solo lo rápido que se llega a él).
#
# 67) Nuevo: -listar-carpetas (carpetas de verdad vs. etiquetas de Gmail):
#    Pedido 2026-10-05/07 (verbatim): "crea un comando para listar, dada
#    una cuenta, lista toda la lista de carpetas que puede tener ten en
#    cuenta en correos gmail lo que son carpetas de verdad, de lo que son
#    etiquetas, si es posible o sea, la funcionalidad de gmail de
#    etiquetar correos con varias etiquetas y que al aplicacion python
#    recupera como si fueran carpetas pero no lo son".
#
#    Acción nueva y aparte (no selecciona ni actúa sobre ningún mensaje,
#    así que no se combina con -last/-buscar/-id/-buscarfecha(s)/
#    -buscarcuerpo/-buscaradjunto* ni con ninguna acción de la sección 5
#    del Manual 2 -- mismo patrón de conflicto que ya usaba -escribir).
#    Con -acc todas o varias cuentas, lista cada una por separado.
#
#    Distinción real vs. etiqueta (solo en cuentas Gmail -- se detecta
#    por el SERVIDOR configurado, gmail.com/googlemail.com, no por si
#    algún nombre de carpeta contiene la palabra "gmail", que dependería
#    del idioma de la cuenta): INBOX, o cualquier carpeta con un
#    atributo SPECIAL-USE de verdad (RFC 6154: \All, \Drafts, \Flagged,
#    \Junk, \Sent, \Trash -- Gmail añade \Important) en su línea LIST,
#    es "carpeta"; todo lo demás es "ETIQUETA" -- de sistema (Updates,
#    Social, Promotions) o creada por el propio usuario, Gmail expone
#    CADA etiqueta por IMAP exactamente como si fuera una carpeta más,
#    sin ningún atributo que la distinga de una real, que es justo la
#    confusión que pidió aclarar el usuario. En cualquier otro proveedor
#    (Yahoo, GMX, Hotmail/Outlook) no existe el concepto de etiqueta, así
#    que ahí nunca se marca nada como tal -- solo se listan, con
#    cualquier atributo especial que el servidor sí mande como dato.
#
#    Cambios:
#      - _parsear_linea_list() gana un 3er valor devuelto: los atributos
#        crudos de la línea LIST (antes se leían para comprobar
#        \Noselect y se tiraban) -- su única llamadora, listar_carpetas(),
#        los sigue ignorando igual que siempre (mismo contrato de
#        siempre para sus muchos llamadores).
#      - Nueva listar_carpetas_con_atributos(imap): como listar_carpetas,
#        con esos atributos como 4º elemento de cada carpeta.
#      - Nuevas _atributos_especiales/_es_cuenta_gmail/
#        _clasificar_carpeta_gmail/_imprimir_carpetas_cuenta: la lógica
#        de clasificación y de impresión, descrita arriba.
#      - -listar-carpetas en _FLAGS_SIN_VALOR, resuelto en comando_directo
#        justo después de conectar (antes de la selección por ID): si
#        hay conflicto, sys.exit igual que -escribir; si no, imprime y
#        termina, sin seguir con el resto de la función.
#
#    Verificado con test_ronda_s_listar_carpetas.py (4 tests): una cuenta
#    Gmail de verdad (con INBOX + las especiales de siempre + 2 etiquetas,
#    una con sub-etiqueta) separa bien carpeta de ETIQUETA; una cuenta
#    Yahoo no usa la palabra "etiqueta" en ningún momento, ni para sus
#    carpetas con atributo especial (Sent); -acc todas lista cada cuenta
#    por separado, cada una según SU propio proveedor; y combinarlo con
#    -last termina en error sin listar nada. Batería completa (las 20
#    anteriores + esta, 21 ficheros en total) — sin regresiones.
#
#    Manual 2 (nueva sección 5.18 y el ÍNDICE) actualizado. Manual 1 no
#    necesitaba cambios (acción nueva solo de comando directo, sin
#    constante de configuración nueva -- mismo criterio que -si-existe,
#    nota 64).
#
# 68) -ver-pdf: incrusta las imágenes del correo, en su sitio (Ronda T):
#    Pedido 2026-10-05/07 (verbatim): "cuando en un escrito se haya
#    puesto por parte de quien lo envio, se haya puesto una imagen
#    incrustada y yo genere un pdf con -ver-pdf no crees el pdf a partir
#    del txt sino que sea como cuando generas una imgen que pones tanto
#    el texto como la imagen del correo electronico porque ahora mismo
#    estas, al generar un pdf, estas simplemente generandolo a partir
#    del archivo de texto txt, olvidando las imagenes incrustadas en el
#    correo electronico ademas pon, en la generacion del pdf, pon en el
#    pdf todas las imagenes que se puedan ver en el correo, comparandolo
#    con la version imagen, si 1 imagen esta en la version imagen,
#    ponla en la version pdf pero en la misma ubicacion, para que se
#    vea de la misma forma, modo y aspecto".
#
#    Hasta ahora, _guardar_texto_completo_pdf generaba el PDF SOLO a
#    partir del texto (_generar_pdf_simple sobre _texto_completo_
#    formateado): cualquier imagen incrustada (cid:/data:) del correo
#    se perdía sin más. Ahora reutiliza la MISMA extracción de contenido
#    que -ver-img (_html_crudo_del_mensaje/_captura_flujo_desde_html/
#    _captura_flujo_desde_texto_plano/_resolver_imagen_para_captura) para
#    incrustar cada imagen EN SU SITIO, con el mismo tratamiento de
#    transparencia/redimensionado-al-ancho que -ver-img -- "misma forma,
#    modo y aspecto" pedido. Admite -con-remotas igual que -ver-img (por
#    defecto NO se descargan imágenes remotas).
#
#    A propósito, DISTINTO de -ver-img: el texto sigue siendo texto de
#    PDF de verdad (vectorial, seleccionable/copiable, no una captura),
#    para no regresionar los enlaces clicables de verdad que -ver-pdf ya
#    tenía desde el pedido 2026-09-29 (nota 56) -- verificado explícita-
#    mente que siguen funcionando. Las imágenes se incrustan como bytes
#    RGB en crudo comprimidos con zlib/FlateDecode (el propio formato
#    PDF lo soporta nativo, sin pasar por ningún formato de fichero de
#    imagen); una imagen más alta que una página se reparte en varias
#    páginas en vez de encogerse. Sin negrita en el texto (igual que
#    siempre); los enlaces sí se colorean como en -ver-img.
#
#    _generar_pdf_con_imagenes es independiente de _generar_pdf_simple
#    (no comparte código con ese generador ya probado, para no arriesgar
#    ninguna regresión en él): éste último queda como alternativa de
#    reserva -- si Pillow no está instalado, o si algo falla al incrustar
#    las imágenes, _guardar_texto_completo_pdf cae a él automáticamente,
#    avisando por qué, así que -ver-pdf nunca se queda sin generar nada.
#
#    Bug real encontrado y arreglado DURANTE esta misma ronda, al
#    inspeccionar visualmente (rasterizando con pdftoppm, y comprobando
#    también con qpdf --check/pdftotext/pdfinfo de forma independiente a
#    los tests propios) un PDF ya generado: con un ancho medio por
#    carácter (pensado solo para decidir saltos de línea con margen de
#    sobra, suficiente para _generar_pdf_simple porque ahí el lector de
#    PDF reparte el texto de una línea entera él solo), una palabra en
#    MAYÚSCULAS -- más ancha que la media en Helvetica, una fuente
#    proporcional -- se dibujaba más ancha de lo calculado y la
#    siguiente palabra se posicionaba ENCIMA de su final ("ANTESde" en
#    vez de "ANTES de"): aquí sí importa, porque cada palabra se
#    posiciona a mano (con Tm) para poder colorear/enlazar cada una por
#    separado. Arreglado con _PDF_ANCHOS_HELVETICA: la tabla AFM
#    ESTÁNDAR de Adobe para Helvetica (ASCII + acentos/eñes/diéresis
#    españoles y de otros idiomas europeos + símbolos de puntuación/
#    moneda habituales), obtenida del Helvetica.afm que trae matplotlib
#    (mismas métricas que usa cualquier lector de PDF para esta fuente
#    base), no de memoria ni inventada -- verificado que cubre, sin
#    ningún hueco, todo lo que _CAPTURA_FUENTE_REGULAR_B64 (-ver-img)
#    dice soportar.
#
#    Verificado con test_ronda_t_verpdf_imagenes.py (13 tests): imagen
#    incrustada en su sitio sin perder el texto de antes/después; sin
#    imágenes no añade ningún /XObject de más; enlace suelto y enlace
#    tipo botón siguen clicables de verdad (con la URI correcta) SIN
#    regresionar la nota 56; -cabeceras/-cabeceras-todas y la lista de
#    adjuntos siguen apareciendo; imagen remota bloqueada sin
#    -con-remotas e incrustada con ella (red simulada); imagen más alta
#    que una página se reparte en varias en vez de encogerse;
#    _pdf_trocear_imagen_alto reparte sin perder alto total y no toca
#    una imagen que ya cabe; sin Pillow (simulado) y ante cualquier otro
#    fallo al incrustar, cae al generador de solo texto avisando por
#    qué; y el bug de solapamiento de palabras anchas, cubierto
#    explícitamente para que no regresione. Batería completa (las 21
#    anteriores + esta, 22 ficheros en total) -- sin regresiones.
#
#    Manual 2 (sección 5.6 ampliada) actualizado. Manual 1 no necesitaba
#    cambios (las constantes nuevas -_PDF_ANCHOS_HELVETICA, _PDF_IMG_
#    HUECO...- viven en correo.py, no en leerCorreoConfiguracion.py).
# =====================================================================
#
# 69) -reanudable y -orden: búsqueda larga que se para y continúa más
#    tarde, descargando cada correo AL MOMENTO (Ronda U, pedido
#    2026-10-05/07, verbatim): "crea una funcionalidad para que al hacer
#    busquedas complejas en largos periodos de tiempo, en muchas cuentas
#    y carpetas, que puedas ir descargando cada correo encontrado,
#    descargarlo inmediatamente, y ademas, que el usuario pueda parar en
#    un momento y horas o dias mas tarde recomenzar en el punto en que
#    lo dejo, suponiendo que los correos de las cuentas y carpetas ya
#    analizadas, que no se volveran a analizar, sino mirando correos mas
#    antiguos, si comenzas por el dia mas antiguo de la serie de
#    fechadesde y fechahasta, y que se pueda indicar que comienze, de
#    entre las 2 fechas, que comienza en la fecha mas antigua o en la
#    fecha mas actual". Aclarado antes de implementar (AskUserQuestion):
#    reanudar tiene que ser AUTOMÁTICO -- repetir la MISMA orden detecta
#    el progreso sin terminar y continúa, sin ningún flag nuevo para
#    "reanudar" en sí.
#
#    Hasta ahora, toda búsqueda por comando directo (_ejecutar_busqueda)
#    reunía TODO antes de poder hacer nada con ello: si se buscaba en
#    muchas cuentas/carpetas con un rango de fechas amplio, no había
#    forma de ir guardando lo encontrado poco a poco, y parar a mitad
#    (Ctrl+C, quedarse sin batería, cerrar Termux) perdía TODO el
#    avance -- la siguiente vez había que volver a mirar absolutamente
#    todo desde el principio.
#
#    Nuevo flag -reanudable (junto con -buscar/-buscarcuerpo/
#    -buscarfecha(s)/-buscaradjunto* -- NO con -id ni -duplicados, ver
#    abajo). Solo admite -descargar como acción (o ninguna, que por
#    defecto hace lo mismo que -descargar -- a propósito distinto del
#    verbo por defecto habitual, -ver, porque aquí lo que de verdad pide
#    el usuario es ir guardando). Nueva función INDEPENDIENTE
#    _ejecutar_busqueda_reanudable (no comparte el reparto "por rondas"
#    de _ejecutar_busqueda -- aquí se analiza una carpeta ENTERA antes
#    de pasar a la siguiente, para que corresponda 1:1 con "esta
#    cuenta+carpeta ya está completa"): descarga cada correo que
#    coincide AL MOMENTO (vía guardar_crudo, igual que -descargar de
#    toda la vida) y guarda en disco, tras CADA descarga y tras
#    completar CADA carpeta (nunca solo al final), un progreso -- un
#    fichero .json por búsqueda distinta, en CARPETA_PROGRESO_BUSQUEDAS
#    (constante nueva en leerCorreoConfiguracion.py) -- con qué
#    cuentas+carpetas ya terminaron ("completa") y qué correos de las
#    que quedaron a medias ya se descargaron, identificados por su
#    id_amigable (estable, derivado del Message-ID -- a diferencia del
#    número de secuencia IMAP que usa el resto del programa, que puede
#    cambiar de una conexión a otra si entra o sale algún mensaje, así
#    que NUNCA se guarda él como referencia para más tarde).
#
#    Repetir después EXACTAMENTE la misma orden (tal como se aclaró que
#    debía funcionar) encuentra sola el progreso guardado -- _clave_
#    busqueda_reanudable calcula una firma estable a partir de todo lo
#    que decide QUÉ se busca (cuentas, -folders, texto/campos, rango de
#    fechas, filtro de adjuntos, -orden), sin importar el ORDEN en que se
#    escribieron los parámetros (irrelevante en todo el resto del
#    programa) ni lo que no cambia QUÉ se busca (-ds/-intervalo/-max/
#    -si-existe) -- y continúa: las cuentas+carpetas completas se saltan
#    enteras (ni se les pide el search), y dentro de una a medias, cada
#    mensaje ya descargado se reconoce por su id y no se vuelve a
#    guardar (sin duplicar ficheros _2/_3...). Si se interrumpe con
#    Ctrl+C, se captura y se avisa con claridad (sin traceback) de que el
#    progreso ya quedó guardado.
#
#    Nuevo -orden antiguos|recientes (solo junto con -reanudable, si no
#    el programa lo rechaza): dentro de cada carpeta, "recientes" (de
#    siempre) sigue siendo el más nuevo primero; "antiguos" invierte el
#    orden -- pedido para poder empezar por la punta más vieja de
#    fechadesde/fechahasta en vez de por la más nueva.
#
#    No se combina con -id (un mensaje concreto ya conocido no necesita
#    progreso guardado) ni con -duplicados (que necesita comparar TODOS
#    los mensajes reunidos entre sí -- incompatible con ir descargando y
#    olvidando cada uno al momento) ni con ninguna otra acción aparte de
#    -descargar -- en los tres casos, sys.exit con un mensaje claro, sin
#    dejar nada a medias ni ambiguo.
#
#    Una vez que una cuenta+carpeta queda "completa" para una búsqueda en
#    concreto, lo queda PARA SIEMPRE (mientras no cambie la firma de esa
#    búsqueda): a propósito, -reanudable es para terminar un trabajo
#    concreto y acotado, no para vigilar una carpeta de forma continua
#    (eso es un pedido DISTINTO del usuario, para -nuevos -- ver el
#    siguiente apartado del pedido original, todavía por implementar en
#    el momento de escribir esto).
#
#    Verificado con test_ronda_u_busqueda_reanudable.py (16 tests):
#    _validar_orden_busqueda y _clave_busqueda_reanudable a solas
#    (estable ante el orden/mayúsculas de -acc, distinta si cambia qué
#    se busca o -orden); guardar/cargar un checkpoint ida y vuelta (con
#    sus sets de ids) y cargar uno que no existe todavía; de punta a
#    punta por línea de comandos -- descarga al momento sin -descargar
#    explícito y marca bien las carpetas completas, repetir la misma
#    orden tras completarse no descarga nada de más, -orden antiguos
#    invierte el orden de descarga, una interrupción simulada (Ctrl+C a
#    mitad) deja progreso aprovechable y reanudar completa sin duplicar,
#    sin texto de búsqueda no repite el fetch de cuerpo completo de lo
#    ya descargado (solo el de cabecera), una carpeta sin ninguna
#    coincidencia se marca completa igual, con -acc todas una cuenta
#    nueva entre una vuelta y la siguiente suma trabajo sin repetir el ya
#    hecho, y -max (con reloj falso, determinista -- mismo patrón que
#    test_ds_intervalo_max.py) corta a media carpeta sin marcarla
#    completa y reanudar sin -max termina el resto sin duplicar; más las
#    validaciones (-id/-duplicados/otra acción/-orden sin -reanudable).
#    Además, comprobado a mano (fuera de la batería automática, por
#    timing real en vez de reloj falso) que -ds y -max/-intervalo juntos
#    con -reanudable no revientan y se comportan igual. Batería completa
#    (las 22 anteriores + esta, 23 ficheros en total) -- sin regresiones.
#
#    Manual 1 (sección 13: CARPETA_PROGRESO_BUSQUEDAS) y Manual 2 (nueva
#    sección 5.19 y el ÍNDICE, más referencias cruzadas desde 5.1 y
#    5.13) actualizados.
# =====================================================================
#
# 70) -nuevos: aviso de qué correos del INBOX son nuevos desde la última
#    vez, mostrados primero (Ronda V, pedido 2026-10-05/07, verbatim):
#    "añade una funcionalidad: si busco correos desde termux, dada una
#    cuenta de correo electrónico, revisa la carpeta inbox (únicamente
#    por ahora esa carpeta en cada cuenta); si busco correos electrónicos
#    nuevos en varios días distintos, deja registro de los resultados de
#    tal manera que puedas saber si existen resultados nuevos y de esta
#    manera ponerlos al principio". Este era el último punto que quedaba
#    de los 6 del pedido original (ver la nota del punto 69, más arriba,
#    que ya lo anunciaba como pendiente).
#
#    Aclarado con el usuario antes de implementar (AskUserQuestion, 3
#    preguntas): (1) un flag NUEVO y dedicado, -nuevos -- NO un cambio de
#    comportamiento automático de ninguna orden ya existente, siguiendo
#    el mismo criterio que todo el resto del programa (toda
#    funcionalidad nueva es opt-in, nunca cambia por sorpresa lo que ya
#    funcionaba); (2) se aplica solo a -descargar y/o -ver sobre el
#    INBOX, SIN -buscar/-id/-buscarfecha(s)/-buscarcuerpo/-buscaradjunto*
#    /-duplicados (tienen su propio sentido de "qué se busca", no el de
#    "lo último del INBOX que todavía no había visto"), sin -acc todas y
#    sin -folders all/todas (que miran MÁS que solo el INBOX -- de
#    momento, igual que pide el texto original, "únicamente por ahora
#    esa carpeta"); (3) de las 3 formas posibles de distinguir lo nuevo
#    (reordenar sin más / marcar cada uno con una etiqueta tipo "[NUEVO]"
#    / un resumen tipo "tienes 3 nuevos" + reordenar), se eligió
#    explícitamente la más simple: reordenar sin ninguna marca ni resumen
#    aparte.
#
#    Nuevo registro por cuenta (CARPETA_REGISTRO_NUEVOS, constante nueva
#    en leerCorreoConfiguracion.py -- un fichero .json por cuenta, p.ej.
#    registro_nuevos_inbox/gmx.json): un conjunto de id_amigable (el
#    mismo id estable, derivado del Message-ID, que ya usan -reanudable y
#    -duplicados -- nunca el número de secuencia IMAP, que puede cambiar
#    de una conexión a otra). _cargar_registro_nuevos/_guardar_registro_
#    nuevos siguen el mismo patrón que el checkpoint de -reanudable
#    (fichero temporal + os.replace para guardar de forma atómica; un
#    registro dañado se avisa y se trata como vacío -- como mucho hace
#    que todo salga como "nuevo" otra vez, nunca interrumpe nada).
#
#    _marcar_nuevos_y_reordenar(items) es la pieza central: recibe la
#    lista de mensajes ya con el cuerpo COMPLETO descargado (sin ninguna
#    llamada IMAP de más -- _navegar_carpeta_n y _recientes_todas_
#    cuentas_n, los dos caminos que ya existían para "últimos N del
#    INBOX" según se dé 1 cuenta o varias, siempre traen el RFC822
#    entero antes de imprimir), separa cada mensaje en "nuevo" o "ya
#    visto" según si su id_amigable estaba en el registro de SU cuenta
#    (recuperada de carpeta_salida, así que funciona igual con 1 cuenta o
#    con varias mezcladas a la vez, p.ej. "-acc gmx yahoo -nuevos"),
#    conservando el orden relativo de cada grupo (el de siempre, más
#    reciente primero) y anteponiendo los nuevos -- y de paso actualiza y
#    guarda el registro de CADA cuenta implicada con TODOS los ids de
#    este paso, para que ninguno vuelva a contar como nuevo la próxima
#    vez. Importante: esto reordena la VISTA, no cambia qué hace
#    -descargar/-ver -- si pides -descargar, se guarda igual TODA la
#    ventana de -last (de siempre; -descargar nunca ha evitado volver a
#    guardar un mensaje ya guardado antes, con o sin -nuevos -- para eso
#    está -si-existe, o -reanudable en el caso de una búsqueda acotada).
#
#    Como _navegar_carpeta_n/_recientes_todas_cuentas_n ya imprimían ellas
#    mismas la lista numerada (antes de que comando_directo pudiera
#    reordenarla), las dos ganan un parámetro nuevo imprimir_listado_final
#    (True de toda la vida, sin cambiar nada para quien no lo use): con
#    -nuevos, comando_directo les pide que NO impriman su listado,
#    reordena con _marcar_nuevos_y_reordenar, y vuelve a numerar/imprimir
#    él mismo (_reimprimir_tras_reordenar_nuevos) -- así el número que se
#    ve en pantalla para cada mensaje corresponde de verdad al orden YA
#    reordenado (si no, el número mostrado y la posición real para
#    -descargar/-ver con una selección concreta no coincidirían).
#
#    Validaciones propias de -nuevos (sys.exit claro, cerrando conexiones,
#    sin dejar nada a medias): cortan si se combina con cualquier filtro
#    de búsqueda (lo que activa modo_busqueda), si no se da -descargar ni
#    -ver (entraría al menú interactivo, que no reordena nada), si se da
#    alguna OTRA acción aparte de esas dos, si se usa con -acc todas, o si
#    se usa con -folders all/todas.
#
#    Verificado con test_ronda_v_correos_nuevos_inbox.py (14 tests):
#    guardar/cargar el registro ida y vuelta, cargar uno que no existe
#    todavía, y uno dañado (avisa y sigue, sin reventar); _marcar_nuevos_
#    y_reordenar a solas (antepone los nuevos conservando el orden
#    relativo de cada grupo, y registra todos); de punta a punta por
#    línea de comandos -- primera vez (descarga normal, crea el
#    registro), dos ejecuciones SEPARADAS detectan lo que llegó después y
#    el registro persiste entre ellas sin marcar nada como nuevo para
#    siempre, la reordenación es de verdad (un correo no visto sale
#    primero aunque NO sea el último en llegar -- no es solo que
#    coincida con el orden de llegada), con varias cuentas reordena
#    CRUZANDO cuentas (la cuenta nunca vista sale entera primero, aunque
#    su correo sea más antiguo que el de la cuenta ya vista) guardando el
#    registro de cada cuenta por separado, y que funciona también con
#    -ver; más las validaciones (con -buscar, sin ninguna acción, con
#    otra acción, con -acc todas, con -folders all). Batería completa
#    (las 23 anteriores + esta, 24 ficheros en total) -- sin regresiones.
#
#    Con esto quedan atendidos los 6 puntos del pedido original del
#    2026-10-05/07.
#
#    Manual 1 (sección 13: CARPETA_REGISTRO_NUEVOS) y Manual 2 (nueva
#    sección 5.20 y el ÍNDICE, más referencia cruzada desde 5.1)
#    actualizados.
# =====================================================================
#
# 71) Búsqueda de 1 día (-buscarfecha/-buscarfechas): filtro del servidor
#    por la fecha de la cabecera Date: (SENTSINCE/SENTBEFORE) en vez de
#    la de llegada al buzón (SINCE/BEFORE). Pedido 2026-10-09, verbatim:
#    "he hecho busquedas en inbox de una cuenta la gmx, y las he hecho
#    para 1 dia por ej 2023-08-01 y me dice que esta buscando en 1.600
#    mensajes. no creo que existan 1.600 mensajes para 1 cuenta para 1
#    dia en inbox de la cuenta gmx. podrias revisar el procedimiento
#    para buscar mensajes en 1 dia?".
#
#    Causa PROBABLE (no verificada contra el servidor real de GMX):
#    SINCE/BEFORE comparan la fecha de LLEGADA al buzón (INTERNALDATE),
#    no la fecha del correo. Si muchos mensajes se movieron/importaron
#    a la vez, comparten esa fecha y una ventana de 1 día los devuelve
#    todos. Ahora se prueba primero SENTSINCE/SENTBEFORE (fecha de la
#    cabecera Date:); si el servidor los rechaza, se reintenta con
#    SINCE/BEFORE (como antes). El filtro exacto posterior no cambia,
#    así que el resultado final es el mismo: solo cambia cuántos
#    mensajes se examinan. Funciones: _criterio_search_segun_fechas
#    (param usar_fecha_envio) y _buscar_ids_por_fechas. Tests nuevos en
#    test_ronda_r_busqueda_fecha_eficiente.py. leerCorreoConfiguracion.py
#    NO cambia en esta ronda.
# =====================================================================
#
# 72) Pantalla descolocada por caracteres invisibles/de control (pedido
#    2026-10-10): al listar los 10 últimos del INBOX de GMX, el mensaje
#    nº 2 "desaparecía" y en otros se leía "Bue¿os días" / "esoá
#    disponible" (captura de Termux+tmux; los mensajes eran de eBiblio,
#    Discord, Quora, InfoJobs).
#
#    Causa probable (por la captura; no se pudieron ver los bytes reales
#    de esos correos): los newsletters llevan en el asunto y sobre todo
#    en el cuerpo/"preheader" caracteres invisibles o de control (ancho
#    cero U+200B-200D, marcas bidi, guion blando U+00AD, BOM, U+034F,
#    rellenos Hangul, selectores de variación, ESC/BS...). El programa
#    los pasaba tal cual a print(); Python los cuenta como 1 carácter
#    pero tmux/Termux calculan mal la posición del cursor con ellos y
#    pintan las líneas siguientes ENCIMA de las anteriores: por eso la
#    línea 2 quedaba pisada (su texto "Tu reserva está disponible"
#    aparecía mezclado al final del mensaje 1) y salían letras
#    cambiadas. El .eml descargado NO se veía afectado (se guarda crudo).
#
#    Arreglo: _sanear_texto_pantalla() (NFC; quita controles, formato
#    Cf, rellenos invisibles, selectores de variación; espacios raros ->
#    espacio; \r\n/\r/U+2028 -> salto) aplicada a asunto, remitente/
#    destinatario (_limpiar_direccion, _direccion_compacta), cabeceras
#    (_limpiar_valor_cabecera) y cuerpo (_limpiar_cuerpo_texto: vista
#    previa, ver completo, .txt, .pdf, -ver-img). La BÚSQUEDA sigue
#    usando el texto sin limpiar. Tests: test_ronda_w_texto_invisible_
#    en_pantalla.py. Manual 1 (sección 12) actualizado.
#    leerCorreoConfiguracion.py NO cambia en esta ronda.
# =====================================================================
#
# 73) -ver-lista con -buscar mostraba el cuerpo entero; nuevos -ver-titulos
#    y -ver-ids (Ronda X, pedido 2026-10-19, verbatim): "si uso -ds
#    -ver-lista con -buscar al terminar no muestra una lista sino el
#    contenido por pantalla de cada correo. haz que solo muestre la lista,
#    o sea, el titulo ( id + origen/destino + asunto , y la 2da linea de
#    vista previa de 3 lineas ) nada mas. ademas, anade 2 comandos. 1.
#    mostrar solo el titulo [...] y otro 2. solo muestre el id en una
#    lista, tipo 1. Id1 2.Id2".
#
#    Causa: en _ejecutar_busqueda, _imprimir_item se llamaba SIN
#    presupuesto_preview; sin término en el cuerpo, vista_previa() devuelve
#    TODAS las líneas y formatear_preview(None) las junta todas -> el
#    cuerpo entero de cada resultado. (-ver-lista en sí no ejecutaba nada
#    más.) Ahora la búsqueda pasa presupuesto: LINEAS_CONTEXTO *
#    LARGO_LINEA_PREVIEW, o (2*LINEAS_CONTEXTO+1) * LARGO_LINEA_PREVIEW si
#    el término se encontró en el cuerpo (conserva el contexto).
#
#    Nuevo: -ver-titulos (alias -ver-titulo) y -ver-ids (alias -ver-id),
#    acciones sin valor como -ver-lista; el nivel de detalle lo fija
#    comando_directo en _ESTADO_LISTADO y lo aplica _imprimir_item (así
#    sirve para -last, búsquedas, varias cuentas...). -ver-ids gana a
#    -ver-titulos. Tests: test_ronda_x_listas_titulos_ids.py. Manual 2
#    (sección 5.4b, índice y reglas generales) actualizado.
#    leerCorreoConfiguracion.py NO cambia en esta ronda.
# =====================================================================
#
# 74) -aqui sin crear carpetas (Ronda Y, pedido 2026-10-22, verbatim):
#    "cuando utilizo -aqui aun asi creas la carpeta mails, y ademas creas
#    subcarpetas dentro de ella, no lo hagas pues utilizo -aqui".
#
#    Causa (hipótesis más probable; la salida de los correos con -aqui ya
#    iba bien al directorio actual): los ficheros de ESTADO propios,
#    _ruta_checkpoint_busqueda (-reanudable, CARPETA_PROGRESO_BUSQUEDAS) y
#    _ruta_registro_nuevos (-nuevos, CARPETA_REGISTRO_NUEVOS), hacían
#    os.makedirs de su carpeta configurada (y de las carpetas padre, si la
#    ruta era del tipo mails/progreso_busquedas) con solo CONSULTAR la ruta,
#    ignorando -aqui. Ahora: _ruta_fichero_de_estado -- con -aqui el
#    fichero va oculto al directorio actual (.progreso_busqueda_<hash>.json,
#    .registro_nuevos_<cuenta>.json) sin crear nada; sin -aqui, igual que
#    antes. Consultar la ruta ya NO crea carpetas: quien guarda las crea
#    justo antes de escribir (_asegurar_carpeta_de).
#
#    Fallo relacionado, encontrado al probar: con -aqui, -nuevos
#    identificaba la cuenta por el nombre del directorio actual
#    (_cuenta_de_carpeta_salida usaba el último trozo de la ruta) y
#    guardaba el registro bajo ese nombre, y varias cuentas se mezclaban.
#    Ahora _carpeta_salida_para_cuenta devuelve (con -aqui) un str
#    especial (_CarpetaDeCuenta) que recuerda su cuenta.
#    Tests: test_ronda_y_aqui_sin_carpetas.py. Manual 2 (5.12, 5.19, 5.20)
#    actualizado. leerCorreoConfiguracion.py NO cambia en esta ronda.
# =====================================================================
#
# 75) Muchas cuentas en la 2ª pasada + nueva opción -json (Ronda Z, pedido
#    2026-11-03, verbatim): "crea una nueva opcion para que envie por
#    stdout en formato json, toda la informacion de los correos. [...]
#    Asimismo revisa el caso de que pidan los 10, 20 o 30 ultimos correos
#    cuando tengo 10 cuentas o 15 cuentas introducidas en el archivo de
#    configuracion porque una vez hace la 1ra pasada, al volver a leer
#    correos en la 2da pasada, ve que las cuentas estan desactivadas, y
#    algunas veces da error al intentar reactivar."
#
#    A) Reconexión con muchas cuentas. Causas encontradas en el código:
#     - conectar() leía cfg["password"] en cada llamada: con "password": ""
#       cada RECONEXIÓN volvía a pedir la contraseña (getpass), 15 veces, y
#       sin terminal interactiva fallaba. Ahora se recuerda en memoria
#       (_PASSWORDS_EN_SESION; nunca a disco).
#     - _refrescar_conexiones reconectaba TODAS las muertas seguidas, sin
#       reintentos ni pausas (límites antiabuso del proveedor), y ante UN
#       fallo BORRABA la cuenta de la sesión para siempre ("desactivada").
#       Ahora: _conectar_con_reintentos (2 reintentos con pausas 2 y 5 s,
#       solo ante fallos TEMPORALES -- _es_error_transitorio_imap; una
#       contraseña mala no se reintenta), pausa de 1 s entre reconexiones y,
#       en la sesión interactiva (_ConexionesSesion), las cuentas caídas se
#       anotan en .caidas y se reintentan en la pasada siguiente.
#     - Sin límite de espera: una conexión medio muerta (típico al dormirse
#       Termux) colgaba el NOOP de comprobación. Ahora timeout de 30 s para
#       conectar (se quita después) y 20 s para el NOOP (_noop_con_limite).
#     - Las conexiones muertas no se cerraban al sustituirlas (se acumulaban
#       abiertas en el servidor): _cerrar_imap_rapido.
#     - Un IMAP4.abort (conexión cortada) se mostraba como "Error de
#       autenticación": ahora se distingue (fallo temporal).
#    Constantes al principio de la sección (no en leerCorreoConfiguracion.py).
#    Tests: test_ronda_z_muchas_cuentas_reconexion.py (incluye 15 cuentas, 2
#    pasadas, con todas las conexiones muertas entre ellas).
#
#    B) -json: acción sin valor que se ejecuta la última y escribe por
#    stdout UN documento JSON (versión 1) con, por mensaje: posición, id,
#    cuenta, carpeta, message_id, fechas, de/para/cc/bcc/responder_a
#    (cabecera completa y lista nombre+correo), asunto, cuerpo_txt, tamaño,
#    adjuntos (nombre, tipo, tamaño, dónde se guardó) y ficheros{eml, txt,
#    pdf, img[]} con ruta absoluta, carpeta, nombre y estado (guardado |
#    ya_existia), y eliminado. Para que stdout sea SOLO JSON, comando_directo
#    es ahora un envoltorio que, con -json, redirige todo lo demás a stderr
#    (el cuerpo de siempre está en _comando_directo_interno). Las rutas las
#    anota _registrar_fichero desde guardar_crudo, _guardar_texto_completo_
#    txt/pdf, _guardar_capturas_pantalla y _guardar_adjuntos (solo con
#    -json activo). Con -nuevos se puede usar; con -reanudable no.
#    Tests: test_ronda_z_json.py. Manual 1 (sección 3 sobre inactividad y
#    sección 15) y Manual 2 (5.21, índice, reglas, sección 8) actualizados.
#    leerCorreoConfiguracion.py NO cambia en esta ronda.
#
# 76) Imágenes incrustadas, OCR y tablas -> CSV (Ronda AA, pedido 2026-11-03)
#    Un correo trajo una tabla de Excel pegada como IMAGEN INCRUSTADA en el cuerpo (no
#    adjunta, por eso -adjuntos/-ver-txt no la veían). Nuevos modificadores, opt-in, para
#    -ver-txt y -adjuntos: -imagenes (guarda las imágenes incrustadas como
#    <correo>_img_N_<nombre>.<ext>; salta las de <3000 bytes o <40 px = firmas/píxeles de
#    seguimiento; añade una sección "IMÁGENES INCRUSTADAS" al .txt), -ocr (tesseract: texto
#    en <img>_ocr.txt y en el .txt del correo), -tabla (reconstruye la tabla y la guarda como
#    <img>.csv, con -sep: ; por defecto, tab, coma, | ...; CSV en UTF-8 con BOM para Excel) y
#    -describir (modelo de visión local tipo SmolVLM; en inglés, lento; solo para fotos/carteles).
#    Tablas: se binariza la imagen (x3), tesseract TSV (palabras con posición), columnas por las
#    líneas verticales de la rejilla (si no hay, por huecos entre palabras), filas por altura.
#    NO se usa un modelo de IA para las celdas: SmolVLM-256/500 inventa cifras y no da posiciones.
#    Herramientas opcionales (aviso único si faltan): tesseract, Pillow (sin numpy). Constantes
#    OPCIONALES leídas con getattr de leerCorreoConfiguracion: OCR_IDIOMAS, VISION_CMD,
#    VISION_PROMPT. -json añade ficheros.imagenes_incrustadas/ocr/tablas_csv/descripciones.
#    Añadido (2026-11-03): -ocr une las palabras partidas con guion a final de línea (prensa a
#    columnas) con _unir_guiones_castellano: solo si antes del guion hay letra y la línea
#    siguiente empieza en minúscula (Madrid-/Barajas y 2020-/2021 no se tocan).
#    Tests: test_ronda_aa_imagenes_incrustadas.py (10). Manual 2 (sección 5.22, índice, reglas).
#    leerCorreoConfiguracion.py NO cambia en esta ronda.
#
# 77) -responder usa "Responder a" (Reply-To) (Ronda AB, pedido 2026-11-04)
#    Si el correo trae cabecera Reply-To con dirección distinta de la propia, -responder (y la
#    respuesta del menú) va a ESA dirección en vez de al De:; con -responder-todos, Reply-To
#    sustituye al De: y se le suman Para/CC como siempre. Si falta, está vacía, no se puede
#    leer o es solo tu dirección, se responde al De: igual que antes. -sin-reply-to (nuevo)
#    fuerza responder al De:; el menú lo pregunta en las opciones avanzadas. Se avisa por
#    stderr cuando la respuesta NO va al De:. Tests: test_ronda_ab_reply_to.py.
#    leerCorreoConfiguracion.py NO cambia en esta ronda.
#
# 78) Error "command FETCH illegal in state AUTH" tras una búsqueda larga (Ronda AC, 2026-11-05)
#    -buscar ... -max 30m terminó por tiempo y, al "Preparar vista previa", reconectó (la
#    conexión llevaba mucho rato muerta) y falló con imaplib.IMAP4.error: FETCH solo vale con
#    una carpeta abierta (SELECTED) y la conexión nueva no tenía ninguna; la fase final creía
#    que la carpeta seguía abierta porque su recuerdo era por cuenta, no por conexión. Arreglo
#    en la raíz: _seleccionar_carpeta anota en la conexión su carpeta abierta y
#    _con_conexion_viva la reabre tras reconectar (vale también para la reconexión a mitad de
#    una ronda de la búsqueda, y para -nuevos/-reanudable); la vista previa comprueba la
#    carpeta de la propia conexión. Tests: test_ronda_ac_reconexion_reselecciona.py.
#    leerCorreoConfiguracion.py NO cambia en esta ronda.
#
# 79) -nuevos mostraba SIEMPRE los mismos correos como nuevos (Ronda AD, 2026-11-05)
#    Causa: CARPETA_REGISTRO_NUEVOS (y CARPETA_PROGRESO_BUSQUEDAS) son rutas relativas y se
#    resolvían contra el directorio desde el que se lanza el programa: llamándolo desde otra
#    carpeta (p. ej. una por fecha) se creaba un registro nuevo y vacío cada vez. Ahora las
#    rutas relativas cuelgan de la carpeta de correo.py (_DIR_PROGRAMA); con -aqui el registro
#    de -nuevos va oculto junto a correo.py (no al directorio actual); lo que ya hubiera en el
#    sitio antiguo se copia solo (migración). -nuevos imprime por stderr una línea de
#    diagnóstico por cuenta: [-nuevos] cuenta: N nuevos de T; registro: <ruta> (...).
#    Tests: test_ronda_ad_nuevos_ruta_fija.py. leerCorreoConfiguracion.py NO cambia.
#
# 80) -solo-nuevos, y -nuevas / parámetros mal escritos (Ronda AE, 2026-11-05)
#    Quejas: "vuelve a mostrar los mismos correos" y "gmx.json no cambia". Era el diseño de -nuevos
#    (pedido 2026-10-07: reordenar, nuevos primero): muestra los últimos N de siempre; y el registro
#    solo cambia si llegan correos nuevos. Nuevo -solo-nuevos (implica -nuevos): muestra y aplica
#    la acción SOLO a los nuevos ("No hay correos nuevos..." si no hay). El encabezado de -nuevos
#    dice ahora cuántos son nuevos. -nuevas (errata) vale como -nuevos; cualquier otro "-algo"
#    desconocido tras -acc ya no se toma en silencio como cuenta: se avisa con sugerencia.
#    Tests: test_ronda_ae_solo_nuevos.py. leerCorreoConfiguracion.py NO cambia.
# =====================================================================
