#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
colab_publicar.py  -  Termux -> Google Drive -> Google Colab

Convierte un .txt con marcadores "=== CELDA n: titulo ===" en un cuaderno
.ipynb (una celda de codigo por bloque), lo sube a Google Drive junto con el
documentos oficiales (.txt o .pdf) y las preguntas, y te da el enlace para abrirlo en
Colab. Solo usa la libreria estandar de Python (no hay que instalar nada).

Autorizacion: flujo OAuth 2.0 "TVs y dispositivos de entrada limitada"
(documentado por Google), con el permiso minimo drive.file: el programa solo
ve los archivos que el crea. Te muestra un codigo; lo escribes en
google.com/device con la cuenta de Google que usas en Colab.

Uso rapido:
    python colab_publicar.py configurar
    python colab_publicar.py autorizar
    python colab_publicar.py publicar colab_rag_salamandra_drive.txt \
        --doc boletin.txt --preguntas preguntas.txt
Ver la guia (guia_automatizacion_colab.txt) para el detalle.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

VERSION = "1.1"

# Direcciones oficiales. Las variables de entorno solo sirven para pruebas.
BASE_OAUTH = os.environ.get("COLABPUB_OAUTH_BASE", "https://oauth2.googleapis.com")
BASE_DRIVE = os.environ.get("COLABPUB_DRIVE_BASE", "https://www.googleapis.com")
URL_USERINFO = os.environ.get(
    "COLABPUB_USERINFO", "https://openidconnect.googleapis.com/v1/userinfo"
)
URL_COLAB = "https://colab.research.google.com/drive/"

# openid + email: solo para mostrar con que cuenta has autorizado.
# drive.file: solo archivos creados por este programa.
SCOPE = "openid email https://www.googleapis.com/auth/drive.file"

DIR_CONFIG = os.environ.get(
    "COLABPUB_HOME", os.path.join(os.path.expanduser("~"), ".config", "colab_publicar")
)
FICH_CLIENTE = os.path.join(DIR_CONFIG, "cliente.json")
FICH_TOKEN = os.path.join(DIR_CONFIG, "token.json")

MIME_CARPETA = "application/vnd.google-apps.folder"
MIMES_CUADERNO = {
    "ipynb": "application/x-ipynb+json",
    "colab": "application/vnd.google.colab",
    "colaboratory": "application/vnd.google.colaboratory",
}


class ErrorApp(Exception):
    """Error esperable, con mensaje para el usuario."""


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def asegurar_dir():
    os.makedirs(DIR_CONFIG, exist_ok=True)
    try:
        os.chmod(DIR_CONFIG, 0o700)
    except OSError:
        pass


def guardar_json(ruta, datos):
    asegurar_dir()
    tmp = ruta + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(datos, f, indent=2, ensure_ascii=False)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, ruta)


def leer_json(ruta):
    try:
        with open(ruta, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except ValueError:
        raise ErrorApp(f"El archivo {ruta} esta danado. Borralo y vuelve a empezar.")


def leer_texto(ruta):
    try:
        with open(ruta, "rb") as f:
            datos = f.read()
    except FileNotFoundError:
        raise ErrorApp(f"No encuentro el archivo: {ruta}")
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return datos.decode(enc)
        except UnicodeDecodeError:
            pass
    return datos.decode("utf-8", errors="ignore")


def http(metodo, url, datos=None, cabeceras=None, form=None, timeout=60, con_cabeceras=False):
    cab = dict(cabeceras or {})
    if form is not None:
        datos = urllib.parse.urlencode(form).encode("utf-8")
        cab["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=datos, headers=cab, method=metodo)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            cuerpo, codigo = r.read(), r.status
            recibidas = {k.lower(): v for k, v in r.headers.items()}
    except urllib.error.HTTPError as e:
        cuerpo, codigo = e.read(), e.code
        recibidas = {k.lower(): v for k, v in e.headers.items()}
    except urllib.error.URLError as e:
        raise ErrorApp(f"No hay conexion con {url}: {e.reason}")
    except TimeoutError:
        raise ErrorApp(f"Tiempo de espera agotado con {url}")
    try:
        js = json.loads(cuerpo.decode("utf-8") or "{}")
    except ValueError:
        js = {"_raw": cuerpo.decode("utf-8", "replace")}
    if con_cabeceras:
        return codigo, js, recibidas
    return codigo, js


def abrir_url(url):
    """Abre la direccion en el navegador del movil si es posible."""
    try:
        if shutil.which("termux-open-url"):
            subprocess.run(["termux-open-url", url], timeout=15, check=False)
            return True
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        import webbrowser

        return bool(webbrowser.open(url))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Configuracion del cliente OAuth
# ---------------------------------------------------------------------------
def cmd_configurar(args):
    print("Configuracion del cliente OAuth (tipo: 'TVs y dispositivos de entrada limitada').")
    print("Los pasos para crearlo estan en la guia. Pega aqui los dos datos.\n")
    actual = leer_json(FICH_CLIENTE) or {}
    cid = input(f"ID de cliente [{actual.get('client_id', '')}]: ").strip() or actual.get("client_id", "")
    sec = input(f"Secreto de cliente [{'*' * 6 if actual.get('client_secret') else ''}]: ").strip() or actual.get("client_secret", "")
    if not cid or not sec:
        raise ErrorApp("Hacen falta el ID de cliente y el secreto de cliente.")
    if not cid.endswith(".apps.googleusercontent.com"):
        print("Aviso: el ID de cliente suele terminar en .apps.googleusercontent.com")
    guardar_json(FICH_CLIENTE, {"client_id": cid, "client_secret": sec})
    print(f"\nGuardado en {FICH_CLIENTE}")
    print("Siguiente paso:  python colab_publicar.py autorizar")


def leer_cliente():
    c = leer_json(FICH_CLIENTE)
    if not c or not c.get("client_id") or not c.get("client_secret"):
        raise ErrorApp("Falta el cliente OAuth. Ejecuta primero:  python colab_publicar.py configurar")
    return c


# ---------------------------------------------------------------------------
# Autorizacion (flujo de dispositivo)
# ---------------------------------------------------------------------------
def pedir_email(access_token):
    try:
        cod, js = http("GET", URL_USERINFO, cabeceras={"Authorization": f"Bearer {access_token}"})
        if cod == 200:
            return js.get("email", "")
    except ErrorApp:
        pass
    return ""


def cmd_autorizar(args):
    cli = leer_cliente()
    if leer_json(FICH_TOKEN) and not args.forzar:
        print("Ya hay una autorizacion guardada. Usa 'estado' para comprobarla")
        print("o 'autorizar --forzar' para cambiar de cuenta.")
        return
    cod, js = http("POST", f"{BASE_OAUTH}/device/code",
                   form={"client_id": cli["client_id"], "scope": SCOPE})
    if cod != 200 or "device_code" not in js:
        detalle = js.get("error_description") or js.get("error") or js
        if js.get("error") == "invalid_client":
            detalle = "Cliente no valido: debe ser de tipo 'TVs y dispositivos de entrada limitada'."
        if js.get("error") == "invalid_scope":
            detalle = "Permiso no permitido en este flujo. Revisa la pantalla de consentimiento."
        raise ErrorApp(f"Google no ha aceptado la peticion ({cod}): {detalle}")

    url = js.get("verification_url") or js.get("verification_uri")
    codigo_usuario = js["user_code"]
    print("\n==================== AUTORIZAR EN GOOGLE ====================")
    print(f" 1) Abre:   {url}")
    print(f" 2) Inicia sesion con la cuenta que usas en COLAB")
    print(f" 3) Escribe este codigo:   {codigo_usuario}")
    print("=============================================================")
    print("Esperando a que lo confirmes (Ctrl+C para cancelar)...")
    abrir_url(url)

    intervalo = int(js.get("interval", 5))
    limite = time.time() + int(js.get("expires_in", 1800))
    while time.time() < limite:
        time.sleep(intervalo)
        cod, t = http("POST", f"{BASE_OAUTH}/token", form={
            "client_id": cli["client_id"],
            "client_secret": cli["client_secret"],
            "device_code": js["device_code"],
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        })
        if cod == 200 and "access_token" in t:
            email = pedir_email(t["access_token"])
            if not t.get("refresh_token"):
                raise ErrorApp("Google no ha devuelto refresh token. Repite con 'autorizar --forzar'.")
            guardar_json(FICH_TOKEN, {
                "refresh_token": t["refresh_token"],
                "access_token": t["access_token"],
                "expires_at": time.time() + int(t.get("expires_in", 3600)),
                "scope": t.get("scope", ""),
                "email": email,
                "creado": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            print(f"\nAutorizado correctamente. Cuenta: {email or '(no disponible)'}")
            print("Compruebalo: debe ser la misma cuenta que usas en Colab.")
            return
        err = t.get("error", "")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            intervalo += 5
            continue
        if err == "access_denied":
            raise ErrorApp("Has rechazado la autorizacion en Google.")
        if err in ("expired_token", "invalid_grant"):
            raise ErrorApp("El codigo ha caducado. Ejecuta 'autorizar' otra vez.")
        raise ErrorApp(f"Error de Google ({cod}): {t.get('error_description') or err or t}")
    raise ErrorApp("Se acabo el tiempo sin confirmar. Ejecuta 'autorizar' otra vez.")


def token_acceso():
    """Devuelve un access token valido, renovandolo con el refresh token."""
    tok = leer_json(FICH_TOKEN)
    if not tok:
        raise ErrorApp("No hay autorizacion. Ejecuta:  python colab_publicar.py autorizar")
    if tok.get("access_token") and tok.get("expires_at", 0) - 60 > time.time():
        return tok["access_token"]
    cli = leer_cliente()
    cod, js = http("POST", f"{BASE_OAUTH}/token", form={
        "client_id": cli["client_id"],
        "client_secret": cli["client_secret"],
        "refresh_token": tok["refresh_token"],
        "grant_type": "refresh_token",
    })
    if cod != 200 or "access_token" not in js:
        if js.get("error") == "invalid_grant":
            raise ErrorApp(
                "La autorizacion ha caducado o fue revocada.\n"
                "  - Si tu app de Google Cloud esta en estado 'Prueba' (Testing), el permiso caduca a los 7 dias.\n"
                "    Solucion definitiva: publicarla (ver guia).\n"
                "  - Para renovar ahora:  python colab_publicar.py autorizar --forzar"
            )
        raise ErrorApp(f"No se ha podido renovar el acceso ({cod}): {js.get('error_description') or js}")
    tok["access_token"] = js["access_token"]
    tok["expires_at"] = time.time() + int(js.get("expires_in", 3600))
    guardar_json(FICH_TOKEN, tok)
    return tok["access_token"]


def cmd_estado(args):
    tok = leer_json(FICH_TOKEN)
    cli = leer_json(FICH_CLIENTE)
    print(f"Cliente OAuth configurado : {'si' if cli else 'NO'}")
    print(f"Autorizacion guardada     : {'si' if tok else 'NO'}")
    if not tok:
        return
    print(f"Cuenta                    : {tok.get('email') or '(desconocida)'}")
    print(f"Autorizado el             : {tok.get('creado', '?')}")
    try:
        token_acceso()
        print("Renovacion de acceso      : correcta (la autorizacion sigue vigente)")
    except ErrorApp as e:
        print(f"Renovacion de acceso      : FALLA\n{e}")


def cmd_cerrar(args):
    tok = leer_json(FICH_TOKEN)
    if not tok:
        print("No hay autorizacion guardada.")
        return
    try:
        http("POST", f"{BASE_OAUTH}/revoke", form={"token": tok.get("refresh_token", "")})
    except ErrorApp:
        print("Aviso: no se pudo avisar a Google; se borra solo la copia local.")
    os.remove(FICH_TOKEN)
    print("Autorizacion revocada y borrada de este movil.")


# ---------------------------------------------------------------------------
# Conversion  .txt  ->  .ipynb
# ---------------------------------------------------------------------------
MARCA = re.compile(r"^===\s*CELDA\s+(\d+)\s*:?\s*(.*?)\s*===\s*$", re.IGNORECASE)


def _lineas_fuente(lineas):
    """Formato 'source' de un cuaderno: lista de lineas, todas con \\n menos la ultima."""
    while lineas and not lineas[0].strip():
        lineas = lineas[1:]
    while lineas and not lineas[-1].strip():
        lineas = lineas[:-1]
    return [l + "\n" for l in lineas[:-1]] + ([lineas[-1]] if lineas else [])


def convertir_texto(texto, carpeta="RAG_Salamandra", gpu=True, titulos=True):
    texto = texto.replace("{{CARPETA}}", carpeta)
    bloques, actual = [], None
    for linea in texto.splitlines():
        m = MARCA.match(linea.strip())
        if m:
            actual = {"n": int(m.group(1)), "titulo": m.group(2).strip(), "lineas": []}
            bloques.append(actual)
        elif actual is not None:
            actual["lineas"].append(linea.rstrip("\r"))
    if not bloques:
        raise ErrorApp("No hay marcadores. Cada bloque debe empezar con una linea:  === CELDA 1: titulo ===")

    celdas = []
    for b in bloques:
        fuente = _lineas_fuente(b["lineas"])
        if not fuente:
            continue
        i = len(celdas)
        if titulos and b["titulo"]:
            celdas.append({
                "cell_type": "markdown", "id": f"md{i:03d}", "metadata": {},
                "source": [f"### {b['n']}. {b['titulo']}"],
            })
            i = len(celdas)
        celdas.append({
            "cell_type": "code", "id": f"c{i:03d}", "metadata": {},
            "execution_count": None, "outputs": [], "source": fuente,
        })
    meta = {
        "colab": {"provenance": []},
        "kernelspec": {"name": "python3", "display_name": "Python 3"},
        "language_info": {"name": "python"},
    }
    if gpu:
        meta["colab"]["gpuType"] = "T4"
        meta["accelerator"] = "GPU"
    return {"nbformat": 4, "nbformat_minor": 5, "metadata": meta, "cells": celdas}


def cmd_convertir(args):
    nb = convertir_texto(leer_texto(args.guion), args.carpeta, not args.sin_gpu, not args.sin_titulos)
    salida = args.salida or os.path.splitext(args.guion)[0] + ".ipynb"
    with open(salida, "w", encoding="utf-8") as f:
        json.dump(nb, f, indent=1, ensure_ascii=False)
    n = sum(1 for c in nb["cells"] if c["cell_type"] == "code")
    print(f"Cuaderno creado: {salida}  ({n} celdas de codigo)")


# ---------------------------------------------------------------------------
# Google Drive
# ---------------------------------------------------------------------------
def api(metodo, ruta, token, **kw):
    cab = {"Authorization": f"Bearer {token}"}
    cab.update(kw.pop("cabeceras", {}))
    con_cab = kw.pop("con_cabeceras", False)
    cod, js, recibidas = http(metodo, f"{BASE_DRIVE}{ruta}", cabeceras=cab, con_cabeceras=True, **kw)
    if cod == 401:
        raise ErrorApp("Drive rechaza el acceso (401). Ejecuta:  python colab_publicar.py autorizar --forzar")
    if cod == 403 and "rate" in json.dumps(js).lower():
        raise ErrorApp("Google limita las peticiones. Espera un minuto y repite.")
    if cod >= 300:
        err = js.get("error", js)
        msg = err.get("message") if isinstance(err, dict) else err
        raise ErrorApp(f"Error de Drive ({cod}): {msg}")
    return (js, recibidas) if con_cab else js


def _esc(s):
    return s.replace("\\", "\\\\").replace("'", "\\'")


def buscar(token, nombre, padre, carpeta=False):
    """Devuelve un diccionario (id, name, mimeType, md5Checksum) o None."""
    q = f"name = '{_esc(nombre)}' and '{padre}' in parents and trashed = false"
    if carpeta:
        q += f" and mimeType = '{MIME_CARPETA}'"
    qs = urllib.parse.urlencode({"q": q, "fields": "files(id,name,mimeType,md5Checksum)",
                                 "pageSize": 10, "spaces": "drive"})
    js = api("GET", f"/drive/v3/files?{qs}", token)
    ficheros = js.get("files", [])
    return ficheros[0] if ficheros else None


def asegurar_carpeta(token, nombre, padre="root"):
    existente = buscar(token, nombre, padre, carpeta=True)
    if existente:
        return existente["id"]
    js = api("POST", "/drive/v3/files?fields=id", token,
             datos=json.dumps({"name": nombre, "mimeType": MIME_CARPETA, "parents": [padre]}).encode("utf-8"),
             cabeceras={"Content-Type": "application/json; charset=UTF-8"})
    return js["id"]


def _multipart(meta, contenido, mime):
    limite = "colabpub" + uuid.uuid4().hex
    cabecera = (
        f"--{limite}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{json.dumps(meta)}\r\n--{limite}\r\nContent-Type: {mime}\r\n\r\n"
    ).encode("utf-8")
    cuerpo = cabecera + contenido + f"\r\n--{limite}--".encode("utf-8")
    return cuerpo, f"multipart/related; boundary={limite}"


LIMITE_SIMPLE = 4 * 1024 * 1024  # por encima de esto se usa la subida reanudable


def _subir_reanudable(token, nombre, contenido, padre, mime, existente):
    """Subida en dos pasos (sesion + contenido) para archivos grandes."""
    if existente:
        ruta = f"/upload/drive/v3/files/{existente['id']}?uploadType=resumable&fields=id,name"
        metodo, meta = "PATCH", {"name": nombre}
    else:
        ruta = "/upload/drive/v3/files?uploadType=resumable&fields=id,name"
        metodo, meta = "POST", {"name": nombre, "parents": [padre], "mimeType": mime}
    _, recibidas = api(
        metodo, ruta, token, datos=json.dumps(meta).encode("utf-8"), con_cabeceras=True,
        cabeceras={"Content-Type": "application/json; charset=UTF-8",
                   "X-Upload-Content-Type": mime, "X-Upload-Content-Length": str(len(contenido))},
    )
    destino = recibidas.get("location")
    if not destino:
        raise ErrorApp("Drive no ha devuelto la direccion de subida de archivos grandes.")
    cod, js = http("PUT", destino, datos=contenido, timeout=900,
                   cabeceras={"Content-Type": mime, "Authorization": f"Bearer {token}"})
    if cod >= 300 or "id" not in js:
        raise ErrorApp(f"Fallo al subir {nombre} ({cod}). Repite el comando: solo se repite lo que falte.")
    return js["id"], ("actualizado" if existente else "creado")


def subir(token, nombre, contenido, padre, mime, forzar=False):
    """Crea el archivo o, si ya existe con ese nombre, reemplaza su contenido (mismo enlace).
    Si el contenido no ha cambiado (mismo md5) no sube nada."""
    existente = buscar(token, nombre, padre)
    if existente and not forzar and existente.get("md5Checksum") == hashlib.md5(contenido).hexdigest():
        return existente["id"], "sin cambios"
    if len(contenido) > LIMITE_SIMPLE:
        return _subir_reanudable(token, nombre, contenido, padre, mime, existente)
    if existente:
        cuerpo, tipo = _multipart({"name": nombre}, contenido, mime)
        js = api("PATCH", f"/upload/drive/v3/files/{existente['id']}?uploadType=multipart&fields=id,name",
                 token, datos=cuerpo, cabeceras={"Content-Type": tipo})
        return js["id"], "actualizado"
    meta = {"name": nombre, "parents": [padre], "mimeType": mime}
    cuerpo, tipo = _multipart(meta, contenido, mime)
    js = api("POST", "/upload/drive/v3/files?uploadType=multipart&fields=id,name",
             token, datos=cuerpo, cabeceras={"Content-Type": tipo})
    return js["id"], "creado"


def leer_documento(ruta):
    """Devuelve (bytes, tipo MIME). Los .txt se guardan siempre en UTF-8."""
    ext = os.path.splitext(ruta)[1].lower()
    if ext == ".pdf":
        try:
            with open(ruta, "rb") as f:
                datos = f.read()
        except FileNotFoundError:
            raise ErrorApp(f"No encuentro el archivo: {ruta}")
        if not datos.startswith(b"%PDF"):
            raise ErrorApp(f"{ruta} no parece un PDF valido.")
        return datos, "application/pdf"
    texto = leer_texto(ruta)
    if not texto.strip():
        raise ErrorApp(f"El documento esta vacio: {ruta}")
    return texto.encode("utf-8"), "text/plain"


def reunir_documentos(args):
    rutas = list(args.doc or [])
    if args.doc_dir:
        if not os.path.isdir(args.doc_dir):
            raise ErrorApp(f"No existe la carpeta: {args.doc_dir}")
        for n in sorted(os.listdir(args.doc_dir)):
            if os.path.splitext(n)[1].lower() in (".txt", ".pdf") and not n.startswith("."):
                rutas.append(os.path.join(args.doc_dir, n))
    vistos = {}
    for r in rutas:
        nombre = os.path.basename(r)
        if nombre in vistos and os.path.abspath(vistos[nombre]) != os.path.abspath(r):
            raise ErrorApp(f"Dos documentos distintos se llaman igual: {nombre}")
        vistos[nombre] = r
    return list(vistos.values())


def cmd_publicar(args):
    docs = reunir_documentos(args)
    if not args.guion and not docs and not args.preguntas:
        raise ErrorApp("No hay nada que subir: indica un guion, --doc, --doc-dir o --preguntas.")
    nb = None
    if args.guion:
        nb = convertir_texto(leer_texto(args.guion), args.carpeta, not args.sin_gpu, not args.sin_titulos)
        n = sum(1 for c in nb["cells"] if c["cell_type"] == "code")
        print(f"Cuaderno convertido: {n} celdas de codigo.")

    token = token_acceso()
    tok = leer_json(FICH_TOKEN) or {}
    print(f"Cuenta de Drive: {tok.get('email') or '(desconocida)'}")

    raiz = asegurar_carpeta(token, args.carpeta)
    sub_docs = asegurar_carpeta(token, "documentos", raiz)

    resumen = {"creado": 0, "actualizado": 0, "sin cambios": 0}
    for i, ruta in enumerate(docs, 1):
        contenido, mime = leer_documento(ruta)
        nombre = os.path.basename(ruta)
        _, estado = subir(token, nombre, contenido, sub_docs, mime, forzar=args.forzar)
        resumen[estado] += 1
        print(f"  [{i}/{len(docs)}] {nombre} ({len(contenido) // 1024} KB): {estado}")
    if docs:
        print(f"Documentos: {resumen['creado']} nuevos, {resumen['actualizado']} actualizados, "
              f"{resumen['sin cambios']} sin cambios.")

    if args.preguntas:
        contenido, _ = leer_documento(args.preguntas)
        _, estado = subir(token, "preguntas.txt", contenido, raiz, "text/plain", forzar=args.forzar)
        print(f"Preguntas {estado}: {args.carpeta}/preguntas.txt")

    if nb is None:
        print("\nNo se ha subido cuaderno (no diste guion). Abre el que ya tienes en Colab y ejecuta de nuevo "
              "las celdas: continuara donde lo dejo.")
        return
    nombre_nb = args.nombre if args.nombre.endswith(".ipynb") else args.nombre + ".ipynb"
    contenido = json.dumps(nb, indent=1, ensure_ascii=False).encode("utf-8")
    ident, estado = subir(token, nombre_nb, contenido, raiz, MIMES_CUADERNO[args.mime], forzar=args.forzar)
    url = URL_COLAB + ident + (f"?authuser={args.authuser}" if args.authuser is not None else "")
    print(f"Cuaderno {estado}: {args.carpeta}/{nombre_nb}")
    print("\nEnlace de Colab:")
    print(f"  {url}")
    print("\nEn Colab: Entorno de ejecucion > Ejecutar todo (Ctrl+F9). Acepta el permiso de Drive en la ventana emergente.")
    print("Si el enlace da error de permisos, abre el archivo desde Drive: Abrir con > Google Colaboratory,")
    print("o prueba con --authuser N (N = posicion de la cuenta en tu navegador: 0, 1, 2...).")
    if not args.sin_abrir:
        abrir_url(url)


# ---------------------------------------------------------------------------
# Linea de ordenes
# ---------------------------------------------------------------------------
def crear_parser():
    p = argparse.ArgumentParser(
        prog="colab_publicar.py",
        description="Convierte un .txt en cuaderno de Colab y lo sube a Google Drive.",
    )
    p.add_argument("--version", action="version", version=VERSION)
    sub = p.add_subparsers(dest="orden", required=True)

    sub.add_parser("configurar", help="guarda el ID y secreto del cliente OAuth").set_defaults(f=cmd_configurar)

    a = sub.add_parser("autorizar", help="autoriza con la cuenta de Colab (codigo en google.com/device)")
    a.add_argument("--forzar", action="store_true", help="repetir la autorizacion / cambiar de cuenta")
    a.set_defaults(f=cmd_autorizar)

    sub.add_parser("estado", help="comprueba cuenta y vigencia de la autorizacion").set_defaults(f=cmd_estado)
    sub.add_parser("cerrar", help="revoca y borra la autorizacion").set_defaults(f=cmd_cerrar)

    def comunes(sp):
        sp.add_argument("--carpeta", default="RAG_Salamandra", help="carpeta en Drive (por defecto RAG_Salamandra)")
        sp.add_argument("--sin-gpu", action="store_true", help="no pedir GPU en el cuaderno")
        sp.add_argument("--sin-titulos", action="store_true", help="no crear celdas de titulo")

    c = sub.add_parser("convertir", help="solo convierte a .ipynb en local")
    c.add_argument("guion")
    c.add_argument("-o", "--salida")
    comunes(c)
    c.set_defaults(f=cmd_convertir)

    u = sub.add_parser("publicar", help="convierte, sube a Drive y da el enlace de Colab")
    u.add_argument("guion", nargs="?", help=".txt con los marcadores === CELDA n: titulo === "
                   "(sin guion solo se suben documentos y preguntas)")
    u.add_argument("--doc", action="append", help="documento .txt o .pdf a subir (se puede repetir)")
    u.add_argument("--doc-dir", help="sube todos los .txt y .pdf de esta carpeta (solo los que hayan cambiado)")
    u.add_argument("--forzar", action="store_true", help="vuelve a subir aunque no haya cambios")
    u.add_argument("--preguntas", help=".txt con una pregunta por linea")
    u.add_argument("--nombre", default="RAG_Salamandra", help="nombre del cuaderno")
    u.add_argument("--authuser", type=int, help="indice de cuenta del navegador para el enlace")
    u.add_argument("--mime", choices=sorted(MIMES_CUADERNO), default="ipynb",
                   help="tipo MIME del cuaderno en Drive (por defecto ipynb)")
    u.add_argument("--sin-abrir", action="store_true", help="no abrir el enlace en el navegador")
    comunes(u)
    u.set_defaults(f=cmd_publicar)
    return p


def main(argv=None):
    args = crear_parser().parse_args(argv)
    try:
        args.f(args)
    except ErrorApp as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelado.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
