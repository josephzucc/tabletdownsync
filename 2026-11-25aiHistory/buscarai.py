#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
buscarai.py - Buscador interactivo del historial exportado de varias IA:
    Claude (chats, sesiones de trabajo en la nube, proyectos, memoria),
    ChatGPT, DeepSeek, Gemini y Keep (Google Takeout), Lumo, Duck.ai,
    búsquedas guardadas por duck.py y, en modo genérico, Kimi y otras.

Uso en Termux (el manual completo está en buscaraiManual.txt):
    python buscarai.py                          (busca solo la exportación)
    python buscarai.py -d                       (descarga todas las partes de Claude)
    python buscarai.py conversations-000.zip    (abre también las demás partes)
    python buscarai.py archivo.zip -u           (¿está actualizado?)
    python buscarai.py -x                       (extrae todos los archivos)
    python buscarai.py --diagnostico            (informe para Claude)

Solo usa la librería estándar de Python: no hay que instalar nada.
"""

import argparse
import array
import base64
import bisect
import glob
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import zipfile
from collections import Counter
from datetime import datetime
from html.parser import HTMLParser

VERSION = "3.7"


# ----------------------------------------------------------------------------
# Colores
# ----------------------------------------------------------------------------
COLOR = sys.stdout.isatty()


def _c(code):
    return lambda s: f"\033[{code}m{s}\033[0m" if COLOR else str(s)


NEG = _c("1")
GRIS = _c("2")
CIAN = _c("1;36")
VERDE = _c("1;32")
AMAR = _c("1;33")
ROJO = _c("1;31")
MAG = _c("1;35")
RESAL = _c("1;30;43")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def sin_ansi(s):
    return ANSI_RE.sub("", s)


# ----------------------------------------------------------------------------
# Normalización (sin tildes, minúsculas, misma longitud que el original)
# ----------------------------------------------------------------------------
def _tabla():
    t = {}
    for cp in range(0x250):
        ch = chr(cp)
        if ch in "ñÑ":
            if ch == "Ñ":
                t[cp] = "ñ"
            continue
        base = "".join(x for x in unicodedata.normalize("NFD", ch)
                       if not unicodedata.combining(x)).lower()
        if len(base) == 1 and base != ch:
            t[cp] = base
    return t


TABLA = _tabla()


def normalizar(s):
    n = s.translate(TABLA)
    low = n.lower()
    if len(low) == len(n):
        return low
    return "".join(ch.lower() if len(ch.lower()) == 1 else ch for ch in n)


def parsear_consulta(q, exacto=False, frase=False):
    """Devuelve [(término, es_frase)]. exacto = respetar mayúsculas y tildes; frase = todo es una frase."""
    for a, b in (("“", '"'), ("”", '"'), ("«", '"'), ("»", '"'), ("'", '"')):
        q = q.replace(a, b)
    conv = (lambda x: x) if exacto else normalizar
    if frase:
        t = conv(q.replace('"', " ").strip())
        return [(t, True)] if t else []
    out = []
    for fr, palabra in re.findall(r'"([^"]+)"|(\S+)', q):
        t = conv((fr or palabra).strip())
        if t:
            out.append((t, bool(fr)))
    return out


def compilar(termino, palabra_completa):
    if isinstance(termino, tuple):
        termino, es_frase = termino
        palabra_completa = palabra_completa or (es_frase and " " in termino)
    cuerpo = r"\s+".join(re.escape(w) for w in termino.split())
    if palabra_completa:
        cuerpo = r"(?<!\w)" + cuerpo + r"(?!\w)"
    return re.compile(cuerpo)


# ----------------------------------------------------------------------------
# Fechas: admite ISO ("2026-09-20T10:00:00Z"), segundos o milisegundos Unix
# ----------------------------------------------------------------------------
def fecha(v):
    if v is None or v == "" or isinstance(v, bool):
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.astimezone()
    try:
        if isinstance(v, str) and re.fullmatch(r"\d+(\.\d+)?", v.strip()):
            v = float(v)
        if isinstance(v, (int, float)):
            if v <= 0:
                return None
            if v > 1e14:
                v /= 1e6
            elif v > 1e11:
                v /= 1000.0
            return datetime.fromtimestamp(v).astimezone()
        if isinstance(v, str):
            s = v.strip().replace("Z", "+00:00").replace(" ", "T", 1)
            s = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], s)
            d = datetime.fromisoformat(s)
            return d.astimezone() if d.tzinfo else d.astimezone()
    except Exception:
        return None
    return None


def primera_fecha(d, claves):
    for k in claves:
        if isinstance(d, dict) and k in d:
            f = fecha(d.get(k))
            if f:
                return f
    return None


def ts(d):
    return d.timestamp() if d else 0


def fmt_fecha(d):
    return d.strftime("%Y-%m-%d %H:%M") if d else "????-??-?? --:--"


def fmt_dia(d):
    return d.strftime("%Y-%m-%d") if d else "????-??-??"


def hace(d):
    if not d:
        return "fecha desconocida"
    ahora = datetime.now().astimezone()
    seg = (ahora - d).total_seconds()
    if seg < 0:
        return "en el futuro (¿reloj?)"
    dias = int(seg // 86400)
    if dias == 0:
        h = int(seg // 3600)
        return "hace menos de 1 hora" if h == 0 else f"hace {h} hora(s)"
    if dias == 1:
        return "hace 1 día"
    return f"hace {dias} días"


# ----------------------------------------------------------------------------
# Modelo de datos
# ----------------------------------------------------------------------------
class Parte:
    __slots__ = ("texto", "norm", "inicios")

    def __init__(self, texto):
        self.texto = texto
        self.norm = normalizar(texto)
        self.inicios = [0] + [m.end() for m in re.finditer("\n", texto)]

    def linea_de(self, pos):
        return bisect.bisect_right(self.inicios, pos) - 1


class Intercambio:
    """Una pregunta tuya + la respuesta de la IA."""
    __slots__ = ("preg", "resp", "fecha")

    def __init__(self, preg, resp, f):
        self.preg = Parte(preg)
        self.resp = Parte(resp)
        self.fecha = f



class Conversacion:
    __slots__ = ("titulo", "fecha", "actividad", "inter", "fuente", "voz", "doc", "archivos")

    def __init__(self, titulo, f, act, inter, fuente, voz=None, doc=False):
        self.titulo = titulo
        self.fecha = f
        self.actividad = act
        self.inter = inter
        self.fuente = fuente
        self.voz = voz or fuente      # cómo se llama a quien responde (CLAUDE, CHATGPT...)
        self.doc = doc                # True = documento (proyecto, memoria...), no un chat
        self.archivos = []


def limpiar(t):
    return (t or "").replace("\r\n", "\n").replace("\r", "\n").strip()


# ----------------------------------------------------------------------------
# Catálogo de archivos: creados/modificados por Claude, subidos por ti,
# documentos de proyectos y memoria
# ----------------------------------------------------------------------------
ORIGENES = {
    "creado": "Creado por Claude",
    "artefacto": "Artefacto de Claude",
    "subido": "Subido por ti",
    "leido": "Leído por Claude",
    "proyecto": "Documento de proyecto",
    "memoria": "Memoria de Claude",
    "export": "Otro archivo de la exportación",
    "gemini": "Archivo de Gemini",
    "keep": "Nota o adjunto de Keep",
    "takeout": "Otro archivo de Google Takeout",
}
EXT_TEXTO = (".txt", ".md", ".markdown", ".py", ".js", ".mjs", ".ts", ".json", ".jsonl", ".html", ".htm",
             ".css", ".csv", ".tsv", ".xml", ".yaml", ".yml", ".sh", ".bash", ".ini", ".toml", ".cfg",
             ".log", ".sql", ".java", ".c", ".h", ".cpp", ".cs", ".vb", ".bas", ".ps1", ".bat", ".svg",
             ".rst", ".tex", ".srt", ".vtt", ".env", ".conf")
EXT_MEDIA = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp",
             "application/pdf": ".pdf"}


class Archivo:
    __slots__ = ("nombre", "ruta", "origen", "versiones", "b64", "conv", "msg", "ei", "fecha",
                 "parcial", "sin_contenido", "entregado", "tipo_media", "ref", "tam_ref")

    def __init__(self, ruta, origen, fecha_, texto=None, b64=None, tipo_media=None):
        self.ruta = ruta or "sin_nombre"
        self.nombre = os.path.basename(self.ruta.rstrip("/")) or self.ruta
        self.origen = origen
        self.versiones = []           # [(fecha, texto, descripción)]
        self.b64 = b64                # contenido binario (imágenes, PDF) en base64
        self.tipo_media = tipo_media
        self.conv = None
        self.msg = 0
        self.ei = 0
        self.fecha = fecha_
        self.parcial = False
        self.sin_contenido = texto is None and b64 is None
        self.entregado = False
        self.ref = None               # (ruta del .zip, nombre interno): binario que se lee solo al guardarlo
        self.tam_ref = 0
        if texto is not None:
            self.versiones.append((fecha_, texto, "original"))

    @property
    def texto(self):
        return self.versiones[-1][1] if self.versiones else ""

    def tam(self):
        if self.ref:
            return self.tam_ref
        if self.b64:
            return len(self.b64) * 3 // 4
        return len(self.texto.encode("utf-8", "replace"))


class Registro:
    """Guarda todos los archivos y aplica las ediciones de Claude para reconstruir
    la versión final de cada uno."""

    def __init__(self):
        self.lista = []
        self.por_ruta = {}            # ruta -> [Archivo...] (el último es el vigente)

    def nuevo(self, ruta, origen, fecha_, texto=None, b64=None, tipo_media=None, ctx=None, mismo_conv=True):
        clave = (ruta or "").strip()
        previos = self.por_ruta.get(clave, [])
        # Reescritura del mismo archivo en la misma conversación = nueva versión
        if texto is not None and previos and ctx is not None and mismo_conv:
            ult = previos[-1]
            if ult.conv is None and ult in ctx.archivos and ult.origen == origen and ult.versiones:
                if ult.texto != texto:
                    ult.versiones.append((fecha_, texto, "reescrito"))
                return ult
        a = Archivo(ruta, origen, fecha_, texto, b64, tipo_media)
        if ctx is not None:
            a.msg = ctx.idx
            ctx.archivos.append(a)
        self.lista.append(a)
        self.por_ruta.setdefault(clave, []).append(a)
        return a

    def editar(self, ruta, viejo, nuevo, todos, fecha_, ctx):
        """Aplica old_str -> new_str sobre la última versión conocida. Devuelve True si pudo."""
        cands = self.por_ruta.get((ruta or "").strip(), [])
        for a in reversed(cands):
            t = a.texto
            if a.versiones and isinstance(viejo, str) and viejo and viejo in t:
                t2 = t.replace(viejo, nuevo, -1 if todos else 1)
                a.versiones.append((fecha_, t2, "editado"))
                if ctx is not None and a not in ctx.archivos and a.conv is not None:
                    ctx.editados.add(a)
                return True
        return False


# ----------------------------------------------------------------------------
# Lectura de los bloques de Claude (chats y sesiones de trabajo)
# ----------------------------------------------------------------------------
# Lo que pone la exportación en lugar de los archivos y artefactos:
RE_MARCADOR = re.compile(
    r"^\s*`{0,3}\s*(This block is not supported on your current device( yet)?\.?|"
    r"Este bloque (aún|todavía) no (es compatible|está disponible|está preparado)[^\n]*)\s*`{0,3}\s*$",
    re.I | re.M)
PERDIDO = "[Aquí había un archivo o artefacto, pero esta exportación no incluye su contenido]"
RE_RECORDATORIO = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
RE_LINEA_CAT = re.compile(r"^ *(\d+)\t(.*)$")
NOMBRES_LECTURA = ("read", "view", "glob", "grep", "ls", "notebookread", "file-reading")
NOMBRES_INTERNOS = ("taskcreate", "taskupdate", "taskget", "tasklist", "todowrite", "toolsearch",
                    "skill", "memory_read", "memory_list", "memory_view", "conversation_search",
                    "recent_chats", "read_conversation", "current_time", "schedulewakeup", "listagents")

ESTAD = Counter()                     # estadísticas para el diagnóstico


class Ctx:
    """Estado mientras se lee una conversación."""

    def __init__(self, opts, registro):
        self.opts = opts
        self.reg = registro
        self.idx = 0                  # nº de mensaje en curso
        self.fecha = None
        self.herr = {}                # id de herramienta -> (nombre, input)
        self.archivos = []
        self.editados = set()


def caja(titulo, texto, pie=None):
    t = f"┌─ {titulo} "
    return f"{t}{'─' * max(3, 58 - len(t))}\n{texto.rstrip()}\n└─ {pie or 'fin'}"


def quitar_marcadores(t):
    n = len(RE_MARCADOR.findall(t or ""))
    if n:
        ESTAD["marcadores_quitados"] += n
        t = RE_MARCADOR.sub("", t)
    return t


def texto_de_contenido(c):
    """Texto de un 'content' que puede ser str o lista de bloques."""
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        out = []
        for b in c:
            if isinstance(b, str):
                out.append(b)
            elif isinstance(b, dict):
                if isinstance(b.get("text"), str):
                    out.append(b["text"])
                elif b.get("type") == "image":
                    out.append("[Imagen]")
                elif isinstance(b.get("content"), (str, list)):
                    out.append(texto_de_contenido(b["content"]))
        return "\n".join(out)
    if isinstance(c, dict):
        return texto_de_contenido(c.get("content") or c.get("text"))
    return ""


def imagenes_de(c):
    """Devuelve [(tipo_media, base64)] de los bloques de imagen/documento con datos."""
    out = []
    if isinstance(c, list):
        for b in c:
            if isinstance(b, dict):
                src = b.get("source")
                if isinstance(src, dict) and src.get("type") == "base64" and isinstance(src.get("data"), str):
                    out.append((src.get("media_type") or "application/octet-stream", src["data"]))
                elif isinstance(b.get("content"), list):
                    out += imagenes_de(b["content"])
    return out


def es_ruta_subida(ruta):
    r = (ruta or "").lower()
    return any(x in r for x in ("/uploads/", "/attachments/", "user-data/uploads", "user-data/attachments"))


def nombre_artefacto(inp):
    titulo = inp.get("title") or inp.get("id") or "artefacto"
    lang = (inp.get("language") or "").lower()
    tipo = (inp.get("type") or "").lower()
    ext = {"python": ".py", "javascript": ".js", "typescript": ".ts", "bash": ".sh", "html": ".html",
           "css": ".css", "json": ".json", "sql": ".sql", "java": ".java", "c": ".c", "cpp": ".cpp",
           "csharp": ".cs", "vb": ".vb", "markdown": ".md"}.get(lang)
    if not ext:
        ext = {"text/html": ".html", "text/markdown": ".md", "image/svg+xml": ".svg",
               "application/vnd.ant.react": ".jsx", "application/vnd.ant.mermaid": ".mmd",
               "application/vnd.ant.code": ".txt"}.get(tipo, ".txt")
    base = re.sub(r"[^\w\-. ]+", "_", str(titulo)).strip() or "artefacto"
    return base if base.lower().endswith(ext) else base + ext


def render_tool_use(b, ctx):
    """Convierte una llamada a herramienta de Claude en texto legible y registra
    los archivos que crea o modifica. Devuelve '' si no hay nada que mostrar."""
    nombre = str(b.get("name") or "?")
    n = nombre.lower().split("__")[-1]
    inp = b.get("input")
    if b.get("id"):
        ctx.herr[b["id"]] = (n, inp if isinstance(inp, dict) else {})
    ESTAD["herr:" + n] += 1
    if not isinstance(inp, dict):
        return f"[Herramienta: {nombre}]"
    det = ctx.opts.detalles

    # --- Artefactos de claude.ai
    if n == "artifacts" or ("command" in inp and "id" in inp and ("content" in inp or "old_str" in inp)):
        cmd = inp.get("command")
        titulo = inp.get("title") or inp.get("id") or "artefacto"
        clave = "artefacto:" + str(inp.get("id") or titulo)
        if cmd == "update":
            previo = ctx.reg.por_ruta.get(clave, [])
            if previo and not inp.get("title"):
                titulo = os.path.splitext(previo[-1].nombre)[0]
            ok = ctx.reg.editar(clave, inp.get("old_str"), inp.get("new_str") or "", False, ctx.fecha, ctx)
            ESTAD["ediciones_ok" if ok else "ediciones_sin_original"] += 1
            return caja(f"Artefacto modificado: {titulo}",
                        f"--- antes:\n{inp.get('old_str') or ''}\n+++ después:\n{inp.get('new_str') or ''}")
        contenido = inp.get("content") or ""
        a = ctx.reg.nuevo(clave, "artefacto", ctx.fecha, contenido, ctx=ctx)
        a.nombre = nombre_artefacto(inp)
        return caja(f"Artefacto: {titulo}", contenido, f"fin del artefacto {titulo}")

    ruta = inp.get("file_path") or inp.get("path") or inp.get("notebook_path")
    viejo = inp.get("old_str", inp.get("old_string"))
    nuevo = inp.get("new_str", inp.get("new_string"))
    escrito = inp.get("file_text") if isinstance(inp.get("file_text"), str) else inp.get("content")

    # --- Edición de un archivo (str_replace, Edit, memory_str_replace...)
    if ruta and isinstance(viejo, str) and isinstance(nuevo, str):
        ok = ctx.reg.editar(ruta, viejo, nuevo, bool(inp.get("replace_all")), ctx.fecha, ctx)
        ESTAD["ediciones_ok" if ok else "ediciones_sin_original"] += 1
        return caja(f"Archivo modificado: {ruta}", f"--- antes:\n{viejo}\n+++ después:\n{nuevo}")
    if ruta and isinstance(inp.get("edits"), list):           # MultiEdit
        trozos = []
        for e in inp["edits"]:
            if isinstance(e, dict):
                v, nv = e.get("old_string", ""), e.get("new_string", "")
                ok = ctx.reg.editar(ruta, v, nv, bool(e.get("replace_all")), ctx.fecha, ctx)
                ESTAD["ediciones_ok" if ok else "ediciones_sin_original"] += 1
                trozos.append(f"--- antes:\n{v}\n+++ después:\n{nv}")
        return caja(f"Archivo modificado: {ruta}", "\n\n".join(trozos))

    # --- Creación / escritura de un archivo (create_file, Write, memory_write...)
    if ruta and isinstance(escrito, str) and n not in NOMBRES_LECTURA:
        origen = "memoria" if n.startswith("memory") else "creado"
        if n == "memory_append":
            a = ctx.reg.por_ruta.get(ruta.strip(), [None])[-1]
            if a is not None and a.versiones:
                a.versiones.append((ctx.fecha, a.texto.rstrip("\n") + "\n" + escrito, "añadido"))
            else:
                ctx.reg.nuevo(ruta, origen, ctx.fecha, escrito, ctx=ctx)
            return caja(f"Añadido a: {ruta}", escrito)
        ctx.reg.nuevo(ruta, origen, ctx.fecha, escrito, ctx=ctx)
        return caja(f"Archivo creado: {ruta}", escrito, f"fin de {os.path.basename(ruta)}")

    # --- Mensajes al usuario dentro de herramientas
    if n in ("sendusermessage", "send_user_message") and isinstance(inp.get("message"), str):
        return inp["message"]
    if n == "askuserquestion" and isinstance(inp.get("questions"), list):
        out = []
        for q in inp["questions"]:
            if isinstance(q, dict):
                out.append("[Pregunta] " + str(q.get("question", "")))
                for o in q.get("options") or []:
                    if isinstance(o, dict):
                        d = f" — {o['description']}" if o.get("description") else ""
                        out.append(f"   · {o.get('label', '')}{d}")
        return "\n".join(out)
    if n in ("senduserfile", "present_files"):
        fs = inp.get("files") or inp.get("filepaths") or []
        if isinstance(fs, str):
            fs = [fs]
        for f in fs:
            for a in ctx.reg.por_ruta.get(str(f).strip(), [])[-1:]:
                a.entregado = True
        cap = f"\n{inp['caption']}" if isinstance(inp.get("caption"), str) else ""
        return "[Archivos entregados: " + ", ".join(os.path.basename(str(f)) for f in fs) + "]" + cap

    # --- Comandos, búsquedas, lecturas
    if isinstance(inp.get("command"), str) and n in ("bash", "bash_tool", "shell", "run_command", "device_bash"):
        return f"[Comando]\n$ {inp['command']}" if det or len(inp["command"]) < 2000 else "[Comando largo]"
    if isinstance(inp.get("code"), str):
        return caja(f"Código ejecutado ({nombre})", inp["code"])
    if isinstance(inp.get("query"), str) and "search" in n:
        return f"[Búsqueda web: {inp['query']}]"
    if isinstance(inp.get("url"), str) and "fetch" in n:
        return f"[Página consultada: {inp['url']}]"
    if n in NOMBRES_LECTURA:
        return f"[Lee: {ruta or inp.get('pattern') or ''}]" if det else ""
    if any(n.startswith(x) for x in NOMBRES_INTERNOS):
        return f"[{nombre}]" if det else ""

    # --- Cualquier otra herramienta: se muestran sus textos
    ESTAD["herr_generica:" + n] += 1
    partes = [f"[Herramienta: {nombre}]"]
    for k, v in inp.items():
        if isinstance(v, str) and v.strip():
            v2 = v if (det or len(v) < 600) else v[:600] + " …"
            partes.append(f"  {k}: {v2}")
    return "\n".join(partes)


def render_tool_result(b, ctx):
    """Resultado de una herramienta. Siempre registra lo que sirve para el catálogo
    (archivos leídos, imágenes); solo se muestra con la opción 'detalles'."""
    nombre, inp = ctx.herr.get(b.get("tool_use_id"), (str(b.get("name") or "?").lower(), {}))
    contenido = b.get("content")
    texto = texto_de_contenido(contenido)
    ruta = (inp or {}).get("file_path") or (inp or {}).get("path")

    # Archivos leídos por Claude (entre ellos, los que tú subiste)
    if nombre in ("read", "view") and ruta and texto and not b.get("is_error"):
        lineas = RE_RECORDATORIO.sub("", texto).split("\n")
        num = [RE_LINEA_CAT.match(l) for l in lineas]
        validas = [m for m in num if m]
        if validas and len(validas) >= 0.8 * len([l for l in lineas if l.strip()]):
            cuerpo = "\n".join(m.group(2) for m in validas)
            origen = "subido" if es_ruta_subida(ruta) else "leido"
            existente = ctx.reg.por_ruta.get(ruta.strip(), [])
            if not existente:
                a = ctx.reg.nuevo(ruta, origen, ctx.fecha, cuerpo, ctx=ctx)
                a.parcial = bool(inp.get("offset") or inp.get("limit")) or validas[0].group(1) != "1"
    for tm, datos in imagenes_de(contenido):
        if ruta and not ctx.reg.por_ruta.get(ruta.strip()):
            origen = "subido" if es_ruta_subida(ruta) else "leido"
            ctx.reg.nuevo(ruta, origen, ctx.fecha, b64=datos, tipo_media=tm, ctx=ctx)

    if nombre == "askuserquestion" and texto.strip():
        return "[Tu respuesta] " + RE_RECORDATORIO.sub("", texto).strip()
    if not ctx.opts.detalles or not texto.strip():
        return ""
    t = RE_RECORDATORIO.sub("", texto).strip()
    return caja(f"Resultado de {nombre}", t)


def render_bloques(contenido, ctx, rol):
    """Devuelve (texto_principal, texto_resultados_de_herramientas)."""
    if isinstance(contenido, str):
        return quitar_marcadores(contenido), ""
    if not isinstance(contenido, list):
        return "", ""
    principal, resultados = [], []
    for b in contenido:
        if isinstance(b, str):
            principal.append(b)
            continue
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        ESTAD["bloque:" + str(t)] += 1
        if t == "text":
            principal.append(quitar_marcadores(b.get("text") or ""))
        elif t in ("thinking", "redacted_thinking"):
            if ctx.opts.razonamiento and b.get("thinking"):
                principal.append(caja("Razonamiento", b["thinking"]))
        elif t in ("tool_use", "server_tool_use"):
            principal.append(render_tool_use(b, ctx))
        elif t in ("tool_result", "web_search_tool_result", "web_fetch_tool_result"):
            r = render_tool_result(b, ctx)
            if r:
                (principal if r.startswith("[Tu respuesta]") else resultados).append(r)
        elif t in ("image", "document"):
            imgs = imagenes_de([b])
            etiqueta = "Imagen" if t == "image" else "Documento"
            if imgs and rol == "user":
                n_img = sum(1 for a in ctx.archivos if a.b64) + 1
                tm = imgs[0][0]
                ctx.reg.nuevo(f"{etiqueta.lower()}_{n_img}{EXT_MEDIA.get(tm, '')}", "subido", ctx.fecha,
                              b64=imgs[0][1], tipo_media=tm, ctx=ctx)
            principal.append(f"[{etiqueta} adjunta]" if rol == "user" else f"[{etiqueta}]")
        elif t in ("token_budget", "flag", "voice_note"):
            continue
        else:
            ESTAD["bloque_desconocido:" + str(t)] += 1
            txt = texto_de_contenido([b])
            if txt.strip():
                principal.append(txt)
    unir = lambda xs: "\n\n".join(x.strip("\n") for x in xs if x and x.strip())
    return unir(principal), unir(resultados)


# ---------------------------------------------------------------- Claude (chats de claude.ai)
def mensajes_claude(c, opts, ctx):
    msgs = c.get("chat_messages") or []
    hoja = c.get("current_leaf_message_uuid")
    if hoja and not opts.todas_ramas:
        por_id = {m.get("uuid"): m for m in msgs if isinstance(m, dict)}
        if hoja in por_id:
            camino, visto, cur = [], set(), hoja
            while cur in por_id and cur not in visto:
                visto.add(cur)
                camino.append(por_id[cur])
                cur = por_id[cur].get("parent_message_uuid")
            msgs = list(reversed(camino))
    out = []
    for m in msgs:
        if not isinstance(m, dict):
            continue
        ctx.idx = len(out)
        ctx.fecha = fecha(m.get("created_at"))
        rol = "user" if m.get("sender") in ("human", "user") else "ai"
        cont = m.get("content")
        if isinstance(cont, list) and cont:
            txt, res = render_bloques(cont, ctx, rol)
        else:
            # Exportaciones antiguas sin 'content': solo queda el resumen 'text'
            bruto = m.get("text") or ""
            n_marc = len(RE_MARCADOR.findall(bruto))
            ESTAD["marcadores_sin_contenido"] += n_marc
            txt, res = RE_MARCADOR.sub(PERDIDO, bruto), ""
        partes = [txt] if txt.strip() else []
        for a in m.get("attachments") or []:
            if isinstance(a, dict):
                nom = a.get("file_name") or "sin nombre"
                partes.append(f"[Adjunto: {nom}]")
                cont_a = a.get("extracted_content")
                if isinstance(cont_a, str) and cont_a.strip():
                    ctx.reg.nuevo(nom, "subido", ctx.fecha, cont_a, ctx=ctx, mismo_conv=False)
                    if opts.adjuntos:
                        partes.append(caja(f"Contenido de {nom}", cont_a))
        for f in (m.get("files") or []) + (m.get("files_v2") or []):
            if isinstance(f, dict):
                nom = f.get("file_name") or "sin nombre"
                if not any(x.nombre == nom for x in ctx.archivos):
                    ctx.reg.nuevo(nom, "subido", ctx.fecha, ctx=ctx, mismo_conv=False)
                partes.append(f"[Archivo: {nom}]")
        if res:
            partes.append(res)
        out.append((rol, limpiar("\n\n".join(partes)), ctx.fecha))
    return out


# ---------------------------------------------------------------- Claude (sesiones de trabajo / Claude Code)
def es_evento(d):
    if not isinstance(d, dict):
        return False
    m = d.get("message")
    if isinstance(m, dict) and ("content" in m or "role" in m):
        return True
    return d.get("type") in ("user", "assistant") and ("content" in d or "uuid" in d)


def lista_eventos(v):
    if isinstance(v, list) and v:
        muestra = [x for x in v[:200] if isinstance(x, dict)]
        if muestra and sum(1 for x in muestra if es_evento(x)) >= max(1, len(muestra) // 4):
            return v
    return None


def mensajes_sesion(c, opts, ctx):
    eventos = c.get("__eventos__") or []
    out = []
    for d in eventos:
        if not es_evento(d):
            continue
        if d.get("isMeta") or (d.get("isSidechain") and not opts.detalles):
            continue
        m = d.get("message") if isinstance(d.get("message"), dict) else d
        rol = str(m.get("role") or d.get("type") or "").lower()
        if rol not in ("user", "human", "assistant"):
            continue
        ctx.idx = len(out)
        ctx.fecha = primera_fecha(d, ("timestamp", "created_at", "createdAt", "time")) or ctx.fecha
        cont = m.get("content")
        if isinstance(cont, str):
            cont = [{"type": "text", "text": cont}]
        if rol in ("user", "human"):
            if d.get("isCompactSummary"):
                if opts.detalles:
                    out.append(("ai", caja("Resumen automático", texto_de_contenido(cont)), ctx.fecha))
                continue
            solo_resultados = isinstance(cont, list) and cont and all(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in cont)
            txt, res = render_bloques(cont, ctx, "user")
            txt = limpiar(RE_RECORDATORIO.sub("", txt))
            if solo_resultados:
                # Los resultados de herramientas no son preguntas tuyas: van con la respuesta
                todo = "\n\n".join(x for x in (txt, res) if x)
                if todo:
                    out.append(("ai", todo, ctx.fecha))
                continue
            if txt or res:
                out.append(("user", txt or "(sin texto)", ctx.fecha))
                if res:
                    out.append(("ai", res, ctx.fecha))
        else:
            txt, res = render_bloques(cont, ctx, "ai")
            todo = "\n\n".join(x for x in (txt, res) if x)
            if todo.strip():
                out.append(("ai", limpiar(todo), ctx.fecha))
    return out


# ---------------------------------------------------------------- árbol "mapping" (ChatGPT / DeepSeek)
def camino_mapping(mapping, actual):
    if actual and actual in mapping:
        camino, visto, cur = [], set(), actual
        while cur and cur in mapping and cur not in visto:
            visto.add(cur)
            camino.append(mapping[cur])
            cur = mapping[cur].get("parent")
        return list(reversed(camino))
    raices = [n for k, n in mapping.items() if isinstance(n, dict) and not n.get("parent")]
    if not raices and "root" in mapping:
        raices = [mapping["root"]]
    if not raices:
        return [n for n in mapping.values() if isinstance(n, dict)]
    camino, visto, nodo = [], set(), raices[0]
    while isinstance(nodo, dict) and id(nodo) not in visto:
        visto.add(id(nodo))
        camino.append(nodo)
        hijos = nodo.get("children") or []
        nodo = mapping.get(hijos[-1]) if hijos else None
    return camino


def mensajes_chatgpt(c, opts, ctx):
    mapping = c.get("mapping") or {}
    out = []
    for nodo in camino_mapping(mapping, c.get("current_node")):
        m = nodo.get("message") if isinstance(nodo, dict) else None
        if not isinstance(m, dict):
            continue
        meta = m.get("metadata") or {}
        if meta.get("is_visually_hidden_from_conversation"):
            continue
        rol = ((m.get("author") or {}).get("role")) or ""
        if rol in ("system", "tool") and not opts.detalles:
            continue
        cont = m.get("content") or {}
        ct = cont.get("content_type", "text")
        partes = []
        if ct in ("text", "multimodal_text"):
            for p in cont.get("parts") or []:
                if isinstance(p, str):
                    partes.append(p)
                elif isinstance(p, dict):
                    if p.get("text"):
                        partes.append(p["text"])
                    elif "image" in str(p.get("content_type", "")) or p.get("asset_pointer"):
                        partes.append("[Imagen]")
        elif ct in ("thoughts", "reasoning_recap"):
            if not opts.razonamiento:
                continue
            if cont.get("content"):
                partes.append("[Razonamiento] " + str(cont["content"]))
            for t in cont.get("thoughts") or []:
                if isinstance(t, dict):
                    partes.append("[Razonamiento] " + (t.get("summary") or "") + "\n" + (t.get("content") or ""))
        elif ct in ("code", "execution_output"):
            if not opts.detalles:
                continue
            partes.append("[Código/herramienta]\n" + str(cont.get("text") or ""))
        elif ct == "user_editable_context":
            continue
        else:
            if cont.get("text") and opts.detalles:
                partes.append(str(cont["text"]))
        for a in meta.get("attachments") or []:
            if isinstance(a, dict):
                partes.append(f"[Adjunto: {a.get('name') or 'sin nombre'}]")
        txt = limpiar("\n".join(partes))
        if txt:
            out.append(("user" if rol == "user" else "ai", txt, fecha(m.get("create_time"))))
    return out


def mensajes_deepseek(c, opts, ctx):
    mapping = c.get("mapping") or {}
    out = []
    for nodo in camino_mapping(mapping, c.get("current_node")):
        m = nodo.get("message") if isinstance(nodo, dict) else None
        if not isinstance(m, dict):
            continue
        f = primera_fecha(m, ("inserted_at", "created_at", "create_time"))
        for fr in m.get("fragments") or []:
            if not isinstance(fr, dict):
                continue
            tipo = str(fr.get("type", "")).upper()
            txt = fr.get("content")
            if not isinstance(txt, str) or not txt.strip():
                continue
            if tipo == "REQUEST":
                out.append(("user", limpiar(txt), f))
            elif tipo == "THINK":
                if opts.razonamiento:
                    out.append(("ai", "[Razonamiento]\n" + limpiar(txt), f))
            elif tipo == "SEARCH":
                continue
            else:
                out.append(("ai", limpiar(txt), f))
        for a in m.get("files") or []:
            if isinstance(a, dict) and (a.get("file_name") or a.get("name")):
                out.append(("user", f"[Adjunto: {a.get('file_name') or a.get('name')}]", f))
    return out


# ---------------------------------------------------------------- genérico (Kimi, Lumo, otras)
CLAVES_MSGS = ("messages", "chat_messages", "chat", "history", "turns", "items",
               "conversation", "dialog", "dialogue", "segments", "contents")
CLAVES_TITULO = ("title", "name", "subject", "topic", "chat_title", "summary")
CLAVES_CREADA = ("created_at", "createdAt", "create_time", "createTime", "created",
                 "inserted_at", "timestamp", "time", "date", "ctime")
CLAVES_ACT = ("updated_at", "updatedAt", "update_time", "updateTime", "updated", "lastEdit",
              "last_updated", "modified", "mtime")
ROLES_USUARIO = ("user", "human", "me", "usuario", "request", "prompt", "question", "you")


def rol_generico(m):
    r = m.get("role") or m.get("sender") or m.get("from") or m.get("speaker") or m.get("type") or m.get("author")
    if isinstance(r, dict):
        r = r.get("role") or r.get("name")
    if m.get("is_user") is True or m.get("isUser") is True:
        return "user"
    r = str(r or "").lower()
    if r in ("system",):
        return "system"
    return "user" if r in ROLES_USUARIO else "ai"


def parece_mensaje(d):
    return isinstance(d, dict) and any(k in d for k in ("role", "sender", "author", "from", "speaker")) \
        and any(k in d for k in ("content", "text", "message", "parts", "contents"))


def lista_mensajes(c):
    for k in CLAVES_MSGS:
        v = c.get(k)
        if isinstance(v, list) and v and any(parece_mensaje(x) for x in v[:20]):
            return v
        if isinstance(v, dict):
            for k2 in CLAVES_MSGS:
                v2 = v.get(k2)
                if isinstance(v2, list) and v2 and any(parece_mensaje(x) for x in v2[:20]):
                    return v2
    return None


def mensajes_generico(c, opts, ctx):
    out = []
    for m in lista_mensajes(c) or []:
        if not isinstance(m, dict):
            continue
        rol = rol_generico(m)
        if rol == "system":
            continue
        ctx.idx = len(out)
        ctx.fecha = primera_fecha(m, CLAVES_CREADA)
        cont = None
        for k in ("content", "text", "message", "parts", "contents"):
            if m.get(k) not in (None, "", [], {}):       # el primero que tenga algo (Duck.ai usa 'parts')
                cont = m[k]
                break
        if isinstance(cont, list) and any(isinstance(b, dict) and "type" in b for b in cont):
            txt, res = render_bloques(cont, ctx, rol)
            if rol == "user" and all(isinstance(b, dict) and b.get("type") == "tool_result" for b in cont):
                rol = "ai"            # resultados de herramientas: van con la respuesta
            txt = "\n\n".join(x for x in (txt, res) if x)
            if rol == "user":
                txt = RE_RECORDATORIO.sub("", txt)
        else:
            txt = texto_de_contenido(cont) if not isinstance(cont, str) else cont
        txt = limpiar(txt)
        if txt:
            out.append((rol, txt, ctx.fecha))
    return out


def encontrar_conversaciones(datos, prof=0):
    """Busca, en un JSON de estructura desconocida, las conversaciones."""
    if prof > 5:
        return []
    ev = lista_eventos(datos)
    if ev:
        # Sesiones de trabajo: se agrupan por sessionId
        grupos, orden = {}, []
        for d in ev:
            if not isinstance(d, dict):
                continue
            sid = d.get("sessionId") or d.get("session_id") or "_"
            if sid not in grupos:
                grupos[sid] = []
                orden.append(sid)
            grupos[sid].append(d)
        return [{"__eventos__": grupos[s], "__sid__": s} for s in orden]
    if isinstance(datos, list):
        if datos and any(parece_mensaje(x) for x in datos[:20]):
            return [{"messages": datos}]
        convs = [x for x in datos if isinstance(x, dict) and (lista_mensajes(x) or "mapping" in x
                                                                  or "chat_messages" in x)]
        if convs:
            return convs
        res = []
        for x in datos[:5000]:
            if isinstance(x, (list, dict)):
                res += [dict(r, **{"__padre__": x}) if "__eventos__" in r and isinstance(x, dict) else r
                        for r in encontrar_conversaciones(x, prof + 1)]
        return res
    if isinstance(datos, dict):
        if lista_mensajes(datos) or "mapping" in datos or "chat_messages" in datos:
            return [datos]
        res = []
        for v in datos.values():
            if isinstance(v, (list, dict)):
                res += [dict(r, **{"__padre__": datos}) if "__eventos__" in r and "__padre__" not in r else r
                        for r in encontrar_conversaciones(v, prof + 1)]
        return res
    return []


def detectar(c, etiqueta, categoria):
    if "__eventos__" in c:
        return "Claude Code" if categoria in (None, "claude_code_cloud") else "Claude", "CLAUDE", mensajes_sesion
    if "chat_messages" in c:
        return "Claude", "CLAUDE", mensajes_claude
    mp = c.get("mapping")
    if isinstance(mp, dict):
        for n in list(mp.values())[:30]:
            m = n.get("message") if isinstance(n, dict) else None
            if isinstance(m, dict):
                if "fragments" in m:
                    return "DeepSeek", "DEEPSEEK", mensajes_deepseek
                if "author" in m:
                    slug = str((m.get("metadata") or {}).get("model_slug") or "").lower()
                    if slug.startswith("lumo") or etiqueta == "Lumo":
                        return "Lumo", "LUMO", mensajes_chatgpt
                    return "ChatGPT", "CHATGPT", mensajes_chatgpt
        return "ChatGPT", "CHATGPT", mensajes_chatgpt
    if categoria == "claude_code_cloud":
        return "Claude Code", "CLAUDE", mensajes_generico
    if c.get("__fuente__"):
        e = c["__fuente__"]
        return e, c.get("__voz__") or e.upper(), mensajes_generico
    e = etiqueta or "IA"
    return e, e.upper(), mensajes_generico


def titulo_sesion(c):
    """Título de una sesión de trabajo: del contenedor, de un registro 'summary' o de la 1.ª pregunta."""
    padre = c.get("__padre__") or {}
    for k in CLAVES_TITULO:
        if isinstance(padre.get(k), str) and padre[k].strip() and len(padre[k]) < 200:
            return padre[k].strip()
    for d in c.get("__eventos__") or []:
        if isinstance(d, dict) and d.get("type") in ("summary", "ai-title", "custom-title", "title"):
            for k in ("summary", "title", "customTitle", "aiTitle"):
                if isinstance(d.get(k), str) and d[k].strip():
                    return d[k].strip()
    return ""


# ----------------------------------------------------------------------------
# Documentos: proyectos, memoria, datos de cuenta, valoraciones...
# ----------------------------------------------------------------------------
NOMBRE_CATEGORIA = {"projects": "Proyecto", "memories": "Memoria", "feedback": "Valoraciones",
                    "light_metadata": "Cuenta", "claude_code_cloud": "Claude Code", "conversations": "Claude"}


def json_a_texto(v, sangria="", prof=0):
    """Convierte cualquier JSON en texto legible 'clave: valor'."""
    if prof > 12:
        return []
    out = []
    if isinstance(v, dict):
        for k, x in v.items():
            if isinstance(x, (dict, list)):
                sub = json_a_texto(x, sangria + "  ", prof + 1)
                if sub:
                    out.append(f"{sangria}{k}:")
                    out += sub
            elif x not in (None, "", [], {}):
                s = str(x)
                if "\n" in s:
                    out.append(f"{sangria}{k}:")
                    out += [sangria + "  " + l for l in s.split("\n")]
                else:
                    out.append(f"{sangria}{k}: {s}")
    elif isinstance(v, list):
        for i, x in enumerate(v):
            if isinstance(x, (dict, list)):
                sub = json_a_texto(x, sangria + "  ", prof + 1)
                if sub:
                    out.append(f"{sangria}- [{i + 1}]")
                    out += sub
            elif x not in (None, ""):
                out.append(f"{sangria}- {x}")
    elif v not in (None, ""):
        out.append(f"{sangria}{v}")
    return out


def buscar_proyectos(v, prof=0):
    if prof > 5:
        return []
    if isinstance(v, dict):
        if isinstance(v.get("docs"), list) or "prompt_template" in v or \
                ("name" in v and ("description" in v or "instructions" in v)):
            return [v]
        res = []
        for x in v.values():
            res += buscar_proyectos(x, prof + 1)
        return res
    if isinstance(v, list):
        res = []
        for x in v:
            res += buscar_proyectos(x, prof + 1)
        return res
    return []


def buscar_documentos(v, prof=0):
    """Dicts con forma de documento: (ruta|nombre, contenido de texto)."""
    if prof > 6:
        return []
    if isinstance(v, dict):
        cont = v.get("content") if isinstance(v.get("content"), str) else v.get("text")
        nom = v.get("path") or v.get("filename") or v.get("file_name") or v.get("name") or v.get("title")
        if isinstance(cont, str) and isinstance(nom, str):
            return [(nom, cont, v)]
        res = []
        for x in v.values():
            res += buscar_documentos(x, prof + 1)
        return res
    if isinstance(v, list):
        res = []
        for x in v:
            res += buscar_documentos(x, prof + 1)
        return res
    return []


# ----------------------------------------------------------------------------
# HTML a texto (Takeout, Gemini, Keep)
# ----------------------------------------------------------------------------
class _HtmlTexto(HTMLParser):
    BLOQUES = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "table", "ul", "ol",
               "blockquote", "section", "article", "hr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.partes, self.saltar = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.saltar += 1
        elif tag == "li":
            self.partes.append("\n- ")
        elif tag in self.BLOQUES:
            self.partes.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.saltar = max(0, self.saltar - 1)
        elif tag in self.BLOQUES:
            self.partes.append("\n")

    def handle_data(self, data):
        if not self.saltar:
            self.partes.append(data)


def html_a_texto(h):
    p = _HtmlTexto()
    try:
        p.feed(h or "")
        p.close()
    except Exception:
        return re.sub(r"<[^>]+>", " ", h or "")
    t = "".join(p.partes).replace("\xa0", " ")
    t = re.sub(r"[ \t]+\n", "\n", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


# ----------------------------------------------------------------------------
# Google Takeout: Gemini, Keep y el resto de productos
# ----------------------------------------------------------------------------
MESES_ES = {"ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6, "jul": 7, "ago": 8, "sept": 9, "sep": 9,
            "oct": 10, "nov": 11, "dic": 12, "jan": 1, "apr": 4, "aug": 8, "dec": 12,
            "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
            "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12}
RE_FECHA_TEXTO = re.compile(r"(\d{1,2})\s+(?:de\s+)?([A-Za-zé]{3,10})\.?\s+(?:de\s+)?(\d{4})"
                            r"(?:,?\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?")
PREFIJOS_PROMPT = ("Prompted ", "Has preguntado ", "Has preguntado: ", "Pregunta: ", "Has escrito ", "Has enviado ",
                   "Petición: ", "Peticion: ", "Has dado la instrucción ", "Instrucción: ")
TITULOS_VACIOS = ("used gemini apps", "has usado gemini apps", "has usado las apps de gemini",
                  "has usado apps de gemini", "used bard", "has usado bard", "gemini apps")


def fecha_texto(s):
    """'13 nov 2026, 12:00:05 CET' / '13 de noviembre de 2026' -> datetime."""
    m = RE_FECHA_TEXTO.search(s or "")
    if not m:
        return None
    mes = MESES_ES.get(m.group(2).lower().rstrip("."))
    if not mes:
        return None
    try:
        d = datetime(int(m.group(3)), mes, int(m.group(1)), int(m.group(4) or 0), int(m.group(5) or 0),
                     int(m.group(6) or 0))
        return d.astimezone()
    except ValueError:
        return None


def es_actividad(d):
    return isinstance(d, dict) and "time" in d and ("header" in d or "products" in d) and "title" in d


def es_gemini(d):
    txt = normalizar(" ".join([str(d.get("header", ""))] + [str(p) for p in d.get("products") or []]))
    return "gemini" in txt or "bard" in txt


def textos_de_json_str(s):
    """Saca los textos legibles de un JSON serializado (variante userInteractions de Gemini)."""
    try:
        v = json.loads(s) if isinstance(s, str) else s
    except (json.JSONDecodeError, TypeError):
        return s if isinstance(s, str) else ""
    out = []

    def rec(x, prof=0):
        if prof > 12:
            return
        if isinstance(x, str):
            if len(x) > 1 and (" " in x.strip() or len(x) > 25) and not re.match(r"^(https?://|[\w\-]{20,}$)", x):
                out.append(x)
        elif isinstance(x, list):
            for y in x:
                rec(y, prof + 1)
        elif isinstance(x, dict):
            for y in x.values():
                rec(y, prof + 1)
    rec(v)
    vistos, final = set(), []
    for t in out:
        if t not in vistos:
            vistos.add(t)
            final.append(t)
    return "\n\n".join(final)


def gemini_item(d):
    """Devuelve (id_conversacion, fecha, pregunta, respuesta, adjuntos) de una actividad de Gemini."""
    f = fecha(d.get("time"))
    m = re.search(r"/app/(?:c/)?([0-9A-Za-z_\-]{8,})", str(d.get("titleUrl") or ""))
    cid = m.group(1) if m else None
    preg, resp = "", ""
    for x in d.get("details") or []:
        if isinstance(x, dict):
            n = normalizar(str(x.get("name", "")))
            if n in ("request", "peticion", "solicitud", "prompt", "pregunta"):
                preg = preg or str(x.get("value") or "")
            elif n in ("response", "respuesta"):
                resp = resp or str(x.get("value") or "")
    for ui in d.get("userInteractions") or []:
        if isinstance(ui, dict):
            u = ui.get("userInteraction") if isinstance(ui.get("userInteraction"), dict) else ui
            preg = preg or textos_de_json_str(u.get("request"))
            resp = resp or textos_de_json_str(u.get("response"))
    if not preg:
        t = str(d.get("title") or "")
        if normalizar(t).strip() not in TITULOS_VACIOS and not normalizar(t).startswith(("used ", "has usado")):
            for p in PREFIJOS_PROMPT:
                if t.startswith(p):
                    t = t[len(p):]
                    break
            preg = t
    if not resp:
        trozos = [html_a_texto(x.get("html")) for x in d.get("safeHtmlItem") or []
                  if isinstance(x, dict) and x.get("html")]
        resp = "\n\n".join(t for t in trozos if t)
    adj = [str(a) for a in d.get("attachedFiles") or [] if a]
    for s in d.get("subtitles") or []:
        if isinstance(s, dict) and s.get("name") and not resp:
            resp = str(s["name"])
    return cid, f, limpiar(preg), limpiar(resp), adj


def gemini_html_items(h):
    """MyActivity.html de Gemini (formato por defecto de Takeout): extracción aproximada."""
    out = []
    for bloque in re.split(r'<div class="outer-cell', h)[1:]:
        m = re.search(r"/app/(?:c/)?([0-9A-Za-z_\-]{8,})", bloque)
        celdas = re.findall(r'<div class="content-cell[^"]*mdl-cell--6-col[^"]*">(.*?)</div>', bloque, re.S)
        texto = html_a_texto(celdas[0] if celdas else bloque)
        lineas = [l for l in texto.split("\n") if l.strip()]
        f, i_f = None, -1
        for i, l in enumerate(lineas[:12]):
            f = fecha_texto(l)
            if f:
                i_f = i
                break
        if i_f < 0:
            continue
        preg = "\n".join(lineas[:i_f])
        for p in PREFIJOS_PROMPT:
            if preg.startswith(p):
                preg = preg[len(p):]
                break
        if normalizar(preg).strip() in TITULOS_VACIOS:
            preg = ""
        resp = "\n".join(lineas[i_f + 1:])
        out.append((m.group(1) if m else None, f, limpiar(preg), limpiar(resp), []))
    return out


def conversaciones_gemini(items):
    """Agrupa las actividades en conversaciones: por id; si no hay id, por cercanía (30 min)."""
    items = [x for x in items if x[2] or x[3]]
    items.sort(key=lambda x: ts(x[1]))
    grupos, orden, ult_sin_id = {}, [], None
    for cid, f, p, r, adj in items:
        if cid:
            clave = cid
        else:
            if ult_sin_id and f and ult_sin_id[1] and (f - ult_sin_id[1]).total_seconds() <= 1800:
                clave = ult_sin_id[0]
            else:
                clave = f"sesion-{len(orden)}"
            ult_sin_id = (clave, f)
        if clave not in grupos:
            grupos[clave] = []
            orden.append(clave)
        grupos[clave].append((f, p, r, adj))
    out = []
    for k in orden:
        msgs = []
        for f, p, r, adj in grupos[k]:
            extra = "".join(f"\n[Adjunto: {a}]" for a in adj)
            msgs.append(("user", (p or "(pregunta no incluida en el Takeout)") + extra, f))
            msgs.append(("ai", r or "(respuesta no incluida en el Takeout)", f))
        out.append(msgs)
    return out


def es_nota_keep(d):
    return isinstance(d, dict) and ("textContent" in d or "listContent" in d or "textContentHtml" in d) \
        and ("createdTimestampUsec" in d or "userEditedTimestampUsec" in d or "isTrashed" in d)


def nota_keep(d):
    titulo = (d.get("title") or "").strip()
    partes = []
    if d.get("textContent"):
        partes.append(d["textContent"])
    elif d.get("textContentHtml"):
        partes.append(html_a_texto(d["textContentHtml"]))
    for it in d.get("listContent") or []:
        if isinstance(it, dict):
            partes.append(("[x] " if it.get("isChecked") else "[ ] ") + str(it.get("text", "")))
    for a in d.get("attachments") or []:
        if isinstance(a, dict):
            partes.append(f"[Adjunto: {a.get('filePath') or a.get('mimetype') or 'archivo'}]")
    for a in d.get("annotations") or []:
        if isinstance(a, dict) and a.get("url"):
            partes.append(f"[Enlace: {a.get('title') or ''} {a['url']}]".replace("  ", " "))
    marcas = []
    if d.get("labels"):
        marcas.append("etiquetas: " + ", ".join(str(l.get("name")) for l in d["labels"] if isinstance(l, dict)))
    for k, n in (("isPinned", "fijada"), ("isArchived", "archivada"), ("isTrashed", "en la papelera")):
        if d.get(k):
            marcas.append(n)
    f = fecha(d.get("createdTimestampUsec")) or fecha(d.get("userEditedTimestampUsec"))
    cab = f"[Nota] {titulo or '(sin título)'}" + (f"  ({'; '.join(marcas)})" if marcas else "")
    return cab, limpiar("\n".join(partes)) or "(nota vacía)", f, titulo


def gemini_desde_keep(d, convs):
    """Nota de Keep creada desde Gemini («guarda esta conversación en Keep») con Pregunta/Respuesta:
    se convierte en una conversación de Gemini. Devuelve True si lo hizo."""
    enlaces = [a.get("url", "") for a in d.get("annotations") or [] if isinstance(a, dict)]
    texto = d.get("textContent") or html_a_texto(d.get("textContentHtml") or "")
    if not any("gemini.google.com" in u for u in enlaces) or \
            not any(RE_PR_PREG.match(l) or RE_PR_RESP.match(l) for l in texto.splitlines()):
        return False
    c = parsear_preguntas_respuestas(d.get("title") or "nota", texto, None, "Gemini")
    msgs = [(("user" if m["role"] == "user" else "ai"), m["content"], None) for m in c["messages"]]
    inter, _ = agrupar(msgs)
    if not any(x["r"] for x in inter):                  # solo una lista de preguntas: se queda como nota
        return False
    f = fecha(d.get("createdTimestampUsec")) or fecha(d.get("userEditedTimestampUsec"))
    enlace = next(u for u in enlaces if "gemini.google.com" in u)
    inter[-1]["r"].append(f"[Guardada en Google Keep desde Gemini: {enlace}]")
    lista = [Intercambio("\n\n".join(x["p"]), "\n\n".join(x["r"]), f) for x in inter]
    titulo = (d.get("title") or c["title"]).strip()
    convs.append(Conversacion(f"{titulo} (nota de Keep)", f, fecha(d.get("userEditedTimestampUsec")) or f,
                              lista, "Gemini", "GEMINI"))
    return True


def descodificar_google(s):
    """Takeout guarda los caracteres no ASCII de los nombres como #UXXXX (Soberan#U00eda -> Soberanía)."""
    return re.sub(r"#U([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), s or "")


def producto_takeout(nombre):
    p = [x for x in descodificar_google(nombre).replace("\\", "/").split("/") if x]
    bajos = [x.lower() for x in p]
    if "takeout" in bajos:
        p = p[bajos.index("takeout") + 1:]
    if not p:
        return "Google"
    if normalizar(p[0]) in ("my activity", "mi actividad", "mis actividades") and len(p) > 2:
        return p[1]
    return p[0] if len(p) > 1 else "Google"


# ----------------------------------------------------------------------------
# Transcripciones en .txt/.md (Lumo gratuito, Lumo exportado en .md, duck.py)
# ----------------------------------------------------------------------------
RE_DUCK_CAB = re.compile(r"^\s*B[ÚU]SQUEDA\s+(\d+)\s*\((\d{4}-\d\d-\d\d[ T]\d\d:\d\d(?::\d\d)?)\)", re.I)
RE_TRANS_PREG = re.compile(r"^\s*(?:PREGUNTA|QUESTION|USUARIO|USER)(?:\s*\d+)?(?:\s*\((?:USUARIO|USER|YO)\))?\s*:?\s*$",
                           re.I)
RE_TRANS_RESP = re.compile(r"^\s*(?:RESPUESTA|ANSWER|RESPONSE)(?:\s*\d+)?\s*(?:\(?\s*([A-Za-z][\w.\- ]{1,20}?)\s*\)?)?"
                           r"\s*:?\s*$", re.I)
RE_SEPARADOR = re.compile(r"^\s*[-=_*~]{5,}\s*$")
RE_EPOCH_MS = re.compile(r"-(1[6-9]\d{11})\.(?:md|markdown|json|txt)$", re.I)


def fecha_de_nombre(nombre):
    m = RE_EPOCH_MS.search(os.path.basename(nombre))
    if m:
        return fecha(int(m.group(1)))
    m = re.search(r"(20\d\d)-(\d\d)-(\d\d)[-_ T](\d\d)[-:](\d\d)[-:](\d\d)", os.path.basename(nombre))
    if m:
        try:
            return datetime(*map(int, m.groups())).astimezone()
        except ValueError:
            return None
    m = re.search(r"(?<!\d)(20\d\d)-(\d\d)-(\d\d)(?!\d)", os.path.basename(nombre))
    if m:                                                   # solo el día: 2026-06-05articulo_...
        try:
            return datetime(*map(int, m.groups()), 12, 0).astimezone()
        except ValueError:
            return None
    return None


def parsear_duck_busqueda(nombre, texto):
    lineas = texto.splitlines()
    m = RE_DUCK_CAB.match(lineas[0]) if lineas else None
    consulta = next((l.split(":", 1)[1].strip() for l in lineas[:8] if l.lower().startswith("consulta")), "")
    return {"title": f"Búsqueda: {consulta or os.path.basename(nombre)}",
            "created_at": m.group(2) if m else None, "__fuente__": "DuckDuckGo", "__voz__": "RESULTADOS",
            "messages": [{"role": "user", "content": f"[Búsqueda en DuckDuckGo] {consulta}"},
                         {"role": "assistant", "content": texto}]}


RE_DUCK_IA = re.compile(r"^\s*DUCK\.AI\s+(\d+)\s*\((\d{4}-\d\d-\d\d[ T]\d\d:\d\d(?::\d\d)?)\)", re.I)


def parsear_transcripcion(nombre, texto, mtime=None):
    """Formato 'PREGUNTA 1 (USUARIO)' / 'RESPUESTA LUMO' (txt que pides a Lumo gratuito, etc.)
    y los .txt de 'duck.py ia' (DUCK.AI n (fecha) / PREGUNTA: / RESPUESTA:)."""
    lineas = texto.splitlines()
    m_duck = RE_DUCK_IA.match(lineas[0]) if lineas else None
    msgs, actual, cab, ia = [], None, [], None
    for l in lineas:
        if RE_SEPARADOR.match(l):
            continue
        if RE_TRANS_PREG.match(l):
            actual = {"role": "user", "content": []}
            msgs.append(actual)
            continue
        mr = RE_TRANS_RESP.match(l)
        if mr and len(l.strip()) < 40:
            ia = ia or (mr.group(1) or "").strip() or None
            actual = {"role": "assistant", "content": []}
            msgs.append(actual)
            continue
        if actual is None:
            cab.append(l.strip())
        elif re.match(r"^\s*FIN DE LA (TRANSCRIPCI[ÓO]N|CONVERSACI[ÓO]N)\s*$", l, re.I):
            actual = None
        else:
            actual["content"].append(l)
    for x in msgs:
        x["content"] = "\n".join(x["content"]).strip()
    cab = [c for c in cab if c]
    titulo, f = None, None
    for c in cab:
        if re.match(r"^(fecha|date)\s*:", c, re.I):
            f = fecha_texto(c) or fecha(c.split(":", 1)[1].strip())
        elif re.match(r"^(formato|format)\s*:", c, re.I) or re.search(r"TRANSCRIPCI[ÓO]N|TRANSCRIPT", c, re.I):
            m = re.search(r"\bCON\s+([A-Za-z][\w.]+)", c, re.I) or re.search(r"\bWITH\s+([A-Za-z][\w.]+)", c, re.I)
            if m and not ia:
                ia = m.group(1)
        elif titulo is None:
            titulo = c
    if m_duck:
        preg = next((x["content"] for x in msgs if x["role"] == "user"), "")
        primera = next((l for l in preg.split("\n") if l.strip()), "") or f"Duck.ai {m_duck.group(1)}"
        return {"title": primera[:80], "created_at": m_duck.group(2), "__fuente__": "Duck.ai", "messages": msgs}
    ia = (ia or "").strip()
    fuente = ia.capitalize() if ia and ia.upper() not in ("IA", "AI", "ASISTENTE", "ASSISTANT") else None
    if fuente and fuente.lower() == "chatgpt":
        fuente = "ChatGPT"
    if fuente and fuente.lower() in ("duck.ai", "duckai"):
        fuente = "Duck.ai"
    return {"title": titulo or os.path.splitext(os.path.basename(nombre))[0],
            "created_at": f or fecha_de_nombre(nombre) or mtime, "__fuente__": fuente, "messages": msgs}


RE_PR_PREG = re.compile(r"^\s*(?:\*\*|__)?\s*(?:pregunta(?:\s+del\s+usuario)?|question|prompt|consulta)"
                        r"(?:\s*\d+)?\s*(?:\*\*|__)?\s*:\s*(?:\*\*|__)?\s*(.*)$", re.I)
RE_PR_RESP = re.compile(r"^\s*(?:\*\*|__)?\s*(?:respuesta|answer|response)(?:\s+de\s+[\w.]+)?(?:\s*\d+)?"
                        r"\s*(?:\*\*|__)?\s*:\s*(?:\*\*|__)?\s*(.*)$", re.I)
RE_PR_SEP = re.compile(r"^\s*([#\-=_*~])\1{4,}\s*$")         # ######  ------  ====== (sin espacios dentro)
RE_PR_FIN_PREG = re.compile(r"^\s*-{3,4}\s*$")                 # «---» entre la pregunta y la respuesta


def _parrafos(lineas):
    out, act = [], []
    for l in lineas:
        if l.strip():
            act.append(l)
        elif act:
            out.append(act)
            act = []
    if act:
        out.append(act)
    return out


def parsear_preguntas_respuestas(nombre, texto, mtime=None, ia=None):
    """Conversaciones copiadas a mano en un .txt (Lumo, Perplexity, Euria, MyAI, Apertus, Gemini...):
    - con marcas «Pregunta:» / «Respuesta:» (también «Pregunta del usuario:», «Pregunta 1:»...), o
    - sin marcas: la pregunta es el primer párrafo y la respuesta el resto.
    Varias preguntas en el mismo archivo se separan con una línea de ##### o ----- (5 o más)."""
    lineas = texto.lstrip("\ufeff").replace("\r\n", "\n").split("\n")
    bloques, act = [], []
    for l in lineas:
        if RE_PR_SEP.match(l):
            bloques.append(act)
            act = []
        else:
            act.append(l)
    bloques.append(act)
    bloques = [b for b in bloques if any(x.strip() for x in b)]
    con_marcas = any(RE_PR_PREG.match(l) or RE_PR_RESP.match(l) for l in lineas)
    msgs, titulo = [], None
    for nb, b in enumerate(bloques):
        hay_preg = any(RE_PR_PREG.match(l) for l in b)
        hay_resp = any(RE_PR_RESP.match(l) for l in b)
        if con_marcas and (hay_preg or hay_resp):
            actual, cab = None, []
            for l in b:
                mp, mr = RE_PR_PREG.match(l), RE_PR_RESP.match(l)
                if mp:
                    actual = {"role": "user", "content": [mp.group(1)] if mp.group(1).strip() else [],
                              "abierta": not hay_resp}
                    msgs.append(actual)
                elif mr:
                    actual = {"role": "assistant", "content": [mr.group(1)] if mr.group(1).strip() else []}
                    msgs.append(actual)
                elif actual is None:
                    cab.append(l.strip())
                elif actual.get("abierta") and (RE_PR_FIN_PREG.match(l) or
                                                (not l.strip() and any(x.strip() for x in actual["content"]))):
                    # Pregunta sin «Respuesta:» (notas de Gemini): termina en el primer hueco o en «---»
                    actual.pop("abierta")
                    actual = {"role": "assistant", "content": []}
                    msgs.append(actual)
                else:
                    actual["content"].append(l)
            cab = [c for c in cab if c]
            if cab and titulo is None and nb == 0:
                titulo = re.sub(r"^(conversaci[oó]n|t[ií]tulo|title)\s*:\s*", "", cab[-1], flags=re.I) \
                    if len(cab) == 1 else cab[0]
            elif cab:                                       # texto antes de la marca: va con la respuesta anterior
                msgs.append({"role": "assistant", "content": cab})
            continue
        ps = _parrafos(b)
        while ps and all(re.fullmatch(r"[\[\]{}()<>.\-\s]*", l) for l in ps[0]):
            ps.pop(0)                                       # «[]» y similares al principio
        if not ps:
            continue
        if con_marcas:                                      # bloque sin marcas en un archivo con marcas
            msgs.append({"role": "user", "content": ["[Texto sin pregunta]"]})
            msgs.append({"role": "assistant", "content": [l for p in ps for l in p + [""]]})
            continue
        n = 1                                               # la pregunta puede tener varios párrafos con «?»
        while n < len(ps) - 1 and n < 25 and len(ps[n]) <= 3 and "?" in " ".join(ps[n]) and \
                "?" in " ".join(ps[n - 1]) and len(" ".join(ps[n])) < 1500:
            n += 1
        msgs.append({"role": "user", "content": [l for p in ps[:n] for l in p + [""]]})
        if ps[n:]:
            msgs.append({"role": "assistant", "content": [l for p in ps[n:] for l in p + [""]]})
    for x in msgs:
        x.pop("abierta", None)
        x["content"] = "\n".join(x["content"]).strip()
        if x["role"] == "assistant":
            x["content"] = re.sub(r"^-{3,4}\s*\n", "", x["content"]).strip()
    msgs = [x for x in msgs if x["content"]]
    if not msgs and texto.strip():                          # nunca se pierde un archivo con texto
        msgs = [{"role": "user", "content": "[Texto sin pregunta]"},
                {"role": "assistant", "content": texto.strip()}]
    ia = ia or etiqueta_por_nombre(nombre)
    tema = tema_de_nombre(nombre, ia)
    return {"title": titulo or tema or os.path.splitext(os.path.basename(nombre))[0],
            "created_at": fecha_de_nombre(nombre) or mtime, "__fuente__": ia, "messages": msgs}


def parsear_texto_conversacion(nombre, texto, mtime=None):
    """Elige el lector adecuado para un .md/.txt fuera de las exportaciones oficiales."""
    primeras = texto[:3000]
    if RE_DUCK_CAB.match(primeras.lstrip().split("\n", 1)[0]) and re.search(r"^Consulta\s*:", primeras, re.M | re.I):
        return parsear_duck_busqueda(nombre, texto)
    if sum(1 for l in texto.splitlines() if RE_TRANS_PREG.match(l)) and \
            sum(1 for l in texto.splitlines() if RE_TRANS_RESP.match(l) and len(l.strip()) < 40):
        return parsear_transcripcion(nombre, texto, mtime)
    marcas_pr = any(RE_PR_PREG.match(l) or RE_PR_RESP.match(l) for l in texto.splitlines())
    c = parsear_markdown(nombre, texto, mtime)
    roles = {m["role"] for m in c["messages"] if m.get("content")}
    if marcas_pr or roles != {"user", "assistant"} or RE_PR_SEP.search(texto) and not \
            re.search(r"^\s*(?:#{1,6}\s*|\*\*)\s*(user|usuario|assistant|asistente|t[uú]|yo|kimi|lumo)\b", texto,
                      re.M | re.I):
        return parsear_preguntas_respuestas(nombre, texto, mtime)
    # Título subrayado con ===== (formato de exportación de Lumo)
    ls = texto.lstrip("\ufeff").splitlines()
    if len(ls) > 1 and ls[0].strip() and re.match(r"^\s*=+\s*$", ls[1]):
        c["title"] = ls[0].strip()
    f = fecha_de_nombre(nombre)
    if f:
        c["created_at"] = f
    if RE_EPOCH_MS.search(os.path.basename(nombre)) and re.search(r"^\*\*(USER|ASSISTANT):\*\*", texto, re.M):
        c["__fuente__"] = "Lumo"
    return c


# ----------------------------------------------------------------------------
# Construcción de todo
# ----------------------------------------------------------------------------
def agrupar(msgs):
    """Une mensajes en intercambios pregunta + respuesta. Devuelve (intercambios, msg->intercambio)."""
    inter, actual, mapa = [], None, []
    for rol, txt, f in msgs:
        if rol == "user":
            if actual is not None and not actual["r"] and txt.startswith(("[Adjunto", "[Archivo", "[Imagen")):
                actual["p"].append(txt)
            else:
                actual = {"p": [txt], "r": [], "f": f}
                inter.append(actual)
        else:
            if actual is None:
                actual = {"p": [], "r": [], "f": f}
                inter.append(actual)
            actual["r"].append(txt)
        mapa.append(len(inter) - 1)
    return inter, mapa


def titulo_de(c, lista):
    for k in CLAVES_TITULO:
        if isinstance(c.get(k), str) and c[k].strip() and len(c[k]) < 300:
            return c[k].strip()
    for x in lista:
        primera = next((l for l in x.preg.texto.strip().split("\n") if l.strip() and not l.startswith("[")), "")
        if primera:
            return (primera[:60] + "…") if len(primera) > 60 else primera
    return "(sin título)"


AVISOS = []                           # avisos para el usuario tras la carga


def tipo_media_de(nombre):
    return {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
            ".webp": "image/webp", ".pdf": "application/pdf", ".heic": "image/heic", ".mp3": "audio/mpeg",
            ".m4a": "audio/mp4", ".3gp": "audio/3gpp", ".mp4": "video/mp4"}.get(os.path.splitext(nombre)[1].lower())


def procesar_takeout(entradas, opts, reg, convs, poner_doc):
    """Google Takeout: Gemini (Mi actividad > Apps de Gemini), Keep y el resto de productos."""
    nombres_json = {e[2][:-5].lower() for e in entradas if e[2].lower().endswith(".json")}
    gem_items, notas, productos = [], [], Counter()
    nb_meta, nb_chats = {}, []                      # NotebookLM: cuaderno -> (título, creado, visto)
    for _, _, nombre, tipo, datos in entradas:
        prod = producto_takeout(nombre)
        partes_nb = [x for x in descodificar_google(nombre).replace("\\", "/").split("/") if x]
        cuaderno = None
        if any(x.lower() == "notebooklm" for x in partes_nb):
            i = [x.lower() for x in partes_nb].index("notebooklm")
            cuaderno = partes_nb[i + 1] if len(partes_nb) > i + 2 else None
            if cuaderno and tipo == "json" and isinstance(datos, dict) and isinstance(datos.get("metadata"), dict) \
                    and len(partes_nb) == i + 3 and "createTime" in datos["metadata"]:
                md = datos["metadata"]
                nb_meta[cuaderno] = (datos.get("title") or cuaderno, fecha(md.get("createTime")),
                                     fecha(md.get("lastViewed")) or fecha(md.get("createTime")))
                continue
            if cuaderno and tipo == "texto" and "chat history" in nombre.lower():
                nb_chats.append((cuaderno, nombre, datos))
                continue
            if cuaderno and tipo == "json":
                if nombre.lower().endswith("metadata.json"):
                    continue                        # datos internos de cada fuente: no aportan texto
                poner_doc("NotebookLM", cuaderno, f"[NotebookLM] {descodificar_google(os.path.basename(nombre))}",
                          "\n".join(json_a_texto(datos)), None)
                continue
        productos[prod] += 1
        en_keep = normalizar(prod) == "keep"
        en_gemini = "gemini" in normalizar(nombre) or "bard" in normalizar(nombre)
        if tipo in ("binref", "binario"):
            origen = "keep" if en_keep else "gemini" if en_gemini else "takeout"
            a = reg.nuevo(nombre, origen, None) if tipo == "binref" else \
                reg.nuevo(nombre, origen, None, b64=base64.b64encode(datos).decode())
            a.tipo_media = tipo_media_de(nombre)
            if tipo == "binref":
                a.ref, a.tam_ref, a.sin_contenido = datos[:2], datos[2], False
            continue
        if tipo == "json":
            lista = datos if isinstance(datos, list) else [datos]
            act = [d for d in lista if es_actividad(d)]
            if act:
                gem = [d for d in act if es_gemini(d)]
                gem_items += [gemini_item(d) for d in gem]
                otras = [d for d in act if not es_gemini(d)]
                if otras:
                    por_mes = {}
                    for d in sorted(otras, key=lambda d: ts(fecha(d.get("time")))):
                        f = fecha(d.get("time"))
                        linea = f"{fmt_fecha(f)}  {d.get('title', '')}"
                        if d.get("titleUrl"):
                            linea += f"\n      {d['titleUrl']}"
                        for st in d.get("subtitles") or []:
                            if isinstance(st, dict) and st.get("name"):
                                linea += f"\n      {st['name']}"
                        por_mes.setdefault(f.strftime("%Y-%m") if f else "sin fecha", []).append((f, linea))
                    for mes, ls in por_mes.items():
                        poner_doc("Google", f"Actividad: {prod}", f"[Actividad {mes}] {prod}",
                                  "\n".join(l for _, l in ls), ls[0][0])
                continue
            if es_nota_keep(datos):
                if gemini_desde_keep(datos, convs):
                    ESTAD["keep_notas_de_gemini"] += 1
                    continue
                notas.append(nota_keep(datos))
                continue
            texto = "\n".join(json_a_texto(datos))
            if texto.strip():
                poner_doc("Google", prod, f"[{prod}] {os.path.basename(nombre)}", texto, None)
            continue
        # tipo texto
        bajo = nombre.lower()
        if bajo.endswith((".html", ".htm")):
            if en_keep and bajo[:-5] in nombres_json:
                continue                                # la nota ya se lee de su .json
            if en_gemini and re.search(r"(my ?activity|mi ?actividad|mis ?actividades)", normalizar(nombre)):
                gem_items += gemini_html_items(datos)
                continue
            texto = html_a_texto(datos)
        else:
            texto = datos
        if not texto.strip():
            ESTAD["takeout_vacio:" + os.path.basename(nombre)] += 1
            continue
        vis = descodificar_google(os.path.basename(nombre))
        if cuaderno:
            poner_doc("NotebookLM", cuaderno, f"[Fuente de NotebookLM] {vis}", texto, None, "takeout", nombre)
        else:
            poner_doc("Google", prod, f"[{prod}] {vis}", texto, None,
                      "keep" if en_keep else "gemini" if en_gemini else "takeout", nombre)

    # NotebookLM: el historial viene con lo más nuevo arriba y cada respuesta ANTES de su pregunta
    for cuaderno, nombre, h in nb_chats:
        bloques = re.split(r"(?m)^(USER|MODEL):\s*", h)
        msgs_nb = []
        for k in range(1, len(bloques) - 1, 2):
            txt = html_a_texto(bloques[k + 1])
            if txt.strip():
                msgs_nb.append(("user" if bloques[k] == "USER" else "ai", txt))
        msgs_nb.reverse()
        titulo, creado, visto = nb_meta.get(cuaderno, (cuaderno, None, None))
        inter, _ = agrupar([(r, t, visto or creado) for r, t in msgs_nb])
        lista = [Intercambio("\n\n".join(x["p"]), "\n\n".join(x["r"]), x["f"]) for x in inter]
        if lista:
            convs.append(Conversacion(f"{titulo} (cuaderno de NotebookLM)", creado, visto, lista, "NotebookLM",
                                      "NOTEBOOKLM"))
    ESTAD["notebooklm_chats"] = len(nb_chats)

    # Gemini: conversaciones reconstruidas
    n_gem = 0
    for msgs in conversaciones_gemini(gem_items):
        inter, _ = agrupar(msgs)
        lista = [Intercambio("\n\n".join(x["p"]), "\n\n".join(x["r"]), x["f"]) for x in inter]
        if not lista:
            continue
        fechas = [f for _, _, f in msgs if f]
        primera = next((l for l in lista[0].preg.texto.split("\n") if l.strip()), "Conversación de Gemini")
        cv = Conversacion((primera[:60] + "…") if len(primera) > 60 else primera, min(fechas, key=ts, default=None),
                          max(fechas, key=ts, default=None), lista, "Gemini", "GEMINI")
        convs.append(cv)
        n_gem += 1
    ESTAD["gemini_conversaciones"] = n_gem
    ESTAD["gemini_actividades"] = len(gem_items)
    if n_gem == 0 and any("gemini" in normalizar(e[2]) for e in entradas):
        AVISOS.append("Este Takeout no trae conversaciones de Gemini (solo Gems u opciones vacías). Las conversaciones "
                      "están en «Mi actividad» → «Apps de Gemini», en formato JSON: mira el apartado 2d del manual.")

    # Keep: una «conversación» con una nota por apartado
    if notas:
        notas.sort(key=lambda n: ts(n[2]))
        lista = [Intercambio(cab, txt, f) for cab, txt, f, _ in notas]
        fechas = [n[2] for n in notas if n[2]]
        cv = Conversacion("Notas de Google Keep", min(fechas, key=ts, default=None),
                          max(fechas, key=ts, default=None), lista, "Keep", "NOTA", doc=True)
        for i, (cab, txt, f, tit) in enumerate(notas):
            a = reg.nuevo(f"{nombre_seguro(tit or 'nota_' + fmt_dia(f), 60)}.txt", "keep", f, txt)
            a.conv, a.ei = cv, i
            cv.archivos.append(a)
        convs.append(cv)
    ESTAD["keep_notas"] = len(notas)
    for p, n in productos.items():
        ESTAD["takeout_producto:" + p] = n


def construir(entradas, opts):
    """entradas: lista de (etiqueta, categoria, nombre, tipo, datos).
    Devuelve (conversaciones, registro_de_archivos)."""
    ESTAD.clear()
    AVISOS.clear()
    reg = Registro()
    convs = []
    desde = datetime.strptime(opts.desde, "%Y-%m-%d").date() if opts.desde else None
    hasta = datetime.strptime(opts.hasta, "%Y-%m-%d").date() if opts.hasta else None
    docs = {}                                       # (fuente, grupo) -> [(titulo, texto, fecha)]

    def poner_doc(fuente, grupo, titulo, texto, f=None, origen=None, ruta=None):
        a = reg.nuevo(ruta or titulo, origen, f, texto) if origen else None
        docs.setdefault((fuente, grupo), []).append((titulo, texto, f, a))

    # Primero los chats y sesiones (crean archivos); después los documentos
    orden = sorted(entradas, key=lambda e: 0 if e[1] in (None, "conversations") else 1)
    takeout = [e for e in orden if e[1] == "takeout"]
    if takeout:
        procesar_takeout(takeout, opts, reg, convs, poner_doc)
    for etiqueta, categoria, nombre, tipo, datos in orden:
        ESTAD[f"entrada:{categoria}:{tipo}"] += 1
        if categoria == "takeout":
            continue
        if tipo == "binref":
            a = reg.nuevo(nombre, {"projects": "proyecto", "memories": "memoria"}.get(categoria, "export"), None)
            a.ref, a.tam_ref, a.sin_contenido, a.tipo_media = datos[:2], datos[2], False, tipo_media_de(nombre)
            continue
        if tipo == "binario":
            tm = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
                  ".webp": "image/webp", ".pdf": "application/pdf"}.get(os.path.splitext(nombre)[1].lower())
            origen = {"projects": "proyecto", "memories": "memoria"}.get(categoria, "export")
            reg.nuevo(nombre, origen, None, b64=base64.b64encode(datos).decode(), tipo_media=tm)
            continue
        if tipo == "texto":
            fuente = NOMBRE_CATEGORIA.get(categoria, "Exportación")
            origen = {"projects": "proyecto", "memories": "memoria"}.get(categoria, "export")
            partes = nombre.replace("\\", "/").split("/")
            grupo = partes[-2] if len(partes) > 1 else fuente
            poner_doc(fuente, grupo, nombre, datos, None, origen, nombre)
            continue

        # ----- JSON
        hallado = False
        if categoria not in ("projects", "memories", "feedback", "light_metadata"):
            for c in encontrar_conversaciones(datos):
                fuente, voz, extractor = detectar(c, etiqueta, categoria)
                ctx = Ctx(opts, reg)
                try:
                    msgs = extractor(c, opts, ctx)
                except Exception as e:
                    ESTAD["error_extractor:" + type(e).__name__] += 1
                    continue
                inter, mapa = agrupar(msgs)
                if not inter:
                    continue
                hallado = True
                f_conv = primera_fecha(c, CLAVES_CREADA) or primera_fecha(c.get("__padre__") or {}, CLAVES_CREADA) \
                    or next((x["f"] for x in inter if x["f"]), None)
                fechas_msg = [f for _, _, f in msgs if f]
                act = max([x for x in [primera_fecha(c, CLAVES_ACT), f_conv] + fechas_msg if x], key=ts, default=None)
                if desde and (not f_conv or f_conv.date() < desde):
                    continue
                if hasta and (not f_conv or f_conv.date() > hasta):
                    continue
                lista = [Intercambio("\n\n".join(x["p"]), "\n\n".join(x["r"]), x["f"] or f_conv) for x in inter]
                titulo = titulo_sesion(c) if "__eventos__" in c else ""
                titulo = titulo or titulo_de(c, lista)
                cv = Conversacion(titulo, f_conv, act, lista, fuente, voz)
                for a in ctx.archivos:
                    a.conv = cv
                    a.ei = mapa[min(a.msg, len(mapa) - 1)] if mapa else 0
                    a.fecha = a.fecha or f_conv
                cv.archivos = list(ctx.archivos) + sorted(ctx.editados, key=lambda a: a.nombre)
                convs.append(cv)
        if hallado:
            continue

        # ----- Proyectos
        fuente = NOMBRE_CATEGORIA.get(categoria, "Exportación")
        if categoria == "projects":
            proys = buscar_proyectos(datos)
            for p in proys:
                pn = p.get("name") or p.get("title") or "Proyecto"
                f = primera_fecha(p, CLAVES_CREADA)
                info = {k: v for k, v in p.items() if k not in ("docs", "files")}
                poner_doc("Proyecto", pn, f"[Proyecto] {pn}", "\n".join(json_a_texto(info)), f)
                for d in (p.get("docs") or []) + (p.get("files") or []):
                    if isinstance(d, dict):
                        dn = d.get("filename") or d.get("file_name") or d.get("name") or "documento"
                        dc = d.get("content") if isinstance(d.get("content"), str) else None
                        if dc is not None:
                            poner_doc("Proyecto", pn, f"[Documento del proyecto] {dn}", dc,
                                      primera_fecha(d, CLAVES_CREADA) or f, "proyecto", dn)
                        else:
                            reg.nuevo(dn, "proyecto", f)
            if proys:
                continue

        # ----- Documentos sueltos (memoria, etc.)
        documentos = buscar_documentos(datos)
        if documentos and categoria in ("memories", "projects"):
            origen = "memoria" if categoria == "memories" else "proyecto"
            for nom, cont, d in documentos:
                poner_doc(fuente, fuente, nom, cont, primera_fecha(d, CLAVES_CREADA), origen, nom)
            continue
        texto = "\n".join(json_a_texto(datos))
        if texto.strip() and categoria is not None:
            poner_doc(fuente, fuente, nombre, texto, None)
        elif categoria is None:
            ESTAD["json_sin_conversaciones"] += 1

    # Documentos agrupados como "conversaciones" para poder buscarlos y leerlos igual
    for (fuente, grupo), items in docs.items():
        lista = [Intercambio(t if t.startswith("[") else f"[Documento] {t}", txt, f) for t, txt, f, _ in items]
        fechas = [f for _, _, f, _ in items if f]
        titulo = grupo if grupo != fuente else {"Memoria": "Memoria de Claude", "Cuenta": "Datos de la cuenta",
                                                 "Valoraciones": "Tus valoraciones"}.get(fuente, fuente)
        cv = Conversacion(titulo, min(fechas, key=ts) if fechas else None, max(fechas, key=ts) if fechas else None,
                          lista, fuente, fuente.upper(), doc=True)
        for i, (_, _, _, a) in enumerate(items):
            if a is not None:
                a.conv, a.ei = cv, i
                cv.archivos.append(a)
        convs.append(cv)

    # Quitar duplicados (el mismo archivo cargado dos veces, la misma conversación en dos exportaciones)
    # Exportaciones sucesivas de la misma conversación: se queda la versión más completa
    mejor = {}
    for cv in convs:
        clave = (cv.fuente, cv.doc, cv.titulo, cv.inter[0].preg.texto[:300] if cv.inter else "")
        prev = mejor.get(clave)
        if prev is None:
            mejor[clave] = cv
            continue
        ESTAD["duplicados_quitados"] += 1
        if (len(cv.inter), ts(cv.actividad)) > (len(prev.inter), ts(prev.actividad)):
            mejor[clave] = cv
    convs = list(mejor.values())
    convs.sort(key=lambda cv: ts(cv.actividad or cv.fecha))
    ESTAD["conversaciones"] = len(convs)
    ESTAD["archivos"] = len(reg.lista)
    return convs, reg


# ----------------------------------------------------------------------------
# Conversaciones guardadas como .md/.txt por extensiones (Kimi, Lumo...)
# ----------------------------------------------------------------------------
RE_HABLANTE = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:\*\*|__)?\s*(?:[^\w\s]{1,3}\s*)?"
    r"(user|usuario|you|t[uú]|human|humano|me|yo|pregunta|prompt|"
    r"assistant|asistente|kimi|lumo|ai|ia|chatgpt|gpt|claude|deepseek|gemini|mistral|bot|respuesta)"
    r"\s*(?:\*\*|__)?\s*:?\s*(?:\*\*|__)?\s*(.*)$", re.I)
USUARIO_MD = ("user", "usuario", "you", "tu", "tú", "human", "humano", "me", "yo", "pregunta", "prompt")


def parsear_markdown(nombre, texto, mtime=None):
    titulo, msgs, actual = None, [], None
    for linea in texto.splitlines():
        m = RE_HABLANTE.match(linea)
        es_cabecera = m and (linea.lstrip().startswith(("#", "**", "__")) or re.match(r"^\s*\S+\s*:", linea))
        if es_cabecera and len(linea) < 200:
            quien = normalizar(m.group(1))
            actual = {"role": "user" if quien in USUARIO_MD else "assistant", "content": []}
            msgs.append(actual)
            resto = m.group(2).strip()
            if resto:
                actual["content"].append(resto)
            continue
        if titulo is None and linea.startswith("# "):
            titulo = linea[2:].strip()
            continue
        if actual is not None:
            actual["content"].append(linea)
    for x in msgs:
        x["content"] = "\n".join(x["content"]).strip()
    fm = re.search(r"(20\d\d-\d\d-\d\d(?:[ T]\d\d:\d\d(?::\d\d)?)?)", texto[:2000]) \
        or re.search(r"(20\d\d-\d\d-\d\d)", os.path.basename(nombre))
    return {"title": titulo or os.path.splitext(os.path.basename(nombre))[0],
            "created_at": fm.group(1) if fm else mtime, "messages": msgs}


# ----------------------------------------------------------------------------
# Lectura de archivos (.json, .jsonl, .md, .zip, carpetas; partes de Claude)
# ----------------------------------------------------------------------------
CATEGORIAS_CLAUDE = ("conversations", "claude_code_cloud", "projects", "memories", "feedback", "light_metadata")
RE_PARTE = re.compile(r"(conversations|claude_code_cloud|projects|memories|feedback|light_metadata)-(\d{3})"
                      r"(?:\s*\(\d+\))?\.zip$", re.I)
CARPETAS_DESCARGA = [os.path.expanduser("~/storage/downloads"),
                     os.path.expanduser("~/storage/shared/Download"), "/sdcard/Download"]


def categoria_de(ruta):
    m = RE_PARTE.search(os.path.basename(ruta))
    return m.group(1).lower() if m else None


def prefijo_parte(ruta):
    """Lo que va delante de la parte: '2026-11-11-11-30-02claude' en '2026-11-11-11-30-02claudeconversations-000.zip'."""
    m = RE_PARTE.search(os.path.basename(ruta))
    return os.path.basename(ruta)[:m.start()] if m else None


IA_POR_NOMBRE = (("kimi", "Kimi"), ("moonshot", "Kimi"), ("lumo", "Lumo"), ("proton", "Lumo"),
                 ("duck.ai", "Duck.ai"), ("duckai", "Duck.ai"), ("savedaichats", "Duck.ai"),
                 ("deepseek", "DeepSeek"), ("chatgpt", "ChatGPT"), ("openai", "ChatGPT"),
                 ("perplexity", "Perplexity"), ("euria", "Euria"), ("eunia", "Euria"), ("infomaniak", "Euria"),
                 ("apertus", "Apertus"), ("myai", "MyAI"), ("my_ai", "MyAI"), ("copilot", "Copilot"),
                 ("grok", "Grok"), ("qwen", "Qwen"), ("lechat", "Mistral"), ("notebooklm", "NotebookLM"),
                 ("claude", "Claude"), ("gemini", "Gemini"), ("bard", "Gemini"), ("mistral", "Mistral"))
# Palabras del nombre que no son el tema: articulo_lumo_suiza.txt -> tema «suiza»
PALABRAS_NO_TEMA = {"articulo", "conversacion", "conversaciones", "chat", "chats", "ai", "ia", "txt", "transcripcion"}


def etiqueta_por_nombre(ruta):
    n = normalizar(os.path.basename(ruta))
    for clave, eti in IA_POR_NOMBRE:
        if clave in n:
            return eti
    if categoria_de(ruta):
        return "Claude"
    # Nombre libre: 2026-11-12-11-11-11_mia_tema.txt o ..._articulo_mia_tema.txt -> «Mia»
    m = re.search(r"(?:^|[_\-])(?:articulo|conversacion|chat)_([a-z][a-z0-9.]{1,20})_", n)
    return m.group(1).capitalize() if m else None


def tema_de_nombre(ruta, ia=None):
    """«2026-06-05articulo_lumo_oportunidadesDigitales.txt» -> «Oportunidades Digitales»."""
    b = os.path.splitext(os.path.basename(ruta))[0]
    b = re.sub(r"^[0-9a-f]{8}-(?=20\d\d)", "", b)                       # prefijo de las subidas a Claude
    b = re.sub(r"^20\d\d-\d\d-\d\d(?:[-_ T]\d\d[-:]\d\d(?:[-:]\d\d)?)?[-_ ]?", "", b)
    claves = {k for k, e in IA_POR_NOMBRE if ia and e == ia} | {normalizar(ia or "")}
    trozos = []
    for t in re.split(r"[_\s]+", b):
        nt = normalizar(t)
        if not t or nt in PALABRAS_NO_TEMA or nt in claves:
            continue
        for k in sorted(claves, key=len, reverse=True):
            if k and nt.startswith(k) and len(nt) > len(k):     # «articuloLumo» o «lumoSuiza»
                t = t[len(k):]
                break
        if normalizar(t) in PALABRAS_NO_TEMA:
            continue
        trozos.append(re.sub(r"(?<=[a-záéíóúñ])(?=[A-ZÁÉÍÓÚÑ])", " ", t))
    tema = " ".join(trozos).strip(" -_")
    return (tema[:1].upper() + tema[1:]) if tema else ""


def parsear_json(nombre, b):
    t = b.decode("utf-8-sig", "replace")
    if nombre.lower().endswith(".jsonl"):
        out = []
        for l in t.splitlines():
            if l.strip():
                try:
                    out.append(json.loads(l))
                except json.JSONDecodeError:
                    ESTAD["jsonl_linea_rota"] += 1
        return out
    return json.loads(t)


def es_texto(nombre, b):
    if nombre.lower().endswith(EXT_TEXTO):
        return True
    if "." in os.path.basename(nombre) or b[:4096].count(b"\0"):
        return False
    try:
        b[:4096].decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def entradas_de_bytes(nombre, b, eti, categoria, mtime=None):
    """Convierte un archivo en entradas (etiqueta, categoria, nombre, tipo, datos)."""
    bajo = nombre.lower()
    if categoria is None and re.search(r"(^|/)takeout/", bajo.replace("\\", "/")):
        categoria = "takeout"
    if bajo.endswith((".json", ".jsonl")):
        if len(b) > 100_000_000:
            print(GRIS(f"   leyendo {os.path.basename(nombre)} ({len(b) / 1048576:,.0f} MB), puede tardar..."), flush=True)
        try:
            datos = parsear_json(nombre, b)
            if isinstance(datos, dict) and str(datos.get("herramienta", "")).startswith("duck.py"):
                eti = "Duck.ai"           # exportación de "duck.py chats" (el nombre es solo la fecha)
            return [(eti, categoria, nombre, "json", datos)]
        except (json.JSONDecodeError, UnicodeDecodeError):
            ESTAD["json_roto"] += 1
            return [(eti, categoria, nombre, "texto", b.decode("utf-8", "replace"))] if categoria else []
    if categoria is None:
        # Fuera de las partes de Claude, un .md/.txt es una conversación guardada (Kimi, Lumo...)
        if bajo.endswith((".md", ".markdown", ".txt")):
            return [(eti, None, nombre, "json",
                     parsear_texto_conversacion(nombre, b.decode("utf-8-sig", "replace"), mtime))]
        return []
    if categoria == "takeout" and bajo.endswith((".html", ".htm", ".csv", ".ics", ".vcf", ".txt", ".md", ".xml")):
        return [(eti, categoria, nombre, "texto", b.decode("utf-8-sig", "replace"))]
    if es_texto(nombre, b):
        return [(eti, categoria, nombre, "texto", b.decode("utf-8-sig", "replace"))]
    return [(eti, categoria, nombre, "binario", b)]


def leer_zip(origen, eti, categoria, prof=0):
    out = []
    with zipfile.ZipFile(io.BytesIO(origen) if isinstance(origen, bytes) else origen) as z:
        nombres = [n for n in z.namelist() if not n.endswith("/") and "__MACOSX" not in n]
        if categoria is None and any(n.lower().startswith("takeout/") for n in nombres):
            categoria = "takeout"
        info = {i.filename: i.file_size for i in z.infolist()}
        if categoria is None:
            # .zip de otras IA (ChatGPT trae también chat.html, imágenes...): solo los JSON
            js = [n for n in nombres if n.lower().endswith((".json", ".jsonl"))]
            conv = [n for n in js if "conversation" in os.path.basename(n).lower()]
            elegidos = conv or js or [n for n in nombres if n.lower().endswith((".md", ".markdown", ".txt"))]
        else:
            elegidos = nombres
        for n in elegidos:
            if n.lower().endswith(".zip"):
                continue
            bajo = n.lower()
            leible = bajo.endswith(EXT_TEXTO + (".ics", ".vcf", ".jsonl")) or "." not in os.path.basename(n)
            es_json = bajo.endswith((".json", ".jsonl"))
            if categoria and isinstance(origen, str) and (not leible or (not es_json and info.get(n, 0) > 20_000_000)):
                # Binario (imágenes, audio, PDF...) o texto enorme (mbox): solo se apunta; se lee al guardarlo
                out.append((eti, categoria, n, "binref", (origen, n, info.get(n, 0))))
                continue
            try:
                out += entradas_de_bytes(n, z.read(n), eti, categoria)
            except Exception:
                ESTAD["error_lectura_zip"] += 1
        if prof < 2:
            for n in nombres:
                if n.lower().endswith(".zip"):
                    try:
                        out += leer_zip(z.read(n), eti, categoria or categoria_de(n), prof + 1)
                    except Exception:
                        ESTAD["error_zip_anidado"] += 1
    return out


RE_IGNORAR = re.compile(r"(buscarai_diagnostico|duckai_diag|archivos_sin_contenido|manual|^config\.json$|"
                        r"^puertos\.txt$|^ai_diag_|requirements|readme)", re.I)


def cargar(ruta):
    """Devuelve lista de entradas (etiqueta, categoria, nombre, tipo, datos)."""
    eti = etiqueta_por_nombre(ruta)
    cat = categoria_de(ruta)
    out = []
    if os.path.isdir(ruta):
        for base, dirs, fs in os.walk(ruta):
            # Varias exportaciones de Claude en la misma carpeta: solo la más reciente
            exps = sorted(d for d in dirs if d.startswith("claude_export_"))
            dirs[:] = sorted(d for d in dirs if not d.startswith((".", "logs", "__"))
                             and (not d.startswith("claude_export_") or d == (exps[-1] if exps else d)))
            for f in sorted(fs):
                p = os.path.join(base, f)
                bajo = f.lower()
                en_takeout = "/takeout/" in (p.replace("\\", "/").lower() + "/")
                ext_ok = (".zip", ".json", ".jsonl", ".md", ".markdown", ".txt") + \
                    ((".html", ".htm", ".csv", ".ics", ".vcf") if en_takeout else ())
                if not bajo.endswith(ext_ok) or (RE_IGNORAR.search(f) and not en_takeout):
                    continue
                if bajo.endswith(".zip"):
                    try:
                        out += leer_zip(p, eti or etiqueta_por_nombre(p), categoria_de(p))
                    except zipfile.BadZipFile:
                        ESTAD["zip_roto"] += 1
                elif not es_manifiesto(p):
                    with open(p, "rb") as fh:
                        out += entradas_de_bytes(p, fh.read(), etiqueta_por_nombre(p) or eti, None,
                                                 os.path.getmtime(p))
    elif ruta.lower().endswith(".zip"):
        out += leer_zip(ruta, eti, cat)
    else:
        with open(ruta, "rb") as fh:
            out += entradas_de_bytes(ruta, fh.read(), eti, None, os.path.getmtime(ruta))
    return out


# ----------------------------------------------------------------------------
# Índice (manifest) de Claude y localización de las partes
# ----------------------------------------------------------------------------
def es_manifiesto(ruta):
    """El índice (manifest-....json) que envía Claude con los enlaces de cada parte."""
    try:
        if not ruta.lower().endswith(".json") or os.path.getsize(ruta) > 200000:
            return None
        with open(ruta, encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception:
        return None
    if isinstance(d, dict) and isinstance(d.get("data_files"), list) and \
            any(isinstance(x, dict) and "export_url" in x for x in d["data_files"]):
        return d
    return None


def mas_reciente_por_parte(rutas):
    """De varias copias (p. ej. 'conversations-000(1).zip') se queda con la más reciente de cada parte."""
    elegidas = {}
    for r in rutas:
        m = RE_PARTE.search(os.path.basename(r))
        clave = (m.group(1).lower(), m.group(2)) if m else r
        orden = lambda p: (prefijo_parte(p) or "", os.path.getmtime(p))
        if clave not in elegidas or orden(r) > orden(elegidas[clave]):
            elegidas[clave] = r
    return [elegidas[k] for k in sorted(elegidas, key=str)]


def partes_en(carpeta):
    return mas_reciente_por_parte([p for p in glob.glob(os.path.join(glob.escape(carpeta), "*.zip"))
                                   if categoria_de(p)])


def partes_descargadas(manif, carpeta_manif):
    """Busca las partes del índice ya descargadas (junto al índice, aquí o en Descargas)."""
    esperadas = [x.get("filename") for x in manif["data_files"] if isinstance(x, dict) and x.get("filename")]
    exports = sorted((e for e in glob.glob(os.path.join(glob.escape(os.getcwd()), "claude_export_*"))
                      if os.path.isdir(e)), key=os.path.getmtime, reverse=True)
    dirs = [carpeta_manif] + exports + [os.getcwd()] + CARPETAS_DESCARGA
    encontradas = []
    for nom in esperadas:
        base, ext = os.path.splitext(nom)
        for d in dirs:
            cands = glob.glob(os.path.join(glob.escape(d), glob.escape(base) + "*" + ext))
            if cands:
                encontradas.append(max(cands, key=os.path.getmtime))
                break
    return esperadas, encontradas


def explicar_manifiesto(ruta, manif):
    print(AMAR("\nEse archivo es el ÍNDICE de la exportación de Claude, no el historial."))
    print("Claude entrega la exportación en varias partes; el índice solo trae los enlaces.")
    print("Lo más cómodo es que el programa las descargue todas:\n")
    print(CIAN(f"  python buscarai.py -d {ruta}\n"))
    print(GRIS(f"Índice creado: {fmt_fecha(fecha(manif.get('created_at')))} · los enlaces caducan a las 24 horas."))


def ampliar_con_partes(rutas):
    """Si se abre una parte de Claude, se añaden las demás partes de la misma carpeta."""
    out = []
    for r in rutas:
        if categoria_de(r) and os.path.isfile(r):
            pre = prefijo_parte(r)
            hermanas = [p for p in glob.glob(os.path.join(glob.escape(os.path.dirname(os.path.abspath(r))), "*.zip"))
                        if categoria_de(p) and prefijo_parte(p) == pre]
            out += mas_reciente_por_parte(hermanas)
        m = re.match(r"^(.*takeout-[0-9A-Za-z]+-\d+-)\d{3}\.zip$", os.path.basename(r), re.I)
        if m and os.path.isfile(r):
            out += sorted(glob.glob(os.path.join(glob.escape(os.path.dirname(os.path.abspath(r))),
                                                 glob.escape(m.group(1)) + "[0-9][0-9][0-9].zip")))
        out.append(r)
    vistos, final = set(), []
    for r in out:
        k = os.path.abspath(r)
        if k not in vistos:
            vistos.add(k)
            final.append(r)
    # Si hay varias copias de la misma parte, solo la más reciente
    pedidas = [r for r in final if categoria_de(r)]
    # Si se piden partes de distintas copias (prefijos), se respetan todas; si no, la más reciente
    if len({prefijo_parte(p) for p in pedidas}) > 1:
        partes = pedidas
    else:
        partes = mas_reciente_por_parte(pedidas)
    return [r for r in final if not categoria_de(r)] + partes


CARPETA_CONFIG = os.path.expanduser("~/buscarai")
FUENTES = os.path.join(CARPETA_CONFIG, "fuentes.txt")
FUENTES_DEFECTO = ["~/historial_ia", "~/duck/chats", "~/duck/txt"]


def forma_guardada(x):
    """Cómo se guarda una fuente: relativa si está junto a buscarai.py, con ~ si está en casa, si no absoluta.
    Si no existe como ruta, se guarda tal cual (sirve como prefijo de nombre, p. ej. 2026-11-11-11-30-02)."""
    p = os.path.expanduser(x)
    if not os.path.exists(p):
        return x
    p = os.path.abspath(p)
    if p == CARPETA_PROGRAMA or p.startswith(CARPETA_PROGRAMA.rstrip("/") + "/"):
        return os.path.relpath(p, CARPETA_PROGRAMA)
    casa = os.path.expanduser("~")
    if p.startswith(casa.rstrip("/") + "/"):
        return "~/" + os.path.relpath(p, casa)
    return p


def leer_fuentes(crear=True):
    """Lista de carpetas/archivos que se cargan cuando no se indica ninguno."""
    if not os.path.exists(FUENTES):
        if not crear:
            return []
        os.makedirs(CARPETA_CONFIG, exist_ok=True)
        os.makedirs(os.path.expanduser("~/historial_ia"), exist_ok=True)
        with open(FUENTES, "w", encoding="utf-8") as fh:
            fh.write("# Fuentes que buscarai.py carga siempre (una carpeta o archivo por línea).\n"
                     "# Las carpetas se recorren enteras: pon en ~/historial_ia todo lo de tus IA\n"
                     "# (claude_export_..., .zip de ChatGPT/DeepSeek/Takeout, archivos de Lumo, .txt...).\n"
                     + "\n".join(FUENTES_DEFECTO) + "\n")
    out = []
    with open(FUENTES, encoding="utf-8") as fh:
        for l in fh:
            l = l.strip()
            if l and not l.startswith("#"):
                out.append(l)
    return out


def guardar_fuentes(lista):
    os.makedirs(CARPETA_CONFIG, exist_ok=True)
    viejas = open(FUENTES, encoding="utf-8").read().splitlines() if os.path.exists(FUENTES) else []
    cab = [l for l in viejas if l.startswith("#")]
    with open(FUENTES, "w", encoding="utf-8") as fh:
        fh.write("\n".join(cab + lista) + "\n")


CARPETA_PROGRAMA = os.path.dirname(os.path.abspath(__file__))


def resolver_ruta(f):
    """'~/x' -> casa; 'x' (relativa) -> junto a buscarai.py; si no existe, se prueba como prefijo de nombre."""
    p = os.path.expanduser(f)
    if not os.path.isabs(p):
        cand = os.path.join(CARPETA_PROGRAMA, p)
        p = cand if os.path.exists(cand) or not os.path.exists(p) else os.path.abspath(p)
    return p


def expandir_prefijo(texto, carpetas=None):
    """'2026-11-11-11-30-02' -> todos los archivos cuyo nombre empieza así (en la carpeta actual, la de
    buscarai.py, Descargas y las carpetas de las fuentes)."""
    base = os.path.basename(texto.rstrip("/"))
    dirs = [os.path.dirname(os.path.expanduser(texto))] if os.path.dirname(texto) else []
    dirs += [os.getcwd(), CARPETA_PROGRAMA] + CARPETAS_DESCARGA + (carpetas or [])
    out, vistos = [], set()
    for d in dirs:
        d = os.path.abspath(os.path.expanduser(d)) if d else ""
        if not d or d in vistos or not os.path.isdir(d):
            continue
        vistos.add(d)
        for p in sorted(glob.glob(os.path.join(glob.escape(d), glob.escape(base) + "*"))):
            if os.path.isfile(p) and p.lower().endswith((".zip", ".json", ".jsonl", ".md", ".txt")) \
                    and not RE_IGNORAR.search(os.path.basename(p)):
                out.append(p)
        if out:
            break
    return out


def rutas_de_fuentes():
    """Todas las fuentes guardadas que existen + la exportación de Claude que se encuentre sola."""
    rutas = []
    for f in leer_fuentes():
        p = resolver_ruta(f)
        if os.path.exists(p):
            rutas.append(p)
        else:
            rutas += expandir_prefijo(f)
    dentro = lambda p: any(os.path.abspath(p).startswith(os.path.abspath(r).rstrip("/") + "/") for r in rutas
                           if os.path.isdir(r))
    extra = [p for p in buscar_archivo_por_defecto() if not dentro(p)]
    return rutas + [p for p in extra if p not in rutas]


def buscar_archivo_por_defecto():
    """Devuelve una lista de rutas: la exportación más reciente que encuentre."""
    exports = [e for e in glob.glob(os.path.join(glob.escape(os.getcwd()), "claude_export_*")) if os.path.isdir(e)]
    if exports:
        ult = max(exports, key=os.path.getmtime)
        p = partes_en(ult)
        if p:
            return p
    dirs = [os.getcwd(), os.path.dirname(os.path.abspath(__file__))] + CARPETAS_DESCARGA
    for d in dirs:
        p = os.path.join(d, "conversations.json")
        if os.path.isfile(p):
            return [p]
    for d in dirs:
        p = partes_en(d)
        if p:
            return p
        zs = sorted(glob.glob(os.path.join(glob.escape(d), "data-*.zip")), key=os.path.getmtime, reverse=True)
        if zs:
            return [zs[0]]
    return []


# ----------------------------------------------------------------------------
# Descarga automática de las partes del índice (Termux + Firefox)
# ----------------------------------------------------------------------------
NAVEGADORES = ["org.mozilla.firefox", "org.mozilla.fenix", "org.mozilla.firefox_beta"]


def carpeta_descargas():
    for d in CARPETAS_DESCARGA:
        if os.path.isdir(d):
            return d
    return None


def buscar_manifiesto_reciente():
    cands = []
    for d in [os.getcwd()] + CARPETAS_DESCARGA:
        cands += glob.glob(os.path.join(glob.escape(d), "manifest-*.json"))
    cands = [c for c in cands if es_manifiesto(c)]
    return max(cands, key=os.path.getmtime) if cands else None


def abrir_en_navegador(url, paquete=None):
    for pkg in ([paquete] if paquete else NAVEGADORES):
        try:
            r = subprocess.run(["am", "start", "-a", "android.intent.action.VIEW", "-d", url, "-p", pkg],
                               capture_output=True, text=True, timeout=20)
            salida = (r.stdout or "") + (r.stderr or "")
            if r.returncode == 0 and "Error" not in salida and "Exception" not in salida:
                return True
        except (OSError, subprocess.SubprocessError):
            break
    try:
        return subprocess.run(["termux-open-url", url], timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def esperar_descarga(carpeta, nombre, desde, limite):
    base, ext = os.path.splitext(nombre)
    patron = os.path.join(glob.escape(carpeta), glob.escape(base) + "*" + ext)
    fin = time.time() + limite
    previo, estable = None, 0
    while time.time() < fin:
        nuevos = [f for f in glob.glob(patron) if os.path.getmtime(f) >= desde - 2]
        if nuevos:
            f = max(nuevos, key=os.path.getmtime)
            tam = os.path.getsize(f)
            if previo and previo == (f, tam) and tam > 0:
                estable += 1
                if estable >= 2:
                    return f
            else:
                estable = 0
            previo = (f, tam)
            print(f"\r   recibiendo {os.path.basename(f)}: {tam / 1024:,.0f} KB      ", end="", flush=True)
        else:
            resto = int(fin - time.time())
            print(f"\r   esperando la descarga... ({resto} s, Ctrl+C para saltar)   ", end="", flush=True)
        time.sleep(1.5)
    return None


def descargar_partes(manif_ruta, destino=None, solo_conv=False, paquete=None, limite=300):
    manif = es_manifiesto(manif_ruta)
    if not manif:
        print(ROJO(f"{manif_ruta} no es un índice (manifest) de Claude."))
        return None
    descargas = carpeta_descargas()
    if not descargas:
        print(ROJO("No veo la carpeta Descargas. Ejecuta una vez: termux-setup-storage"))
        return None
    creado = fecha(manif.get("created_at"))
    if creado and (datetime.now().astimezone() - creado).total_seconds() > 24 * 3600:
        print(AMAR("Aviso: el índice tiene más de 24 horas; los enlaces probablemente han caducado."))
    sello = creado.strftime("%Y-%m-%d_%H%M") if creado else datetime.now().strftime("%Y-%m-%d_%H%M")
    destino = destino or os.path.join(os.getcwd(), f"claude_export_{sello}")
    os.makedirs(destino, exist_ok=True)
    try:
        shutil.copy2(manif_ruta, os.path.join(destino, os.path.basename(manif_ruta)))
    except shutil.SameFileError:
        pass

    partes = [x for x in manif["data_files"] if isinstance(x, dict) and x.get("export_url")]
    if solo_conv:
        partes = [x for x in partes if x.get("category") == "conversations"]
    partes.sort(key=lambda x: x.get("category") != "conversations")

    print(separador("Descarga de la exportación de Claude", VERDE))
    print(GRIS(f"Índice: {os.path.basename(manif_ruta)} · creado {fmt_fecha(creado)}"))
    print(GRIS(f"Descargas de Firefox: {descargas}"))
    print(GRIS(f"Destino: {destino}"))
    print(GRIS("Firefox se abrirá con cada enlace. Si pregunta, pulsa «Descargar»;"))
    print(GRIS("después vuelve a Termux. Ctrl+C salta la parte actual.\n"))
    try:
        subprocess.run(["termux-wake-lock"], timeout=10, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        pass

    ok, fallos = [], []
    for i, x in enumerate(partes, 1):
        nombre = x.get("filename") or f"parte-{i:03d}.zip"
        final = os.path.join(destino, nombre)
        if os.path.isfile(final) and zipfile.is_zipfile(final):
            print(f"[{i}/{len(partes)}] {nombre}: {VERDE('ya estaba descargado')}")
            ok.append(final)
            continue
        print(f"[{i}/{len(partes)}] {NEG(nombre)}  ({x.get('category', '')})")
        inicio = time.time()
        if not abrir_en_navegador(x["export_url"], paquete):
            print(ROJO("   No he podido abrir el navegador. Abre a mano este enlace:"))
            print("   " + x["export_url"])
        try:
            f = esperar_descarga(descargas, nombre, inicio, limite)
        except KeyboardInterrupt:
            f = None
            print(AMAR("\n   Saltado."))
        print()
        if not f:
            print(ROJO(f"   No ha llegado {nombre}."))
            fallos.append(x)
            continue
        if not zipfile.is_zipfile(f):
            print(ROJO(f"   {os.path.basename(f)} no es un .zip válido (¿enlace caducado o ya usado?)."))
            fallos.append(x)
            continue
        shutil.move(f, final)
        print(VERDE(f"   OK → {final}  ({os.path.getsize(final) / 1024:,.0f} KB)"))
        ok.append(final)

    try:
        subprocess.run(["termux-wake-unlock"], timeout=10, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        pass
    print()
    print(separador(f"{len(ok)} de {len(partes)} parte(s) descargada(s)", VERDE if not fallos else AMAR))
    for x in fallos:
        print(ROJO(f"  Falta {x.get('filename')}: ") + x.get("export_url"))
    if fallos:
        print(GRIS("  Puedes repetir el comando: las partes ya bajadas no se vuelven a pedir."))
        print(GRIS("  Si los enlaces ya no funcionan, pide otra exportación a Claude."))
    return ok


# ----------------------------------------------------------------------------
# Extracción de archivos a disco
# ----------------------------------------------------------------------------
def sello():
    return datetime.now().strftime("%Y-%m-%d-%H-%M-%S")


def nombre_seguro(s, maximo=60):
    s = re.sub(r"[^\w\-. ]+", "_", str(s)).strip(" ._")
    return (s[:maximo].strip(" ._") or "sin_nombre")


def guardar_archivo(a, carpeta, todas_versiones=False):
    """Guarda un Archivo en la carpeta. Devuelve la lista de rutas escritas."""
    os.makedirs(carpeta, exist_ok=True)
    escritos = []
    base, ext = os.path.splitext(nombre_seguro(a.nombre, 80))
    if not ext and a.tipo_media:
        ext = EXT_MEDIA.get(a.tipo_media, "")

    def libre(nombre):
        p = os.path.join(carpeta, nombre)
        k = 2
        while os.path.exists(p):
            b2, e2 = os.path.splitext(nombre)
            p = os.path.join(carpeta, f"{b2}_{k}{e2}")
            k += 1
        return p

    if a.ref:
        p = libre(base + ext)
        with zipfile.ZipFile(a.ref[0]) as z, z.open(a.ref[1]) as src, open(p, "wb") as dst:
            shutil.copyfileobj(src, dst)
        return [p]
    if a.b64:
        p = libre(base + ext)
        with open(p, "wb") as fh:
            fh.write(base64.b64decode(a.b64))
        return [p]
    if not a.versiones:
        return []
    vers = list(enumerate(a.versiones, 1)) if todas_versiones and len(a.versiones) > 1 else [(len(a.versiones), a.versiones[-1])]
    for n, (_, texto, _) in vers:
        sufijo = f"_v{n}" if todas_versiones and len(a.versiones) > 1 else ""
        p = libre(base + sufijo + ext)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(texto)
        escritos.append(p)
    return escritos


def extraer_todo(reg, destino, todas_versiones=False):
    """Extrae todos los archivos del catálogo en carpetas por origen y conversación."""
    total, sin = 0, []
    for a in reg.lista:
        if a.sin_contenido:
            sin.append(a)
            continue
        conv = a.conv.titulo if a.conv else ""
        f = a.fecha or (a.conv.fecha if a.conv else None)
        dia = fmt_dia(f) if f else ""
        sub = os.path.join(destino, nombre_seguro(ORIGENES.get(a.origen, a.origen), 40),
                           nombre_seguro("_".join(x for x in (dia, conv) if x) or "sin_fecha", 70))
        total += len(guardar_archivo(a, sub, todas_versiones))
    if sin:
        with open(os.path.join(destino, f"archivos_sin_contenido_{sello()}.txt"), "w", encoding="utf-8") as fh:
            fh.write("Archivos que aparecen en tus conversaciones pero cuyo contenido NO viene en la\n"
                     "exportación de Claude (normalmente imágenes o PDF que subiste a claude.ai):\n\n")
            for a in sin:
                fh.write(f"{fmt_fecha(a.fecha)}  {a.nombre}  ({a.conv.titulo if a.conv else '-'})\n")
    return total, len(sin)


# ----------------------------------------------------------------------------
# Diagnóstico: estructura de los datos, sin el contenido
# ----------------------------------------------------------------------------
CLAVES_VALOR = ("type", "role", "sender", "name", "content_type", "command", "category", "subtype")


def esqueleto(v, prof=0, cont=None):
    """Describe la forma de un JSON (claves y tipos) sin incluir textos."""
    if prof > 7:
        return "…"
    if isinstance(v, dict):
        partes = []
        for k, x in list(v.items())[:60]:
            if k in CLAVES_VALOR and isinstance(x, str) and len(x) < 50 and cont is not None:
                cont[f"{k}={x}"] += 1
            partes.append(f"{k}:{esqueleto(x, prof + 1, cont)}")
        return "{" + ", ".join(partes) + "}"
    if isinstance(v, list):
        if not v:
            return "[]"
        formas = Counter()
        for x in v[:40]:
            formas[esqueleto(x, prof + 1, cont)] += 1
        for x in v[40:400]:
            esqueleto(x, prof + 1, cont)
        f = formas.most_common(2)
        return f"[{len(v)}× " + " | ".join(s for s, _ in f) + "]"
    if isinstance(v, str):
        return "str"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "num"
    return "null" if v is None else type(v).__name__


def escribir_diagnostico(app, ruta_salida):
    lin = [f"DIAGNÓSTICO buscarai.py {VERSION}  ·  {datetime.now():%Y-%m-%d %H:%M}",
           "No contiene el texto de tus conversaciones: solo nombres de archivo, estructura y contadores.", ""]
    lin.append("== Archivos abiertos")
    for r in app.rutas:
        try:
            lin.append(f"  {r}  ({os.path.getsize(r) / 1024:,.0f} KB)" if os.path.isfile(r) else f"  {r}/")
            if r.lower().endswith(".zip"):
                with zipfile.ZipFile(r) as z:
                    for i in z.infolist()[:300]:
                        lin.append(f"      {i.filename}  {i.file_size:,} bytes")
        except Exception as e:
            lin.append(f"  {r}  ERROR {e}")
    lin += ["", "== Estructura de cada JSON"]
    valores = Counter()
    for eti, cat, nombre, tipo, datos in app.entradas:
        if tipo == "json":
            esq = esqueleto(datos, 0, valores)
            lin.append(f"  [{cat or '-'}] {nombre}:")
            for i in range(0, min(len(esq), 6000), 115):
                lin.append("     " + esq[i:i + 115])
        else:
            lin.append(f"  [{cat or '-'}] {nombre}: {tipo}")
    lin += ["", "== Valores de campos de tipo (type/role/name...)"]
    for k, n in valores.most_common(250):
        lin.append(f"  {n:>7}  {k}")
    lin += ["", "== Resultado de la carga"]
    por_fuente = Counter(cv.fuente for cv in app.convs)
    for f, n in por_fuente.most_common():
        nint = sum(len(cv.inter) for cv in app.convs if cv.fuente == f)
        lin.append(f"  {f}: {n} conversación(es)/documento(s), {nint} pregunta(s)/apartado(s)")
    por_origen = Counter(a.origen for a in app.reg.lista)
    for o, n in por_origen.most_common():
        lin.append(f"  archivos {ORIGENES.get(o, o)}: {n}")
    lin.append(f"  archivos sin contenido en la exportación: {sum(1 for a in app.reg.lista if a.sin_contenido)}")
    quedan = sum(len(RE_MARCADOR.findall(ex.resp.texto)) for cv in app.convs for ex in cv.inter)
    lin.append(f"  marcadores 'not supported' que siguen visibles: {quedan}")
    lin += ["", "== Avisos"] + [f"  {a}" for a in AVISOS] if AVISOS else []
    lin += ["", "== Contadores internos"]
    for k, n in sorted(ESTAD.items()):
        lin.append(f"  {n:>7}  {k}")
    with open(ruta_salida, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lin) + "\n")
    return ruta_salida
# ----------------------------------------------------------------------------
# Entrada / salida en pantalla
# ----------------------------------------------------------------------------
def preguntar(texto):
    try:
        return input(texto).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return "q"


def paginar(lineas):
    tam = shutil.get_terminal_size((80, 24))
    alto, ancho = max(5, tam.lines - 2), max(20, tam.columns)
    usadas = 0
    for l in lineas:
        h = max(1, -(-len(sin_ansi(l)) // ancho))
        if usadas + h > alto and usadas > 0:
            r = preguntar(GRIS("── Enter: seguir · q: dejar de leer ──"))
            if r.lower() == "q":
                return
            usadas = 0
        print(l)
        usadas += h


def resaltar(linea, ini, spans):
    if not COLOR:
        return linea
    fin = ini + len(linea)
    trozos = sorted((max(s, ini) - ini, min(e, fin) - ini) for s, e in spans if not (e <= ini or s >= fin))
    if not trozos:
        return linea
    unidos = [list(trozos[0])]
    for s, e in trozos[1:]:
        if s <= unidos[-1][1]:
            unidos[-1][1] = max(unidos[-1][1], e)
        else:
            unidos.append([s, e])
    out, p = [], 0
    for s, e in unidos:
        out += [linea[p:s], RESAL(linea[s:e])]
        p = e
    out.append(linea[p:])
    return "".join(out)


def separador(txt, color=CIAN):
    ancho = min(60, max(20, shutil.get_terminal_size((80, 24)).columns))
    t = f"── {txt} "
    return color(t + "─" * max(3, ancho - len(t)))


def carpeta_salida():
    for d in (os.path.expanduser("~/storage/downloads"), os.path.expanduser("~/storage/shared/Download")):
        if os.path.isdir(d) and os.access(d, os.W_OK):
            return d
    return os.getcwd()


# ----------------------------------------------------------------------------
# Aplicación
# ----------------------------------------------------------------------------
# ----------------------------------------------------------------------------
# Búsqueda semántica (por significado) con una IA local ligera
#   - llama.cpp (pkg install llama-cpp) + modelo multilingual-e5-small (~130 MB), o
#   - Ollama con un modelo de "embeddings", si ya lo tienes.
#   Sin IA disponible: búsqueda aproximada por relevancia (sin significado).
# El índice es una base SQLite compartida: varias sesiones/paneles de tmux pueden buscar a la vez, y solo una
# indexa en cada momento (las demás buscan en lo que ya esté indexado).
# ----------------------------------------------------------------------------
import fcntl
import hashlib
import math
import sqlite3
import struct
import urllib.error
import urllib.request

DIR_SEM = os.path.join(CARPETA_CONFIG, "semantica")
CFG_SEM = os.path.join(CARPETA_CONFIG, "semantica.json")
MODELO_URL = ("https://huggingface.co/janusz-bit/multilingual-e5-small-gguf/resolve/main/"
              "multilingual-e5-small-q8_0.gguf")
MODELO_NOMBRE = "multilingual-e5-small-q8_0.gguf"
CFG_SEM_DEFECTO = {
    "motor": "auto",                     # auto | llamacpp | ollama | ninguno
    "llamacpp_url": "http://127.0.0.1:8784",   # puerto libre según puertos.txt v6
    "llamacpp_modelo": "~/buscarai/modelos/" + MODELO_NOMBRE,
    "llamacpp_arrancar": True,           # arrancar llama-server solo si no está en marcha (compartido entre sesiones)
    "llamacpp_hilos": 4,
    "ollama_url": "http://127.0.0.1:11434",
    "ollama_modelo": "",                 # vacío = el primer modelo de embeddings que tenga Ollama
    "prefijo_consulta": "auto",          # auto = "query: " / "passage: " si el modelo es e5
    "prefijo_texto": "auto",
    "lote": 8,                           # textos por petición
    "max_caracteres": "auto",            # tamaño de cada trozo: auto = 1200 (e5) o 450 (paraphrase, ~128 tokens)
    "trozos_por_respuesta": "auto",      # máximo de trozos por respuesta: auto = 6 (e5) o 12 (paraphrase)
    "resultados": 40,
}
PALABRAS_VACIAS = set("""a al algo como con de del el ella ellos en entre era es esa ese eso esta este esto fue ha han
hay la las le les lo los mas me mi mis muy no nos o para pero por que se si sin sobre su sus te tu tus un una uno y ya yo
the a an and or of to in on for is are was be it this that with as by at from i you he she we they not do does
der die das und ist zu mit von den ein eine im""".split())


def cfg_semantica():
    os.makedirs(CARPETA_CONFIG, exist_ok=True)
    cfg = dict(CFG_SEM_DEFECTO)
    if os.path.exists(CFG_SEM):
        try:
            with open(CFG_SEM, encoding="utf-8") as fh:
                cfg.update(json.load(fh))
        except Exception as e:
            print(AMAR(f"semantica.json ilegible ({e}); uso los valores por defecto."))
    else:
        with open(CFG_SEM, "w", encoding="utf-8") as fh:
            json.dump(CFG_SEM_DEFECTO, fh, indent=2, ensure_ascii=False)
    return cfg


def http_json(url, datos=None, timeout=120):
    req = urllib.request.Request(url, data=json.dumps(datos).encode() if datos is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8") or "null")


class Motor:
    """Convierte textos en vectores (embeddings) con llama.cpp u Ollama."""

    def __init__(self, cfg):
        self.cfg, self.tipo, self.modelo, self.info = cfg, None, None, []

    # -------- detección
    def _llamacpp_vivo(self):
        try:
            r = http_json(self.cfg["llamacpp_url"].rstrip("/") + "/health", timeout=3)
            return isinstance(r, dict) and r.get("status") in ("ok", None)
        except Exception:
            return False

    def _ollama_modelos(self):
        try:
            r = http_json(self.cfg["ollama_url"].rstrip("/") + "/api/tags", timeout=3)
            return [m.get("name", "") for m in (r or {}).get("models", [])]
        except Exception:
            return None

    def _arrancar_llamacpp(self):
        exe = shutil.which("llama-server")
        modelo = os.path.expanduser(self.cfg["llamacpp_modelo"])
        if not exe:
            self.info.append("llama-server no está instalado (pkg install llama-cpp).")
            return False
        if not os.path.exists(modelo):
            self.info.append(f"Falta el modelo {modelo} (python buscarai.py --semantica-instalar).")
            return False
        os.makedirs(DIR_SEM, exist_ok=True)
        # Cerrojo: si dos sesiones de tmux arrancan a la vez, solo una lanza el servidor
        with open(os.path.join(DIR_SEM, "arranque.lock"), "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            if self._llamacpp_vivo():
                return True
            puerto = self.cfg["llamacpp_url"].rstrip("/").rsplit(":", 1)[-1]
            log = open(os.path.join(DIR_SEM, f"llama-server_{sello()}.log"), "w")
            print(GRIS(f"Arrancando llama-server en el puerto {puerto} (se queda abierto para todas las sesiones)..."))
            subprocess.Popen([exe, "-m", modelo, "--embeddings", "--pooling", "mean", "-c", "512", "-b", "512",
                              "-ub", "512", "--host", "127.0.0.1", "--port", puerto,
                              "-t", str(self.cfg.get("llamacpp_hilos", 4))],
                             stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
            for _ in range(120):
                time.sleep(0.5)
                if self._llamacpp_vivo():
                    return True
        self.info.append("llama-server no respondió a tiempo; mira el último ~/buscarai/semantica/llama-server_*.log")
        return False

    def preparar(self):
        m = self.cfg.get("motor", "auto")
        if m in ("auto", "llamacpp"):
            hay_modelo = os.path.exists(os.path.expanduser(self.cfg["llamacpp_modelo"]))
            puede = shutil.which("llama-server") and hay_modelo
            if m == "auto" and not puede and not self._llamacpp_vivo():
                self.info.append("llama.cpp: " + ("no instalado" if not shutil.which("llama-server") else
                                                  "falta el modelo " + self.cfg["llamacpp_modelo"]) + " (opcional)")
            elif self._llamacpp_vivo() or (self.cfg.get("llamacpp_arrancar", True) and self._arrancar_llamacpp()):
                self.tipo, self.modelo = "llamacpp", os.path.basename(os.path.expanduser(self.cfg["llamacpp_modelo"]))
                return True
        if m in ("auto", "ollama"):
            mods = self._ollama_modelos()
            if mods is None and shutil.which("ollama"):
                os.makedirs(DIR_SEM, exist_ok=True)
                with open(os.path.join(DIR_SEM, "arranque.lock"), "w") as lk:
                    fcntl.flock(lk, fcntl.LOCK_EX)
                    mods = self._ollama_modelos()
                    if mods is None:
                        print(GRIS("Arrancando Ollama (ollama serve; se queda abierto para todas las sesiones)..."))
                        log = open(os.path.join(DIR_SEM, f"ollama_{sello()}.log"), "w")
                        subprocess.Popen(["ollama", "serve"], stdout=log, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL, start_new_session=True)
                        for _ in range(60):
                            time.sleep(0.5)
                            mods = self._ollama_modelos()
                            if mods is not None:
                                break
            if mods:
                elegido = self.cfg.get("ollama_modelo") or next(
                    (x for x in mods if re.search(r"embed|e5|minilm|bge|nomic|arctic|mxbai|paraphrase", x, re.I)), "")
                if elegido:
                    self.tipo, self.modelo = "ollama", elegido
                    return True
                self.info.append("Ollama está en marcha pero no tiene ningún modelo de embeddings "
                                 "(p. ej.: ollama pull sam860/granite-embedding-multilingual:107m-Q8_0).")
            elif mods is None and shutil.which("ollama"):
                self.info.append("Ollama está instalado pero no está en marcha (ollama serve &).")
        return False

    def prefijos(self):
        pc, pt = self.cfg.get("prefijo_consulta", "auto"), self.cfg.get("prefijo_texto", "auto")
        e5 = "e5" in (self.modelo or "").lower()
        return (("query: " if e5 else "") if pc == "auto" else pc), (("passage: " if e5 else "") if pt == "auto" else pt)

    # -------- vectores
    def vectores(self, textos):
        if self.tipo == "ollama":
            r = http_json(self.cfg["ollama_url"].rstrip("/") + "/api/embed", {"model": self.modelo, "input": textos}, 600)
            vs = r.get("embeddings") or []
        else:
            r = http_json(self.cfg["llamacpp_url"].rstrip("/") + "/v1/embeddings", {"input": textos}, 600)
            vs = [d["embedding"] for d in sorted(r.get("data", []), key=lambda d: d.get("index", 0))]
        if len(vs) != len(textos):
            raise RuntimeError(f"el motor devolvió {len(vs)} vectores para {len(textos)} textos")
        return [normalizar_vec(v) for v in vs]


def normalizar_vec(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def empaquetar(v):
    return struct.pack(f"{len(v)}e", *v)


def desempaquetar(b):
    return struct.unpack(f"{len(b) // 2}e", b)


class Indice:
    """Base SQLite con un vector por trozo de texto (clave = huella del texto). Compartida entre sesiones."""

    def __init__(self, modelo):
        os.makedirs(DIR_SEM, exist_ok=True)
        self.ruta = os.path.join(DIR_SEM, "indice_" + re.sub(r"[^\w.-]+", "_", modelo) + ".db")
        self.db = sqlite3.connect(self.ruta, timeout=120)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=120000")
        self.db.execute("CREATE TABLE IF NOT EXISTS v(h TEXT PRIMARY KEY, vec BLOB)")
        self.db.commit()

    def tiene(self, hs):
        out = set()
        hs = list(hs)
        for i in range(0, len(hs), 500):
            trozo = hs[i:i + 500]
            out |= {r[0] for r in self.db.execute(
                f"SELECT h FROM v WHERE h IN ({','.join('?' * len(trozo))})", trozo)}
        return out

    def cargar(self, hs):
        out = {}
        hs = list(hs)
        for i in range(0, len(hs), 500):
            trozo = hs[i:i + 500]
            for h, b in self.db.execute(f"SELECT h, vec FROM v WHERE h IN ({','.join('?' * len(trozo))})", trozo):
                out[h] = desempaquetar(b)
        return out

    def guardar(self, pares):
        self.db.executemany("INSERT OR REPLACE INTO v(h, vec) VALUES(?, ?)", [(h, empaquetar(v)) for h, v in pares])
        self.db.commit()


def trocear(texto, maximo):
    """Parte un texto en trozos de como mucho `maximo` caracteres, por párrafos y frases."""
    texto = re.sub(r"[ \t]+", " ", texto or "").strip()
    if len(texto) <= maximo:
        return [texto] if texto else []
    out, actual = [], ""
    for parte in re.split(r"(?<=[.!?\n])\s+", texto):
        while len(parte) > maximo:
            if actual:
                out.append(actual)
                actual = ""
            out.append(parte[:maximo])
            parte = parte[maximo:]
        if len(actual) + len(parte) + 1 > maximo:
            out.append(actual)
            actual = parte
        else:
            actual = (actual + " " + parte).strip()
    if actual:
        out.append(actual)
    return out


def tam_trozos(cfg, modelo):
    """Modelos de frases cortas (paraphrase-multilingual, MiniLM: ~128 tokens) necesitan trozos más pequeños."""
    corto = bool(re.search(r"paraphrase|minilm|mpnet|labse", modelo or "", re.I))
    mx = cfg.get("max_caracteres", "auto")
    tpr = cfg.get("trozos_por_respuesta", "auto")
    mx = (450 if corto else 1200) if mx in ("auto", "", None) else int(mx)
    tpr = (12 if corto else 6) if tpr in ("auto", "", None) else int(tpr)
    return mx, tpr


def trozos_de(convs, cfg, prefijo_texto, modelo):
    """[(huella, texto_para_el_modelo, ci, ei, texto_visible)] de todas las conversaciones cargadas."""
    out = []
    mx, tpr = tam_trozos(cfg, modelo)
    for ci, cv in enumerate(convs):
        for ei, ex in enumerate(cv.inter):
            p = RE_RECORDATORIO.sub("", ex.preg.texto).strip()
            piezas = []
            if p:
                piezas.append(p[:mx])
            cab = ("P: " + p[:min(250, mx // 4)] + "\nR: ") if p else ""
            for t in trocear(ex.resp.texto, mx - len(cab))[:tpr]:
                piezas.append(cab + t)
            for t in piezas:
                h = hashlib.sha1((modelo + "\0" + prefijo_texto + t).encode("utf-8", "replace")).hexdigest()[:24]
                out.append((h, prefijo_texto + t, ci, ei, t))
    return out


def tokens_lexicos(t):
    return [w[:6] for w in re.findall(r"\w{3,}", normalizar(t)) if w not in PALABRAS_VACIAS]


def busqueda_aproximada(convs, consulta, n):
    """Sin IA: relevancia BM25 sobre raíces de palabras (no entiende sinónimos, pero ordena por relevancia)."""
    q = set(tokens_lexicos(consulta))
    if not q:
        return []
    docs, df = [], Counter()
    for ci, cv in enumerate(convs):
        for ei, ex in enumerate(cv.inter):
            tk = Counter(tokens_lexicos(ex.preg.texto + " " + ex.resp.texto[:20000]))
            docs.append((ci, ei, tk, sum(tk.values())))
            for w in q & set(tk):
                df[w] += 1
    N = len(docs) or 1
    media = sum(d[3] for d in docs) / N or 1
    res = []
    for ci, ei, tk, L in docs:
        s = 0.0
        for w in q:
            f = tk.get(w, 0)
            if f:
                idf = math.log(1 + (N - df[w] + 0.5) / (df[w] + 0.5))
                s += idf * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * L / media))
        if s > 0:
            res.append((s, ci, ei))
    res.sort(reverse=True)
    return res[:n]


def diagnostico_semantica():
    cfg = cfg_semantica()
    m = Motor(cfg)
    lin = [f"DIAGNÓSTICO BÚSQUEDA SEMÁNTICA buscarai.py {VERSION} · {datetime.now():%Y-%m-%d %H:%M:%S}", ""]
    lin.append(f"Configuración: {CFG_SEM}")
    for k, v in cfg.items():
        lin.append(f"  {k}: {v}")
    lin.append("")
    for exe in ("llama-server", "llama-embedding", "llama-cli", "ollama"):
        lin.append(f"  {exe}: {shutil.which(exe) or 'no instalado'}")
    modelo = os.path.expanduser(cfg["llamacpp_modelo"])
    lin.append(f"  modelo GGUF: {modelo} -> {'%.0f MB' % (os.path.getsize(modelo) / 1048576) if os.path.exists(modelo) else 'no existe'}")
    lin.append(f"  llama-server en marcha ({cfg['llamacpp_url']}): {'sí' if m._llamacpp_vivo() else 'no'}")
    mods = m._ollama_modelos()
    lin.append(f"  Ollama ({cfg['ollama_url']}): {'no responde' if mods is None else ', '.join(mods) or 'sin modelos'}")
    try:
        libre = shutil.disk_usage(os.path.expanduser("~")).free / 1048576
        lin.append(f"  espacio libre en casa: {libre:,.0f} MB")
    except OSError:
        pass
    for f in sorted(glob.glob(os.path.join(DIR_SEM, "indice_*.db"))):
        try:
            n = sqlite3.connect(f, timeout=5).execute("SELECT count(*) FROM v").fetchone()[0]
        except Exception as e:
            n = f"error {e}"
        lin.append(f"  índice {os.path.basename(f)}: {n} trozos, {os.path.getsize(f) / 1048576:,.1f} MB")
    ok = m.preparar()
    lin.append("")
    lin.append(f"Motor elegido: {m.tipo} · modelo {m.modelo}" if ok else "Motor elegido: NINGUNO (se usará la búsqueda aproximada)")
    lin += ["  " + x for x in m.info]
    if ok:
        try:
            t0 = time.time()
            v = m.vectores([m.prefijos()[0] + "prueba de funcionamiento"])
            lin.append(f"  prueba: vector de {len(v[0])} dimensiones en {time.time() - t0:.2f} s")
        except Exception as e:
            lin.append(f"  prueba FALLIDA: {e}")
    p = os.path.join(carpeta_salida(), f"buscarai_semantica_diag_{sello()}.txt")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lin) + "\n")
    print("\n".join(lin))
    print(VERDE(f"\nGuardado en: {p}"))


def instalar_semantica():
    """Instala llama.cpp (pkg) y descarga el modelo multilingüe ligero (~130 MB)."""
    cfg = cfg_semantica()
    m = Motor(cfg)
    mods = m._ollama_modelos() or []
    emb = [x for x in mods if re.search(r"embed|e5|minilm|bge|nomic|arctic|mxbai|paraphrase", x, re.I)]
    if emb and preguntar(f"Ya tienes Ollama con {emb[0]}: no hace falta instalar nada. "
                         "¿Instalar igualmente llama.cpp + e5-small (~130 MB, más rápido)? (s/N) ").lower() not in ("s", "si", "sí"):
        print(VERDE("Nada que instalar. Prueba:  python buscarai.py --semantica-diag"))
        return
    if not shutil.which("llama-server"):
        print("Instalando llama.cpp con pkg (necesita internet)...")
        r = subprocess.run(["pkg", "install", "-y", "llama-cpp"])
        if r.returncode != 0 or not shutil.which("llama-server"):
            print(ROJO("No se pudo instalar llama-cpp. Prueba a mano: pkg update && pkg install llama-cpp"))
            return
    destino = os.path.expanduser(cfg["llamacpp_modelo"])
    if os.path.exists(destino) and os.path.getsize(destino) > 50_000_000:
        print(VERDE(f"El modelo ya está: {destino}"))
    else:
        libre = shutil.disk_usage(os.path.expanduser("~")).free / 1048576
        if libre < 250:
            print(ROJO(f"Solo hay {libre:.0f} MB libres; hacen falta unos 250 MB (modelo + índice)."))
            return
        os.makedirs(os.path.dirname(destino), exist_ok=True)
        tmp = destino + ".parcial"
        print(f"Descargando {MODELO_NOMBRE} (~130 MB) a {destino} ...")
        try:
            with urllib.request.urlopen(MODELO_URL, timeout=60) as r, open(tmp, "wb") as fh:
                total, hecho = int(r.headers.get("Content-Length") or 0), 0
                while True:
                    b = r.read(1 << 20)
                    if not b:
                        break
                    fh.write(b)
                    hecho += len(b)
                    print(f"\r   {hecho / 1048576:,.0f} / {total / 1048576:,.0f} MB", end="", flush=True)
            os.replace(tmp, destino)
            print()
        except Exception as e:
            print(ROJO(f"\nNo se pudo descargar: {e}"))
            print(f"Descárgalo con Firefox desde:\n  {MODELO_URL}\ny muévelo a {destino}")
            return
    print(VERDE("Listo. Prueba:  python buscarai.py --semantica-diag"))


class App:
    def __init__(self, rutas, opts):
        self.rutas = rutas
        self.opts = opts
        self.entradas = []
        for r in rutas:
            print(GRIS(f"Cargando {r} ..."))
            try:
                self.entradas += cargar(r)
            except (json.JSONDecodeError, UnicodeDecodeError, zipfile.BadZipFile, OSError) as e:
                print(ROJO(f"No se pudo leer {r}: {e}"))
        self.convs, self.reg = construir(self.entradas, opts)
        for av in AVISOS:
            print(AMAR("Aviso: " + av))
        self.pats, self.consulta, self.res, self.pos = [], "", [], -1
        self.filtro_ia = None          # None = todas las IA; si no, conjunto de fuentes
        self.sem_info = {}             # (ci, ei) -> (puntuación, trozo) en búsquedas semánticas
        self.sem_consulta = ""
        self._sem = None               # (cfg, motor|None, índice|None)
        self._vecs = {}                # caché de vectores en memoria (array 'f')

    def varias_fuentes(self):
        return len({cv.fuente for cv in self.convs}) > 1

    def etiqueta(self, cv):
        return MAG(f"[{cv.fuente}] ") if self.varias_fuentes() else ""

    # ---------------- búsqueda ----------------
    def spans(self, parte):
        out = []
        for p in self.pats:
            out.extend(m.span() for m in p.finditer(self.campo(parte)))
        return sorted(out)

    def campo(self, parte):
        """Texto en el que se busca: original (--exacto) o sin mayúsculas ni tildes."""
        return parte.texto if self.opts.exacto else parte.norm

    def fuentes_presentes(self):
        return Counter(cv.fuente for cv in self.convs)

    def elegir_ia(self):
        """Pide qué IA ver. Devuelve un conjunto de fuentes o None (todas)."""
        c = self.fuentes_presentes()
        ops = [f for f, _ in c.most_common()]
        print()
        print(separador("¿Qué IA?", VERDE))
        print(" 0) Todas")
        for i, f in enumerate(ops, 1):
            print(f" {i}) {f} ({c[f]})")
        v = preguntar("Número (o varios separados por comas): ")
        sel = {ops[int(x) - 1] for x in re.split(r"[,\s]+", v) if x.isdigit() and 0 < int(x) <= len(ops)}
        return sel or None

    def texto_filtro_ia(self):
        return "todas las IA" if not self.filtro_ia else ", ".join(sorted(self.filtro_ia))

    def buscar(self, consulta):
        # «ia:claude,lumo palabras» limita la búsqueda a esas IA
        m = re.search(r"(?:^|\s)ia:(\S+)", consulta)
        if m:
            pedidas = [normalizar(x) for x in m.group(1).split(",") if x]
            sel = {f for f in self.fuentes_presentes() if any(p in normalizar(f) for p in pedidas)}
            self.filtro_ia = None if any(p in ("todas", "todo", "all") for p in pedidas) else (sel or None)
            consulta = (consulta[:m.start()] + " " + consulta[m.end():]).strip()
        self.sem_info = {}
        terminos = parsear_consulta(consulta, self.opts.exacto, self.opts.frase)
        if not terminos:
            return False
        self.pats = [compilar(t, self.opts.palabra) for t in terminos]
        self.consulta = consulta
        res = []
        for ci, cv in enumerate(self.convs):
            if self.filtro_ia and cv.fuente not in self.filtro_ia:
                continue
            for ei, ex in enumerate(cv.inter):
                hallados, n = set(), 0
                for ti, p in enumerate(self.pats):
                    for parte in (ex.preg, ex.resp):
                        k = sum(1 for _ in p.finditer(self.campo(parte)))
                        if k:
                            hallados.add(ti)
                            n += k
                ok = (len(hallados) == len(self.pats)) if self.opts.modo == "y" else bool(hallados)
                if ok:
                    res.append((ci, ei, n))
        if self.opts.recientes:
            res.reverse()
        self.res, self.pos = res, -1
        return True

    # ---------------- dibujo ----------------
    def render_parte(self, parte, spans, contexto=None, max_ventanas=None):
        if not parte.texto.strip():
            return [GRIS("   (vacío)")]
        lineas = parte.texto.split("\n")
        tocadas = set()
        for s, e in spans:
            for ln in range(parte.linea_de(s), parte.linea_de(max(s, e - 1)) + 1):
                tocadas.add(ln)
        if contexto is None:
            ventanas = [(0, len(lineas) - 1)]
        else:
            ventanas = []
            for h in sorted(tocadas):
                a, b = max(0, h - contexto), min(len(lineas) - 1, h + contexto)
                if ventanas and a <= ventanas[-1][1] + 1:
                    ventanas[-1] = (ventanas[-1][0], max(b, ventanas[-1][1]))
                else:
                    ventanas.append((a, b))
            if not ventanas:
                ventanas = [(0, min(len(lineas) - 1, contexto))]
            if max_ventanas:
                ventanas = ventanas[:max_ventanas]
        out, previa = [], None
        for a, b in ventanas:
            if contexto is not None and ((previa is None and a > 0) or (previa is not None and a > previa + 1)):
                out.append(GRIS("      ⋯"))
            for ln in range(a, b + 1):
                marca = AMAR("▶") if ln in tocadas else " "
                num = GRIS(f"{ln + 1:>4}│") if contexto is not None else ""
                out.append(f"{marca}{num} {resaltar(lineas[ln], parte.inicios[ln], spans)}")
            previa = b
        if contexto is not None and previa is not None and previa < len(lineas) - 1:
            out.append(GRIS("      ⋯"))
        return out

    def lineas_intercambio(self, ci, ei, contexto=None):
        cv = self.convs[ci]
        ex = cv.inter[ei]
        l = [separador(f"Conversación {ci + 1}/{len(self.convs)}", VERDE),
             self.etiqueta(cv) + NEG(cv.titulo),
             GRIS(f"Creada: {fmt_fecha(cv.fecha)} · Últ. actividad: {fmt_fecha(cv.actividad)}"),
             GRIS(f"Pregunta {ei + 1} de {len(cv.inter)}  ({fmt_fecha(ex.fecha)})")]
        if self.pats:
            l.append(GRIS(f"Búsqueda activa: {self.consulta}"))
        aqui = [a for a in cv.archivos if a.conv is cv and a.ei == ei]
        if cv.archivos:
            l.append(GRIS(f"Archivos en esta conversación: {len(cv.archivos)}"
                          + (f" (en esta pregunta: {len(aqui)})" if aqui else "") + " · tecla r para verlos"))
        l.append(separador("TÚ"))
        l += self.render_parte(ex.preg, self.spans(ex.preg), contexto)
        l.append(separador(cv.voz))
        l += self.render_parte(ex.resp, self.spans(ex.resp), contexto)
        return l

    # ---------------- lista de resultados ----------------
    def vista_resultados(self):
        if not self.res:
            print(AMAR(f"\nSin resultados para: {self.consulta}"))
            return "b"
        nconv = len({r[0] for r in self.res})
        por = 5
        total = (len(self.res) - 1) // por + 1
        pag = 0
        while True:
            por_ia = Counter(self.convs[r[0]].fuente for r in self.res)
            lineas = ["", separador(f"{len(self.res)} resultado(s) en {nconv} conversación(es)", VERDE),
                      GRIS(f"Búsqueda: {self.consulta}   ·   en {self.texto_filtro_ia()}   ·   página {pag + 1}/{total}"),
                      GRIS("Por IA: " + ", ".join(f"{f} {n}" for f, n in por_ia.most_common()))]
            for k in range(pag * por, min(len(self.res), (pag + 1) * por)):
                ci, ei, n = self.res[k]
                cv = self.convs[ci]
                ex = cv.inter[ei]
                lineas.append("")
                lineas.append(f"{AMAR(f'[{k + 1}]')} {fmt_fecha(ex.fecha)} · {self.etiqueta(cv)}{CIAN(cv.titulo)}")
                if self.sem_info:
                    punt, trozo = self.sem_info.get((ci, ei), (n, None))
                    medida = f"similitud {punt:.2f}" if self._sem and self._sem[1] else f"relevancia {punt:.1f}"
                    lineas.append(GRIS(f"     pregunta {ei + 1}/{len(cv.inter)} · {medida}"))
                    muestra = trozo or (ex.preg.texto.strip() + "\n" + ex.resp.texto.strip())
                    for l in [x for x in muestra.split("\n") if x.strip()][:5]:
                        lineas.append("     " + (l[:240] + "…" if len(l) > 240 else l))
                    continue
                lineas.append(GRIS(f"     pregunta {ei + 1}/{len(cv.inter)} · {n} coincidencia(s)"))
                for etq, parte in (("TÚ", ex.preg), (cv.voz, ex.resp)):
                    sp = self.spans(parte)
                    if sp:
                        lineas.append(GRIS(f"   {etq}:"))
                        lineas += self.render_parte(parte, sp, self.opts.contexto, max_ventanas=1)
                        break
            paginar(lineas)
            print()
            r = preguntar(NEG("Nº para abrir") + GRIS(" · Enter/s: pág. sig. · a: pág. ant. · i: elegir IA · b: nueva búsqueda"
                                                      " · q: menú > "))
            if r.isdigit():
                k = int(r) - 1
                if 0 <= k < len(self.res):
                    self.pos = k
                    ci, ei, _ = self.res[k]
                    if self.vista_intercambio(ci, ei) == "q":
                        return "q"
                else:
                    print(ROJO("Número fuera de rango"))
            elif r.lower() in ("", "s"):
                if pag + 1 < total:
                    pag += 1
                else:
                    print(AMAR("Ya estás en la última página."))
            elif r.lower() == "a":
                pag = max(0, pag - 1)
            elif r.lower() == "i":
                self.filtro_ia = self.elegir_ia()
                if self.sem_info:
                    self.busqueda_semantica(self.sem_consulta, preguntar_indexar=False)
                else:
                    self.buscar(self.consulta)
                if not self.res:
                    print(AMAR(f"Sin resultados en {self.texto_filtro_ia()}."))
                    return "b"
                nconv = len({x[0] for x in self.res})
                total, pag = (len(self.res) - 1) // por + 1, 0
            elif r.lower() == "b":
                return "b"
            elif r.lower() == "q":
                return "q"

    # ---------------- lectura de una pregunta-respuesta ----------------
    MENU = ("n/p: pregunta sig./ant.   s/a: conversación sig./ant.\n"
            "+/-: coincidencia sig./ant.   c: completo ⇄ solo contexto\n"
            "i: índice de preguntas   t: conversación entera\n"
            "r: archivos de la conversación   g: guardar .txt\n"
            "l: lista resultados   q: menú")

    def vista_intercambio(self, ci, ei):
        completo, aviso = True, ""
        while True:
            print()
            paginar(self.lineas_intercambio(ci, ei, None if completo else self.opts.contexto))
            print()
            if aviso:
                print(AMAR(aviso))
                aviso = ""
            print(GRIS(self.MENU))
            r = preguntar(NEG("> ")).lower()
            nconv = len(self.convs[ci].inter)
            if r == "n":
                if ei + 1 < nconv:
                    ei += 1
                else:
                    aviso = "Es la última pregunta de esta conversación (s = siguiente conversación)."
            elif r == "p":
                if ei > 0:
                    ei -= 1
                else:
                    aviso = "Es la primera pregunta de esta conversación (a = conversación anterior)."
            elif r == "s":
                if ci + 1 < len(self.convs):
                    ci, ei = ci + 1, 0
                else:
                    aviso = "Es la conversación más reciente."
            elif r == "a":
                if ci > 0:
                    ci, ei = ci - 1, 0
                else:
                    aviso = "Es la conversación más antigua."
            elif r in ("+", "-"):
                if not self.res:
                    aviso = "No hay búsqueda activa."
                else:
                    nueva = 0 if self.pos < 0 else self.pos + (1 if r == "+" else -1)
                    if 0 <= nueva < len(self.res):
                        self.pos = nueva
                        ci, ei, _ = self.res[nueva]
                        aviso = f"Coincidencia {nueva + 1} de {len(self.res)}"
                    else:
                        aviso = "No hay más coincidencias en esa dirección."
            elif r == "c":
                completo = not completo
                aviso = "Mostrando todo." if completo else f"Mostrando solo {self.opts.contexto} líneas antes/después de cada coincidencia."
            elif r == "i":
                ei = self.vista_indice(ci, ei)
            elif r == "t":
                self.vista_conversacion_entera(ci)
            elif r == "g":
                aviso = self.guardar(ci, ei)
            elif r == "r":
                cv = self.convs[ci]
                if not cv.archivos:
                    aviso = "Esta conversación no tiene archivos."
                else:
                    destino = self.vista_archivos(cv.archivos, f"Archivos de «{cv.titulo}»")
                    if destino:
                        ci, ei = destino
            elif r == "l":
                return "l"
            elif r == "q":
                return "q"
            elif r.isdigit():
                k = int(r) - 1
                if 0 <= k < nconv:
                    ei = k
                else:
                    aviso = "Número de pregunta fuera de rango."

    def vista_indice(self, ci, ei):
        cv = self.convs[ci]
        lineas = ["", separador(f"Índice · {cv.titulo}", VERDE)]
        for k, x in enumerate(cv.inter):
            t = x.preg.texto.strip()
            primera = (t.split("\n")[0] if t else "(sin pregunta)")[:70]
            marca = AMAR("*") if self.pats and (self.spans(x.preg) or self.spans(x.resp)) else " "
            actual = VERDE("◀") if k == ei else ""
            lineas.append(f"{marca}{k + 1:>3}. {GRIS(fmt_fecha(x.fecha))} {primera} {actual}")
        if self.pats:
            lineas.append(GRIS("   * = contiene la búsqueda"))
        paginar(lineas)
        r = preguntar(NEG("Número de pregunta (Enter para volver): "))
        if r.isdigit() and 0 < int(r) <= len(cv.inter):
            return int(r) - 1
        return ei

    def vista_conversacion_entera(self, ci):
        cv = self.convs[ci]
        lineas = ["", separador(f"Conversación {ci + 1}/{len(self.convs)}", VERDE), self.etiqueta(cv) + NEG(cv.titulo),
                  GRIS(f"Creada: {fmt_fecha(cv.fecha)} · {len(cv.inter)} pregunta(s)")]
        for k, ex in enumerate(cv.inter):
            lineas += ["", separador(f"Pregunta {k + 1}/{len(cv.inter)} · {fmt_fecha(ex.fecha)}", AMAR), CIAN("TÚ:")]
            lineas += self.render_parte(ex.preg, self.spans(ex.preg))
            lineas.append(CIAN(f"{cv.voz}:"))
            lineas += self.render_parte(ex.resp, self.spans(ex.resp))
        paginar(lineas)
        preguntar(GRIS("── Fin de la conversación. Enter para volver ──"))

    def guardar(self, ci, ei):
        global COLOR
        r = preguntar("Guardar: 1) esta pregunta-respuesta  2) conversación entera  (Enter cancela): ")
        if r not in ("1", "2"):
            return "Cancelado."
        cv = self.convs[ci]
        guardado, COLOR = COLOR, False
        try:
            if r == "1":
                lineas, suf = self.lineas_intercambio(ci, ei), f"_P{ei + 1}"
            else:
                lineas, suf = [f"[{cv.fuente}] {cv.titulo}", f"Creada: {fmt_fecha(cv.fecha)}", ""], ""
                for k, ex in enumerate(cv.inter):
                    lineas += ["", "=" * 60, f"Pregunta {k + 1}/{len(cv.inter)} · {fmt_fecha(ex.fecha)}", "=" * 60, "",
                               "TÚ:", ex.preg.texto, "", f"{cv.voz}:", ex.resp.texto]
        finally:
            COLOR = guardado
        nombre = re.sub(r"[^\w\-]+", "_", normalizar(cv.titulo)).strip("_")[:40] or "conversacion"
        archivo = os.path.join(carpeta_salida(), f"{cv.fuente}_{fmt_dia(cv.fecha)}_{nombre}{suf}.txt")
        with open(archivo, "w", encoding="utf-8") as f:
            f.write("\n".join(sin_ansi(x) for x in lineas) + "\n")
        return f"Guardado en: {archivo}"

    # ---------------- NUEVO: última conversación ----------------
    def lineas_ultima(self):
        """Información para saber si el historial descargado está al día."""
        l = ["", separador("Última conversación del historial", VERDE)]
        for ruta in self.rutas:
            try:
                mt = datetime.fromtimestamp(os.path.getmtime(ruta)).astimezone()
                l.append(GRIS(f"Archivo {os.path.basename(ruta.rstrip('/')) or ruta}: descargado/modificado {fmt_fecha(mt)} ({hace(mt)})"))
            except OSError:
                pass
        ultimos = {}
        for ci, cv in enumerate(self.convs):
            if not cv.doc:
                ultimos[cv.fuente] = ci  # la lista está ordenada por actividad: gana la última
        for fuente, ci in ultimos.items():
            cv = self.convs[ci]
            ex = cv.inter[-1]
            lins = [x for x in ex.preg.texto.strip().split("\n") if x.strip()]
            preg = (next((x for x in lins if not x.startswith("[")), lins[0] if lins else "(sin texto)"))[:90]
            l += ["", NEG(f"{fuente}:") + " " + CIAN(cv.titulo),
                  f"   Creada:                 {fmt_fecha(cv.fecha)}",
                  f"   Último mensaje:         {fmt_fecha(cv.actividad)}  ({hace(cv.actividad)})",
                  f"   Preguntas que contiene: {len(cv.inter)}",
                  f"   Última pregunta:        {preg}"]
        l += ["", GRIS("Compárala con la primera conversación del panel izquierdo de la app."),
              GRIS("Si en la app hay conversaciones más nuevas, conviene volver a pedir la exportación.")]
        return l, list(ultimos.values())

    def vista_ultima(self):
        lineas, indices = self.lineas_ultima()
        paginar(lineas)
        if len(indices) == 1:
            r = preguntar(NEG("Enter: volver · a: abrirla > ")).lower()
            if r == "a":
                ci = indices[0]
                self.vista_intercambio(ci, len(self.convs[ci].inter) - 1)
        else:
            preguntar(GRIS("── Enter para volver ──"))

    # ---------------- lista de conversaciones ----------------
    def vista_lista(self):
        filtro, pag, por, docs = "", 0, 15, True
        while True:
            idx = [i for i, cv in enumerate(self.convs) if normalizar(filtro) in normalizar(cv.titulo)
                   and (not self.filtro_ia or cv.fuente in self.filtro_ia) and (docs or not cv.doc)]
            idx.sort(key=lambda i: ts(self.convs[i].actividad or self.convs[i].fecha), reverse=self.opts.recientes)
            total = max(1, (len(idx) - 1) // por + 1)
            pag = min(pag, total - 1)
            por_ia = Counter(self.convs[i].fuente for i in idx)
            lineas = ["", separador(f"Conversaciones (todo): {len(idx)}" + (f" con «{filtro}»" if filtro else ""), VERDE),
                      GRIS(f"{self.texto_filtro_ia()}" + ("" if docs else " · sin documentos")
                           + " · " + ", ".join(f"{f} {n}" for f, n in por_ia.most_common()))]
            for k in range(pag * por, min(len(idx), (pag + 1) * por)):
                cv = self.convs[idx[k]]
                lineas.append(f"{AMAR(f'{k + 1:>4}.')} {GRIS(fmt_dia(cv.actividad))} {self.etiqueta(cv)}{cv.titulo[:60]} {GRIS(f'({len(cv.inter)})')}")
            lineas.append(GRIS(f"página {pag + 1}/{total} · fecha = última actividad · (n) = nº de preguntas"))
            paginar(lineas)
            r = preguntar(NEG("Nº para abrir") + GRIS(" · Enter/s: sig. · a: ant. · f: filtrar título · i: elegir IA"
                                                      " · d: documentos sí/no · o: orden · q: menú > "))
            if r.isdigit():
                k = int(r) - 1
                if 0 <= k < len(idx):
                    if self.vista_intercambio(idx[k], 0) == "q":
                        return
            elif r.lower() in ("", "s"):
                if pag + 1 < total:
                    pag += 1
            elif r.lower() == "a":
                pag = max(0, pag - 1)
            elif r.lower() == "f":
                filtro = preguntar("Texto del título (Enter = todas): ")
                if filtro.lower() == "q":
                    filtro = ""
                pag = 0
            elif r.lower() == "i":
                self.filtro_ia, pag = self.elegir_ia(), 0
            elif r.lower() == "d":
                docs, pag = not docs, 0
            elif r.lower() == "o":
                self.opts.recientes, pag = not self.opts.recientes, 0
            elif r.lower() == "q":
                return


    # ---------------- fuentes ----------------
    def recargar(self, rutas):
        print(GRIS("Recargando..."))
        self.rutas = ampliar_con_partes(rutas)
        self.entradas = []
        for r in self.rutas:
            try:
                self.entradas += cargar(r)
            except (json.JSONDecodeError, UnicodeDecodeError, zipfile.BadZipFile, OSError) as e:
                print(ROJO(f"No se pudo leer {r}: {e}"))
        self.convs, self.reg = construir(self.entradas, self.opts)
        self.res, self.pos, self.filtro_ia = [], -1, None

    def vista_fuentes(self):
        """Gestiona ~/buscarai/fuentes.txt. Devuelve True si hay que recargar."""
        cambiado = False
        while True:
            lista = leer_fuentes()
            print()
            print(separador("Fuentes (se cargan siempre que abres buscarai.py sin indicar archivos)", VERDE))
            print(GRIS(f"Archivo: {FUENTES}"))
            for i, f in enumerate(lista, 1):
                p = resolver_ruta(f)
                estado = VERDE("ok") if os.path.exists(p) else ROJO("no existe")
                print(f" {i:>2}) {f}  {estado}")
            print(GRIS("Cargado ahora: " + ", ".join(f"{k} {v}" for k, v in self.fuentes_presentes().most_common())))
            print(GRIS("a: añadir · b NÚMERO: quitar · r: recargar ahora · q: volver"))
            r = preguntar(NEG("> ")).strip()
            if r.lower() == "a":
                nueva = preguntar("Carpeta o archivo (p. ej. ~/storage/downloads/lumo): ").strip()
                if nueva and nueva.lower() != "q":
                    nueva = forma_guardada(nueva)
                    if not os.path.exists(resolver_ruta(nueva)) and not expandir_prefijo(nueva):
                        print(AMAR("Ojo: ahora mismo no existe; se añade igualmente."))
                    guardar_fuentes(lista + [nueva])
                    cambiado = True
            elif re.match(r"^b\s*\d+$", r.lower()):
                k = int(r[1:].strip()) - 1
                if 0 <= k < len(lista):
                    guardar_fuentes(lista[:k] + lista[k + 1:])
                    cambiado = True
            elif r.lower() == "r":
                return True
            elif r.lower() in ("q", ""):
                return cambiado

    # ---------------- archivos ----------------
    def extraer(self, destino=None, todas=None):
        if destino is None:
            def_dest = os.path.join(os.getcwd(), "buscarai_archivos")
            destino = preguntar(f"Carpeta de destino (Enter = {def_dest}): ") or def_dest
            if destino.lower() == "q":
                return
        if todas is None:
            todas = preguntar("¿Guardar también las versiones anteriores de cada archivo? (s/N) ").lower() in ("s", "si", "sí")
        n, sin = extraer_todo(self.reg, os.path.expanduser(destino), todas)
        print(VERDE(f"{n} archivo(s) guardado(s) en {destino}"))
        if sin:
            print(GRIS(f"{sin} archivo(s) aparecen en tus chats pero su contenido no viene en la exportación;"))
            print(GRIS("la lista está en archivos_sin_contenido_<fecha-hora>.txt"))

    def vista_archivos(self, lista, titulo):
        """Lista de archivos con filtro. Devuelve (ci, ei) si el usuario pide ir a la conversación."""
        filtro, en_contenido, origen, pag, por = "", False, None, 0, 12
        orden = sorted(lista, key=lambda a: ts(a.fecha), reverse=self.opts.recientes)
        while True:
            nf = normalizar(filtro)
            vis = [a for a in orden
                   if (origen is None or a.origen == origen)
                   and (not nf or nf in normalizar(a.ruta) or (en_contenido and nf in normalizar(a.texto)))]
            total = max(1, (len(vis) - 1) // por + 1)
            pag = min(pag, total - 1)
            cab = f"{titulo}: {len(vis)}"
            if filtro:
                cab += f" · {'contienen' if en_contenido else 'nombre con'} «{filtro}»"
            if origen:
                cab += f" · {ORIGENES[origen]}"
            lineas = ["", separador(cab, VERDE)]
            for k in range(pag * por, min(len(vis), (pag + 1) * por)):
                a = vis[k]
                extra = []
                if len(a.versiones) > 1:
                    extra.append(f"{len(a.versiones)} versiones")
                if a.parcial:
                    extra.append("parcial")
                if a.entregado:
                    extra.append("entregado")
                if a.sin_contenido:
                    extra.append("sin contenido en la exportación")
                t_b = a.tam()
                tam = "" if a.sin_contenido else (f"{t_b} B" if t_b < 1024 else f"{t_b / 1024:,.1f} KB")
                lineas.append(f"{AMAR(f'{k + 1:>4}.')} {GRIS(fmt_dia(a.fecha))} {NEG(a.nombre)} {GRIS(tam)}")
                lineas.append(GRIS(f"       {ORIGENES.get(a.origen, a.origen)}"
                                   + (f" · {a.conv.titulo[:45]}" if a.conv else "")
                                   + (f" · {', '.join(extra)}" if extra else "")))
            lineas.append(GRIS(f"página {pag + 1}/{total}"))
            paginar(lineas)
            print(GRIS("Nº: abrir · Enter/s: sig. · a: ant. · f: filtrar nombre · b: buscar en el contenido\n"
                       "o: tipo de origen · x: extraer los de esta lista · q: volver"))
            r = preguntar(NEG("> ")).strip()
            rl = r.lower()
            if r.isdigit():
                k = int(r) - 1
                if 0 <= k < len(vis):
                    res = self.vista_archivo(vis[k])
                    if res:
                        return res
            elif rl in ("", "s"):
                if pag + 1 < total:
                    pag += 1
            elif rl == "a":
                pag = max(0, pag - 1)
            elif rl in ("f", "b"):
                filtro = preguntar("Texto (Enter = quitar filtro): ")
                filtro = "" if filtro.lower() == "q" else filtro
                en_contenido, pag = rl == "b", 0
            elif rl == "o":
                ops = [o for o in ORIGENES if any(a.origen == o for a in lista)]
                for i, o in enumerate(ops, 1):
                    print(f" {i}) {ORIGENES[o]} ({sum(1 for a in lista if a.origen == o)})")
                print(" 0) Todos")
                v = preguntar("> ")
                origen = ops[int(v) - 1] if v.isdigit() and 0 < int(v) <= len(ops) else None
                pag = 0
            elif rl == "x":
                destino = preguntar("Carpeta (Enter = buscarai_archivos): ") or "buscarai_archivos"
                n = 0
                for a in vis:
                    if not a.sin_contenido:
                        n += len(guardar_archivo(a, os.path.join(os.path.expanduser(destino),
                                                                 nombre_seguro(ORIGENES.get(a.origen, a.origen), 40))))
                print(VERDE(f"{n} archivo(s) guardado(s) en {destino}"))
            elif rl == "q":
                return None

    def vista_archivo(self, a):
        v = len(a.versiones)
        while True:
            lineas = ["", separador(a.nombre, VERDE),
                      GRIS(f"Ruta: {a.ruta}"),
                      GRIS(f"{ORIGENES.get(a.origen, a.origen)} · {fmt_fecha(a.fecha)}"
                           + (f" · conversación: {a.conv.titulo}" if a.conv else ""))]
            if a.sin_contenido:
                lineas.append(AMAR("La exportación de Claude no incluye el contenido de este archivo "
                                   "(solo su nombre)."))
            elif a.b64 or a.ref:
                lineas.append(GRIS(f"Archivo binario ({a.tipo_media or 'desconocido'}, {a.tam() / 1024:,.0f} KB): "
                                   "usa g para guardarlo y abrirlo con otra app."))
            else:
                f_v, texto, desc = a.versiones[v - 1]
                if len(a.versiones) > 1:
                    lineas.append(AMAR(f"Versión {v} de {len(a.versiones)} ({desc}, {fmt_fecha(f_v)})"))
                if a.parcial:
                    lineas.append(AMAR("Ojo: Claude solo leyó una parte de este archivo."))
                lineas.append(separador("contenido"))
                sp = []
                if self.pats:
                    nt = texto if self.opts.exacto else normalizar(texto)
                    for p in self.pats:
                        sp += [m.span() for m in p.finditer(nt)]
                lineas += self.render_parte(Parte(texto), sorted(sp))
            paginar(lineas)
            print(GRIS("g: guardar en Descargas · v: otra versión · c: ir a la conversación · q: volver"))
            r = preguntar(NEG("> ")).lower()
            if r == "g":
                todas = len(a.versiones) > 1 and preguntar("¿Todas las versiones? (s/N) ").lower() in ("s", "si", "sí")
                for p in guardar_archivo(a, carpeta_salida(), todas):
                    print(VERDE(f"Guardado: {p}"))
            elif r == "v" and len(a.versiones) > 1:
                x = preguntar(f"Versión (1-{len(a.versiones)}): ")
                if x.isdigit() and 0 < int(x) <= len(a.versiones):
                    v = int(x)
            elif r == "c":
                if a.conv in self.convs:
                    return self.convs.index(a.conv), min(a.ei, len(a.conv.inter) - 1)
                print(AMAR("Este archivo no está asociado a ninguna conversación."))
            elif r in ("q", ""):
                return None

    # ---------------- opciones ----------------
    def vista_opciones(self):
        while True:
            o = self.opts
            print()
            print(separador("Opciones", VERDE))
            print(f" 1) Líneas de contexto antes/después: {NEG(o.contexto)}")
            print(f" 2) Con varias palabras: {NEG('TODAS deben aparecer' if o.modo == 'y' else 'CUALQUIERA vale')}")
            print(f" 3) Solo palabra completa: {NEG('sí' if o.palabra else 'no')}")
            print(f" 4) Orden de listas: {NEG('más recientes primero' if o.recientes else 'más antiguas primero')}")
            print(f" 5) Incluir el razonamiento interno de la IA: {NEG('sí' if o.razonamiento else 'no')}")
            print(f" 6) Mostrar texto de adjuntos dentro de la pregunta: {NEG('sí' if o.adjuntos else 'no')}")
            print(f" 7) Incluir ramas editadas (Claude): {NEG('sí' if o.todas_ramas else 'no')}")
            print(f" 8) Detalles técnicos (resultados de herramientas, lecturas): {NEG('sí' if o.detalles else 'no')}")
            print(f" 9) Respetar mayúsculas y tildes al buscar: {NEG('sí' if o.exacto else 'no')}")
            print(" 0) Volver")
            r = preguntar("> ")
            rehacer = False
            if r == "1":
                v = preguntar("Nº de líneas (ej. 3 o 5): ")
                if v.isdigit():
                    o.contexto = int(v)
            elif r == "2":
                o.modo = "o" if o.modo == "y" else "y"
            elif r == "3":
                o.palabra = not o.palabra
            elif r == "4":
                o.recientes = not o.recientes
            elif r == "5":
                o.razonamiento, rehacer = not o.razonamiento, True
            elif r == "6":
                o.adjuntos, rehacer = not o.adjuntos, True
            elif r == "7":
                o.todas_ramas, rehacer = not o.todas_ramas, True
            elif r == "8":
                o.detalles, rehacer = not o.detalles, True
            elif r == "9":
                o.exacto = not o.exacto
            elif r in ("0", "q", ""):
                if self.consulta:
                    self.buscar(self.consulta)
                return
            if rehacer:
                print(GRIS("Reconstruyendo índice..."))
                self.convs, self.reg = construir(self.entradas, o)
                self.res, self.pos = [], -1


    # ---------------- búsqueda semántica ----------------
    def preparar_semantica(self):
        if self._sem is None:
            cfg = cfg_semantica()
            m = Motor(cfg)
            if m.preparar():
                self._sem = (cfg, m, Indice(m.modelo))
            else:
                self._sem = (cfg, None, None)
                print(AMAR("No hay IA local para la búsqueda semántica; uso la búsqueda aproximada (por relevancia)."))
                for x in m.info:
                    print(GRIS("  " + x))
                print(GRIS("  Para activarla: python buscarai.py --semantica-instalar   (ver manual, apartado 7b)"))
        return self._sem

    def indexar(self, interactivo=True):
        """Calcula los vectores que faltan. Devuelve True si el índice queda completo."""
        cfg, m, ind = self.preparar_semantica()
        if not m:
            return False
        trozos = trozos_de(self.convs, cfg, m.prefijos()[1], m.modelo)
        unicos = {t[0]: t for t in trozos}
        hechos = ind.tiene(unicos)
        faltan = [t for h, t in unicos.items() if h not in hechos]
        if not faltan:
            print(VERDE(f"Índice completo: {len(unicos)} trozos."))
            return True
        lk = open(os.path.join(DIR_SEM, "indexando.lock"), "w")
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(AMAR("Otra sesión está indexando ahora mismo; esta no indexa (usa lo que ya esté hecho)."))
            lk.close()
            return False
        try:
            hechos = ind.tiene(unicos)               # otra sesión puede haber avanzado mientras tanto
            faltan = [t for h, t in unicos.items() if h not in hechos]
            lote, t0, hechos_ahora, fallos = max(1, int(cfg.get("lote", 8))), time.time(), 0, 0
            print(f"Indexando {len(faltan)} trozos con {m.tipo} ({m.modelo}). Ctrl+C para parar: lo hecho se guarda.")
            for i in range(0, len(faltan), lote):
                grupo = faltan[i:i + lote]
                try:
                    vs = m.vectores([t[1] for t in grupo])
                    ind.guardar([(t[0], v) for t, v in zip(grupo, vs)])
                except KeyboardInterrupt:
                    raise
                except Exception:
                    for t in grupo:                  # de uno en uno, recortando si es demasiado largo
                        try:
                            v = m.vectores([t[1][:len(t[1]) // 2]])[0]
                            ind.guardar([(t[0], v)])
                        except KeyboardInterrupt:
                            raise
                        except Exception:
                            fallos += 1
                hechos_ahora += len(grupo)
                vel = hechos_ahora / max(0.1, time.time() - t0)
                resto = (len(faltan) - hechos_ahora) / max(vel, 0.01)
                print(f"\r   {hechos_ahora}/{len(faltan)} · {vel:.1f} trozos/s · quedan ~{resto / 60:.0f} min   ",
                      end="", flush=True)
            print()
            if fallos:
                print(AMAR(f"{fallos} trozo(s) no se pudieron indexar (se omiten)."))
            return True
        except KeyboardInterrupt:
            print(AMAR("\nIndexación parada; lo hecho queda guardado y se continúa la próxima vez."))
            return False
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)
            lk.close()

    def busqueda_semantica(self, consulta, preguntar_indexar=True):
        consulta = consulta.strip()
        if not consulta:
            return False
        cfg, m, ind = self.preparar_semantica()
        self.pats, self.sem_consulta = [], consulta
        self.consulta = "≈ " + consulta
        convs_ok = lambda ci: not self.filtro_ia or self.convs[ci].fuente in self.filtro_ia
        n = int(cfg.get("resultados", 40))
        if not m:
            res = busqueda_aproximada(self.convs, consulta, n * 3)
            res = [(s, ci, ei) for s, ci, ei in res if convs_ok(ci)][:n]
            self.sem_info = {(ci, ei): (s, None) for s, ci, ei in res}
            self.res, self.pos = [(ci, ei, s) for s, ci, ei in res], -1
            return True
        trozos = trozos_de(self.convs, cfg, m.prefijos()[1], m.modelo)
        unicos = {t[0] for t in trozos}
        hechos = ind.tiene(unicos)
        if len(hechos) < len(unicos):
            faltan = len(unicos) - len(hechos)
            print(AMAR(f"Faltan {faltan} de {len(unicos)} trozos por indexar (la primera vez tarda; luego solo lo nuevo)."))
            r = preguntar("i: indexar ahora · Enter: buscar solo en lo ya indexado · q: cancelar > ").lower() \
                if preguntar_indexar else ""
            if r == "q":
                return False
            if r == "i":
                self.indexar()
                hechos = ind.tiene(unicos)
        if not hechos:
            print(AMAR("Aún no hay nada indexado: indexa primero (tecla i, o python buscarai.py --indexar)."))
            return False
        pendientes = [h for h in hechos if h not in self._vecs]
        for h, v in ind.cargar(pendientes).items():
            self._vecs[h] = array.array("f", v)
        q = array.array("f", m.vectores([m.prefijos()[0] + consulta])[0])
        mejor = {}
        for h, _, ci, ei, visible in trozos:
            v = self._vecs.get(h)
            if v is None or not convs_ok(ci):
                continue
            s = sum(a * b for a, b in zip(q, v))
            if s > mejor.get((ci, ei), (-2, None))[0]:
                mejor[(ci, ei)] = (s, visible)
        orden = sorted(mejor.items(), key=lambda kv: kv[1][0], reverse=True)[:n]
        self.sem_info = dict(orden)
        self.res, self.pos = [(ci, ei, s) for (ci, ei), (s, _) in orden], -1
        if len(hechos) < len(unicos):
            print(GRIS(f"(búsqueda en {len(hechos)} de {len(unicos)} trozos indexados)"))
        return True

    def ciclo_semantica(self, consulta=None):
        while True:
            if consulta is None:
                print()
                print(GRIS("Escribe lo que buscas con tus palabras (una idea, una pregunta...); "
                           "se ordena por parecido de significado."))
                consulta = preguntar(NEG(f"Búsqueda semántica en {self.texto_filtro_ia()} (Enter = volver): "))
                if consulta in ("", "q"):
                    return
            print(GRIS("Buscando por significado..."))
            if not self.busqueda_semantica(consulta):
                return
            if self.vista_resultados() == "q":
                return
            consulta = None

    # ---------------- menú principal ----------------
    def ciclo_busqueda(self, consulta=None):
        while True:
            if consulta is None:
                print()
                print(GRIS('Palabras sueltas = deben aparecer todas · "entre comillas" = frase exacta'))
                consulta = preguntar(NEG("Buscar (Enter = volver): "))
                if consulta in ("", "q"):
                    return
            if not self.buscar(consulta):
                return
            if self.vista_resultados() == "q":
                return
            consulta = None

    def principal(self):
        while True:
            npreg = sum(len(cv.inter) for cv in self.convs if not cv.doc)
            fechas = [cv.fecha for cv in self.convs if cv.fecha]
            fuentes = sorted({cv.fuente for cv in self.convs})
            print()
            print(separador("Buscador de conversaciones de IA", VERDE))
            nchat = sum(1 for cv in self.convs if not cv.doc)
            ndoc = len(self.convs) - nchat
            print(GRIS(f"{', '.join(fuentes)} · {nchat} conversaciones · {npreg} preguntas"
                       + (f" · {ndoc} grupo(s) de documentos" if ndoc else "")
                       + f" · {len(self.reg.lista)} archivos"))
            if fechas:
                print(GRIS(f"Desde {fmt_dia(min(fechas))} hasta {fmt_dia(max(cv.actividad or cv.fecha for cv in self.convs if cv.actividad or cv.fecha))}"))
            print(f" 1) Buscar (en {self.texto_filtro_ia()})")
            print(" 2) Conversaciones (todo, por fecha de la última actividad)")
            if self.res:
                print(f" 3) Volver a los resultados de: {self.consulta}")
            print(" 4) Opciones")
            print(" 5) Última conversación (¿está actualizado el historial?)")
            print(f" 6) Archivos: creados por Claude y subidos por ti ({len(self.reg.lista)})")
            print(" 7) Extraer todos los archivos a una carpeta")
            print(" 8) Generar diagnóstico para Claude")
            print(" 9) Fuentes: carpetas y archivos que se cargan siempre")
            print(" s) Búsqueda semántica (por significado, con IA local)")
            print(" i) Elegir IA (para buscar y para la lista)")
            print(" 0) Salir")
            r = preguntar(NEG("> (o escribe directamente lo que buscas) "))
            if r in ("0", "q", "salir"):
                return
            elif r == "1":
                self.ciclo_busqueda()
            elif r == "2":
                self.vista_lista()
            elif r == "3" and self.res:
                if self.vista_resultados() == "b":
                    self.ciclo_busqueda()
            elif r == "4":
                self.vista_opciones()
            elif r == "5":
                self.vista_ultima()
            elif r == "6":
                destino = self.vista_archivos(self.reg.lista, "Todos los archivos")
                if destino:
                    self.vista_intercambio(*destino)
            elif r == "7":
                self.extraer()
            elif r == "8":
                p = escribir_diagnostico(self, os.path.join(carpeta_salida(), f"buscarai_diagnostico_{sello()}.txt"))
                print(VERDE(f"Diagnóstico guardado en: {p}"))
                print(GRIS("Envíaselo a Claude si algo no se ve bien. No incluye el texto de tus conversaciones."))
            elif r == "9":
                if self.vista_fuentes():
                    self.recargar(rutas_de_fuentes())
            elif r.lower() == "i":
                self.filtro_ia = self.elegir_ia()
            elif r.lower() == "s":
                self.ciclo_semantica()
            elif r:
                self.ciclo_busqueda(r)




def main():
    global COLOR
    ap = argparse.ArgumentParser(
        description="Buscador del historial exportado de Claude (chats, sesiones, proyectos, memoria), "
                    "ChatGPT, DeepSeek, Kimi, Lumo y otras IA.",
        epilog="Manual completo: buscaraiManual.txt")
    ap.add_argument("archivos", nargs="*",
                    help="partes de Claude (conversations-000.zip...), manifest-....json, carpeta claude_export_..., "
                         "conversations.json, .zip de otras IA, .jsonl, .md/.txt (uno o varios)")
    ap.add_argument("-b", "--buscar", help="texto a buscar nada más arrancar (palabras sueltas: deben aparecer todas)")
    ap.add_argument("-f", "--frase", metavar="TEXTO",
                    help='busca exactamente esa frase, con las palabras juntas y en ese orden: -f "quieres que"')
    ap.add_argument("-e", "--exacto", action="store_true",
                    help="respetar mayúsculas y tildes (por defecto da igual: Quieres = quieres = QUIERES)")
    ap.add_argument("-d", "--descargar", nargs="?", const="", metavar="MANIFEST",
                    help="descarga con Firefox todas las partes del índice de Claude y las mueve a esta carpeta "
                         "(sin MANIFEST usa el manifest-*.json más reciente)")
    ap.add_argument("--solo-conversaciones", action="store_true", help="con -d, descargar solo las conversaciones")
    ap.add_argument("--destino", help="con -d, carpeta donde dejar las partes (por defecto claude_export_FECHA/)")
    ap.add_argument("--navegador", help="con -d, paquete Android del navegador (por defecto Firefox)")
    ap.add_argument("--espera", type=int, default=300, help="con -d, segundos máximos de espera por parte (300)")
    ap.add_argument("--fuentes", action="store_true", help="muestra las fuentes guardadas (~/buscarai/fuentes.txt) y sale")
    ap.add_argument("--anadir", nargs="+", metavar="RUTA", help="añade carpetas/archivos a las fuentes guardadas")
    ap.add_argument("--quitar", nargs="+", metavar="RUTA_O_N", help="quita fuentes (por ruta o por número)")
    ap.add_argument("--ia", help="limitar a estas IA, p. ej. --ia claude,lumo")
    ap.add_argument("-s", "--semantica", metavar="TEXTO", help="búsqueda semántica (por significado) nada más arrancar")
    ap.add_argument("--indexar", action="store_true",
                    help="prepara el índice semántico de todo lo cargado y sale (ideal en otro panel de tmux)")
    ap.add_argument("--semantica-diag", action="store_true",
                    help="comprueba qué IA local hay para la búsqueda semántica y guarda un diagnóstico")
    ap.add_argument("--semantica-instalar", action="store_true",
                    help="instala llama.cpp (pkg) y descarga el modelo multilingüe ligero (~130 MB)")
    ap.add_argument("-u", "--ultima", action="store_true",
                    help="muestra la última conversación del historial y sale")
    ap.add_argument("-x", "--extraer", nargs="?", const="buscarai_archivos", metavar="CARPETA",
                    help="extrae todos los archivos (creados por Claude, subidos por ti, proyectos, memoria) y sale")
    ap.add_argument("--todas-versiones", action="store_true", help="con -x, guardar también versiones anteriores")
    ap.add_argument("--diagnostico", action="store_true",
                    help="genera buscarai_diagnostico_AAAA-MM-DD-HH-MM-SS.txt (estructura, sin tus textos) y sale")
    ap.add_argument("-c", "--contexto", type=int, default=3, help="líneas antes/después (por defecto 3)")
    ap.add_argument("-o", "--cualquiera", action="store_true", help="con varias palabras basta con que aparezca una")
    ap.add_argument("-w", "--palabra", action="store_true", help="solo palabras completas")
    ap.add_argument("--antiguas", action="store_true", help="listar primero las más antiguas")
    ap.add_argument("--razonamiento", action="store_true", help="incluir el razonamiento interno de la IA")
    ap.add_argument("--detalles", action="store_true",
                    help="incluir detalles técnicos: resultados de herramientas, lecturas de archivos, subagentes")
    ap.add_argument("--adjuntos", action="store_true", help="mostrar el texto de los adjuntos dentro de la pregunta")
    ap.add_argument("--todas-ramas", action="store_true", help="incluir versiones editadas de mensajes (Claude)")
    ap.add_argument("--desde", help="solo conversaciones creadas desde AAAA-MM-DD")
    ap.add_argument("--hasta", help="solo conversaciones creadas hasta AAAA-MM-DD")
    ap.add_argument("--sin-color", action="store_true", help="sin colores")
    ap.add_argument("--version", action="version", version=f"buscarai.py {VERSION}")
    a = ap.parse_args()
    if a.sin_color:
        COLOR = False

    if a.semantica_diag:
        diagnostico_semantica()
        return
    if a.semantica_instalar:
        instalar_semantica()
        return

    if a.descargar is not None:
        manif = a.descargar or buscar_manifiesto_reciente()
        if not manif or not os.path.isfile(manif):
            print(ROJO("No encuentro el índice manifest-....json. Pásalo así:"))
            print("  python buscarai.py -d ~/storage/downloads/manifest-XXXX.json")
            sys.exit(1)
        partes = descargar_partes(manif, a.destino, a.solo_conversaciones, a.navegador, a.espera)
        if not partes:
            sys.exit(1)
        r = preguntar(NEG("\n¿Abrir ahora el buscador con lo descargado? (S/n) ")).lower()
        if r in ("n", "no", "q"):
            return
        a.archivos = partes

    if a.anadir or a.quitar or a.fuentes:
        lista = leer_fuentes()
        for x in a.anadir or []:
            x = forma_guardada(x)
            if x not in lista:
                lista.append(x)
        quitar = set()
        for x in a.quitar or []:
            if x.isdigit() and 0 < int(x) <= len(lista):
                quitar.add(lista[int(x) - 1])
            else:
                quitar |= {f for f in lista if resolver_ruta(f).rstrip("/") ==
                           os.path.abspath(os.path.expanduser(x)).rstrip("/") or f == x or f == forma_guardada(x)}
        lista = [f for f in lista if f not in quitar]
        if a.anadir or a.quitar:
            guardar_fuentes(lista)
        print(f"Fuentes ({FUENTES}):")
        for i, f in enumerate(lista, 1):
            p = resolver_ruta(f)
            estado = "ok" if os.path.exists(p) else (f"prefijo: {len(expandir_prefijo(f))} archivo(s)"
                                                     if expandir_prefijo(f) else "NO EXISTE")
            print(f"  {i:>2}) {f}  {estado}")
        return

    rutas = []
    for r in a.archivos or []:
        if os.path.exists(r):
            rutas.append(r)
        else:
            ex = expandir_prefijo(r, [resolver_ruta(f) for f in leer_fuentes(crear=False)])
            if ex:
                print(GRIS(f"«{r}»: {len(ex)} archivo(s) que empiezan así"))
            rutas += ex or [r]
    rutas = rutas or rutas_de_fuentes()
    faltan = [r for r in rutas if not os.path.exists(r)]

    # Índice (manifest) de Claude: usar las partes ya descargadas o explicar qué hacer
    nuevas = []
    for r in rutas:
        manif = es_manifiesto(r) if r not in faltan and os.path.isfile(r) else None
        if not manif:
            nuevas.append(r)
            continue
        esperadas, halladas = partes_descargadas(manif, os.path.dirname(os.path.abspath(r)))
        if halladas:
            print(GRIS("Índice de Claude detectado; uso las partes descargadas."))
            if len(halladas) < len(esperadas):
                print(AMAR(f"Ojo: faltan {len(esperadas) - len(halladas)} parte(s) por descargar (usa -d)."))
            nuevas += halladas
        else:
            explicar_manifiesto(r, manif)
    if rutas and not nuevas:
        sys.exit(1)
    rutas = ampliar_con_partes([r for r in nuevas if r not in faltan]) + [r for r in nuevas if r in faltan]

    if not rutas or faltan:
        if faltan:
            print(ROJO("No existe: " + ", ".join(faltan)))
        else:
            print(ROJO("No encuentro el archivo de conversaciones."))
        print("Indica la ruta, por ejemplo:")
        print("  python buscarai.py ~/storage/downloads/conversations-000.zip")
        print("  python buscarai.py -d        (descarga la exportación de Claude)")
        print(f"o pon tus archivos en ~/historial_ia (o añade carpetas con --anadir RUTA). Fuentes: {FUENTES}")
        sys.exit(1)

    a.modo = "o" if a.cualquiera else "y"
    a.recientes = not a.antiguas
    texto_frase, a.frase = a.frase, False
    if texto_frase:
        a.buscar = '"' + texto_frase.replace('"', "") + '"'
    app = App(rutas, a)
    if a.ia:
        pedidas = [normalizar(x) for x in a.ia.split(",") if x]
        app.filtro_ia = {f for f in app.fuentes_presentes() if any(p in normalizar(f) for p in pedidas)} or None
    if a.diagnostico:
        p = escribir_diagnostico(app, os.path.join(carpeta_salida(), f"buscarai_diagnostico_{sello()}.txt"))
        print(VERDE(f"Diagnóstico guardado en: {p}"))
        return
    if a.extraer:
        app.extraer(a.extraer, a.todas_versiones)
        return
    if not app.convs:
        print(AMAR("No he encontrado conversaciones con mensajes en ese archivo."))
        print("Genera el diagnóstico (python buscarai.py ARCHIVO --diagnostico) y envíaselo a Claude.")
        sys.exit(1)
    if a.ultima:
        for l in app.lineas_ultima()[0]:
            print(l)
        return
    if a.indexar:
        app.indexar(interactivo=False)
        return
    if a.semantica:
        app.ciclo_semantica(a.semantica)
    elif a.buscar:
        app.ciclo_busqueda(a.buscar)
    app.principal()
    print("Hasta luego.")


if __name__ == "__main__":
    main()
