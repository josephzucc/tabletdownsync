#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import sys
import html
import mimetypes
from pathlib import Path
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser


# ============================================================
# CONFIGURACIÓN
# ============================================================

OUTPUT_DIR_NAME = "adjuntos_eml"


# ============================================================
# HTML -> TEXTO
# ============================================================

class HTMLToTextParser(HTMLParser):

    BLOCK_TAGS = {
        "address", "article", "aside", "blockquote", "body",
        "caption", "dd", "div", "dl", "dt", "fieldset",
        "figcaption", "figure", "footer", "form",
        "h1", "h2", "h3", "h4", "h5", "h6",
        "header", "hr", "li", "main", "nav", "ol",
        "p", "pre", "section", "table", "tbody",
        "td", "tfoot", "th", "thead", "tr", "ul"
    }

    SKIP_TAGS = {
        "head", "script", "style", "noscript",
        "svg", "template", "iframe", "object", "embed"
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.resultado = []
        self.skip_level = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()

        if tag in self.SKIP_TAGS:
            self.skip_level += 1
            return

        if self.skip_level:
            return

        if tag == "br":
            self.resultado.append("\n")

        elif tag in self.BLOCK_TAGS:
            self.resultado.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()

        if tag in self.SKIP_TAGS:
            if self.skip_level:
                self.skip_level -= 1
            return

        if self.skip_level:
            return

        if tag in self.BLOCK_TAGS:
            self.resultado.append("\n")

    def handle_data(self, data):
        if not self.skip_level:
            self.resultado.append(data)

    def get_text(self):
        texto = "".join(self.resultado)

        texto = html.unescape(texto)

        # Normalizar espacios
        texto = texto.replace("\xa0", " ")

        # Eliminar espacios antes de saltos
        texto = re.sub(r"[ \t]+\n", "\n", texto)

        # Eliminar demasiados saltos
        texto = re.sub(r"\n[ \t]*\n[ \t]*\n+", "\n\n", texto)

        return texto.strip()


def html_a_texto(texto):
    if not texto:
        return ""

    try:
        parser = HTMLToTextParser()
        parser.feed(texto)
        parser.close()
        return parser.get_text()
    except Exception:
        # Fallback para HTML especialmente defectuoso
        texto = re.sub(
            r"(?is)<(script|style).*?>.*?</\1>",
            "",
            texto
        )

        texto = re.sub(r"(?i)<br\s*/?>", "\n", texto)
        texto = re.sub(r"(?i)</p\s*>", "\n\n", texto)
        texto = re.sub(r"(?i)</div\s*>", "\n", texto)

        texto = re.sub(r"<[^>]+>", " ", texto)

        return html.unescape(texto).strip()


def parece_html(texto):

    if not texto:
        return False

    patrones = [
        r"<html\b",
        r"<body\b",
        r"<div\b",
        r"<span\b",
        r"<p\b",
        r"<br\b",
        r"<table\b",
        r"<tr\b",
        r"<td\b",
        r"<th\b",
        r"<ul\b",
        r"<ol\b",
        r"<li\b",
        r"<head\b",
        r"<style\b",
    ]

    texto_lower = texto.lower()

    return any(re.search(p, texto_lower) for p in patrones)


# ============================================================
# DECODIFICACIÓN DE CABECERAS MIME
# ============================================================

def decodificar_cabecera(valor, valor_defecto=""):

    if valor is None:
        return valor_defecto

    try:
        valor = str(valor)
    except Exception:
        return valor_defecto

    if not valor.strip():
        return valor_defecto

    # Primero intentamos el mecanismo estándar de Python
    try:
        resultado = str(make_header(decode_header(valor)))

        if resultado.strip():
            return resultado.strip()

    except Exception:
        pass

    # Fallback manual
    partes = []

    try:
        for parte, charset in decode_header(valor):

            if isinstance(parte, bytes):

                decodificado = None

                codificaciones = []

                if charset:
                    codificaciones.append(charset)

                codificaciones.extend([
                    "utf-8",
                    "windows-1252",
                    "iso-8859-1",
                    "latin-1"
                ])

                for cod in codificaciones:

                    try:
                        decodificado = parte.decode(cod)
                        break
                    except Exception:
                        continue

                if decodificado is None:
                    decodificado = parte.decode(
                        "utf-8",
                        errors="replace"
                    )

                partes.append(decodificado)

            else:
                partes.append(str(parte))

        resultado = "".join(partes)

        if resultado.strip():
            return resultado.strip()

    except Exception:
        pass

    return valor.strip()


# ============================================================
# FECHAS
# ============================================================

def obtener_fecha_datetime(msg):

    """
    Intenta obtener la fecha del encabezado Date:.
    Si no es válida, intenta Received:.
    """

    # --------------------------------------------------------
    # 1. Date:
    # --------------------------------------------------------

    try:

        valor = msg.get("Date")

        if valor:

            valor = str(valor).strip()

            try:
                dt = parsedate_to_datetime(valor)

                if dt is not None:

                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)

                    return dt

            except Exception:
                pass

    except Exception:
        pass

    # --------------------------------------------------------
    # 2. Received:
    # --------------------------------------------------------

    try:

        received = msg.get_all("Received", [])

        for valor in reversed(received):

            if not valor:
                continue

            valor = str(valor)

            # Normalmente la fecha está después del último ;
            candidatos = []

            if ";" in valor:
                candidatos.append(
                    valor.rsplit(";", 1)[-1].strip()
                )

            candidatos.append(valor.strip())

            for candidato in candidatos:

                try:
                    dt = parsedate_to_datetime(candidato)

                    if dt is not None:

                        if dt.tzinfo is None:
                            dt = dt.replace(
                                tzinfo=timezone.utc
                            )

                        return dt

                except Exception:
                    continue

    except Exception:
        pass

    return None


def fecha_para_mostrar(msg):

    dt = obtener_fecha_datetime(msg)

    if dt is None:
        return "SIN FECHA"

    try:
        # Hora local del dispositivo
        dt_local = dt.astimezone()

        return dt_local.strftime(
            "%Y-%m-%d %H:%M"
        )

    except Exception:
        return dt.strftime(
            "%Y-%m-%d %H:%M"
        )


def timestamp_orden(path):

    try:

        msg = abrir_eml(path)

        if msg is None:
            return 0

        dt = obtener_fecha_datetime(msg)

        if dt is None:
            return 0

        return dt.timestamp()

    except Exception:
        return 0


# ============================================================
# LECTURA EML
# ============================================================

def abrir_eml(path):

    try:

        with open(path, "rb") as f:

            msg = BytesParser(
                policy=policy.default
            ).parse(f)

        return msg

    except Exception as e:

        print(
            f"\nERROR leyendo {path.name}: {e}"
        )

        return None


def obtener_contenido_textual(part):

    try:

        contenido = part.get_content()

        if isinstance(contenido, str):
            return contenido

    except Exception:
        pass

    try:

        payload = part.get_payload(
            decode=True
        )

    except Exception:
        payload = None

    if payload is None:

        try:
            payload = part.get_payload()
        except Exception:
            return ""

    if isinstance(payload, str):
        return payload

    if not isinstance(payload, bytes):
        return str(payload)

    charset = part.get_content_charset()

    codificaciones = []

    if charset:
        codificaciones.append(charset)

    codificaciones.extend([
        "utf-8",
        "windows-1252",
        "iso-8859-1",
        "latin-1"
    ])

    for cod in codificaciones:

        try:
            return payload.decode(cod)
        except Exception:
            continue

    return payload.decode(
        "utf-8",
        errors="replace"
    )


# ============================================================
# ADJUNTOS
# ============================================================

class Attachment:

    def __init__(
        self,
        part,
        filename,
        content_type,
        disposition,
        location,
        is_eml=False
    ):

        self.part = part
        self.filename = filename
        self.content_type = content_type
        self.disposition = disposition
        self.location = location
        self.is_eml = is_eml

    def obtener_bytes(self):

        try:

            data = self.part.get_payload(
                decode=True
            )

            if data is not None:
                return data

        except Exception:
            pass

        try:

            payload = self.part.get_payload()

            if isinstance(payload, bytes):
                return payload

            if isinstance(payload, str):
                return payload.encode(
                    self.part.get_content_charset()
                    or "utf-8",
                    errors="replace"
                )

        except Exception:
            pass

        return b""


def es_eml_part(part, filename=None):

    if filename:

        if filename.lower().endswith(".eml"):
            return True

    try:

        if part.get_content_type().lower() == \
                "message/rfc822":
            return True

    except Exception:
        pass

    return False


# ============================================================
# RECORRER MIME
# ============================================================

def recorrer_partes(
    msg,
    attachments,
    text_parts,
    html_parts,
    other_text_parts,
    ruta="1"
):

    try:

        if msg.is_multipart():

            for numero, subpart in enumerate(
                msg.iter_parts(),
                start=1
            ):

                recorrer_partes(
                    subpart,
                    attachments,
                    text_parts,
                    html_parts,
                    other_text_parts,
                    f"{ruta}.{numero}"
                )

            return

    except Exception:
        pass

    # --------------------------------------------------------
    # Filename
    # --------------------------------------------------------

    try:
        filename = msg.get_filename()
    except Exception:
        filename = None

    if filename:
        filename = decodificar_cabecera(filename)

    # --------------------------------------------------------
    # Content-Disposition
    # --------------------------------------------------------

    try:
        disposition = msg.get_content_disposition()
    except Exception:
        disposition = None

    # --------------------------------------------------------
    # Content-Type
    # --------------------------------------------------------

    try:
        content_type = msg.get_content_type().lower()
    except Exception:
        content_type = "application/octet-stream"

    # --------------------------------------------------------
    # EML adjunto
    # --------------------------------------------------------

    if es_eml_part(msg, filename):

        if not filename:
            filename = "correo_adjunto.eml"

        attachments.append(
            Attachment(
                msg,
                filename,
                content_type,
                disposition,
                ruta,
                True
            )
        )

        # message/rfc822 puede contener directamente
        # otro mensaje
        try:

            payload = msg.get_payload()

            if isinstance(payload, list):

                for numero, subpart in enumerate(
                    payload,
                    start=1
                ):

                    recorrer_partes(
                        subpart,
                        attachments,
                        text_parts,
                        html_parts,
                        other_text_parts,
                        f"{ruta}.eml{numero}"
                    )

        except Exception:
            pass

        return

    # --------------------------------------------------------
    # Archivo adjunto normal
    # --------------------------------------------------------

    es_adjunto = (
        disposition == "attachment"
        or bool(filename)
    )

    if es_adjunto:

        if not filename:

            extension = mimetypes.guess_extension(
                content_type
            ) or ""

            filename = (
                f"adjunto_{ruta}"
                f"{extension}"
            )

        attachments.append(
            Attachment(
                msg,
                filename,
                content_type,
                disposition,
                ruta,
                False
            )
        )

        return

    # --------------------------------------------------------
    # Texto
    # --------------------------------------------------------

    if content_type == "text/plain":

        texto = obtener_contenido_textual(msg)

        # Algunos EML declaran text/plain
        # pero contienen HTML
        if parece_html(texto):

            texto = html_a_texto(texto)

        if texto.strip():
            text_parts.append(texto)

        return

    if content_type == "text/html":

        texto = obtener_contenido_textual(msg)

        texto = html_a_texto(texto)

        if texto.strip():
            html_parts.append(texto)

        return

    # Otros text/*
    if content_type.startswith("text/"):

        texto = obtener_contenido_textual(msg)

        if texto.strip():
            other_text_parts.append(texto)


# ============================================================
# EXTRAER TEXTO
# ============================================================

def extraer_texto(msg):

    attachments = []
    text_parts = []
    html_parts = []
    other_text_parts = []

    recorrer_partes(
        msg,
        attachments,
        text_parts,
        html_parts,
        other_text_parts
    )

    if text_parts:
        texto = "\n\n".join(text_parts)

    elif html_parts:
        texto = "\n\n".join(html_parts)

    elif other_text_parts:
        texto = "\n\n".join(other_text_parts)

    else:
        texto = ""

    return texto.strip(), attachments


# ============================================================
# CABECERAS
# ============================================================

def mostrar_cabeceras(msg):

    print()
    print("-" * 80)

    asunto = decodificar_cabecera(
        msg.get("Subject"),
        "(sin asunto)"
    )

    remitente = decodificar_cabecera(
        msg.get("From"),
        "(desconocido)"
    )

    destinatario = decodificar_cabecera(
        msg.get("To"),
        "(desconocido)"
    )

    cc = decodificar_cabecera(
        msg.get("Cc"),
        ""
    )

    fecha = fecha_para_mostrar(msg)

    print(f"Asunto:      {asunto}")
    print(f"De:          {remitente}")
    print(f"Para:        {destinatario}")

    if cc:
        print(f"CC:          {cc}")

    print(f"Fecha/hora:  {fecha}")

    print("-" * 80)


# ============================================================
# MOSTRAR ADJUNTOS
# ============================================================

def mostrar_lista_adjuntos(attachments):

    if not attachments:

        print()
        print("No hay archivos adjuntos.")
        return

    print()
    print("ADJUNTOS")
    print("-" * 80)

    for i, adj in enumerate(
        attachments,
        start=1
    ):

        try:
            size = len(adj.obtener_bytes())
        except Exception:
            size = 0

        tipo = adj.content_type

        marca = ""

        if adj.is_eml:
            marca = " [EML - CORREO]"

        print(
            f"{i:3d}. "
            f"{adj.filename}"
            f"{marca}"
            f"  ({tipo}, {size:,} bytes)"
        )

    print("-" * 80)


# ============================================================
# LEER EML ADJUNTO
# ============================================================

def leer_eml_adjunto(adj):

    data = adj.obtener_bytes()

    if not data:

        print("\nNo se ha podido obtener el contenido del EML.")
        return

    try:

        msg = BytesParser(
            policy=policy.default
        ).parsebytes(data)

    except Exception as e:

        print(
            f"\nERROR leyendo el EML adjunto: {e}"
        )

        return

    procesar_mensaje(
        msg,
        f"EML adjunto: {adj.filename}"
    )


# ============================================================
# DESCARGAR ADJUNTOS
# ============================================================

def nombre_seguro(nombre):

    nombre = os.path.basename(nombre)

    nombre = re.sub(
        r'[\\/:*?"<>|]',
        "_",
        nombre
    )

    nombre = nombre.strip()

    if not nombre:
        nombre = "adjunto"

    return nombre


def ruta_unica(path):

    if not path.exists():
        return path

    contador = 1

    while True:

        nuevo = (
            path.parent
            / f"{path.stem}_{contador}"
            f"{path.suffix}"
        )

        if not nuevo.exists():
            return nuevo

        contador += 1


def descargar_adjuntos(
    eml_path,
    attachments,
    nombre_base=None
):

    if not attachments:

        print("\nNo hay adjuntos.")
        return

    print()

    seleccion = input(
        "Adjuntos a descargar "
        "(1,3,5 / 1-4 / todos / 0=cancelar): "
    ).strip()

    if seleccion.lower() in (
        "",
        "0",
        "cancelar",
        "q"
    ):
        return

    if seleccion.lower() == "todos":

        indices = list(
            range(len(attachments))
        )

    else:

        indices = parsear_seleccion(
            seleccion,
            len(attachments)
        )

    if not indices:

        print("\nSelección no válida.")
        return

    carpeta = (
        Path(OUTPUT_DIR_NAME)
        / (
            nombre_base
            or eml_path.stem
        )
    )

    carpeta.mkdir(
        parents=True,
        exist_ok=True
    )

    print()

    for indice in indices:

        adj = attachments[indice]

        data = adj.obtener_bytes()

        if not data:

            print(
                f"No se pudo descargar: "
                f"{adj.filename}"
            )
            continue

        nombre = nombre_seguro(
            adj.filename
        )

        destino = ruta_unica(
            carpeta / nombre
        )

        try:

            with open(destino, "wb") as f:
                f.write(data)

            print(
                f"Guardado: {destino}"
            )

        except Exception as e:

            print(
                f"ERROR guardando "
                f"{adj.filename}: {e}"
            )


# ============================================================
# SELECCIÓN
# ============================================================

def parsear_seleccion(texto, maximo):

    resultado = set()

    partes = texto.split(",")

    for parte in partes:

        parte = parte.strip()

        if not parte:
            continue

        if "-" in parte:

            extremos = parte.split("-", 1)

            try:

                inicio = int(
                    extremos[0].strip()
                )

                fin = int(
                    extremos[1].strip()
                )

                if inicio > fin:
                    inicio, fin = fin, inicio

                for numero in range(
                    inicio,
                    fin + 1
                ):

                    if 1 <= numero <= maximo:
                        resultado.add(
                            numero - 1
                        )

            except ValueError:
                continue

        else:

            try:

                numero = int(parte)

                if 1 <= numero <= maximo:
                    resultado.add(
                        numero - 1
                    )

            except ValueError:
                continue

    return sorted(resultado)


def seleccionar_eml(files):

    print()
    print("=" * 110)
    print("CORREOS EML - MÁS RECIENTE -> MÁS ANTIGUO")
    print("=" * 110)

    if not files:

        print()
        print(
            "No se encontraron archivos "
            ".eml en la carpeta actual."
        )

        return []

    informacion = []

    for path in files:

        try:

            msg = abrir_eml(path)

            if msg:

                fecha = fecha_para_mostrar(
                    msg
                )

                asunto = decodificar_cabecera(
                    msg.get("Subject"),
                    "(sin asunto)"
                )

                asunto = (
                    asunto
                    .replace("\r", " ")
                    .replace("\n", " ")
                )

            else:

                fecha = "ERROR"
                asunto = "(no se pudo leer)"

        except Exception as e:

            fecha = "ERROR"
            asunto = "(cabecera no válida)"

        informacion.append(
            (
                path,
                fecha,
                asunto
            )
        )

    print()

    print(
        f"{'Nº':>3}  "
        f"{'FECHA/HORA':<20}  "
        f"{'ASUNTO':<60}  "
        f"NOMBRE DEL EML"
    )

    print("-" * 110)

    for i, (
        path,
        fecha,
        asunto
    ) in enumerate(
        informacion,
        start=1
    ):

        asunto_mostrar = asunto

        if len(asunto_mostrar) > 60:

            asunto_mostrar = (
                asunto_mostrar[:57]
                + "..."
            )

        nombre = path.name

        if len(nombre) > 25:

            nombre = (
                nombre[:22]
                + "..."
            )

        print(
            f"{i:3d}  "
            f"{fecha:<20}  "
            f"{asunto_mostrar:<60}  "
            f"{nombre}"
        )

    print("-" * 110)

    print()
    print("Seleccione los correos:")
    print()
    print("  1        = un correo")
    print("  1,3,5    = varios correos")
    print("  1-4      = intervalo")
    print("  1,3-6,9  = combinación")
    print("  todos    = todos los correos")
    print("  0        = salir")
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

    if seleccion.lower() == "todos":

        return files

    indices = parsear_seleccion(
        seleccion,
        len(files)
    )

    return [
        files[i]
        for i in indices
    ]


# ============================================================
# PROCESAR MENSAJE
# ============================================================

def procesar_mensaje(
    msg,
    titulo="CORREO"
):

    print()
    print("=" * 80)
    print(titulo)
    print("=" * 80)

    mostrar_cabeceras(msg)

    texto, attachments = extraer_texto(
        msg
    )

    print()

    if texto:

        print("-" * 80)
        print("CUERPO DEL MENSAJE")
        print("-" * 80)
        print()
        print(texto)
        print()
        print("-" * 80)

    else:

        print()
        print(
            "No se ha encontrado contenido "
            "textual legible."
        )

    mostrar_lista_adjuntos(
        attachments
    )

    return attachments


# ============================================================
# MENÚ DEL CORREO
# ============================================================

def menu_correo(
    eml_path,
    msg,
    attachments
):

    while True:

        print()
        print("=" * 80)
        print("MENÚ")
        print("=" * 80)

        print("1. Leer cuerpo del correo")
        print("2. Ver lista de adjuntos")
        print("3. Descargar adjuntos")
        print("4. Leer un EML adjunto")
        print("0. Volver a la lista")

        print()

        opcion = input(
            "Opción: "
        ).strip()

        if opcion == "1":

            texto, _ = extraer_texto(
                msg
            )

            print()
            print("-" * 80)
            print("CUERPO DEL MENSAJE")
            print("-" * 80)
            print()

            if texto:
                print(texto)
            else:
                print(
                    "No hay texto legible."
                )

            print()

        elif opcion == "2":

            mostrar_lista_adjuntos(
                attachments
            )

        elif opcion == "3":

            descargar_adjuntos(
                eml_path,
                attachments
            )

        elif opcion == "4":

            emls = [
                adj
                for adj in attachments
                if adj.is_eml
            ]

            if not emls:

                print()
                print(
                    "Este correo no contiene "
                    "otros EML adjuntos."
                )

                continue

            print()
            print("EML ADJUNTOS")
            print("-" * 80)

            for i, adj in enumerate(
                emls,
                start=1
            ):
                print(
                    f"{i}. {adj.filename}"
                )

            print()

            seleccion = input(
                "Seleccione EML (0=volver): "
            ).strip()

            if seleccion in ("", "0"):
                continue

            try:

                numero = int(
                    seleccion
                )

                if 1 <= numero <= len(emls):

                    leer_eml_adjunto(
                        emls[numero - 1]
                    )

                else:

                    print(
                        "\nNúmero no válido."
                    )

            except ValueError:

                print(
                    "\nNúmero no válido."
                )

        elif opcion == "0":

            break

        else:

            print(
                "\nOpción no válida."
            )


# ============================================================
# PROCESAR EML
# ============================================================

def procesar_eml(path):

    msg = abrir_eml(path)

    if msg is None:
        return

    attachments = procesar_mensaje(
        msg,
        f"CORREO: {path.name}"
    )

    menu_correo(
        path,
        msg,
        attachments
    )


# ============================================================
# OBTENER EML Y ORDENAR
# ============================================================

def obtener_eml_actual():

    carpeta = Path(".")

    files = [
        p
        for p in carpeta.iterdir()
        if p.is_file()
        and p.suffix.lower() == ".eml"
    ]

    # Más reciente -> más antiguo
    files.sort(
        key=timestamp_orden,
        reverse=True
    )

    return files


# ============================================================
# MAIN
# ============================================================

def main():

    print()
    print("=" * 80)
    print("LECTOR DE CORREOS EML")
    print("=" * 80)

    print()
    print(
        "Carpeta actual:"
    )
    print(
        Path.cwd()
    )

    while True:

        files = obtener_eml_actual()

        seleccionados = seleccionar_eml(
            files
        )

        if not seleccionados:
            break

        for path in seleccionados:

            print()
            print(
                f"\nAbriendo: {path.name}"
            )

            procesar_eml(path)

    print()
    print("Fin.")


if __name__ == "__main__":
    main()
