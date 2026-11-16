#!/usr/bin/env python3
"""
Configuración de leerCorreo.py — cuentas de correo y valores ajustables.

Este fichero NO se ejecuta por sí solo: leerCorreo.py lo importa para
saber con qué cuentas trabajar y con qué ajustes. Edítalo para añadir,
quitar o modificar cuentas, o para cambiar cualquiera de los valores de
más abajo — no hace falta tocar leerCorreo.py para nada de eso.
"""

# ============== CONFIGURA TUS CUENTAS AQUÍ ==============
# Para tener varias cuentas del mismo proveedor (dos Yahoo, dos GMX...),
# usa una clave distinta para cada una, por ejemplo:
#   "yahoo_trabajo": {
#       "server": "imap.mail.yahoo.com", "port": 993,
#       "user": "otro_correo@yahoo.com", "password": "", "folder": "INBOX",
#   },
CUENTAS = {
    "yahoo": {
        "server": "imap.mail.yahoo.com",
        "port": 993,
        "smtp_server": "smtp.mail.yahoo.com",
        "smtp_port": 587,
        "user": "jose.zuccoli@yahoo.es",
        "password": "utetxncithjleoco",                   # vacío = te la pide al ejecutar
        "folder": "INBOX",
    },
    "gmx": {
        "server": "imap.gmx.net",         # usa imap.gmx.com si tu cuenta es @gmx.com
        "port": 993,
        "smtp_server": "mail.gmx.net",    # usa mail.gmx.com si tu cuenta es @gmx.com
        "smtp_port": 587,
        "user": "jose.zuccoli@gmx.es",
        "password": "N3WGM5PAMFAK7TJ6OZXF",
        "folder": "INBOX",
    },
    "consdigg": {
        "server": "imap.gmx.net",
        "port": 993,
        "smtp_server": "mail.gmx.net",
        "smtp_port": 587,
        "user": "consultordigitalizacion@gmx.es",
        "password": "',}e3Y}W4UWppFJ",
        "folder": "INBOX",
    },
    "busqp" : {
        "server": "imap.gmx.net",
        "port": 993,
        "smtp_server": "mail.gmx.net",
        "smtp_port": 587,
        "user": "busquedapiso2026@gmx.es",
        "password": "V36ZS#KdddKpG$",
        "folder": "INBOX",
    },
    "rbrtmichun": {
        "server": "imap.gmail.com",
        "port": 993,
        "smtp_server": "smtp.gmail.com",
        "smtp_port": 587,
        "user": "rbrt.michun@gmail.com",
        "password": "wbufxfhngnuemytj",
        "folder": "INBOX",
    },
    "jad": {
        "server": "imap.gmail.com",
        "port": 993,
        "smtp_server": "smtp.gmail.com",
        "smtp_port": 587,
        "user": "josez.laadrada@gmail.com",
        "password": "Pituti2026",
        "folder": "INBOX",
    },
    "josech": {
        "server": "imap.gmx.net",         # usa imap.gmx.com si tu cuenta es @gmx.com
        "port": 993,
        "smtp_server": "mail.gmx.net",    # usa mail.gmx.com si tu cuenta es @gmx.com
        "smtp_port": 587,
        "user": "joseph.zuccoli@gmx.ch",
        "password": "45*cp#cYax!uae6hvnWgCB",
        "folder": "INBOX",
    },
    "jjzu12": {
        "server": "imap.gmail.com",
        "port": 993,
        "smtp_server": "smtp.gmail.com",
        "smtp_port": 587,
        "user": "jjzu12@gmail.com",
        "password": "zewepbfhbwgjsvkk",
        "folder": "INBOX",
    },
    "ciberseguridadconsultor": {
        "server": "imap.gmail.com",
        "port": 993,
        "smtp_server": "smtp.gmail.com",
        "smtp_port": 587,
        "user": "ciberseguridadconsultor@gmail.com",
        "password": "cfywhxbyesnoipsi",
        "folder": "INBOX",
    },
    "ciberseguridadmadridnorte": {
        "server": "imap.gmail.com",
        "port": 993,
        "smtp_server": "smtp.gmail.com",
        "smtp_port": 587,
        "user": "ciberseguridadmadridnorte@gmail.com",
        "password": "ezixbvtzrkxuajyl",
        "folder": "INBOX",
    },
    "govsif2": {
        "server": "imap.gmail.com",
        "port": 993,
        "smtp_server": "smtp.gmail.com",
        "smtp_port": 587,
        "user": "governeursifterkingshot@gmail.com",
        "password": "htxjovncuaoemwdw",
        "folder": "INBOX",
    },
}

SALIDA = "mails"     # carpeta local; se crea una subcarpeta por cuenta
MAX_INICIAL = 200          # cuántos mensajes recientes se miran por carpeta, de entrada
INTERVALO_AVISO_INICIAL = 30  # segundos antes del primer aviso "¿seguir buscando?" y entre avisos — se puede alargar con 2x/4x/etc. al responder
TAMANO_RONDA_BUSQUEDA = 50    # al buscar en varias cuentas/carpetas, cuántos mensajes se miran de cada una antes de pasar a la siguiente (ronda a ronda) — así una cuenta con mucho correo no retrasa llegar a las demás
LIMITE_ESPERA_RESPUESTA = 10   # segundos que espera una respuesta cuando el aviso ya es de 5+ minutos (o siempre, en modo comando directo); si no hay respuesta, sigue sola
ANCHO_TERMINAL = 100      # ancho de pantalla/terminal asumido, en caracteres — súbelo o bájalo si en tu pantalla las líneas de título se cortan o sobra hueco; de aquí salen tanto el ancho de la línea de título como el presupuesto de la vista previa
LINEAS_CONTEXTO = 3        # líneas de CONTEXTO antes/después de un término encontrado en el cuerpo — prueba 5 si quieres más
LARGO_LINEA_PREVIEW = ANCHO_TERMINAL   # presupuesto TOTAL de caracteres de la vista previa en "recientes" (no en búsqueda) — se multiplica por LINEAS_CONTEXTO
LARGO_URL_MAX = 20         # a cuántos caracteres se recortan los enlaces http(s) que aparezcan en el texto

# --- -ver-img (pedido 2026-10-01): tamaño de las imágenes que genera ---
ANCHO_CAPTURA_PANTALLA = 1080   # ancho en píxeles de las imágenes de -ver-img -- 1080 es un ancho típico de móvil; súbelo o bájalo según tu pantalla
ALTO_CAPTURA_PANTALLA = 1920    # alto en píxeles de CADA "pantalla" (-pantallas) de -ver-img -- normalmente el alto útil de la pantalla de tu móvil/tablet
TAMANO_FUENTE_CAPTURA = 30      # tamaño de letra dentro de esas imágenes, en píxeles -- súbelo si se ve pequeño, bájalo si se ve grande
# --- -escribir -lista / -lista-fichero (pedido 2026-10-02): envío del mismo
# correo (mismo asunto, cuerpo y adjuntos) a una lista de direcciones, una
# POR UNA (un correo independiente por destinatario, nadie ve a los demás) ---
RETRASO_ENTRE_ENVIOS_LISTA = 2   # segundos de pausa entre cada envío de la lista -- para no disparar los límites antiabuso del proveedor SMTP; súbelo si tu proveedor es estricto, o ponlo a 0 para no esperar nada

# --- -reanudable (pedido 2026-10-05/07): búsqueda larga que se puede parar
# (Ctrl+C, o -max) y reanudar más tarde (horas o días después) repitiendo
# la MISMA orden -- va descargando cada correo encontrado al momento, y
# recuerda qué cuentas/carpetas ya terminó y qué correos ya descargó de
# cada una que quedó a medias, para no volver a descargarlos ---
CARPETA_PROGRESO_BUSQUEDAS = "progreso_busquedas"   # carpeta local donde se guarda ese progreso (un fichero .json por búsqueda distinta) -- no borres ficheros de aquí a mano salvo que quieras "olvidar" el progreso de esa búsqueda y que la próxima vez empiece desde cero

# --- -nuevos (pedido 2026-10-05/07): recuerda qué correos del INBOX de cada
# cuenta ya se han visto en una ejecución anterior, para poder distinguir
# los nuevos desde la última vez y mostrarlos los primeros ---
CARPETA_REGISTRO_NUEVOS = "registro_nuevos_inbox"   # carpeta local donde se guarda ese registro (un fichero .json por cuenta) -- no borres ficheros de aquí a mano salvo que quieras que la próxima vez TODO el INBOX de esa cuenta cuente como "nuevo" otra vez
