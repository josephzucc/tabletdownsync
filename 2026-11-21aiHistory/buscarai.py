#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
buscarai.py - Buscador interactivo del historial exportado de varias IA:
    Claude, ChatGPT, DeepSeek y, en modo genérico, Kimi, Lumo y otras.

Uso en Termux (el manual completo está en buscaraiManual.txt):
    python buscarai.py                              (busca el archivo solo)
    python buscarai.py conversations.json
    python buscarai.py data-2026-09-20.zip          (el .zip tal cual)
    python buscarai.py claude.zip chatgpt.zip       (varias IA a la vez)
    python buscarai.py archivo.zip --ultima         (¿está actualizado?)
    python buscarai.py archivo.zip -b "frase" -c 5

Solo usa la librería estándar de Python: no hay que instalar nada.
"""

import argparse
import bisect
import glob
import io
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
import zipfile
import time
from datetime import datetime

VERSION = "2.2"

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


def parsear_consulta(q):
    for a, b in (("“", '"'), ("”", '"'), ("«", '"'), ("»", '"')):
        q = q.replace(a, b)
    out = []
    for frase, palabra in re.findall(r'"([^"]+)"|(\S+)', q):
        t = normalizar((frase or palabra).strip())
        if t:
            out.append(t)
    return out


def compilar(termino, palabra_completa):
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
    __slots__ = ("titulo", "fecha", "actividad", "inter", "fuente")

    def __init__(self, titulo, f, act, inter, fuente):
        self.titulo = titulo
        self.fecha = f
        self.actividad = act
        self.inter = inter
        self.fuente = fuente


def limpiar(t):
    return (t or "").replace("\r\n", "\n").replace("\r", "\n").strip()


# ---------------------------------------------------------------- Claude
def mensajes_claude(c, opts):
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
        partes, hubo = [], False
        cont = m.get("content")
        if isinstance(cont, list):
            for b in cont:
                if not isinstance(b, dict):
                    continue
                t = b.get("type")
                if t == "text" and b.get("text"):
                    partes.append(b["text"])
                    hubo = True
                elif t == "thinking" and opts.razonamiento and b.get("thinking"):
                    partes.append("[Razonamiento]\n" + b["thinking"])
                elif t == "tool_use":
                    partes.append(f"[Herramienta usada: {b.get('name', '?')}]")
        if not hubo and isinstance(m.get("text"), str) and m["text"].strip():
            partes.insert(0, m["text"])
        for a in m.get("attachments") or []:
            if isinstance(a, dict):
                partes.append(f"[Adjunto: {a.get('file_name') or 'sin nombre'}]")
                if opts.adjuntos and a.get("extracted_content"):
                    partes.append(a["extracted_content"])
        for f in m.get("files") or []:
            if isinstance(f, dict):
                partes.append(f"[Archivo: {f.get('file_name') or 'sin nombre'}]")
        rol = "user" if m.get("sender") in ("human", "user") else "ai"
        out.append((rol, limpiar("\n".join(p for p in partes if p)), fecha(m.get("created_at"))))
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
    # Sin nodo actual: desde la raíz, siguiendo siempre la última rama
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


def mensajes_chatgpt(c, opts):
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
        if rol in ("system", "tool") and not opts.razonamiento:
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
            if not opts.razonamiento:
                continue
            partes.append("[Código/herramienta]\n" + str(cont.get("text") or ""))
        elif ct == "user_editable_context":
            continue
        else:
            if cont.get("text") and opts.razonamiento:
                partes.append(str(cont["text"]))
        for a in meta.get("attachments") or []:
            if isinstance(a, dict):
                partes.append(f"[Adjunto: {a.get('name') or 'sin nombre'}]")
        txt = limpiar("\n".join(partes))
        if not txt:
            continue
        out.append(("user" if rol == "user" else "ai", txt, fecha(m.get("create_time"))))
    return out


def mensajes_deepseek(c, opts):
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
CLAVES_ACT = ("updated_at", "updatedAt", "update_time", "updateTime", "updated",
              "last_updated", "modified", "mtime")
ROLES_USUARIO = ("user", "human", "me", "usuario", "request", "prompt", "question", "you")


def texto_generico(v, opts, prof=0):
    if prof > 6 or v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float)):
        return ""
    if isinstance(v, list):
        return "\n".join(t for t in (texto_generico(x, opts, prof + 1) for x in v) if t)
    if isinstance(v, dict):
        tipo = str(v.get("type", "")).lower()
        if tipo in ("thinking", "think", "reasoning", "thought") and not opts.razonamiento:
            return ""
        for k in ("text", "content", "parts", "message", "value", "body", "markdown", "msg"):
            if k in v:
                t = texto_generico(v[k], opts, prof + 1)
                if t:
                    return t
    return ""


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


def mensajes_generico(c, opts):
    out = []
    for m in lista_mensajes(c) or []:
        if not isinstance(m, dict):
            continue
        rol = rol_generico(m)
        if rol == "system":
            continue
        cont = None
        for k in ("content", "text", "message", "parts", "contents"):
            if k in m:
                cont = m[k]
                break
        txt = limpiar(texto_generico(cont, opts))
        if txt:
            out.append((rol, txt, primera_fecha(m, CLAVES_CREADA)))
    return out


def encontrar_conversaciones(datos, prof=0):
    """Busca, en un JSON de estructura desconocida, la lista de conversaciones."""
    if prof > 4:
        return []
    if isinstance(datos, list):
        if datos and any(parece_mensaje(x) for x in datos[:20]):
            return [{"messages": datos}]
        convs = [x for x in datos if isinstance(x, dict) and (lista_mensajes(x) or "mapping" in x or "chat_messages" in x)]
        if convs:
            return convs
        res = []
        for x in datos[:5000]:
            res += encontrar_conversaciones(x, prof + 1)
        return res
    if isinstance(datos, dict):
        if lista_mensajes(datos) or "mapping" in datos or "chat_messages" in datos:
            return [datos]
        res = []
        for v in datos.values():
            if isinstance(v, (list, dict)):
                res += encontrar_conversaciones(v, prof + 1)
        return res
    return []


def detectar(c, etiqueta):
    if "chat_messages" in c:
        return "Claude", mensajes_claude
    mp = c.get("mapping")
    if isinstance(mp, dict):
        for n in list(mp.values())[:30]:
            m = n.get("message") if isinstance(n, dict) else None
            if isinstance(m, dict):
                if "fragments" in m:
                    return "DeepSeek", mensajes_deepseek
                if "author" in m:
                    return "ChatGPT", mensajes_chatgpt
        return "ChatGPT", mensajes_chatgpt
    return etiqueta or "IA", mensajes_generico


def construir(fuentes, opts):
    """fuentes: lista de (etiqueta, datos_json)."""
    convs = []
    desde = datetime.strptime(opts.desde, "%Y-%m-%d").date() if opts.desde else None
    hasta = datetime.strptime(opts.hasta, "%Y-%m-%d").date() if opts.hasta else None
    for etiqueta, datos in fuentes:
        for c in encontrar_conversaciones(datos):
            nombre, extractor = detectar(c, etiqueta)
            try:
                msgs = extractor(c, opts)
            except Exception:
                continue
            inter, actual = [], None
            for rol, txt, f in msgs:
                if rol == "user":
                    if actual is not None and not actual["r"] and txt.startswith("[Adjunto"):
                        actual["p"].append(txt)
                        continue
                    actual = {"p": [txt], "r": [], "f": f}
                    inter.append(actual)
                else:
                    if actual is None:
                        actual = {"p": [], "r": [], "f": f}
                        inter.append(actual)
                    actual["r"].append(txt)
            if not inter:
                continue
            f_conv = primera_fecha(c, CLAVES_CREADA) or next((x["f"] for x in inter if x["f"]), None)
            fechas_msg = [f for _, _, f in msgs if f]
            act = max([x for x in [primera_fecha(c, CLAVES_ACT), f_conv] + fechas_msg if x], key=ts, default=None)
            if desde and (not f_conv or f_conv.date() < desde):
                continue
            if hasta and (not f_conv or f_conv.date() > hasta):
                continue
            lista = [Intercambio("\n\n".join(x["p"]), "\n\n".join(x["r"]), x["f"] or f_conv) for x in inter]
            titulo = ""
            for k in CLAVES_TITULO:
                if isinstance(c.get(k), str) and c[k].strip():
                    titulo = c[k].strip()
                    break
            if not titulo:
                primera = lista[0].preg.texto.strip().split("\n")[0]
                titulo = (primera[:50] + "…") if len(primera) > 50 else (primera or "(sin título)")
            convs.append(Conversacion(titulo, f_conv, act, lista, nombre))
    # Orden como en el panel de las apps: por última actividad
    convs.sort(key=lambda cv: ts(cv.actividad or cv.fecha))
    return convs


# ----------------------------------------------------------------------------
# Lectura de archivos (.json, .jsonl, .zip o carpeta)
# ----------------------------------------------------------------------------
def etiqueta_por_nombre(ruta):
    n = normalizar(os.path.basename(ruta))
    for clave, eti in (("kimi", "Kimi"), ("moonshot", "Kimi"), ("lumo", "Lumo"), ("proton", "Lumo"),
                       ("deepseek", "DeepSeek"), ("chatgpt", "ChatGPT"), ("openai", "ChatGPT"),
                       ("claude", "Claude"), ("gemini", "Gemini"), ("mistral", "Mistral")):
        if clave in n:
            return eti
    return None


RE_HABLANTE = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:\*\*|__)?\s*(?:[^\w\s]{1,3}\s*)?"
    r"(user|usuario|you|t[uú]|human|humano|me|yo|pregunta|prompt|"
    r"assistant|asistente|kimi|lumo|ai|ia|chatgpt|gpt|claude|deepseek|gemini|mistral|bot|respuesta)"
    r"\s*(?:\*\*|__)?\s*:?\s*(?:\*\*|__)?\s*(.*)$", re.I)
USUARIO_MD = ("user", "usuario", "you", "tu", "tú", "human", "humano", "me", "yo", "pregunta", "prompt")


def parsear_markdown(nombre, texto, mtime=None):
    """Conversación guardada como .md/.txt por extensiones de navegador (Kimi, Lumo...)."""
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


def parsear_bytes(nombre, b, mtime=None):
    t = b.decode("utf-8-sig", "replace")
    if nombre.lower().endswith((".md", ".markdown", ".txt")):
        return parsear_markdown(nombre, t, mtime)
    if nombre.lower().endswith(".jsonl"):
        return [json.loads(l) for l in t.splitlines() if l.strip()]
    return json.loads(t)


def elegir_nombres(nombres):
    js = [n for n in nombres if n.lower().endswith((".json", ".jsonl")) and "__MACOSX" not in n]
    conv = [n for n in js if "conversation" in os.path.basename(n).lower()]
    if conv or js:
        return conv or js
    return [n for n in nombres if n.lower().endswith((".md", ".markdown", ".txt")) and "__MACOSX" not in n]


def leer_zip(origen, eti, prof=0):
    """Lee un .zip (ruta o bytes); si dentro hay otros .zip, los abre también."""
    out = []
    z = zipfile.ZipFile(io.BytesIO(origen) if isinstance(origen, bytes) else origen)
    with z:
        nombres = z.namelist()
        for n in elegir_nombres(nombres):
            try:
                out.append((eti, parsear_bytes(n, z.read(n))))
            except Exception:
                pass
        if prof < 2:
            for n in nombres:
                if n.lower().endswith(".zip"):
                    try:
                        out += leer_zip(z.read(n), eti, prof + 1)
                    except Exception:
                        pass
    return out


def es_manifiesto(ruta):
    """El índice (manifest-....json) que envía Claude con los enlaces de cada parte."""
    if not ruta.lower().endswith(".json") or os.path.getsize(ruta) > 200000:
        return None
    try:
        with open(ruta, encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception:
        return None
    if isinstance(d, dict) and isinstance(d.get("data_files"), list) and \
            any(isinstance(x, dict) and "export_url" in x for x in d["data_files"]):
        return d
    return None


def partes_descargadas(manif, carpeta_manif):
    """Busca en Descargas (y junto al índice) las partes de conversaciones ya bajadas."""
    nombres = [x.get("filename") for x in manif["data_files"]
               if isinstance(x, dict) and x.get("category") == "conversations" and x.get("filename")]
    dirs = [carpeta_manif, os.getcwd()] + CARPETAS_DESCARGA
    encontrados = []
    for nom in nombres:
        base, ext = os.path.splitext(nom)
        for d in dirs:
            # Firefox añade "(1)", "(2)"... si el nombre ya existe: coge el más reciente
            cands = glob.glob(os.path.join(d, glob.escape(base) + "*" + ext))
            if cands:
                encontrados.append(max(cands, key=os.path.getmtime))
                break
    return nombres, encontrados


def explicar_manifiesto(ruta, manif):
    print(AMAR("\nEse archivo es el ÍNDICE de la exportación de Claude, no el historial."))
    print("Claude entrega la exportación en varias partes; el índice solo trae los enlaces.")
    print("Tus conversaciones están en la(s) parte(s) «conversations». Abre en Firefox")
    print("(con la sesión de Claude iniciada) este/estos enlace(s) y guarda el .zip:\n")
    for x in manif["data_files"]:
        if isinstance(x, dict) and x.get("category") == "conversations":
            print(f"   {CIAN(x.get('filename', '?'))}")
            print(f"   {x.get('export_url')}\n")
    print(GRIS("Los enlaces caducan a las 24 horas y pueden dejar de funcionar tras usarlos una vez:"))
    print(GRIS("descarga cada archivo completo al primer intento."))
    print(GRIS(f"Índice creado: {fmt_fecha(fecha(manif.get('created_at')))}"))
    print("\nDespués ejecuta de nuevo el programa con el índice o con el .zip descargado:")
    print(f"  python buscarai.py {ruta}")
    print("  python buscarai.py ~/storage/downloads/conversations-000.zip")


def cargar(ruta):
    """Devuelve lista de (etiqueta, datos)."""
    eti = etiqueta_por_nombre(ruta)
    out = []
    if os.path.isdir(ruta):
        nombres = []
        for base, _, fs in os.walk(ruta):
            nombres += [os.path.join(base, f) for f in fs]
        for n in elegir_nombres(nombres):
            try:
                with open(n, "rb") as fh:
                    out.append((eti, parsear_bytes(n, fh.read(), os.path.getmtime(n))))
            except Exception:
                pass
    elif ruta.lower().endswith(".zip"):
        out += leer_zip(ruta, eti)
    else:
        with open(ruta, "rb") as fh:
            out.append((eti, parsear_bytes(ruta, fh.read(), os.path.getmtime(ruta))))
    return out


CARPETAS_DESCARGA = [os.path.expanduser("~/storage/downloads"),
                     os.path.expanduser("~/storage/shared/Download"), "/sdcard/Download"]


def buscar_archivo_por_defecto():
    """Devuelve una lista de rutas (puede haber varias partes)."""
    dirs = [os.getcwd(), os.path.dirname(os.path.abspath(__file__))] + CARPETAS_DESCARGA
    # Carpetas creadas por --descargar: la más reciente va primero
    exports = sorted(glob.glob(os.path.join(os.getcwd(), "claude_export_*")), key=os.path.getmtime, reverse=True)
    exports = [e for e in exports if os.path.isdir(e)]
    if exports:
        dirs = [exports[0]] + dirs
    for d in dirs:
        p = os.path.join(d, "conversations.json")
        if os.path.isfile(p):
            return [p]
    for d in dirs:
        # Formato nuevo de Claude: conversations-000.zip, conversations-001.zip...
        partes = {}
        for z in glob.glob(os.path.join(d, "conversations-*.zip")):
            m = re.match(r"conversations-(\d+)", os.path.basename(z))
            if m and (m.group(1) not in partes or os.path.getmtime(z) > os.path.getmtime(partes[m.group(1)])):
                partes[m.group(1)] = z
        if partes:
            return [partes[k] for k in sorted(partes)]
        zs = sorted(glob.glob(os.path.join(d, "data-*.zip")), key=os.path.getmtime, reverse=True)
        if zs:
            return [zs[0]]
    return []


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
class App:
    def __init__(self, rutas, opts):
        self.rutas = rutas
        self.opts = opts
        self.fuentes = []
        for r in rutas:
            print(GRIS(f"Cargando {r} ..."))
            try:
                self.fuentes += cargar(r)
            except (json.JSONDecodeError, UnicodeDecodeError, zipfile.BadZipFile) as e:
                print(ROJO(f"No se pudo leer {r}: {e}"))
        self.convs = construir(self.fuentes, opts)
        self.pats, self.consulta, self.res, self.pos = [], "", [], -1

    def varias_fuentes(self):
        return len({cv.fuente for cv in self.convs}) > 1

    def etiqueta(self, cv):
        return MAG(f"[{cv.fuente}] ") if self.varias_fuentes() else ""

    # ---------------- búsqueda ----------------
    def spans(self, parte):
        out = []
        for p in self.pats:
            out.extend(m.span() for m in p.finditer(parte.norm))
        return sorted(out)

    def buscar(self, consulta):
        terminos = parsear_consulta(consulta)
        if not terminos:
            return False
        self.pats = [compilar(t, self.opts.palabra) for t in terminos]
        self.consulta = consulta
        res = []
        for ci, cv in enumerate(self.convs):
            for ei, ex in enumerate(cv.inter):
                hallados, n = set(), 0
                for ti, p in enumerate(self.pats):
                    for parte in (ex.preg, ex.resp):
                        k = sum(1 for _ in p.finditer(parte.norm))
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
        l.append(separador("TÚ"))
        l += self.render_parte(ex.preg, self.spans(ex.preg), contexto)
        l.append(separador(cv.fuente.upper()))
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
            lineas = ["", separador(f"{len(self.res)} resultado(s) en {nconv} conversación(es)", VERDE),
                      GRIS(f"Búsqueda: {self.consulta}   ·   página {pag + 1}/{total}")]
            for k in range(pag * por, min(len(self.res), (pag + 1) * por)):
                ci, ei, n = self.res[k]
                cv = self.convs[ci]
                ex = cv.inter[ei]
                lineas.append("")
                lineas.append(f"{AMAR(f'[{k + 1}]')} {fmt_fecha(ex.fecha)} · {self.etiqueta(cv)}{CIAN(cv.titulo)}")
                lineas.append(GRIS(f"     pregunta {ei + 1}/{len(cv.inter)} · {n} coincidencia(s)"))
                for etq, parte in (("TÚ", ex.preg), (cv.fuente.upper(), ex.resp)):
                    sp = self.spans(parte)
                    if sp:
                        lineas.append(GRIS(f"   {etq}:"))
                        lineas += self.render_parte(parte, sp, self.opts.contexto, max_ventanas=1)
                        break
            paginar(lineas)
            print()
            r = preguntar(NEG("Nº para abrir") + GRIS(" · Enter/s: pág. siguiente · a: pág. anterior · b: nueva búsqueda · q: menú > "))
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
            elif r.lower() == "b":
                return "b"
            elif r.lower() == "q":
                return "q"

    # ---------------- lectura de una pregunta-respuesta ----------------
    MENU = ("n/p: pregunta sig./ant.   s/a: conversación sig./ant.\n"
            "+/-: coincidencia sig./ant.   c: completo ⇄ solo contexto\n"
            "i: índice de preguntas   t: conversación entera\n"
            "g: guardar .txt   l: lista resultados   q: menú")

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
            lineas.append(CIAN(f"{cv.fuente.upper()}:"))
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
                               "TÚ:", ex.preg.texto, "", f"{cv.fuente.upper()}:", ex.resp.texto]
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
                l.append(GRIS(f"Archivo {os.path.basename(ruta)}: descargado/modificado {fmt_fecha(mt)} ({hace(mt)})"))
            except OSError:
                pass
        ultimos = {}
        for ci, cv in enumerate(self.convs):
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
        filtro, pag, por = "", 0, 15
        while True:
            idx = [i for i, cv in enumerate(self.convs) if normalizar(filtro) in normalizar(cv.titulo)]
            if self.opts.recientes:
                idx.reverse()
            total = max(1, (len(idx) - 1) // por + 1)
            pag = min(pag, total - 1)
            lineas = ["", separador(f"{len(idx)} conversación(es)" + (f" con «{filtro}»" if filtro else ""), VERDE)]
            for k in range(pag * por, min(len(idx), (pag + 1) * por)):
                cv = self.convs[idx[k]]
                lineas.append(f"{AMAR(f'{k + 1:>4}.')} {GRIS(fmt_dia(cv.actividad))} {self.etiqueta(cv)}{cv.titulo[:60]} {GRIS(f'({len(cv.inter)})')}")
            lineas.append(GRIS(f"página {pag + 1}/{total} · fecha = última actividad · (n) = nº de preguntas"))
            paginar(lineas)
            r = preguntar(NEG("Nº para abrir") + GRIS(" · Enter/s: sig. · a: ant. · f: filtrar título · q: menú > "))
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
            elif r.lower() == "q":
                return

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
            print(f" 5) Incluir razonamiento/herramientas de la IA: {NEG('sí' if o.razonamiento else 'no')}")
            print(f" 6) Buscar dentro del texto de adjuntos (Claude): {NEG('sí' if o.adjuntos else 'no')}")
            print(f" 7) Incluir ramas editadas (Claude): {NEG('sí' if o.todas_ramas else 'no')}")
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
            elif r in ("0", "q", ""):
                if self.consulta:
                    self.buscar(self.consulta)
                return
            if rehacer:
                print(GRIS("Reconstruyendo índice..."))
                self.convs = construir(self.fuentes, o)

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
            npreg = sum(len(cv.inter) for cv in self.convs)
            fechas = [cv.fecha for cv in self.convs if cv.fecha]
            fuentes = sorted({cv.fuente for cv in self.convs})
            print()
            print(separador("Buscador de conversaciones de IA", VERDE))
            print(GRIS(f"{', '.join(fuentes)} · {len(self.convs)} conversaciones · {npreg} preguntas"))
            if fechas:
                print(GRIS(f"Desde {fmt_dia(min(fechas))} hasta {fmt_dia(max(cv.actividad or cv.fecha for cv in self.convs if cv.actividad or cv.fecha))}"))
            print(" 1) Buscar")
            print(" 2) Ver lista de conversaciones")
            if self.res:
                print(f" 3) Volver a los resultados de: {self.consulta}")
            print(" 4) Opciones")
            print(" 5) Última conversación (¿está actualizado el historial?)")
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
            elif r:
                self.ciclo_busqueda(r)



# ----------------------------------------------------------------------------
# Descarga automática de las partes del índice de Claude (Termux + Firefox)
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
        cands += glob.glob(os.path.join(d, "manifest-*.json"))
    cands = [c for c in cands if es_manifiesto(c)]
    return max(cands, key=os.path.getmtime) if cands else None


def abrir_en_navegador(url, paquete=None):
    """Abre la URL en Firefox (o el navegador indicado). Devuelve True si lo consigue."""
    for pkg in ([paquete] if paquete else NAVEGADORES):
        try:
            r = subprocess.run(["am", "start", "-a", "android.intent.action.VIEW", "-d", url, "-p", pkg],
                               capture_output=True, text=True, timeout=20)
            salida = (r.stdout or "") + (r.stderr or "")
            if r.returncode == 0 and "Error" not in salida and "Exception" not in salida:
                return True
        except (OSError, subprocess.SubprocessError):
            break
    try:  # plan B: navegador por defecto
        return subprocess.run(["termux-open-url", url], timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def esperar_descarga(carpeta, nombre, desde, limite):
    """Espera a que aparezca en Descargas un archivo nuevo con ese nombre
    (o con "(1)", "(2)"... añadido por Firefox) y a que deje de crecer."""
    base, ext = os.path.splitext(nombre)
    patron = os.path.join(carpeta, glob.escape(base) + "*" + ext)
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
    shutil.copy2(manif_ruta, os.path.join(destino, os.path.basename(manif_ruta)))

    partes = [x for x in manif["data_files"] if isinstance(x, dict) and x.get("export_url")]
    if solo_conv:
        partes = [x for x in partes if x.get("category") == "conversations"]
    # Las conversaciones primero: es lo importante si algo falla después
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
        nombre = x.get("filename") or f"parte-{i}.zip"
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
    conv = [p for p in ok if os.path.basename(p).startswith("conversations-")]
    return conv


def main():
    global COLOR
    ap = argparse.ArgumentParser(
        description="Buscador del historial exportado de Claude, ChatGPT, DeepSeek, Kimi, Lumo y otras IA.",
        epilog="Manual completo: buscaraiManual.txt")
    ap.add_argument("archivos", nargs="*", help="conversations-000.zip, manifest-....json (índice de Claude), conversations.json, .zip, .jsonl, .md/.txt o carpeta (uno o varios)")
    ap.add_argument("-b", "--buscar", help="texto a buscar nada más arrancar")
    ap.add_argument("-d", "--descargar", nargs="?", const="", metavar="MANIFEST",
                    help="descarga con Firefox todas las partes del índice de Claude y las mueve a esta carpeta "
                         "(sin MANIFEST usa el manifest-*.json más reciente de Descargas)")
    ap.add_argument("--solo-conversaciones", action="store_true",
                    help="con -d, descargar solo la parte de conversaciones")
    ap.add_argument("--destino", help="con -d, carpeta donde dejar las partes (por defecto claude_export_FECHA/)")
    ap.add_argument("--navegador", help="con -d, paquete Android del navegador (por defecto Firefox)")
    ap.add_argument("--espera", type=int, default=300, help="con -d, segundos máximos de espera por parte (300)")
    ap.add_argument("-u", "--ultima", action="store_true",
                    help="muestra la última conversación del historial y sale (para saber si hay que volver a exportarlo)")
    ap.add_argument("-c", "--contexto", type=int, default=3, help="líneas antes/después (por defecto 3)")
    ap.add_argument("-o", "--cualquiera", action="store_true", help="con varias palabras basta con que aparezca una")
    ap.add_argument("-w", "--palabra", action="store_true", help="solo palabras completas")
    ap.add_argument("--antiguas", action="store_true", help="listar primero las más antiguas")
    ap.add_argument("--razonamiento", action="store_true", help="incluir razonamiento y herramientas de la IA")
    ap.add_argument("--adjuntos", action="store_true", help="buscar dentro del texto de los adjuntos (Claude)")
    ap.add_argument("--todas-ramas", action="store_true", help="incluir versiones editadas de mensajes (Claude)")
    ap.add_argument("--desde", help="solo conversaciones creadas desde AAAA-MM-DD")
    ap.add_argument("--hasta", help="solo conversaciones creadas hasta AAAA-MM-DD")
    ap.add_argument("--sin-color", action="store_true", help="sin colores")
    ap.add_argument("--version", action="version", version=f"buscarai.py {VERSION}")
    a = ap.parse_args()
    if a.sin_color:
        COLOR = False

    if a.descargar is not None:
        manif = a.descargar or buscar_manifiesto_reciente()
        if not manif or not os.path.isfile(manif):
            print(ROJO("No encuentro el índice manifest-....json. Pásalo así:"))
            print("  python buscarai.py -d ~/storage/downloads/manifest-XXXX.json")
            sys.exit(1)
        conv = descargar_partes(manif, a.destino, a.solo_conversaciones, a.navegador, a.espera)
        if not conv:
            sys.exit(1)
        r = preguntar(NEG("\n¿Abrir ahora el buscador con las conversaciones descargadas? (S/n) ")).lower()
        if r in ("n", "no", "q"):
            return
        a.archivos = conv

    rutas = a.archivos or buscar_archivo_por_defecto()
    faltan = [r for r in rutas if not os.path.exists(r)]

    # Índice (manifest) de Claude: usar las partes ya descargadas o explicar qué bajar
    nuevas = []
    for r in rutas:
        manif = es_manifiesto(r) if r not in faltan else None
        if not manif:
            nuevas.append(r)
            continue
        esperadas, halladas = partes_descargadas(manif, os.path.dirname(os.path.abspath(r)))
        if halladas:
            print(GRIS("Índice de Claude detectado; uso las partes descargadas: " + ", ".join(halladas)))
            if len(halladas) < len(esperadas):
                print(AMAR(f"Ojo: faltan {len(esperadas) - len(halladas)} parte(s) de conversaciones por descargar."))
            nuevas += halladas
        else:
            explicar_manifiesto(r, manif)
    if rutas and not nuevas:
        sys.exit(1)
    rutas = nuevas
    if not rutas or faltan:
        if faltan:
            print(ROJO("No existe: " + ", ".join(faltan)))
        else:
            print(ROJO("No encuentro el archivo de conversaciones."))
        print("Indica la ruta, por ejemplo:")
        print("  python buscarai.py ~/storage/downloads/conversations.json")
        print("  python buscarai.py ~/storage/downloads/conversations-000.zip")
        sys.exit(1)

    a.modo = "o" if a.cualquiera else "y"
    a.recientes = not a.antiguas
    app = App(rutas, a)
    if not app.convs:
        print(AMAR("No he encontrado conversaciones con mensajes en ese archivo."))
        print("Si es de Kimi, Lumo u otra IA con un formato distinto, envía a Claude un trozo")
        print("del archivo (sin datos personales) para adaptarlo.")
        sys.exit(1)
    if a.ultima:
        for l in app.lineas_ultima()[0]:
            print(l)
        return
    if a.buscar:
        app.ciclo_busqueda(a.buscar)
    app.principal()
    print("Hasta luego.")


if __name__ == "__main__":
    main()
