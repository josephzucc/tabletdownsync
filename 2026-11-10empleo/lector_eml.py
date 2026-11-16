#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
lector_eml.py

Lector interactivo de archivos EML para Termux.

Características:
- Busca EML en la carpeta actual.
- Permite seleccionar uno o varios EML.
- Extrae text/plain.
- Extrae text/html y lo convierte a texto.
- Soporta multipart/*.
- Soporta message/rfc822 anidados.
- Soporta quoted-printable y base64 mediante email.policy.
- Gestiona diferentes charset.
- Detecta adjuntos normales e inline con nombre de archivo.
- Permite seleccionar individualmente los adjuntos que se desean guardar.
- Guarda los adjuntos en una carpeta independiente por cada EML.
- Evita path traversal mediante nombres de archivo peligrosos.
"""

import os
import re
import sys
import html
from pathlib import Path
from email import policy
from email.parser import BytesParser
from email.message import Message
from html.parser import HTMLParser


# ============================================================
# CONFIGURACIÓN
# ============================================================

OUTPUT_DIR_NAME = "adjuntos_eml"


# ============================================================
# CONVERSIÓN HTML -> TEXTO
# ============================================================

class HTMLToTextParser(HTMLParser):
    """
    Conversor sencillo de HTML a texto.
    No intenta reproducir el diseño visual del correo.
    Su objetivo es obtener texto legible.
    """

    BLOCK_TAGS = {
        "p", "div", "br", "hr",
        "li", "ul", "ol",
        "table", "tr",
        "h1", "h2", "h3", "h4", "h5", "h6",
        "blockquote",
        "section", "article",
        "header", "footer",
    }

    SKIP_TAGS = {
        "script", "style", "head", "noscript", "svg"
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip_level = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()

        if tag in self.SKIP_TAGS:
            self.skip_level += 1
            return

        if self.skip_level:
            return

        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

        if tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()

        if tag in self.SKIP_TAGS:
            if self.skip_level:
                self.skip_level -= 1
            return

        if self.skip_level:
            return

        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.skip_level:
            return

        self.parts.append(data)

    def get_text(self):
        text = "".join(self.parts)

        # Normalización de espacios
        text = text.replace("\xa0", " ")

        # Elimina espacios antes/después de saltos de línea
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n[ \t]+", "\n", text)

        # Máximo dos líneas vacías consecutivas
        text = re.sub(r"\n{3,}", "\n\n", text)

        return text.strip()


def html_to_text(data):
    """
    Convierte HTML a texto.
    data puede ser str o bytes.
    """

    if isinstance(data, bytes):
        data = data.decode("utf-8", errors="replace")

    # Primero desescapamos entidades HTML.
    data = html.unescape(data)

    parser = HTMLToTextParser()

    try:
        parser.feed(data)
        parser.close()
        return parser.get_text()
    except Exception:
        # Si existe HTML especialmente defectuoso,
        # devolvemos al menos una versión razonablemente legible.
        text = re.sub(r"<[^>]+>", " ", data)
        text = html.unescape(text)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


# ============================================================
# UTILIDADES
# ============================================================

def limpiar_nombre_archivo(nombre):
    """
    Limpia un nombre de archivo procedente de una cabecera MIME.

    Evita:
      ../../archivo
      /etc/passwd
      C:\\Windows\\...
    """

    if not nombre:
        return "adjunto_sin_nombre"

    nombre = os.path.basename(nombre)

    # Elimina caracteres de control
    nombre = "".join(
        c for c in nombre
        if ord(c) >= 32 and c not in '<>:"/\\|?*'
    )

    nombre = nombre.strip().strip(".")

    if not nombre:
        nombre = "adjunto_sin_nombre"

    return nombre


def nombre_unico(path):
    """
    Si el archivo ya existe, genera:
      archivo.pdf
      archivo_1.pdf
      archivo_2.pdf
    """

    if not path.exists():
        return path

    stem = path.stem
    suffix = path.suffix

    contador = 1

    while True:
        nuevo = path.with_name(
            f"{stem}_{contador}{suffix}"
        )

        if not nuevo.exists():
            return nuevo

        contador += 1


def pedir_entero(mensaje, minimo, maximo):
    while True:
        try:
            valor = input(mensaje).strip()

            if not valor:
                return None

            valor = int(valor)

            if minimo <= valor <= maximo:
                return valor

            print(
                f"Introduzca un número entre "
                f"{minimo} y {maximo}."
            )

        except ValueError:
            print("Introduzca un número válido.")


def parsear_seleccion(texto, maximo):
    """
    Permite:
      1
      1,3,5
      1-4
      1,3-6,9
      todos
      0 = ninguno
    """

    texto = texto.strip().lower()

    if texto in ("", "0", "ninguno", "ninguna"):
        return []

    if texto in ("todos", "todas", "all", "*"):
        return list(range(maximo))

    resultado = set()

    partes = texto.split(",")

    for parte in partes:
        parte = parte.strip()

        if not parte:
            continue

        if "-" in parte:
            try:
                a, b = parte.split("-", 1)
                a = int(a.strip())
                b = int(b.strip())

                if a > b:
                    a, b = b, a

                for n in range(a, b + 1):
                    if 1 <= n <= maximo:
                        resultado.add(n - 1)

            except ValueError:
                continue

        else:
            try:
                n = int(parte)

                if 1 <= n <= maximo:
                    resultado.add(n - 1)

            except ValueError:
                continue

    return sorted(resultado)


# ============================================================
# INFORMACIÓN DEL MENSAJE
# ============================================================

def obtener_asunto(msg):
    asunto = msg.get("Subject")

    if not asunto:
        return "(sin asunto)"

    return str(asunto)


def obtener_remitente(msg):
    remitente = msg.get("From")

    if not remitente:
        return "(desconocido)"

    return str(remitente)


def obtener_fecha(msg):
    fecha = msg.get("Date")

    if not fecha:
        return "(sin fecha)"

    return str(fecha)


# ============================================================
# REPRESENTACIÓN DE ADJUNTOS
# ============================================================

class Attachment:
    def __init__(
        self,
        part,
        filename,
        content_type,
        disposition,
        location
    ):
        self.part = part
        self.filename = filename
        self.content_type = content_type
        self.disposition = disposition
        self.location = location

    @property
    def size(self):
        try:
            payload = self.part.get_payload(decode=True)

            if payload is None:
                return 0

            return len(payload)

        except Exception:
            return 0


# ============================================================
# RECORRIDO MIME
# ============================================================

def recorrer_partes(
    msg,
    attachments,
    text_parts,
    html_parts,
    other_text_parts,
    ruta="1"
):
    """
    Recorre recursivamente toda la estructura MIME.

    Detecta:

    multipart/*
    message/rfc822
    text/plain
    text/html
    otros text/*
    adjuntos
    """

    # --------------------------------------------------------
    # Multipart
    # --------------------------------------------------------

    if msg.is_multipart():

        payload = msg.get_payload()

        if isinstance(payload, list):

            for indice, subparte in enumerate(payload, 1):

                nueva_ruta = f"{ruta}.{indice}"

                if isinstance(subparte, Message):

                    recorrer_partes(
                        subparte,
                        attachments,
                        text_parts,
                        html_parts,
                        other_text_parts,
                        nueva_ruta
                    )

        return

    # --------------------------------------------------------
    # message/rfc822
    # --------------------------------------------------------

    if msg.get_content_type().lower() == "message/rfc822":

        payload = msg.get_payload()

        if isinstance(payload, list):

            for indice, subparte in enumerate(payload, 1):

                if isinstance(subparte, Message):

                    recorrer_partes(
                        subparte,
                        attachments,
                        text_parts,
                        html_parts,
                        other_text_parts,
                        f"{ruta}.rfc822.{indice}"
                    )

        elif isinstance(payload, Message):

            recorrer_partes(
                payload,
                attachments,
                text_parts,
                html_parts,
                other_text_parts,
                f"{ruta}.rfc822"
            )

        return

    # --------------------------------------------------------
    # Datos básicos de la parte
    # --------------------------------------------------------

    content_type = msg.get_content_type().lower()

    disposition = msg.get_content_disposition()

    filename = msg.get_filename()

    content_id = msg.get("Content-ID")

    # --------------------------------------------------------
    # ¿Es adjunto?
    # --------------------------------------------------------

    es_adjunto = False

    if disposition == "attachment":
        es_adjunto = True

    elif filename:
        # Un archivo con nombre suele ser un adjunto aunque
        # el servidor no haya puesto correctamente
        # Content-Disposition.
        es_adjunto = True

    # --------------------------------------------------------
    # text/plain
    # --------------------------------------------------------

    if content_type == "text/plain":

        try:
            contenido = msg.get_content()
        except Exception:
            payload = msg.get_payload(decode=True)

            if payload is None:
                contenido = ""
            else:
                charset = msg.get_content_charset() or "utf-8"

                try:
                    contenido = payload.decode(
                        charset,
                        errors="replace"
                    )
                except Exception:
                    contenido = payload.decode(
                        "utf-8",
                        errors="replace"
                    )

        if es_adjunto:
            attachments.append(
                Attachment(
                    msg,
                    limpiar_nombre_archivo(filename),
                    content_type,
                    disposition,
                    ruta
                )
            )
        else:
            text_parts.append(
                (ruta, contenido)
            )

        return

    # --------------------------------------------------------
    # text/html
    # --------------------------------------------------------

    if content_type == "text/html":

        try:
            contenido = msg.get_content()
        except Exception:
            payload = msg.get_payload(decode=True)

            if payload is None:
                contenido = ""
            else:
                charset = msg.get_content_charset() or "utf-8"

                try:
                    contenido = payload.decode(
                        charset,
                        errors="replace"
                    )
                except Exception:
                    contenido = payload.decode(
                        "utf-8",
                        errors="replace"
                    )

        if es_adjunto:
            attachments.append(
                Attachment(
                    msg,
                    limpiar_nombre_archivo(filename),
                    content_type,
                    disposition,
                    ruta
                )
            )
        else:
            html_parts.append(
                (ruta, contenido)
            )

        return

    # --------------------------------------------------------
    # Otros text/*
    # --------------------------------------------------------

    if content_type.startswith("text/"):

        try:
            contenido = msg.get_content()
        except Exception:
            payload = msg.get_payload(decode=True)

            if payload is None:
                contenido = ""
            else:
                charset = msg.get_content_charset() or "utf-8"

                try:
                    contenido = payload.decode(
                        charset,
                        errors="replace"
                    )
                except Exception:
                    contenido = payload.decode(
                        "utf-8",
                        errors="replace"
                    )

        if es_adjunto:
            attachments.append(
                Attachment(
                    msg,
                    limpiar_nombre_archivo(filename),
                    content_type,
                    disposition,
                    ruta
                )
            )
        else:
            other_text_parts.append(
                (ruta, content_type, contenido)
            )

        return

    # --------------------------------------------------------
    # Todo lo demás
    # --------------------------------------------------------

    # application/pdf
    # image/jpeg
    # application/zip
    # audio/*
    # video/*
    # application/octet-stream
    # etc.
    #
    # Si tiene filename o Content-Disposition attachment,
    # lo tratamos como adjunto.
    #
    # También consideramos como adjunto una parte binaria
    # con disposition inline si tiene filename.

    if es_adjunto or content_id:

        nombre = filename

        if not nombre:
            # Intentamos generar un nombre razonable.
            extension = ""

            if "/" in content_type:
                subtipo = content_type.split("/", 1)[1]

                if subtipo and subtipo not in (
                    "octet-stream",
                    "binary"
                ):
                    extension = "." + subtipo

            nombre = f"adjunto_{len(attachments) + 1}{extension}"

        attachments.append(
            Attachment(
                msg,
                limpiar_nombre_archivo(nombre),
                content_type,
                disposition,
                ruta
            )
        )


# ============================================================
# OBTENER TEXTO DEL EMAIL
# ============================================================

def extraer_texto(msg):
    text_parts = []
    html_parts = []
    other_text_parts = []
    attachments = []

    recorrer_partes(
        msg,
        attachments,
        text_parts,
        html_parts,
        other_text_parts
    )

    # --------------------------------------------------------
    # Preferencia:
    #
    # 1. text/plain
    # 2. text/html
    # 3. otros text/*
    # --------------------------------------------------------

    bloques = []

    if text_parts:

        for ruta, texto in text_parts:

            if texto and texto.strip():

                bloques.append(
                    f"[text/plain - parte MIME {ruta}]\n\n"
                    f"{texto.strip()}"
                )

    elif html_parts:

        for ruta, html_data in html_parts:

            texto = html_to_text(html_data)

            if texto:

                bloques.append(
                    f"[text/html - parte MIME {ruta}]\n\n"
                    f"{texto}"
                )

    elif other_text_parts:

        for ruta, tipo, texto in other_text_parts:

            if texto and texto.strip():

                bloques.append(
                    f"[{tipo} - parte MIME {ruta}]\n\n"
                    f"{texto.strip()}"
                )

    if not bloques:

        return "(Este EML no contiene un cuerpo de texto legible.)", attachments

    return "\n\n" + ("\n\n" + ("-" * 70) + "\n\n").join(
        bloques
    ), attachments


# ============================================================
# LEER EML
# ============================================================

def abrir_eml(path):

    try:

        with open(path, "rb") as f:

            msg = BytesParser(
                policy=policy.default
            ).parse(f)

        return msg

    except Exception as e:

        print()
        print("ERROR leyendo EML:")
        print(e)
        return None


# ============================================================
# MOSTRAR INFORMACIÓN DEL EML
# ============================================================

def mostrar_email(path, msg, texto, attachments):

    print()
    print("=" * 78)
    print("ARCHIVO:", path.name)
    print("=" * 78)

    print("Asunto   :", obtener_asunto(msg))
    print("De       :", obtener_remitente(msg))
    print("Fecha    :", obtener_fecha(msg))

    to = msg.get("To")

    if to:
        print("Para     :", str(to))

    cc = msg.get("Cc")

    if cc:
        print("CC       :", str(cc))

    print()
    print("-" * 78)
    print("CUERPO DEL MENSAJE")
    print("-" * 78)

    print(texto)

    print()
    print("-" * 78)

    if attachments:

        print(
            f"ADJUNTOS ENCONTRADOS: "
            f"{len(attachments)}"
        )

        for i, adj in enumerate(attachments, 1):

            print(
                f"{i:3d}. "
                f"{adj.filename} "
                f"[{adj.content_type}] "
                f"{adj.size:,} bytes "
                f"({adj.disposition or 'sin disposition'})"
            )

    else:

        print("No se han encontrado adjuntos.")

    print("=" * 78)


# ============================================================
# DESCARGAR ADJUNTOS
# ============================================================

def descargar_adjuntos(
    eml_path,
    attachments
):

    if not attachments:
        print("\nEste EML no tiene adjuntos.")
        return

    print()
    print("ADJUNTOS DISPONIBLES")
    print("-" * 78)

    for i, adj in enumerate(attachments, 1):

        print(
            f"{i:3d}. "
            f"{adj.filename} "
            f"[{adj.content_type}] "
            f"{adj.size:,} bytes"
        )

    print()
    print("Seleccione los adjuntos que desea descargar.")
    print()
    print("Ejemplos:")
    print("  1")
    print("  1,3,5")
    print("  1-4")
    print("  1,3-6,9")
    print("  todos")
    print("  0 = ninguno")
    print()

    seleccion = input(
        "Selección: "
    ).strip()

    indices = parsear_seleccion(
        seleccion,
        len(attachments)
    )

    if not indices:

        print("No se ha seleccionado ningún adjunto.")
        return

    # Carpeta:
    #
    # carpeta_actual/
    #     adjuntos_eml/
    #         nombre_del_eml/
    #
    carpeta_base = (
        Path.cwd()
        / OUTPUT_DIR_NAME
    )

    nombre_carpeta = limpiar_nombre_archivo(
        eml_path.stem
    )

    destino = (
        carpeta_base
        / nombre_carpeta
    )

    destino.mkdir(
        parents=True,
        exist_ok=True
    )

    print()
    print("Guardando en:")
    print(destino)
    print()

    guardados = 0

    for indice in indices:

        adj = attachments[indice]

        try:

            datos = adj.part.get_payload(
                decode=True
            )

            if datos is None:

                # Algunos EML pueden contener payload
                # de una forma poco habitual.
                payload = adj.part.get_payload()

                if isinstance(payload, bytes):
                    datos = payload

                elif isinstance(payload, str):
                    datos = payload.encode(
                        adj.part.get_content_charset()
                        or "utf-8",
                        errors="replace"
                    )

                else:
                    print(
                        f"ERROR: no se pudo obtener "
                        f"el contenido de {adj.filename}"
                    )
                    continue

            archivo = destino / adj.filename

            archivo = nombre_unico(archivo)

            with open(archivo, "wb") as f:
                f.write(datos)

            print(
                f"[OK] {adj.filename} "
                f"-> {archivo.name} "
                f"({len(datos):,} bytes)"
            )

            guardados += 1

        except Exception as e:

            print(
                f"[ERROR] {adj.filename}: {e}"
            )

    print()
    print(
        f"Adjuntos guardados: "
        f"{guardados}/{len(indices)}"
    )


# ============================================================
# SELECCIÓN DE EML
# ============================================================

def obtener_eml_actual():

    return sorted(
        [
            p for p in Path.cwd().iterdir()
            if p.is_file()
            and p.suffix.lower() == ".eml"
        ],
        key=lambda x: x.name.lower()
    )


def seleccionar_eml(files):

    print()
    print("=" * 78)
    print("ARCHIVOS EML DE LA CARPETA ACTUAL")
    print("=" * 78)

    if not files:

        print()
        print(
            "No se encontraron archivos .eml "
            "en la carpeta actual."
        )

        return []

    for i, path in enumerate(files, 1):

        try:
            size = path.stat().st_size

            print(
                f"{i:3d}. "
                f"{path.name} "
                f"({size:,} bytes)"
            )

        except Exception:

            print(
                f"{i:3d}. "
                f"{path.name}"
            )

    print()
    print("Puede seleccionar:")
    print("  1")
    print("  1,3,5")
    print("  1-4")
    print("  todos")
    print("  0 = salir")
    print()

    seleccion = input(
        "EML a abrir: "
    ).strip()

    if seleccion.lower() in (
        "",
        "0",
        "salir",
        "q",
        "quit"
    ):
        return []

    indices = parsear_seleccion(
        seleccion,
        len(files)
    )

    return [
        files[i]
        for i in indices
    ]


# ============================================================
# PROCESAR UN EML
# ============================================================

def procesar_eml(path):

    print()
    print("Leyendo:", path.name)

    msg = abrir_eml(path)

    if msg is None:
        input("\nPulse ENTER...")
        return

    texto, attachments = extraer_texto(msg)

    mostrar_email(
        path,
        msg,
        texto,
        attachments
    )

    if attachments:

        print()
        respuesta = input(
            "¿Desea descargar adjuntos de este EML? "
            "[s/N]: "
        ).strip().lower()

        if respuesta in ("s", "si", "sí", "y", "yes"):

            descargar_adjuntos(
                path,
                attachments
            )

    print()
    input("Pulse ENTER para continuar...")


# ============================================================
# PROGRAMA PRINCIPAL
# ============================================================

def main():

    print()
    print("=" * 78)
    print("LECTOR DE ARCHIVOS EML - TERMUX")
    print("=" * 78)
    print()
    print("Carpeta actual:")
    print(Path.cwd())
    print()

    while True:

        files = obtener_eml_actual()

        seleccionados = seleccionar_eml(files)

        if not seleccionados:
            print()
            print("Fin.")
            break

        for path in seleccionados:

            procesar_eml(path)

        print()

        otra = input(
            "¿Desea seleccionar más EML? [S/n]: "
        ).strip().lower()

        if otra in ("n", "no", "q", "quit"):

            print()
            print("Fin.")
            break


if __name__ == "__main__":
    main()
